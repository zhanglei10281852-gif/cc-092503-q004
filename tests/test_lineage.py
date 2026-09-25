from __future__ import annotations

import pytest


def _location(client, admin, code="LIN-L-01"):
    response = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={"code": code, "building": "科研楼", "room": "低温间", "cabinet": "柜一", "shelf": "二层", "sensitivity": "normal", "capacity_units": 100},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _sample(client, admin, *, code, batch_code, quantity, unit, location=None):
    location = location or _location(client, admin, code=f"{code}-LOC")
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": batch_code, "project_code": "P-LIN", "expected_count": 5},
    )
    assert batch.status_code == 201, batch.text
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={"sample_code": code, "batch_id": batch.json()["id"], "sample_type": "土壤", "quantity": quantity, "unit": unit, "location_id": location["id"]},
    )
    assert sample.status_code == 201, sample.text
    return sample.json()


def _aliquot(client, admin, sample_id, payload):
    return client.post(f"/api/samples/{sample_id}/aliquots", headers=admin["headers"], json=payload)


def _register_rule(client, admin, **overrides):
    payload = {"rule_code": "KG-G", "from_unit": "kg", "to_unit": "g", "factor": "1000", "default_tolerance": 0.000001, "note": "质量换算"}
    payload.update(overrides)
    return client.post("/api/sample-operations/conversion-rules", headers=admin["headers"], json=payload)


def _connection():
    from app.database import get_connection

    return get_connection()


def test_conversion_rule_is_versioned_and_replayable(client, admin):
    first = _register_rule(client, admin)
    assert first.status_code == 201, first.text
    assert first.json()["version"] == 1
    assert first.json()["replayed"] is False

    replay = _register_rule(client, admin)
    assert replay.status_code == 201
    assert replay.json()["version"] == 1
    assert replay.json()["replayed"] is True

    changed = _register_rule(client, admin, factor="999.5", note="校准后系数")
    assert changed.status_code == 201
    assert changed.json()["version"] == 2

    rules = client.get("/api/sample-operations/conversion-rules", headers=admin["headers"], params={"rule_code": "KG-G"})
    assert [item["version"] for item in rules.json()] == [1, 2]
    assert rules.json()[0]["factor"] == "1000"

    invalid = _register_rule(client, admin, rule_code="BAD", from_unit="g", to_unit="g")
    assert invalid.status_code == 422


def test_cross_unit_aliquot_pins_conversion_snapshot(client, admin):
    _register_rule(client, admin)
    root = _sample(client, admin, code="LIN-ROOT", batch_code="LIN-B-1", quantity=2, unit="kg")
    response = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_code": "OP-FREEZE-1",
            "operation_kind": "freeze_drying",
            "requested_quantity": 1.5,
            "output_unit": "g",
            "conversion_rule_code": "KG-G",
            "loss_quantity": 100,
            "loss_reason": "冻干水分流失",
            "children": [{"sample_code": "LIN-POWDER", "quantity": 1400}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["parent"]["quantity"] == 0.5
    assert body["children"][0]["unit"] == "g"
    operation = body["operation"]
    assert operation["input_unit"] == "kg"
    assert operation["output_unit"] == "g"
    assert operation["conversion_rule_code"] == "KG-G"
    assert operation["conversion_rule_version"] == 1
    assert operation["conversion_factor"] == "1000"
    assert operation["loss_reason"] == "冻干水分流失"
    assert operation["version"] == 1
    assert operation["residual"] == 0


def test_aliquot_validates_conservation_in_transaction(client, admin):
    root = _sample(client, admin, code="LIN-BAL", batch_code="LIN-B-2", quantity=100, unit="g")
    response = _aliquot(
        client,
        admin,
        root["id"],
        {"requested_quantity": 30, "loss_quantity": 1, "children": [{"sample_code": "LIN-BAL-A", "quantity": 28}]},
    )
    assert response.status_code == 422
    detail = client.get(f"/api/samples/{root['id']}", headers=admin["headers"])
    assert detail.json()["quantity"] == 100
    assert detail.json()["children"] == []
    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"])
    assert report.json()["summary"]["operation_count"] == 0


def test_aliquot_unit_rule_mismatch_rejected(client, admin):
    _register_rule(client, admin)
    root = _sample(client, admin, code="LIN-MM", batch_code="LIN-B-3", quantity=10, unit="kg")
    missing_rule = _aliquot(
        client, admin, root["id"],
        {"requested_quantity": 1, "output_unit": "g", "children": [{"sample_code": "LIN-MM-A", "quantity": 1000}]},
    )
    assert missing_rule.status_code == 422
    same_unit_with_rule = _aliquot(
        client, admin, root["id"],
        {"requested_quantity": 1, "conversion_rule_code": "KG-G", "children": [{"sample_code": "LIN-MM-B", "quantity": 1}]},
    )
    assert same_unit_with_rule.status_code == 422
    wrong_direction = _aliquot(
        client, admin, root["id"],
        {"requested_quantity": 1, "output_unit": "mg", "conversion_rule_code": "KG-G", "children": [{"sample_code": "LIN-MM-C", "quantity": 1000}]},
    )
    assert wrong_direction.status_code == 422
    unknown_rule = _aliquot(
        client, admin, root["id"],
        {"requested_quantity": 1, "output_unit": "g", "conversion_rule_code": "NOPE", "children": [{"sample_code": "LIN-MM-D", "quantity": 1000}]},
    )
    assert unknown_rule.status_code == 404


def test_new_rule_version_does_not_rewrite_history(client, admin):
    _register_rule(client, admin, rule_code="VIAL-G", from_unit="vial", to_unit="g", factor="2")
    root = _sample(client, admin, code="LIN-HIST", batch_code="LIN-B-4", quantity=10, unit="vial")
    created = _aliquot(
        client, admin, root["id"],
        {
            "operation_code": "OP-HIST",
            "requested_quantity": 3,
            "output_unit": "g",
            "conversion_rule_code": "VIAL-G",
            "loss_quantity": 1,
            "children": [{"sample_code": "LIN-HIST-A", "quantity": 5}],
        },
    )
    assert created.status_code == 201, created.text
    before = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()

    _register_rule(client, admin, rule_code="VIAL-G", from_unit="vial", to_unit="g", factor="2.5", note="重新标定")

    history = client.get("/api/sample-operations/operations/OP-HIST", headers=admin["headers"])
    assert history.status_code == 200
    version_one = history.json()["versions"][0]
    assert version_one["conversion_rule_version"] == 1
    assert version_one["conversion_factor"] == "2"
    after = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    assert before == after


def test_ancestors_traces_chain_to_root(client, admin):
    _register_rule(client, admin)
    root = _sample(client, admin, code="LIN-ANC", batch_code="LIN-B-5", quantity=2, unit="kg")
    first = _aliquot(
        client, admin, root["id"],
        {
            "operation_code": "OP-ANC-1",
            "requested_quantity": 1.5,
            "output_unit": "g",
            "conversion_rule_code": "KG-G",
            "loss_quantity": 100,
            "children": [{"sample_code": "LIN-ANC-A", "quantity": 1400}],
        },
    ).json()
    child = first["children"][0]
    second = _aliquot(
        client, admin, child["id"],
        {"operation_code": "OP-ANC-2", "requested_quantity": 400, "loss_quantity": 10, "children": [{"sample_code": "LIN-ANC-B", "quantity": 390}]},
    ).json()
    grandchild = second["children"][0]

    trace = client.get(f"/api/sample-operations/{grandchild['id']}/lineage/ancestors", headers=admin["headers"])
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["complete"] is True
    assert body["root_sample_id"] == root["id"]
    assert [hop["sample"]["sample_code"] for hop in body["hops"]] == ["LIN-ANC-A", "LIN-ANC"]
    assert body["hops"][0]["operation"]["operation_code"] == "OP-ANC-2"
    assert body["hops"][1]["operation"]["conversion_factor"] == "1000"


def test_rollup_aggregates_subtree_and_is_stable(client, admin):
    _register_rule(client, admin)
    root = _sample(client, admin, code="LIN-ROLL", batch_code="LIN-B-6", quantity=2, unit="kg")
    first = _aliquot(
        client, admin, root["id"],
        {
            "operation_code": "OP-ROLL-1",
            "requested_quantity": 1.5,
            "output_unit": "g",
            "conversion_rule_code": "KG-G",
            "loss_quantity": 100,
            "children": [{"sample_code": "LIN-ROLL-A", "quantity": 1400}],
        },
    ).json()
    child = first["children"][0]
    second = _aliquot(
        client, admin, child["id"],
        {"operation_code": "OP-ROLL-2", "requested_quantity": 400, "loss_quantity": 10, "children": [{"sample_code": "LIN-ROLL-B", "quantity": 390}]},
    ).json()
    grandchild = second["children"][0]
    consumed = client.post(
        f"/api/samples/{grandchild['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-ROLL", "quantity": 40, "idempotency_key": "roll-consume-1"},
    )
    assert consumed.status_code == 201, consumed.text

    rollup = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"])
    assert rollup.status_code == 200, rollup.text
    summary = rollup.json()["summary"]
    assert summary["remaining_by_unit"] == {"g": 1350.0, "kg": 0.5}
    assert summary["consumed_by_unit"] == {"g": 40.0}
    assert summary["loss_by_unit"] == {"g": 110.0}
    normalized = summary["normalized"]
    assert normalized["unit"] == "kg"
    assert normalized["complete"] is True
    assert normalized["initial_quantity"] == 2.0
    assert normalized["remaining"] == 1.85
    assert normalized["consumed"] == 0.04
    assert normalized["loss"] == 0.11
    assert normalized["conservation_delta"] == 0.0

    again = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"])
    assert again.json() == rollup.json()

    sub = client.get(f"/api/sample-operations/{child['id']}/lineage/rollup", headers=admin["headers"])
    sub_summary = sub.json()["summary"]
    assert sub_summary["normalized"]["unit"] == "g"
    assert sub_summary["normalized"]["initial_quantity"] == 1400.0
    assert sub_summary["normalized"]["conservation_delta"] == 0.0


def test_report_is_clean_for_conserved_lineage(client, admin):
    _register_rule(client, admin)
    root = _sample(client, admin, code="LIN-CLEAN", batch_code="LIN-B-7", quantity=2, unit="kg")
    _aliquot(
        client, admin, root["id"],
        {
            "operation_code": "OP-CLEAN-1",
            "requested_quantity": 1.5,
            "output_unit": "g",
            "conversion_rule_code": "KG-G",
            "loss_quantity": 100,
            "children": [{"sample_code": "LIN-CLEAN-A", "quantity": 1400}],
        },
    )
    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"])
    assert report.status_code == 200, report.text
    body = report.json()
    assert body["problems"] == []
    assert body["summary"]["problem_counts"] == {"cycle": 0, "broken_link": 0, "duplicate_code": 0, "tolerance_exceeded": 0}
    assert body["summary"]["normalized"]["conservation_delta"] == 0.0
    repeat = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"])
    assert repeat.json() == body


def test_report_flags_all_problem_kinds_with_stable_order(client, admin):
    root = _sample(client, admin, code="LIN-PROB", batch_code="LIN-B-8", quantity=100, unit="g")
    first = _aliquot(
        client, admin, root["id"],
        {"operation_code": "OP-PROB-1", "requested_quantity": 30, "loss_quantity": 2, "children": [{"sample_code": "LIN-PROB-A", "quantity": 28}]},
    ).json()
    child = first["children"][0]
    second = _aliquot(
        client, admin, root["id"],
        {"operation_code": "OP-PROB-2", "requested_quantity": 10, "children": [{"sample_code": "LIN-PROB-B", "quantity": 10}]},
    ).json()
    sibling = second["children"][0]
    other = _sample(client, admin, code="LIN-OTHER", batch_code="LIN-B-9", quantity=5, unit="g")

    connection = _connection()
    # 循环：根样品的父样指向自己的子样
    connection.execute("UPDATE samples SET parent_sample_id=? WHERE id=?", (sibling["id"], root["id"]))
    # 断链：子样父样指向另一棵谱系树
    connection.execute("UPDATE samples SET parent_sample_id=? WHERE id=?", (other["id"], child["id"]))
    # 重复编码：同一操作编码出现两个生效版本
    connection.execute(
        """INSERT INTO lineage_operations(
               operation_code,version,status,operation_kind,parent_sample_id,
               input_unit,output_unit,input_quantity,output_quantity,loss_quantity,loss_reason,
               conversion_factor,tolerance,residual,operator_user_id,occurred_at,note,created_at
           ) VALUES('OP-PROB-2',99,'active','aliquot',?,'g','g',10,10,0,'','1',0.000001,0,1,'2026-01-01T00:00:00+00:00','','2026-01-01T00:00:00+00:00')""",
        (root["id"],),
    )
    # 超出容差：直接写入一条不守恒的历史操作
    connection.execute(
        """INSERT INTO lineage_operations(
               operation_code,version,status,operation_kind,parent_sample_id,
               input_unit,output_unit,input_quantity,output_quantity,loss_quantity,loss_reason,
               conversion_factor,tolerance,residual,operator_user_id,occurred_at,note,created_at
           ) VALUES('OP-PROB-BAD',1,'active','aliquot',?,'g','g',10,8,1,'','1',0.000001,1,1,'2026-01-01T00:00:00+00:00','','2026-01-01T00:00:00+00:00')""",
        (root["id"],),
    )

    report = client.get(f"/api/sample-operations/{child['id']}/lineage/report", headers=admin["headers"])
    assert report.status_code == 200, report.text
    body = report.json()
    kinds = [problem["kind"] for problem in body["problems"]]
    assert set(kinds) == {"cycle", "broken_link", "duplicate_code", "tolerance_exceeded"}
    assert kinds == sorted(kinds, key=lambda kind: {"cycle": 0, "broken_link": 1, "duplicate_code": 2, "tolerance_exceeded": 3}[kind])
    by_kind = {}
    for problem in body["problems"]:
        by_kind.setdefault(problem["kind"], []).append(problem)
    assert by_kind["cycle"][0]["details"]["sample_ids"] == sorted([root["id"], sibling["id"]])
    assert any(problem["code"] == "LIN-PROB-A" for problem in by_kind["broken_link"])
    assert any(problem["code"] == "OP-PROB-2" for problem in by_kind["duplicate_code"])
    assert by_kind["tolerance_exceeded"][0]["code"] == "OP-PROB-BAD"
    assert by_kind["tolerance_exceeded"][0]["details"]["residual"] == 1.0
    assert body["summary"]["problem_counts"] == {"cycle": 1, "broken_link": 2, "duplicate_code": 1, "tolerance_exceeded": 1}

    repeat = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"])
    assert repeat.status_code == 200
    assert repeat.json()["digest"] == body["digest"]
    assert repeat.json()["problems"] == body["problems"]

    connection.execute("UPDATE samples SET parent_sample_id=? WHERE id=?", (root["id"], child["id"]))
    connection.execute("UPDATE samples SET parent_sample_id=NULL WHERE id=?", (root["id"],))


def test_ancestors_reports_cycle_without_hanging(client, admin):
    root = _sample(client, admin, code="LIN-CYC", batch_code="LIN-B-10", quantity=50, unit="g")
    first = _aliquot(
        client, admin, root["id"],
        {"operation_code": "OP-CYC-1", "requested_quantity": 20, "children": [{"sample_code": "LIN-CYC-A", "quantity": 20}]},
    ).json()
    child = first["children"][0]
    connection = _connection()
    connection.execute("UPDATE samples SET parent_sample_id=? WHERE id=?", (child["id"], root["id"]))

    trace = client.get(f"/api/sample-operations/{child['id']}/lineage/ancestors", headers=admin["headers"])
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert body["complete"] is False
    assert body["problems"][0]["kind"] == "cycle"

    connection.execute("UPDATE samples SET parent_sample_id=NULL WHERE id=?", (root["id"],))


def test_correction_appends_version_without_rewriting_history(client, admin):
    root = _sample(client, admin, code="LIN-COR", batch_code="LIN-B-11", quantity=100, unit="g")
    created = _aliquot(
        client, admin, root["id"],
        {
            "operation_code": "OP-COR-1",
            "requested_quantity": 30,
            "loss_quantity": 2,
            "loss_reason": "研磨挂壁",
            "tolerance": 0.5,
            "children": [{"sample_code": "LIN-COR-A", "quantity": 28}],
        },
    )
    assert created.status_code == 201, created.text

    corrected = client.post(
        "/api/sample-operations/operations/OP-COR-1/corrections",
        headers=admin["headers"],
        json={"loss_quantity": 1.8, "loss_reason": "复测后修正损耗", "note": "复测损耗为 1.8g"},
    )
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["version"] == 2
    assert corrected.json()["loss_quantity"] == 1.8
    assert corrected.json()["status"] == "active"

    history = client.get("/api/sample-operations/operations/OP-COR-1", headers=admin["headers"]).json()
    assert [item["version"] for item in history["versions"]] == [1, 2]
    assert history["versions"][0]["status"] == "superseded"
    assert history["versions"][0]["loss_quantity"] == 2
    assert history["versions"][0]["loss_reason"] == "研磨挂壁"
    assert history["versions"][1]["children"][0]["sample_code"] == "LIN-COR-A"

    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"])
    assert report.json()["summary"]["loss_by_unit"] == {"g": 1.8}
    assert report.json()["problems"] == []

    unbalanced = client.post(
        "/api/sample-operations/operations/OP-COR-1/corrections",
        headers=admin["headers"],
        json={"loss_quantity": 0.5, "note": "把损耗改小"},
    )
    assert unbalanced.status_code == 422

    missing = client.post(
        "/api/sample-operations/operations/OP-NONE/corrections",
        headers=admin["headers"],
        json={"note": "不存在的操作"},
    )
    assert missing.status_code == 404
