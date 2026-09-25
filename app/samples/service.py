from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.samples.lineage import (
    DEFAULT_TOLERANCE_RATIO,
    ConversionRuleRepository,
    LineageRepository,
    canonical_digest,
)
from app.samples.repository import AnomalyRepository, ApprovalRepository, BatchRepository, LocationRepository, SampleRepository
from app.services.audit import AuditService


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class LocationService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.locations = LocationRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        if self.locations.by_code(data["code"]):
            raise ConflictError("位置编码已经存在")
        now = to_storage(self.clock.now())
        location = self.locations.create(data, now)
        self.audit.record(principal, "location.create", "storage_location", str(location["id"]), after=location)
        return self.present(principal, location)

    def present(self, principal: Principal, location: dict[str, Any]) -> dict[str, Any]:
        result = dict(location)
        exact = "*" in principal.permissions or "locations.read_sensitive" in principal.permissions
        if not exact and location["sensitivity"] != "normal":
            result["building"] = "受限区域"
            result["room"] = "***"
            result["cabinet"] = "***"
            result["shelf"] = "***"
            result["code"] = f"MASKED-{location['id']:04d}"
        return result

    def list(self, principal: Principal) -> list[dict[str, Any]]:
        principal.require("samples.read")
        return [self.present(principal, item) for item in self.locations.list()]


class SampleLifecycleService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.batches = BatchRepository(connection)
        self.locations = LocationRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create_batch(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        now = to_storage(self.clock.now())
        payload = f"sample-batch:{data['batch_code']}:{data['project_code']}"
        batch = self.batches.create(data, principal.user_id, payload, now)
        self.audit.record(principal, "batch.receive", "receipt_batch", str(batch["id"]), after=batch)
        return batch

    def register_sample(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        if self.samples.by_code(data["sample_code"]):
            raise ConflictError("样品编码已经存在")
        self.batches.get(data["batch_id"])
        if data.get("location_id"):
            self.locations.get(data["location_id"])
        now = to_storage(self.clock.now())
        values = dict(data)
        values.update(lifecycle_state="available", custody_user_id=principal.user_id, lineage_depth=0)
        sample = self.samples.create(values, now)
        self.samples.append_event(sample["id"], "received", principal.user_id, now, to_state="available", details={"batch_id": data["batch_id"]})
        self.batches.update_counts(data["batch_id"], now)
        self.audit.record(principal, "sample.register", "sample", str(sample["id"]), after=sample)
        return sample

    def list_samples(self, principal: Principal, state: str | None, batch_id: int | None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        exact = "*" in principal.permissions or "locations.read_sensitive" in principal.permissions
        result = self.samples.list(state=state, batch_id=batch_id)
        for sample in result:
            if sample.get("location_sensitivity") != "normal" and not exact:
                sample["location_code"] = f"MASKED-{sample['location_id']:04d}" if sample.get("location_id") else None
        return result

    def detail(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.samples.get(sample_id)
        sample["events"] = self.samples.events(sample_id)
        sample["children"] = self.samples.children(sample_id)
        if sample.get("location_sensitivity") != "normal" and not (
            "*" in principal.permissions or "locations.read_sensitive" in principal.permissions
        ):
            sample["location_code"] = f"MASKED-{sample['location_id']:04d}" if sample.get("location_id") else None
        return sample

    def aliquot(self, principal: Principal, sample_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """提交一次谱系操作（分装/冻干/研磨/处理）。

        在路由开启的事务内完成直接守恒校验与全部写入：子样合计加损耗必须在容差内
        等于按换算规则折算后的分装数量；操作固化处理前后单位、换算规则快照、
        可解释损耗、容差快照与操作版本，历史不受后续规则升级影响。
        """
        principal.require("samples.write")
        parent = self.samples.get(sample_id)
        lineage = LineageRepository(self.connection)
        input_unit = parent["unit"]
        output_unit = data.get("output_unit") or input_unit
        rule_code = data.get("conversion_rule_code")
        rule_version = None
        factor = 1.0
        if output_unit != input_unit and not rule_code:
            raise ValidationError("处理前后单位不一致，必须提供换算规则")
        if rule_code:
            rule = ConversionRuleRepository(self.connection).active(rule_code)
            if rule is None:
                raise ValidationError("换算规则不存在或已停用")
            if rule["from_unit"] != input_unit or rule["to_unit"] != output_unit:
                raise ValidationError("换算规则的单位方向与本次操作不一致")
            if (
                data.get("conversion_rule_version") is not None
                and data["conversion_rule_version"] != rule["version"]
            ):
                raise ConflictError("换算规则已升级为新版本，请确认后重试")
            factor = float(rule["factor"])
            rule_version = int(rule["version"])
        tolerance = data.get("tolerance_ratio")
        if tolerance is None:
            tolerance = DEFAULT_TOLERANCE_RATIO
        loss = float(data.get("loss_quantity", 0))
        loss_reason = (data.get("loss_reason") or "").strip()
        if loss > 1e-9 and not loss_reason:
            raise ValidationError("存在损耗时必须填写可解释的损耗原因")
        children_data = data["children"]
        codes = [item["sample_code"] for item in children_data]
        if len(set(codes)) != len(codes):
            raise ValidationError("子样编码在请求内重复")
        produced = round(sum(item["quantity"] for item in children_data), 9)
        expected = round(data["requested_quantity"] * factor, 9)
        if abs(produced + loss - expected) > max(1e-6, tolerance * abs(expected)):
            raise ValidationError("子样数量与损耗之和必须在容差内等于换算后的分装数量")
        if parent["quantity"] - parent["reserved_quantity"] < data["requested_quantity"]:
            raise ConflictError("可用数量不足")
        digest = canonical_digest(
            {
                "parent_sample_id": sample_id,
                "operation_kind": data.get("operation_kind", "aliquot"),
                "requested_quantity": data["requested_quantity"],
                "input_unit": input_unit,
                "output_unit": output_unit,
                "conversion_rule_code": rule_code,
                "conversion_rule_version": rule_version,
                "conversion_factor": factor,
                "loss_quantity": loss,
                "loss_reason": loss_reason,
                "tolerance_ratio": tolerance,
                "children": [
                    {
                        "sample_code": item["sample_code"],
                        "quantity": item["quantity"],
                        "sample_type": item.get("sample_type"),
                        "location_id": item.get("location_id"),
                    }
                    for item in children_data
                ],
            }
        )
        operation_code = data.get("operation_code")
        if operation_code:
            existing = lineage.operation_by_code(operation_code)
            if existing:
                if existing["request_digest"] != digest:
                    raise ConflictError("操作编码已被不同的分装请求占用")
                return {
                    "operation_code": operation_code,
                    "operation": existing,
                    "parent": self.samples.get(sample_id),
                    "children": [
                        self.samples.get(child_id)
                        for child_id in lineage.operation_children(existing["id"])
                    ],
                    "replayed": True,
                }
        else:
            operation_code = f"ALI-{uuid.uuid4().hex[:12]}"
        for code in sorted(codes):
            if self.samples.by_code(code):
                raise ConflictError(f"子样编码已经存在: {code}")
        now = to_storage(self.clock.now())
        updated_parent = self.samples.change_quantity(
            sample_id, -data["requested_quantity"], parent["version"], now
        )
        operation = lineage.insert_operation(
            {
                "operation_code": operation_code,
                "parent_sample_id": sample_id,
                "operation_kind": data.get("operation_kind", "aliquot"),
                "requested_quantity": data["requested_quantity"],
                "produced_quantity": produced,
                "loss_quantity": loss,
                "input_unit": input_unit,
                "output_unit": output_unit,
                "conversion_rule_code": rule_code,
                "conversion_rule_version": rule_version,
                "conversion_factor": factor,
                "loss_reason": loss_reason,
                "tolerance_ratio": tolerance,
                "request_digest": digest,
                "operator_user_id": principal.user_id,
                "note": data.get("note", ""),
            },
            now,
        )
        children = []
        for position, item in enumerate(children_data, start=1):
            child = self.samples.create(
                {
                    "sample_code": item["sample_code"],
                    "batch_id": parent["batch_id"],
                    "collection_event_id": parent["collection_event_id"],
                    "parent_sample_id": sample_id,
                    "root_sample_id": parent["root_sample_id"],
                    "sample_type": item.get("sample_type") or parent["sample_type"],
                    "quantity": item["quantity"],
                    "unit": output_unit,
                    "lifecycle_state": "available",
                    "location_id": item.get("location_id", parent["location_id"]),
                    "custody_user_id": principal.user_id,
                    "lineage_depth": parent["lineage_depth"] + 1,
                },
                now,
            )
            lineage.insert_child_link(operation["id"], child["id"], item["quantity"], position)
            self.samples.append_event(child["id"], "aliquot.created", principal.user_id, now, to_state="available", details={"parent_sample_id": sample_id, "operation_code": operation_code})
            children.append(child)
        self.samples.append_event(sample_id, "aliquot.source", principal.user_id, now, quantity_delta=-data["requested_quantity"], details={"operation_code": operation_code, "child_ids": [item["id"] for item in children]})
        self.audit.record(principal, "sample.aliquot", "sample", str(sample_id), before=parent, after=updated_parent, metadata={"operation_code": operation_code})
        return {"operation_code": operation_code, "operation": operation, "parent": updated_parent, "children": children, "replayed": False}

    def consume(self, principal: Principal, sample_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.consume")
        sample = self.samples.get(sample_id)
        if sample["lifecycle_state"] in {"destroyed", "pending_destruction", "quarantined"}:
            raise ConflictError("当前状态禁止消耗")
        existing = self.connection.execute(
            "SELECT * FROM consumption_records WHERE sample_id=? AND idempotency_key=?",
            (sample_id, data["idempotency_key"]),
        ).fetchone()
        if existing:
            return {"record": dict(existing), "sample": self.samples.get(sample_id), "replayed": True}
        if sample["quantity"] - sample["reserved_quantity"] < data["quantity"]:
            raise ConflictError("可用数量不足")
        now = to_storage(self.clock.now())
        updated = self.samples.change_quantity(sample_id, -data["quantity"], sample["version"], now)
        new_state = "consumed" if updated["quantity"] == 0 else "partially_consumed"
        updated = self.samples.set_state(sample_id, new_state, updated["version"], now)
        cursor = self.connection.execute(
            """INSERT INTO consumption_records(sample_id,experiment_code,quantity,operator_user_id,idempotency_key,occurred_at,note,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (sample_id, data["experiment_code"], data["quantity"], principal.user_id, data["idempotency_key"], now, data.get("note", ""), now),
        )
        record = dict(self.connection.execute("SELECT * FROM consumption_records WHERE id=?", (cursor.lastrowid,)).fetchone())
        self.samples.append_event(sample_id, "consumed", principal.user_id, now, quantity_delta=-data["quantity"], from_state=sample["lifecycle_state"], to_state=new_state, details={"experiment_code": data["experiment_code"]})
        self.audit.record(principal, "sample.consume", "sample", str(sample_id), before=sample, after=updated)
        return {"record": record, "sample": updated, "replayed": False}


class LoanService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("loans.manage")
        sample = self.samples.get(data["sample_id"])
        if sample["lifecycle_state"] not in {"available", "partially_consumed"}:
            raise ConflictError("样品当前不可借用")
        if sample["quantity"] - sample["reserved_quantity"] < data["quantity"]:
            raise ConflictError("可借数量不足")
        now = to_storage(self.clock.now())
        loan_code = data.get("loan_code") or f"LOAN-{uuid.uuid4().hex[:12]}"
        cursor = self.connection.execute(
            """INSERT INTO loans(loan_code,sample_id,borrower_user_id,quantity,due_at,state,created_at,updated_at)
               VALUES(?,?,?,?,?,'active',?,?)""",
            (loan_code, data["sample_id"], data["borrower_user_id"], data["quantity"], data["due_at"], now, now),
        )
        self.connection.execute(
            "UPDATE samples SET reserved_quantity=reserved_quantity+?,lifecycle_state='loaned',version=version+1,updated_at=? WHERE id=?",
            (data["quantity"], now, data["sample_id"]),
        )
        loan = dict(self.connection.execute("SELECT * FROM loans WHERE id=?", (cursor.lastrowid,)).fetchone())
        self.samples.append_event(data["sample_id"], "loaned", principal.user_id, now, from_state=sample["lifecycle_state"], to_state="loaned", details={"loan_id": loan["id"], "borrower_user_id": data["borrower_user_id"]})
        self.audit.record(principal, "loan.create", "loan", str(loan["id"]), after=loan)
        return loan

    def return_loan(self, principal: Principal, loan_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("loans.manage")
        loan_row = self.connection.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not loan_row:
            raise NotFoundError("借用记录不存在")
        loan = dict(loan_row)
        if loan["state"] not in {"active", "partially_returned", "overdue"}:
            raise ConflictError("借用记录已经结束")
        remaining = loan["quantity"] - loan["returned_quantity"]
        if data["quantity"] > remaining:
            raise ValidationError("归还数量超过未归还数量")
        now = to_storage(self.clock.now())
        returned = loan["returned_quantity"] + data["quantity"]
        state = "returned" if abs(returned - loan["quantity"]) < 1e-9 else "partially_returned"
        self.connection.execute(
            "UPDATE loans SET returned_quantity=?,state=?,version=version+1,updated_at=? WHERE id=?",
            (returned, state, now, loan_id),
        )
        self.connection.execute(
            """UPDATE samples SET reserved_quantity=reserved_quantity-?,
               lifecycle_state=CASE WHEN reserved_quantity-?=0 THEN CASE WHEN quantity=0 THEN 'consumed' ELSE 'available' END ELSE 'loaned' END,
               version=version+1,updated_at=? WHERE id=?""",
            (data["quantity"], data["quantity"], now, loan["sample_id"]),
        )
        result = dict(self.connection.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone())
        self.samples.append_event(loan["sample_id"], "returned", principal.user_id, now, quantity_delta=0, details={"loan_id": loan_id, "returned_quantity": data["quantity"]})
        self.audit.record(principal, "loan.return", "loan", str(loan_id), before=loan, after=result)
        return result


class ApprovalService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.approvals = ApprovalRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        if data["action_type"] == "destruction":
            principal.require("samples.destroy")
        elif data["action_type"] == "inventory_adjustment":
            principal.require("inventory.manage")
        else:
            principal.require("samples.write")
        now_dt = self.clock.now()
        values = dict(data)
        values["expires_at"] = values.get("expires_at") or to_storage(now_dt + timedelta(days=3))
        request_code = values.get("request_code") or f"APR-{uuid.uuid4().hex[:12]}"
        request = self.approvals.create(values, principal.user_id, request_code, to_storage(now_dt))
        self.audit.record(principal, "approval.request", "approval_request", str(request["id"]), after=request)
        return request

    def decide(self, principal: Principal, request_id: int, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("approvals.decide")
        before = self.approvals.get(request_id)
        result = self.approvals.decide(request_id, principal.user_id, data["decision"], data.get("comment", ""), to_storage(self.clock.now()))
        self.audit.record(principal, "approval.decide", "approval_request", str(request_id), before=before, after=result)
        return result


class AnomalyService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.anomalies = AnomalyRepository(connection)
        self.samples = SampleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def create(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("anomalies.manage")
        if not data.get("sample_id") and not data.get("batch_id"):
            raise ValidationError("异常必须关联样品或接收批次")
        if data.get("sample_id"):
            self.samples.get(data["sample_id"])
        case_code = data.get("case_code") or f"ANM-{uuid.uuid4().hex[:12]}"
        case = self.anomalies.create(data, principal.user_id, case_code, to_storage(self.clock.now()))
        self.audit.record(principal, "anomaly.create", "anomaly_case", str(case["id"]), after=case)
        return case

    def list(self, principal: Principal, state: str | None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        return self.anomalies.list(state)
