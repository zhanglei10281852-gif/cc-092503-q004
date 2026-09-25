"""分装谱系的换算规则、向上追溯、向下汇总与跨层守恒报告。

设计约定：
- 每个谱系操作（分装/冻干/研磨/处理）在写入时固化处理前后单位、换算规则编码与版本、
  换算系数快照、可解释损耗和容差快照；后续换算规则升级只产生新版本，绝不回写历史操作。
- 汇总与报告只使用操作上的快照系数，因此相同数据永远得到相同的谱系摘要与问题排序。
- 所有列表输出按稳定键排序，摘要附带 canonical JSON 的 SHA-256 摘要值便于比对。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.core.security import Principal
from app.services.audit import AuditService

DEFAULT_TOLERANCE_RATIO = 0.005
ABS_TOLERANCE = 1e-6
OPERATION_KINDS = ("aliquot", "freeze_dry", "grind", "process")

ISSUE_SEVERITY = {
    "cycle": "critical",
    "broken_link": "high",
    "duplicate_code": "high",
    "tolerance_exceeded": "medium",
    "data_inconsistency": "medium",
}


def canonical_digest(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _round(value: float) -> float:
    return round(float(value), 9)


def _row(row: sqlite3.Row | None, message: str) -> dict[str, Any]:
    if row is None:
        raise NotFoundError(message)
    return dict(row)


# ---------------------------------------------------------------------------
# 纯函数：谱系图遍历与校验（不依赖数据库，便于确定性与单元测试）
# ---------------------------------------------------------------------------


def find_cycles(parent_of: dict[int, int | None]) -> list[list[int]]:
    """在父子映射中查找循环，返回去重后的循环路径列表（按发现顺序，起点 id 升序）。"""
    cycles: list[list[int]] = []
    reported: set[frozenset[int]] = set()
    for start in sorted(parent_of):
        path: list[int] = []
        index: dict[int, int] = {}
        node: int | None = start
        while node is not None and node in parent_of and node not in index:
            index[node] = len(path)
            path.append(node)
            node = parent_of[node]
        if node is not None and node in index:
            cycle = path[index[node] :]
            key = frozenset(cycle)
            if key not in reported:
                reported.add(key)
                cycles.append(cycle)
    return cycles


def compute_root(start_id: int, parent_of: dict[int, int | None]) -> int | None:
    """从节点向上走到最高已知祖先；遇到循环返回 None。"""
    seen: set[int] = set()
    node = start_id
    while True:
        parent = parent_of.get(node)
        if parent is None or parent not in parent_of:
            return node
        if node in seen:
            return None
        seen.add(node)
        node = parent


def detect_issues(
    nodes: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """跨层校验：断链、循环、重复编码、超出容差与数据不一致。

    输入为谱系节点、操作与子样链接的行字典；输出按 (kind, context) 稳定排序。
    """
    issues: list[dict[str, Any]] = []

    def add(kind: str, message: str, context: dict[str, Any]) -> None:
        issues.append(
            {
                "kind": kind,
                "severity": ISSUE_SEVERITY[kind],
                "message": message,
                "context": context,
            }
        )

    by_id = {node["id"]: node for node in nodes}
    parent_of = {node["id"]: node["parent_sample_id"] for node in nodes}

    for cycle in find_cycles(parent_of):
        add("cycle", "谱系父子关系存在循环", {"sample_ids": cycle})

    for node in sorted(nodes, key=lambda item: item["id"]):
        parent_id = node["parent_sample_id"]
        if parent_id is not None and parent_id not in by_id:
            add(
                "broken_link",
                "父样不在当前谱系范围内",
                {
                    "sample_id": node["id"],
                    "sample_code": node["sample_code"],
                    "parent_sample_id": parent_id,
                },
            )
            continue
        root = compute_root(node["id"], parent_of)
        if root is not None and root != (node["root_sample_id"] or node["id"]):
            add(
                "broken_link",
                "根样标识与实际谱系不一致",
                {
                    "sample_id": node["id"],
                    "sample_code": node["sample_code"],
                    "recorded_root_sample_id": node["root_sample_id"],
                    "actual_root_sample_id": root,
                },
            )

    sample_codes: dict[str, int] = {}
    for node in nodes:
        sample_codes[node["sample_code"]] = sample_codes.get(node["sample_code"], 0) + 1
    for code in sorted(code for code, count in sample_codes.items() if count > 1):
        add("duplicate_code", "样品编码在谱系内重复", {"sample_code": code})

    operation_codes: dict[str, int] = {}
    for operation in operations:
        code = operation["operation_code"]
        operation_codes[code] = operation_codes.get(code, 0) + 1
    for code in sorted(code for code, count in operation_codes.items() if count > 1):
        add("duplicate_code", "操作编码在谱系内重复", {"operation_code": code})

    links_by_child: dict[int, list[dict[str, Any]]] = {}
    links_by_operation: dict[int, list[dict[str, Any]]] = {}
    for link in links:
        links_by_child.setdefault(link["child_sample_id"], []).append(link)
        links_by_operation.setdefault(link["operation_id"], []).append(link)
    for child_id in sorted(links_by_child):
        child_links = links_by_child[child_id]
        if len(child_links) > 1:
            add(
                "broken_link",
                "子样被多个操作重复关联",
                {
                    "sample_id": child_id,
                    "operation_ids": sorted(link["operation_id"] for link in child_links),
                },
            )

    for operation in sorted(operations, key=lambda item: item["id"]):
        context = {
            "operation_id": operation["id"],
            "operation_code": operation["operation_code"],
        }
        parent = by_id.get(operation["parent_sample_id"])
        if parent is None:
            add("broken_link", "操作引用的母样不在谱系范围内", context)
            continue
        input_unit = operation.get("input_unit")
        output_unit = operation.get("output_unit")
        if input_unit and input_unit != parent["unit"]:
            add(
                "data_inconsistency",
                "操作处理前单位与母样单位不一致",
                {**context, "input_unit": input_unit, "parent_unit": parent["unit"]},
            )
        if input_unit and output_unit and input_unit != output_unit and not operation.get(
            "conversion_rule_code"
        ):
            add("data_inconsistency", "单位发生变化但缺少换算规则快照", context)
        factor = operation.get("conversion_factor")
        if not factor or factor <= 0:
            add("data_inconsistency", "换算系数快照无效", context)
            continue
        operation_links = links_by_operation.get(operation["id"], [])
        linked_quantity = _round(sum(link["quantity"] for link in operation_links))
        if operation_links and abs(linked_quantity - operation["produced_quantity"]) > ABS_TOLERANCE:
            add(
                "data_inconsistency",
                "操作产出数量与子样链接合计不一致",
                {
                    **context,
                    "produced_quantity": _round(operation["produced_quantity"]),
                    "linked_quantity": linked_quantity,
                },
            )
        if not operation_links and operation["produced_quantity"] > ABS_TOLERANCE:
            add("broken_link", "操作产出未关联任何子样", context)
        expected = operation["requested_quantity"] * factor
        produced = linked_quantity if operation_links else operation["produced_quantity"]
        deviation = abs(produced + operation["loss_quantity"] - expected)
        tolerance_ratio = operation.get("tolerance_ratio")
        if tolerance_ratio is None:
            tolerance_ratio = DEFAULT_TOLERANCE_RATIO
        allowed = max(ABS_TOLERANCE, tolerance_ratio * abs(expected))
        if deviation > allowed:
            add(
                "tolerance_exceeded",
                "操作守恒偏差超出容差",
                {
                    **context,
                    "expected_quantity": _round(expected),
                    "accounted_quantity": _round(produced + operation["loss_quantity"]),
                    "deviation": _round(deviation),
                    "allowed_deviation": _round(allowed),
                },
            )

    for node in sorted(nodes, key=lambda item: item["id"]):
        if node["parent_sample_id"] is not None and node["id"] not in links_by_child:
            add(
                "broken_link",
                "子样缺少来源操作链接",
                {"sample_id": node["id"], "sample_code": node["sample_code"]},
            )

    issues.sort(
        key=lambda item: (
            item["kind"],
            json.dumps(item["context"], ensure_ascii=False, sort_keys=True),
        )
    )
    return issues


def compute_rollup(
    base_id: int,
    nodes: list[dict[str, Any]],
    operations: list[dict[str, Any]],
    links: list[dict[str, Any]],
    consumptions: list[dict[str, Any]],
    destructions: list[dict[str, Any]],
    event_deltas: dict[int, float],
) -> dict[str, Any]:
    """以 base_id 为根向下汇总存量与累计消耗，并归一化到根节点单位。

    归一化系数沿谱系路径逐步累积：子样单位 → 父样单位使用产生该子样的操作
    所固化的换算系数快照，因此历史操作不受后续换算规则变更影响。
    """
    by_id = {node["id"]: node for node in nodes}
    base = by_id[base_id]
    children_index: dict[int, list[dict[str, Any]]] = {}
    for node in nodes:
        parent_id = node["parent_sample_id"]
        if parent_id in by_id:
            children_index.setdefault(parent_id, []).append(node)
    for siblings in children_index.values():
        siblings.sort(key=lambda item: item["id"])
    link_by_child: dict[int, dict[str, Any]] = {}
    for link in sorted(links, key=lambda item: item["id"]):
        link_by_child.setdefault(link["child_sample_id"], link)
    operation_by_id = {operation["id"]: operation for operation in operations}

    order: list[int] = []
    factor: dict[int, float | None] = {base_id: 1.0}
    queue = [base_id]
    visited = {base_id}
    while queue:
        current = queue.pop(0)
        order.append(current)
        for child in children_index.get(current, []):
            child_id = child["id"]
            if child_id in visited:
                continue
            visited.add(child_id)
            child_factor: float | None = None
            parent_factor = factor[current]
            link = link_by_child.get(child_id)
            operation = operation_by_id.get(link["operation_id"]) if link else None
            if (
                operation
                and operation.get("conversion_factor")
                and operation["conversion_factor"] > 0
                and parent_factor is not None
            ):
                child_factor = parent_factor / operation["conversion_factor"]
            elif child["unit"] == by_id[current]["unit"]:
                child_factor = parent_factor
            factor[child_id] = child_factor
            queue.append(child_id)

    subtree = [by_id[sample_id] for sample_id in order]
    subtree_ids = set(order)
    unnormalizable_samples = sorted(
        sample_id for sample_id in order if factor[sample_id] is None
    )

    remaining_by_unit: dict[str, float] = {}
    for node in subtree:
        remaining_by_unit[node["unit"]] = remaining_by_unit.get(node["unit"], 0.0) + node["quantity"]

    consumed_by_unit: dict[str, float] = {}
    consumed_total = 0.0
    for record in sorted(consumptions, key=lambda item: item["id"]):
        if record["sample_id"] not in subtree_ids:
            continue
        unit = by_id[record["sample_id"]]["unit"]
        consumed_by_unit[unit] = consumed_by_unit.get(unit, 0.0) + record["quantity"]
        sample_factor = factor.get(record["sample_id"])
        if sample_factor is not None:
            consumed_total += record["quantity"] * sample_factor

    destroyed_by_unit: dict[str, float] = {}
    destroyed_total = 0.0
    for record in sorted(destructions, key=lambda item: item["id"]):
        if record["sample_id"] not in subtree_ids:
            continue
        unit = by_id[record["sample_id"]]["unit"]
        destroyed_by_unit[unit] = destroyed_by_unit.get(unit, 0.0) + record["destroyed_quantity"]
        sample_factor = factor.get(record["sample_id"])
        if sample_factor is not None:
            destroyed_total += record["destroyed_quantity"] * sample_factor

    loss_by_unit: dict[str, float] = {}
    loss_total = 0.0
    unnormalizable_operations: list[int] = []
    subtree_operations = [
        operation
        for operation in sorted(operations, key=lambda item: item["id"])
        if operation["parent_sample_id"] in subtree_ids
    ]
    for operation in subtree_operations:
        unit = operation.get("output_unit") or by_id[operation["parent_sample_id"]]["unit"]
        loss_by_unit[unit] = loss_by_unit.get(unit, 0.0) + operation["loss_quantity"]
        parent_factor = factor.get(operation["parent_sample_id"])
        operation_factor = operation.get("conversion_factor")
        if parent_factor is None or not operation_factor or operation_factor <= 0:
            if operation["loss_quantity"] > 0:
                unnormalizable_operations.append(operation["id"])
            continue
        loss_total += operation["loss_quantity"] * parent_factor / operation_factor

    remaining_total = 0.0
    for node in subtree:
        node_factor = factor[node["id"]]
        if node_factor is not None:
            remaining_total += node["quantity"] * node_factor

    initial_quantity = base["quantity"] - event_deltas.get(base_id, 0.0)
    accounted_total = remaining_total + consumed_total + destroyed_total + loss_total
    unaccounted = initial_quantity - accounted_total
    tolerance = max(ABS_TOLERANCE, DEFAULT_TOLERANCE_RATIO * abs(initial_quantity))
    complete = not unnormalizable_samples and not unnormalizable_operations

    return {
        "node_count": len(subtree),
        "operation_count": len(subtree_operations),
        "remaining_by_unit": {unit: _round(remaining_by_unit[unit]) for unit in sorted(remaining_by_unit)},
        "consumed_by_unit": {unit: _round(consumed_by_unit[unit]) for unit in sorted(consumed_by_unit)},
        "destroyed_by_unit": {unit: _round(destroyed_by_unit[unit]) for unit in sorted(destroyed_by_unit)},
        "loss_by_unit": {unit: _round(loss_by_unit[unit]) for unit in sorted(loss_by_unit)},
        "normalized": {
            "unit": base["unit"],
            "initial_quantity": _round(initial_quantity),
            "remaining_total": _round(remaining_total),
            "consumed_total": _round(consumed_total),
            "destroyed_total": _round(destroyed_total),
            "loss_total": _round(loss_total),
            "accounted_total": _round(accounted_total),
            "unaccounted": _round(unaccounted),
            "tolerance": _round(tolerance),
            "complete": complete,
            "conserved": complete and abs(unaccounted) <= tolerance,
        },
        "unnormalizable_sample_ids": unnormalizable_samples,
        "unnormalizable_operation_ids": sorted(unnormalizable_operations),
    }


# ---------------------------------------------------------------------------
# 换算规则：版本化、不可改写
# ---------------------------------------------------------------------------


class ConversionRuleRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def create(
        self,
        data: dict[str, Any],
        version: int,
        actor_user_id: int,
        now: str,
    ) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO unit_conversion_rules(rule_code,from_unit,to_unit,factor,version,status,note,created_by,created_at)
               VALUES(?,?,?,?,?,'active',?,?,?)""",
            (
                data["rule_code"],
                data["from_unit"],
                data["to_unit"],
                data["factor"],
                version,
                data.get("note", ""),
                actor_user_id,
                now,
            ),
        )
        return self.get(cursor.lastrowid)

    def get(self, rule_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE id=?", (rule_id,)
            ).fetchone(),
            "换算规则不存在",
        )

    def active(self, rule_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT * FROM unit_conversion_rules
               WHERE rule_code=? AND status='active' ORDER BY version DESC LIMIT 1""",
            (rule_code,),
        ).fetchone()
        return dict(row) if row else None

    def supersede(self, rule_id: int) -> None:
        self.connection.execute(
            "UPDATE unit_conversion_rules SET status='superseded' WHERE id=?",
            (rule_id,),
        )

    def list(self, rule_code: str | None = None) -> list[dict[str, Any]]:
        if rule_code:
            rows = self.connection.execute(
                "SELECT * FROM unit_conversion_rules WHERE rule_code=? ORDER BY version",
                (rule_code,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM unit_conversion_rules ORDER BY rule_code,version"
            ).fetchall()
        return [dict(row) for row in rows]


class ConversionRuleService:
    """注册换算规则：相同编码与参数幂等重放；系数变化产生新版本，旧版本标记废止。

    历史操作保存规则编码、版本与系数快照，因此规则升级不会改写已发生的谱系事件。
    """

    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None):
        self.connection = connection
        self.clock = clock or SystemClock()
        self.rules = ConversionRuleRepository(connection)
        self.audit = AuditService(connection, self.clock)

    def register(self, principal: Principal, data: dict[str, Any]) -> dict[str, Any]:
        principal.require("samples.write")
        if data["from_unit"] == data["to_unit"]:
            raise ValidationError("换算前后单位不能相同")
        now = to_storage(self.clock.now())
        existing = self.rules.active(data["rule_code"])
        if existing is None:
            rule = self.rules.create(data, 1, principal.user_id, now)
            self.audit.record(
                principal, "conversion_rule.register", "unit_conversion_rule", str(rule["id"]), after=rule
            )
            return {**rule, "replayed": False}
        if (existing["from_unit"], existing["to_unit"]) != (data["from_unit"], data["to_unit"]):
            raise ValidationError("同一规则编码的换算方向不可变更，请使用新的规则编码")
        if abs(float(existing["factor"]) - float(data["factor"])) < 1e-12:
            return {**existing, "replayed": True}
        self.rules.supersede(existing["id"])
        rule = self.rules.create(data, int(existing["version"]) + 1, principal.user_id, now)
        self.audit.record(
            principal,
            "conversion_rule.register",
            "unit_conversion_rule",
            str(rule["id"]),
            before=existing,
            after=rule,
            metadata={"superseded_rule_id": existing["id"]},
        )
        return {**rule, "replayed": False}

    def list(self, principal: Principal, rule_code: str | None = None) -> list[dict[str, Any]]:
        principal.require("samples.read")
        return self.rules.list(rule_code)


# ---------------------------------------------------------------------------
# 谱系数据访问
# ---------------------------------------------------------------------------


class LineageRepository:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection

    def sample(self, sample_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute("SELECT * FROM samples WHERE id=?", (sample_id,)).fetchone(),
            "样品不存在",
        )

    def samples_by_ids(self, sample_ids: list[int]) -> list[dict[str, Any]]:
        if not sample_ids:
            return []
        placeholders = ",".join("?" for _ in sample_ids)
        rows = self.connection.execute(
            f"SELECT * FROM samples WHERE id IN ({placeholders}) ORDER BY id",
            tuple(sample_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def children_of(self, sample_ids: list[int]) -> list[dict[str, Any]]:
        if not sample_ids:
            return []
        placeholders = ",".join("?" for _ in sample_ids)
        rows = self.connection.execute(
            f"SELECT * FROM samples WHERE parent_sample_id IN ({placeholders}) ORDER BY id",
            tuple(sample_ids),
        ).fetchall()
        return [dict(row) for row in rows]

    def insert_operation(self, data: dict[str, Any], now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            """INSERT INTO aliquot_operations(
                   operation_code,parent_sample_id,operation_kind,requested_quantity,produced_quantity,
                   loss_quantity,input_unit,output_unit,conversion_rule_code,conversion_rule_version,
                   conversion_factor,loss_reason,tolerance_ratio,request_digest,version,
                   operator_user_id,occurred_at,note,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?)""",
            (
                data["operation_code"],
                data["parent_sample_id"],
                data["operation_kind"],
                data["requested_quantity"],
                data["produced_quantity"],
                data["loss_quantity"],
                data["input_unit"],
                data["output_unit"],
                data.get("conversion_rule_code"),
                data.get("conversion_rule_version"),
                data["conversion_factor"],
                data["loss_reason"],
                data["tolerance_ratio"],
                data["request_digest"],
                data["operator_user_id"],
                now,
                data.get("note", ""),
                now,
            ),
        )
        return self.operation(cursor.lastrowid)

    def operation(self, operation_id: int) -> dict[str, Any]:
        return _row(
            self.connection.execute(
                "SELECT * FROM aliquot_operations WHERE id=?", (operation_id,)
            ).fetchone(),
            "谱系操作不存在",
        )

    def operation_by_code(self, operation_code: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM aliquot_operations WHERE operation_code=?", (operation_code,)
        ).fetchone()
        return dict(row) if row else None

    def insert_child_link(
        self, operation_id: int, child_sample_id: int, quantity: float, position: int
    ) -> None:
        self.connection.execute(
            """INSERT INTO aliquot_operation_children(operation_id,child_sample_id,quantity,position)
               VALUES(?,?,?,?)""",
            (operation_id, child_sample_id, quantity, position),
        )

    def operation_for_child(self, child_sample_id: int) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT o.* FROM aliquot_operations o
               JOIN aliquot_operation_children c ON c.operation_id=o.id
               WHERE c.child_sample_id=? ORDER BY o.id LIMIT 1""",
            (child_sample_id,),
        ).fetchone()
        return dict(row) if row else None

    def operation_children(self, operation_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT c.child_sample_id FROM aliquot_operation_children c
               WHERE c.operation_id=? ORDER BY c.position""",
            (operation_id,),
        ).fetchall()
        return [row["child_sample_id"] for row in rows]

    def tree_dataset(self, sample_ids: list[int]) -> dict[str, Any]:
        """取某个节点集合涉及的操作、子样链接、消耗、销毁与数量事件合计。"""
        operations: dict[int, dict[str, Any]] = {}
        if sample_ids:
            placeholders = ",".join("?" for _ in sample_ids)
            for row in self.connection.execute(
                f"SELECT * FROM aliquot_operations WHERE parent_sample_id IN ({placeholders}) ORDER BY id",
                tuple(sample_ids),
            ).fetchall():
                operations[row["id"]] = dict(row)
            link_rows = [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM aliquot_operation_children WHERE child_sample_id IN ({placeholders}) ORDER BY id",
                    tuple(sample_ids),
                ).fetchall()
            ]
            missing = [link["operation_id"] for link in link_rows if link["operation_id"] not in operations]
            for operation_id in sorted(set(missing)):
                operations[operation_id] = self.operation(operation_id)
        else:
            link_rows = []
        links: list[dict[str, Any]] = []
        if operations:
            placeholders = ",".join("?" for _ in operations)
            links = [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM aliquot_operation_children WHERE operation_id IN ({placeholders}) ORDER BY id",
                    tuple(operations.keys()),
                ).fetchall()
            ]
        consumptions: list[dict[str, Any]] = []
        destructions: list[dict[str, Any]] = []
        event_deltas: dict[int, float] = {}
        if sample_ids:
            placeholders = ",".join("?" for _ in sample_ids)
            consumptions = [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM consumption_records WHERE sample_id IN ({placeholders}) ORDER BY id",
                    tuple(sample_ids),
                ).fetchall()
            ]
            destructions = [
                dict(row)
                for row in self.connection.execute(
                    f"SELECT * FROM destruction_records WHERE sample_id IN ({placeholders}) ORDER BY id",
                    tuple(sample_ids),
                ).fetchall()
            ]
            for row in self.connection.execute(
                f"SELECT sample_id,SUM(quantity_delta) AS total FROM sample_events WHERE sample_id IN ({placeholders}) GROUP BY sample_id",
                tuple(sample_ids),
            ).fetchall():
                event_deltas[row["sample_id"]] = float(row["total"] or 0.0)
        return {
            "operations": [operations[key] for key in sorted(operations)],
            "links": links,
            "consumptions": consumptions,
            "destructions": destructions,
            "event_deltas": event_deltas,
        }


# ---------------------------------------------------------------------------
# 向上追溯与向下汇总
# ---------------------------------------------------------------------------

SAMPLE_BRIEF_FIELDS = (
    "id",
    "sample_code",
    "sample_type",
    "quantity",
    "unit",
    "lifecycle_state",
    "parent_sample_id",
    "root_sample_id",
    "lineage_depth",
)

OPERATION_BRIEF_FIELDS = (
    "id",
    "operation_code",
    "operation_kind",
    "requested_quantity",
    "produced_quantity",
    "loss_quantity",
    "loss_reason",
    "input_unit",
    "output_unit",
    "conversion_rule_code",
    "conversion_rule_version",
    "conversion_factor",
    "tolerance_ratio",
    "version",
    "operator_user_id",
    "occurred_at",
)


def _brief(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: row.get(field) for field in fields}


class LineageTraceService:
    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.repo = LineageRepository(connection)

    def ancestors(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        """从任意节点向上追溯来源：返回自根到该节点的链路与每步操作快照。"""
        principal.require("samples.read")
        sample = self.repo.sample(sample_id)
        chain: list[dict[str, Any]] = []
        seen: set[int] = set()
        cycle_detected = False
        node: dict[str, Any] | None = sample
        while node is not None:
            if node["id"] in seen:
                cycle_detected = True
                break
            seen.add(node["id"])
            operation = self.repo.operation_for_child(node["id"])
            chain.append(
                {
                    "sample": _brief(node, SAMPLE_BRIEF_FIELDS),
                    "operation": _brief(operation, OPERATION_BRIEF_FIELDS) if operation else None,
                }
            )
            parent_id = node["parent_sample_id"]
            node = self.repo.sample(parent_id) if parent_id is not None else None
        chain.reverse()
        for depth, item in enumerate(chain):
            item["depth"] = depth
        return {
            "sample_id": sample_id,
            "cycle_detected": cycle_detected,
            "chain": chain,
        }

    def rollup(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        """从任意节点向下汇总所有存量与累计消耗，并归一化到该节点单位。"""
        principal.require("samples.read")
        base = self.repo.sample(sample_id)
        nodes = self._subtree_nodes(sample_id)
        dataset = self.repo.tree_dataset([node["id"] for node in nodes])
        result = compute_rollup(sample_id, nodes, **dataset)
        result.update(
            {
                "sample_id": sample_id,
                "sample_code": base["sample_code"],
                "unit": base["unit"],
            }
        )
        result["digest"] = canonical_digest(result)
        return result

    def _subtree_nodes(self, root_id: int) -> list[dict[str, Any]]:
        nodes: dict[int, dict[str, Any]] = {}
        frontier = [root_id]
        while frontier:
            batch = self.repo.samples_by_ids(frontier)
            for row in batch:
                nodes[row["id"]] = row
            children = self.repo.children_of([row["id"] for row in batch])
            frontier = [row["id"] for row in children if row["id"] not in nodes]
        return [nodes[key] for key in sorted(nodes)]


class LineageReportService:
    """跨层守恒报告：汇总整条谱系并标出断链、循环、重复编码与超出容差的步骤。"""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.repo = LineageRepository(connection)

    def report(self, principal: Principal, sample_id: int) -> dict[str, Any]:
        principal.require("samples.read")
        sample = self.repo.sample(sample_id)
        nodes = self._collect_tree(sample)
        node_ids = [node["id"] for node in nodes]
        dataset = self.repo.tree_dataset(node_ids)
        issues = detect_issues(nodes, dataset["operations"], dataset["links"])
        parent_of = {node["id"]: node["parent_sample_id"] for node in nodes}
        true_root = compute_root(sample_id, parent_of)
        base_id = true_root if true_root is not None else (sample["root_sample_id"] or sample_id)
        if base_id not in parent_of:
            base_id = sample_id
        summary = compute_rollup(base_id, nodes, **dataset)
        issue_counts: dict[str, int] = {}
        for issue in issues:
            issue_counts[issue["kind"]] = issue_counts.get(issue["kind"], 0) + 1
        result = {
            "queried_sample_id": sample_id,
            "root_sample_id": base_id,
            "sample_count": len(nodes),
            "operation_count": len(dataset["operations"]),
            "summary": summary,
            "issue_counts": {kind: issue_counts[kind] for kind in sorted(issue_counts)},
            "issues": issues,
        }
        result["digest"] = canonical_digest(result)
        return result

    def _collect_tree(self, sample: dict[str, Any]) -> list[dict[str, Any]]:
        """收集谱系节点：登记根集合 ∪ 实际祖先链 ∪ 操作关联的子样（迭代至收敛）。"""
        stored_root_id = sample["root_sample_id"] or sample["id"]
        nodes: dict[int, dict[str, Any]] = {}
        rows = self.connection.execute(
            "SELECT * FROM samples WHERE root_sample_id=? OR id=? ORDER BY id",
            (stored_root_id, stored_root_id),
        ).fetchall()
        for row in rows:
            nodes[row["id"]] = dict(row)
        ancestor: dict[str, Any] | None = sample
        seen: set[int] = set()
        while ancestor is not None and ancestor["id"] not in seen:
            seen.add(ancestor["id"])
            nodes.setdefault(ancestor["id"], ancestor)
            parent_id = ancestor["parent_sample_id"]
            ancestor = self.repo.sample(parent_id) if parent_id is not None else None
        while True:
            dataset_ids = sorted(nodes)
            placeholders = ",".join("?" for _ in dataset_ids)
            linked_children = [
                row[0]
                for row in self.connection.execute(
                    f"""SELECT DISTINCT c.child_sample_id FROM aliquot_operation_children c
                        JOIN aliquot_operations o ON o.id=c.operation_id
                        WHERE o.parent_sample_id IN ({placeholders})""",
                    tuple(dataset_ids),
                ).fetchall()
            ]
            missing = [child_id for child_id in linked_children if child_id not in nodes]
            if not missing:
                break
            for row in self.repo.samples_by_ids(missing):
                nodes[row["id"]] = row
        return [nodes[key] for key in sorted(nodes)]
