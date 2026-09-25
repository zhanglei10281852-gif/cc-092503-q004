from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal, InvalidOperation
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService

DEFAULT_TOLERANCE = 0.000001
QUANTIZE = Decimal("0.000000001")

# 报告问题类别的固定排序，保证相同数据得到相同问题顺序
PROBLEM_KIND_ORDER = {
    "cycle": 0,
    "broken_link": 1,
    "duplicate_code": 2,
    "tolerance_exceeded": 3,
}


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"无法解析的数值：{value}") from exc


def _number(value: Decimal) -> float:
    """汇总数值统一保留 9 位小数，保证相同数据生成完全相同的摘要。"""
    return float(value.quantize(QUANTIZE))


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(payload: Any) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def conservation_residual(
    input_quantity: Any,
    factor: Any,
    output_quantity: Any,
    loss_quantity: Any,
) -> Decimal:
    """直接守恒残差（输出单位）：换算后投入 - 产出 - 可解释损耗。"""
    return _decimal(input_quantity) * _decimal(factor) - _decimal(output_quantity) - _decimal(loss_quantity)


class LineageRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def sample(self, sample_id: int) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone()
        return dict(row) if row else None

    def tree_samples(self, root_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM samples WHERE root_sample_id=? OR id=? ORDER BY id",
            (root_id, root_id),
        ).fetchall()
        return [dict(row) for row in rows]

    def children_of(self, parent_ids: list[int]) -> list[dict[str, Any]]:
        if not parent_ids:
            return []
        placeholders = ",".join("?" for _ in parent_ids)
        rows = self.connection.execute(
            f"SELECT * FROM samples WHERE parent_sample_id IN ({placeholders}) ORDER BY id",
            tuple(parent_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def operations_for_parents(self, parent_ids: list[int], *, active_only: bool = True) -> list[dict[str, Any]]:
        if not parent_ids:
            return []
        placeholders = ",".join("?" for _ in parent_ids)
        sql = f"SELECT * FROM lineage_operations WHERE parent_sample_id IN ({placeholders})"
        if active_only:
            sql += " AND status='active'"
        sql += " ORDER BY operation_code,version"
        return [dict(row) for row in self.connection.execute(sql, tuple(parent_ids)).fetchall()]

    def operation_versions(self, operation_code: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM lineage_operations WHERE operation_code=? ORDER BY version",
            (operation_code,),
        ).fetchall()
        return [dict(row) for row in rows]

    def active_operation(self, operation_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM lineage_operations WHERE operation_code=? AND status='active' ORDER BY version DESC LIMIT 1",
            (operation_code,),
        ).fetchone()
        return dict(row) if row else None

    def operation_children(self, operation_ids: list[int]) -> list[dict[str, Any]]:
        if not operation_ids:
            return []
        placeholders = ",".join("?" for _ in operation_ids)
        rows = self.connection.execute(
            f"""SELECT c.operation_id,c.child_sample_id,c.quantity,
                       o.parent_sample_id AS operation_parent_id,
                       s.sample_code,s.parent_sample_id,s.unit
                FROM lineage_operation_children c
                JOIN lineage_operations o ON o.id=c.operation_id
                JOIN samples s ON s.id=c.child_sample_id
                WHERE c.operation_id IN ({placeholders})
                ORDER BY c.operation_id,c.child_sample_id""",
            tuple(operation_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def consumptions(self, sample_ids: list[int]) -> list[dict[str, Any]]:
        if not sample_ids:
            return []
        placeholders = ",".join("?" for _ in sample_ids)
        rows = self.connection.execute(
            f"""SELECT c.sample_id,c.quantity,s.unit FROM consumption_records c
                JOIN samples s ON s.id=c.sample_id WHERE c.sample_id IN ({placeholders}) ORDER BY c.id""",
            tuple(sample_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def destructions(self, sample_ids: list[int]) -> list[dict[str, Any]]:
        if not sample_ids:
            return []
        placeholders = ",".join("?" for _ in sample_ids)
        rows = self.connection.execute(
            f"""SELECT d.sample_id,d.destroyed_quantity,s.unit FROM destruction_records d
                JOIN samples s ON s.id=d.sample_id WHERE d.sample_id IN ({placeholders}) ORDER BY d.id""",
            tuple(sample_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def quantity_deltas(self, sample_id: int) -> float:
        row = self.connection.execute(
            "SELECT COALESCE(SUM(quantity_delta),0) FROM sample_events WHERE sample_id=?",
            (sample_id,),
        ).fetchone()
        return float(row[0])


class ConversionRuleService:
    """换算规则按 rule_code 版本化追加，历史版本永不修改，保证旧操作可复算。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.audit = AuditService(connection, self.clock)

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        factor = _decimal(data["factor"])
        if factor <= 0:
            raise ValidationError("换算系数必须为正数")
        if data["from_unit"] == data["to_unit"]:
            raise ValidationError("源单位与目标单位不能相同")
        latest_row = self.connection.execute(
            "SELECT * FROM unit_conversion_rules WHERE rule_code=? ORDER BY version DESC LIMIT 1",
            (data["rule_code"],),
        ).fetchone()
        if latest_row:
            latest = dict(latest_row)
            identical = (
                latest["from_unit"] == data["from_unit"]
                and latest["to_unit"] == data["to_unit"]
                and _decimal(latest["factor"]) == factor
                and abs(float(latest["default_tolerance"]) - float(data.get("default_tolerance", DEFAULT_TOLERANCE))) < 1e-12
                and latest["note"] == data.get("note", "")
            )
            if identical:
                return {**latest, "replayed": True}
            version = int(latest["version"]) + 1
        else:
            version = 1
        now = to_storage(self.clock.now())
        cursor = self.connection.execute(
            """INSERT INTO unit_conversion_rules(
                   rule_code,version,from_unit,to_unit,factor,default_tolerance,note,created_by,created_at
               ) VALUES(?,?,?,?,?,?,?,?,?)""",
            (
                data["rule_code"], version, data["from_unit"], data["to_unit"],
                str(factor), data.get("default_tolerance", DEFAULT_TOLERANCE),
                data.get("note", ""), principal.user_id, now,
            ),
        )
        rule = dict(
            self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
        )
        self.audit.record(
            principal,
            "conversion_rule.register",
            "unit_conversion_rule",
            str(rule["id"]),
            after=rule,
            metadata={"rule_code": rule["rule_code"], "version": version},
        )
        return {**rule, "replayed": False}

    def list(self, principal: Principal, rule_code: str | None = None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        if rule_code:
            rows = self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE rule_code=? ORDER BY version", (rule_code,)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM unit_conversion_rules ORDER BY rule_code,version"
            ).fetchall()
        return [dict(row) for row in rows]

    def resolve(self, rule_code: str, version: int | None = None) -> dict[str, Any]:
        if version is None:
            row = self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE rule_code=? ORDER BY version DESC LIMIT 1",
                (rule_code,),
            ).fetchone()
        else:
            row = self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE rule_code=? AND version=?",
                (rule_code, version),
            ).fetchone()
        if not row:
            raise NotFoundError("换算规则不存在")
        return dict(row)


class LineageOperationService:
    """谱系操作：提交时固化换算快照与守恒残差，更正只追加新版本，历史版本永不改写。"""

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = LineageRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def record(
        self,
        *,
        operation_code: str,
        operation_kind: str,
        parent_sample_id: int,
        input_unit: str,
        output_unit: str,
        input_quantity: float,
        output_quantity: float,
        loss_quantity: float,
        loss_reason: str,
        rule: dict[str, Any] | None,
        factor: Decimal,
        tolerance: float,
        children: list[dict[str, Any]],
        operator_user_id: int,
        note: str,
        now: str,
    ) -> dict[str, Any]:
        residual = conservation_residual(input_quantity, factor, output_quantity, loss_quantity)
        cursor = self.connection.execute(
            """INSERT INTO lineage_operations(
                   operation_code,version,status,operation_kind,parent_sample_id,
                   input_unit,output_unit,input_quantity,output_quantity,loss_quantity,loss_reason,
                   conversion_rule_id,conversion_rule_code,conversion_rule_version,conversion_factor,
                   tolerance,residual,operator_user_id,occurred_at,note,created_at
               ) VALUES(?,1,'active',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_code, operation_kind, parent_sample_id, input_unit, output_unit,
                input_quantity, output_quantity, loss_quantity, loss_reason,
                rule["id"] if rule else None,
                rule["rule_code"] if rule else None,
                rule["version"] if rule else None,
                str(factor), tolerance, float(residual), operator_user_id, now, note, now,
            ),
        )
        operation_id = cursor.lastrowid
        for child in children:
            self.connection.execute(
                "INSERT INTO lineage_operation_children(operation_id,child_sample_id,quantity) VALUES(?,?,?)",
                (operation_id, child["id"], child["quantity"]),
            )
        return dict(
            self.connection.execute(
                "SELECT * FROM lineage_operations WHERE id=?", (operation_id,)
            ).fetchone()
        )

    def history(self, principal: Principal, operation_code: str) -> dict[str, Any]:
        principal.require("samples.read")
        versions = self.repository.operation_versions(operation_code)
        if not versions:
            raise NotFoundError("谱系操作不存在")
        links = self.repository.operation_children([item["id"] for item in versions])
        by_operation: dict[int, list[dict[str, Any]]] = {}
        for link in links:
            by_operation.setdefault(link["operation_id"], []).append(link)
        for item in versions:
            item["children"] = by_operation.get(item["id"], [])
        return {"operation_code": operation_code, "versions": versions}

    def correct(self, principal: Principal, operation_code: str, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        active = self.repository.active_operation(operation_code)
        if not active:
            raise NotFoundError("谱系操作不存在")
        loss_quantity = float(data["loss_quantity"]) if data.get("loss_quantity") is not None else float(active["loss_quantity"])
        loss_reason = data["loss_reason"] if data.get("loss_reason") is not None else active["loss_reason"]
        tolerance = float(data["tolerance"]) if data.get("tolerance") is not None else float(active["tolerance"])
        if loss_quantity < 0:
            raise ValidationError("损耗数量不能为负数")
        if tolerance < 0:
            raise ValidationError("容差不能为负数")
        # 换算快照沿用历史版本，更正只允许修订损耗解释与容差，不得回写换算规则
        residual = conservation_residual(
            active["input_quantity"], active["conversion_factor"], active["output_quantity"], loss_quantity
        )
        if abs(residual) > _decimal(tolerance):
            raise ValidationError(
                "更正后仍不满足直接守恒",
                context={"residual": float(residual), "tolerance": tolerance},
            )
        now = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE lineage_operations SET status='superseded' WHERE id=?", (active["id"],)
        )
        cursor = self.connection.execute(
            """INSERT INTO lineage_operations(
                   operation_code,version,status,operation_kind,parent_sample_id,
                   input_unit,output_unit,input_quantity,output_quantity,loss_quantity,loss_reason,
                   conversion_rule_id,conversion_rule_code,conversion_rule_version,conversion_factor,
                   tolerance,residual,operator_user_id,occurred_at,note,created_at
               ) VALUES(?,?,'active',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                operation_code, int(active["version"]) + 1, active["operation_kind"],
                active["parent_sample_id"], active["input_unit"], active["output_unit"],
                active["input_quantity"], active["output_quantity"], loss_quantity, loss_reason,
                active["conversion_rule_id"], active["conversion_rule_code"],
                active["conversion_rule_version"], active["conversion_factor"],
                tolerance, float(residual), principal.user_id, now, data["note"], now,
            ),
        )
        new_id = cursor.lastrowid
        for link in self.repository.operation_children([active["id"]]):
            self.connection.execute(
                "INSERT INTO lineage_operation_children(operation_id,child_sample_id,quantity) VALUES(?,?,?)",
                (new_id, link["child_sample_id"], link["quantity"]),
            )
        corrected = dict(
            self.connection.execute("SELECT * FROM lineage_operations WHERE id=?", (new_id,)).fetchone()
        )
        self.audit.record(
            principal,
            "lineage_operation.correct",
            "lineage_operation",
            str(new_id),
            before=active,
            after=corrected,
            metadata={"operation_code": operation_code, "version": corrected["version"]},
        )
        return corrected


def _sample_brief(sample: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": sample["id"],
        "sample_code": sample["sample_code"],
        "quantity": sample["quantity"],
        "unit": sample["unit"],
        "lifecycle_state": sample["lifecycle_state"],
        "lineage_depth": sample["lineage_depth"],
    }


def _operation_brief(operation: dict[str, Any]) -> dict[str, Any]:
    return {
        "operation_code": operation["operation_code"],
        "version": operation["version"],
        "operation_kind": operation["operation_kind"],
        "input_unit": operation["input_unit"],
        "output_unit": operation["output_unit"],
        "input_quantity": operation["input_quantity"],
        "output_quantity": operation["output_quantity"],
        "loss_quantity": operation["loss_quantity"],
        "loss_reason": operation["loss_reason"],
        "conversion_rule_code": operation["conversion_rule_code"],
        "conversion_rule_version": operation["conversion_rule_version"],
        "conversion_factor": operation["conversion_factor"],
    }


def _problem_sort_key(problem: dict[str, Any]) -> tuple:
    return (
        PROBLEM_KIND_ORDER.get(problem["kind"], 99),
        str(problem["code"]),
        _canonical(problem["details"]),
    )


class LineageTraceService:
    """从任意节点沿 parent_sample_id 向上追溯到谱系根。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.repository = LineageRepository(connection)

    def ancestors(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.repository.sample(sample_id)
        if not sample:
            raise NotFoundError("样品不存在")
        problems: list[dict[str, Any]] = []
        hops: list[dict[str, Any]] = []
        visited = {sample_id}
        current: dict[str, Any] | None = sample
        root_id: int | None = None
        while current and current["parent_sample_id"] is not None:
            parent_id = int(current["parent_sample_id"])
            parent = self.repository.sample(parent_id)
            if parent is None:
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": current["sample_code"],
                        "message": "父样记录不存在，谱系断链",
                        "details": {"sample_id": current["id"], "missing_parent_id": parent_id},
                    }
                )
                current = None
                break
            operation = self._producing_operation(parent["id"], current["id"])
            hops.append(
                {
                    "depth": parent["lineage_depth"],
                    "sample": _sample_brief(parent),
                    "operation": _operation_brief(operation) if operation else None,
                }
            )
            if parent["id"] in visited:
                problems.append(
                    {
                        "kind": "cycle",
                        "code": parent["sample_code"],
                        "message": "谱系存在循环引用",
                        "details": {"sample_id": parent["id"], "sample_code": parent["sample_code"]},
                    }
                )
                current = None
                break
            visited.add(parent["id"])
            current = parent
        if current and current["parent_sample_id"] is None:
            root_id = current["id"]
            if current["root_sample_id"] not in (None, current["id"]):
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": current["sample_code"],
                        "message": "谱系根编码与实际根不一致",
                        "details": {"sample_id": current["id"], "stored_root_id": current["root_sample_id"]},
                    }
                )
        return {
            "sample_id": sample_id,
            "root_sample_id": root_id,
            "complete": not problems,
            "hops": hops,
            "problems": sorted(problems, key=_problem_sort_key),
        }

    def _producing_operation(self, parent_id: int, child_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT o.* FROM lineage_operations o
               JOIN lineage_operation_children c ON c.operation_id=o.id
               WHERE o.parent_sample_id=? AND c.child_sample_id=? AND o.status='active'
               ORDER BY o.id LIMIT 1""",
            (parent_id, child_id),
        ).fetchone()
        return dict(row) if row else None


class LineageAnalysisService:
    """向下汇总与跨层报告：只读、确定性输出，相同数据必得相同摘要与问题排序。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.repository = LineageRepository(connection)

    def rollup(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.repository.sample(sample_id)
        if not sample:
            raise NotFoundError("样品不存在")
        nodes = self._subtree(sample_id)
        summary = self._aggregate(nodes, anchor_id=sample_id)
        return {
            "sample_id": sample_id,
            "sample_code": sample["sample_code"],
            "summary": summary,
            "digest": _digest({"sample_id": sample_id, "summary": summary}),
        }

    def report(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.repository.sample(sample_id)
        if not sample:
            raise NotFoundError("样品不存在")
        root_id = self._resolve_root(sample)
        nodes = self.repository.tree_samples(root_id)
        problems = self._detect_problems(nodes, root_id)
        summary = self._aggregate(nodes, anchor_id=root_id)
        counts = {kind: 0 for kind in PROBLEM_KIND_ORDER}
        for problem in problems:
            counts[problem["kind"]] = counts.get(problem["kind"], 0) + 1
        summary["problem_counts"] = counts
        ordered = sorted(problems, key=_problem_sort_key)
        return {
            "root_sample_id": root_id,
            "summary": summary,
            "problems": ordered,
            "digest": _digest({"root_sample_id": root_id, "summary": summary, "problems": ordered}),
        }

    def _resolve_root(self, sample: dict[str, Any]) -> int:
        """谱系归属以登记的 root_sample_id 为准；缺失时才沿父链向上推断。"""
        stored = sample["root_sample_id"]
        if stored and self.repository.sample(int(stored)):
            return int(stored)
        visited = {sample["id"]}
        current = sample
        while current["parent_sample_id"] is not None:
            parent = self.repository.sample(int(current["parent_sample_id"]))
            if parent is None or parent["id"] in visited:
                break
            visited.add(parent["id"])
            current = parent
        return current["id"]

    def _subtree(self, root_id: int) -> list[dict[str, Any]]:
        root = self.repository.sample(root_id)
        if root is None:
            return []
        nodes: list[dict[str, Any]] = []
        visited: set[int] = {root_id}
        frontier = [root_id]
        while frontier:
            children = [child for child in self.repository.children_of(frontier) if child["id"] not in visited]
            if not children:
                break
            visited.update(child["id"] for child in children)
            nodes.extend(children)
            frontier = [child["id"] for child in children]
        return [root] + nodes

    def _aggregate(self, nodes: list[dict[str, Any]], *, anchor_id: int) -> dict[str, Any]:
        by_id = {node["id"]: node for node in nodes}
        ids = sorted(by_id)
        operations = self.repository.operations_for_parents(ids)
        links = self.repository.operation_children([item["id"] for item in operations])
        consumption_rows = self.repository.consumptions(ids)
        destruction_rows = self.repository.destructions(ids)
        factors, incomplete = self._path_factors(by_id, operations, links, anchor_id)

        remaining: dict[str, Decimal] = {}
        for node in nodes:
            remaining[node["unit"]] = remaining.get(node["unit"], Decimal(0)) + _decimal(node["quantity"])
        consumed: dict[str, Decimal] = {}
        for row in consumption_rows:
            consumed[row["unit"]] = consumed.get(row["unit"], Decimal(0)) + _decimal(row["quantity"])
        destroyed: dict[str, Decimal] = {}
        for row in destruction_rows:
            destroyed[row["unit"]] = destroyed.get(row["unit"], Decimal(0)) + _decimal(row["destroyed_quantity"])
        loss: dict[str, Decimal] = {}
        for operation in operations:
            unit = operation["output_unit"]
            loss[unit] = loss.get(unit, Decimal(0)) + _decimal(operation["loss_quantity"])

        normalized = self._normalize(
            by_id,
            operations,
            factors,
            incomplete,
            anchor_id,
            consumption_rows=consumption_rows,
            destruction_rows=destruction_rows,
        )
        depths = [int(node["lineage_depth"]) for node in nodes]
        return {
            "sample_count": len(nodes),
            "operation_count": len(operations),
            "max_depth": max(depths) if depths else 0,
            "remaining_by_unit": {unit: _number(value) for unit, value in sorted(remaining.items())},
            "consumed_by_unit": {unit: _number(value) for unit, value in sorted(consumed.items())},
            "destroyed_by_unit": {unit: _number(value) for unit, value in sorted(destroyed.items())},
            "loss_by_unit": {unit: _number(value) for unit, value in sorted(loss.items())},
            "normalized": normalized,
        }

    def _path_factors(
        self,
        by_id: dict[int, dict[str, Any]],
        operations: list[dict[str, Any]],
        links: list[dict[str, Any]],
        anchor_id: int,
    ) -> tuple[dict[int, Decimal], list[str]]:
        """每个节点换算到锚点单位的系数；缺换算信息的谱系分支记入 incomplete。"""
        factors: dict[int, Decimal] = {anchor_id: Decimal(1)}
        incomplete: set[str] = set()
        producing: dict[int, dict[str, Any]] = {}
        for operation in operations:
            for link in links:
                if link["operation_id"] == operation["id"]:
                    producing.setdefault(link["child_sample_id"], operation)
        children_by_parent: dict[int, list[dict[str, Any]]] = {}
        for node in by_id.values():
            parent_id = node["parent_sample_id"]
            if parent_id is not None:
                children_by_parent.setdefault(int(parent_id), []).append(node)
        queue = [anchor_id]
        while queue:
            next_queue: list[int] = []
            for parent_id in queue:
                parent = by_id.get(parent_id)
                if parent is None or parent_id not in factors:
                    continue
                for node in children_by_parent.get(parent_id, []):
                    if node["id"] in factors:
                        continue
                    operation = producing.get(node["id"])
                    if operation is None:
                        if node["unit"] == parent["unit"]:
                            factors[node["id"]] = factors[parent_id]
                        else:
                            incomplete.add(f"{node['sample_code']}:缺少{parent['unit']}到{node['unit']}的换算记录")
                    else:
                        factor = _decimal(operation["conversion_factor"])
                        if factor <= 0:
                            incomplete.add(f"{node['sample_code']}:换算快照缺失")
                        else:
                            factors[node["id"]] = factors[parent_id] / factor
                    next_queue.append(node["id"])
            queue = next_queue
        for node in by_id.values():
            if node["id"] not in factors:
                incomplete.add(f"{node['sample_code']}:无法连接到谱系锚点")
        return factors, sorted(incomplete)

    def _normalize(
        self,
        by_id: dict[int, dict[str, Any]],
        operations: list[dict[str, Any]],
        factors: dict[int, Decimal],
        incomplete: list[str],
        anchor_id: int,
        *,
        consumption_rows: list[dict[str, Any]],
        destruction_rows: list[dict[str, Any]],
    ) -> dict[str, Any]:
        anchor = by_id.get(anchor_id)
        if anchor is None:
            return {"unit": None, "complete": False, "incomplete_reasons": incomplete}
        unit = anchor["unit"]
        remaining_total = sum(
            (_decimal(node["quantity"]) * factors[node["id"]] for node in by_id.values() if node["id"] in factors),
            Decimal(0),
        )
        consumed_total = sum(
            (_decimal(row["quantity"]) * factors[row["sample_id"]] for row in consumption_rows if row["sample_id"] in factors),
            Decimal(0),
        )
        destroyed_total = sum(
            (_decimal(row["destroyed_quantity"]) * factors[row["sample_id"]] for row in destruction_rows if row["sample_id"] in factors),
            Decimal(0),
        )
        loss_total = Decimal(0)
        for operation in operations:
            parent_id = operation["parent_sample_id"]
            factor = _decimal(operation["conversion_factor"])
            if parent_id in factors and factor > 0:
                loss_total += _decimal(operation["loss_quantity"]) * factors[parent_id] / factor
        accounted = remaining_total + consumed_total + destroyed_total + loss_total
        initial = _decimal(anchor["quantity"]) - _decimal(self.repository.quantity_deltas(anchor_id))
        return {
            "unit": unit,
            "complete": not incomplete,
            "incomplete_reasons": incomplete,
            "initial_quantity": _number(initial),
            "remaining": _number(remaining_total),
            "consumed": _number(consumed_total),
            "destroyed": _number(destroyed_total),
            "loss": _number(loss_total),
            "conservation_delta": _number(initial - accounted),
        }

    def _detect_problems(self, nodes: list[dict[str, Any]], root_id: int) -> list[dict[str, Any]]:
        by_id = {node["id"]: node for node in nodes}
        operations = self.repository.operations_for_parents(sorted(by_id))
        links = self.repository.operation_children([item["id"] for item in operations])
        problems: list[dict[str, Any]] = []
        problems.extend(self._cycle_problems(by_id))
        problems.extend(self._link_problems(by_id, root_id, links))
        problems.extend(self._duplicate_problems(by_id, operations, links))
        problems.extend(self._tolerance_problems(operations))
        return problems

    def _cycle_problems(self, by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        problems = []
        seen_loops: set[frozenset[int]] = set()
        for node in sorted(by_id.values(), key=lambda item: item["id"]):
            chain: list[int] = []
            positions: dict[int, int] = {}
            current_id = node["id"]
            while True:
                if current_id in positions:
                    loop = frozenset(chain[positions[current_id]:])
                    if loop and loop not in seen_loops:
                        seen_loops.add(loop)
                        codes = []
                        for member_id in sorted(loop):
                            member = by_id.get(member_id) or self.repository.sample(member_id)
                            if member:
                                codes.append(member["sample_code"])
                        anchor_id = min(loop)
                        anchor = by_id.get(anchor_id) or self.repository.sample(anchor_id)
                        problems.append(
                            {
                                "kind": "cycle",
                                "code": anchor["sample_code"] if anchor else str(anchor_id),
                                "message": "谱系存在循环引用",
                                "details": {"sample_ids": sorted(loop), "sample_codes": sorted(codes)},
                            }
                        )
                    break
                positions[current_id] = len(chain)
                chain.append(current_id)
                row = by_id.get(current_id) or self.repository.sample(current_id)
                if row is None or row["parent_sample_id"] is None:
                    break
                current_id = int(row["parent_sample_id"])
        return problems

    def _link_problems(
        self,
        by_id: dict[int, dict[str, Any]],
        root_id: int,
        links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        problems = []
        root = by_id.get(root_id)
        if root and root["root_sample_id"] not in (None, root_id):
            problems.append(
                {
                    "kind": "broken_link",
                    "code": root["sample_code"],
                    "message": "根样品的根编码指向其他样品",
                    "details": {"sample_id": root_id, "stored_root_id": root["root_sample_id"]},
                }
            )
        computed_depth = self._computed_depths(by_id, root_id)
        for node in sorted(by_id.values(), key=lambda item: item["id"]):
            if node["id"] == root_id:
                continue
            parent_id = node["parent_sample_id"]
            if parent_id is None:
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": node["sample_code"],
                        "message": "非根样品缺少父样引用",
                        "details": {"sample_id": node["id"]},
                    }
                )
                continue
            if int(parent_id) not in by_id:
                parent = self.repository.sample(int(parent_id))
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": node["sample_code"],
                        "message": "父样不在本谱系内",
                        "details": {
                            "sample_id": node["id"],
                            "parent_sample_id": int(parent_id),
                            "parent_found": parent is not None,
                        },
                    }
                )
                continue
            if int(node["root_sample_id"] or 0) != root_id:
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": node["sample_code"],
                        "message": "谱系根编码与父链不一致",
                        "details": {"sample_id": node["id"], "stored_root_id": node["root_sample_id"]},
                    }
                )
            depth = computed_depth.get(node["id"])
            if depth is not None and depth != int(node["lineage_depth"]):
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": node["sample_code"],
                        "message": "谱系深度与父链不一致",
                        "details": {
                            "sample_id": node["id"],
                            "stored_depth": node["lineage_depth"],
                            "computed_depth": depth,
                        },
                    }
                )
        # 父样在谱系内、但自身根编码指向别处的“掉队”子样
        if by_id:
            placeholders = ",".join("?" for _ in by_id)
            rows = self.connection.execute(
                f"""SELECT id,sample_code,parent_sample_id,root_sample_id FROM samples
                    WHERE parent_sample_id IN ({placeholders}) AND (root_sample_id IS NULL OR root_sample_id<>?)
                    ORDER BY id""",
                (*sorted(by_id), root_id),
            ).fetchall()
            for row in rows:
                if row["id"] in by_id:
                    continue
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": row["sample_code"],
                        "message": "子样根编码与父样谱系不一致",
                        "details": {
                            "sample_id": row["id"],
                            "parent_sample_id": row["parent_sample_id"],
                            "stored_root_id": row["root_sample_id"],
                        },
                    }
                )
        # 操作记录的父子关系与样品实际父链不一致
        for link in links:
            child = by_id.get(link["child_sample_id"])
            if child is None:
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": link["sample_code"],
                        "message": "操作产出的子样不在本谱系内",
                        "details": {
                            "operation_id": link["operation_id"],
                            "child_sample_id": link["child_sample_id"],
                        },
                    }
                )
            elif int(link["operation_parent_id"]) != int(child["parent_sample_id"] or 0):
                problems.append(
                    {
                        "kind": "broken_link",
                        "code": link["sample_code"],
                        "message": "操作记录的父样与子样实际父样不一致",
                        "details": {
                            "operation_id": link["operation_id"],
                            "child_sample_id": link["child_sample_id"],
                        },
                    }
                )
        return problems

    def _computed_depths(self, by_id: dict[int, dict[str, Any]], root_id: int) -> dict[int, int]:
        depths: dict[int, int] = {root_id: 0}
        changed = True
        while changed:
            changed = False
            for node in sorted(by_id.values(), key=lambda item: item["id"]):
                if node["id"] in depths:
                    continue
                parent_id = node["parent_sample_id"]
                if parent_id is not None and int(parent_id) in depths:
                    depths[node["id"]] = depths[int(parent_id)] + 1
                    changed = True
        return depths

    def _duplicate_problems(
        self,
        by_id: dict[int, dict[str, Any]],
        operations: list[dict[str, Any]],
        links: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        problems = []
        codes: dict[str, list[int]] = {}
        for node in by_id.values():
            codes.setdefault(node["sample_code"], []).append(node["id"])
        for code, ids in sorted(codes.items()):
            if len(ids) > 1:
                problems.append(
                    {
                        "kind": "duplicate_code",
                        "code": code,
                        "message": "样品编码在谱系内重复",
                        "details": {"sample_ids": sorted(ids)},
                    }
                )
        active_codes: dict[str, list[int]] = {}
        for operation in operations:
            active_codes.setdefault(operation["operation_code"], []).append(operation["id"])
        for code, ids in sorted(active_codes.items()):
            if len(ids) > 1:
                problems.append(
                    {
                        "kind": "duplicate_code",
                        "code": code,
                        "message": "操作编码存在多个生效版本",
                        "details": {"operation_ids": sorted(ids)},
                    }
                )
        claimed: dict[int, list[int]] = {}
        for link in links:
            claimed.setdefault(link["child_sample_id"], []).append(link["operation_id"])
        for child_id, operation_ids in sorted(claimed.items()):
            if len(operation_ids) > 1:
                sample = by_id.get(child_id) or self.repository.sample(child_id) or {}
                problems.append(
                    {
                        "kind": "duplicate_code",
                        "code": sample.get("sample_code", str(child_id)),
                        "message": "同一子样被多个生效操作重复登记产出",
                        "details": {"child_sample_id": child_id, "operation_ids": sorted(operation_ids)},
                    }
                )
        return problems

    def _tolerance_problems(self, operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        problems = []
        for operation in operations:
            residual = conservation_residual(
                operation["input_quantity"],
                operation["conversion_factor"],
                operation["output_quantity"],
                operation["loss_quantity"],
            )
            if abs(residual) > _decimal(operation["tolerance"]):
                problems.append(
                    {
                        "kind": "tolerance_exceeded",
                        "code": operation["operation_code"],
                        "message": "操作守恒残差超出容差",
                        "details": {
                            "operation_id": operation["id"],
                            "parent_sample_id": operation["parent_sample_id"],
                            "residual": _number(residual),
                            "tolerance": float(operation["tolerance"]),
                        },
                    }
                )
        return problems
