from __future__ import annotations

from app.samples.lineage import detect_issues


def _bootstrap(client, admin, quantity=500.0, unit="mL", code="ROOT-1"):
    location = client.post(
        "/api/samples/locations",
        headers=admin["headers"],
        json={
            "code": f"LOC-{code}",
            "building": "样品楼",
            "room": "常温库",
            "cabinet": "一号柜",
            "shelf": "一层",
            "sensitivity": "normal",
            "capacity_units": 100,
        },
    ).json()
    batch = client.post(
        "/api/samples/batches",
        headers=admin["headers"],
        json={"batch_code": f"BATCH-{code}", "project_code": "LINEAGE", "expected_count": 1},
    ).json()
    sample = client.post(
        "/api/samples",
        headers=admin["headers"],
        json={
            "sample_code": code,
            "batch_id": batch["id"],
            "sample_type": "水样",
            "quantity": quantity,
            "unit": unit,
            "location_id": location["id"],
        },
    ).json()
    return location, batch, sample


def _register_rule(client, admin, code="ML-TO-G", from_unit="mL", to_unit="g", factor=0.005):
    return client.post(
        "/api/sample-operations/conversion-rules",
        headers=admin["headers"],
        json={"rule_code": code, "from_unit": from_unit, "to_unit": to_unit, "factor": factor},
    )


def _aliquot(client, admin, sample_id, payload):
    return client.post(f"/api/samples/{sample_id}/aliquots", headers=admin["headers"], json=payload)


def _build_processed_tree(client, admin):
    """ROOT-1(500 mL) --冻干--> FD-1(2.4 g) --研磨--> GD-1(2.3 g) --分装--> A-1/A-2。"""
    _, _, root = _bootstrap(client, admin)
    _register_rule(client, admin)
    freeze_dry = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_code": "OP-FD-1",
            "operation_kind": "freeze_dry",
            "requested_quantity": 500,
            "output_unit": "g",
            "conversion_rule_code": "ML-TO-G",
            "loss_quantity": 0.1,
            "loss_reason": "冻干飞散损耗",
            "children": [{"sample_code": "FD-1", "quantity": 2.4, "sample_type": "冻干粉"}],
        },
    )
    assert freeze_dry.status_code == 201, freeze_dry.text
    fd_child = freeze_dry.json()["children"][0]
    grind = _aliquot(
        client,
        admin,
        fd_child["id"],
        {
            "operation_code": "OP-GRIND-1",
            "operation_kind": "grind",
            "requested_quantity": 2.4,
            "loss_quantity": 0.1,
            "loss_reason": "研磨残留",
            "children": [{"sample_code": "GD-1", "quantity": 2.3}],
        },
    )
    assert grind.status_code == 201, grind.text
    gd_child = grind.json()["children"][0]
    split = _aliquot(
        client,
        admin,
        gd_child["id"],
        {
            "operation_code": "OP-SPLIT-1",
            "requested_quantity": 2.3,
            "children": [
                {"sample_code": "A-1", "quantity": 1.1},
                {"sample_code": "A-2", "quantity": 1.2},
            ],
        },
    )
    assert split.status_code == 201, split.text
    return root, fd_child, gd_child, split.json()["children"]


def _db_execute(sql, params=()):
    from app.database import get_connection

    connection = get_connection()
    connection.execute(sql, params)


def test_conversion_rule_register_replay_and_versioning(client, admin):
    created = _register_rule(client, admin)
    assert created.status_code == 201, created.text
    assert created.json()["version"] == 1
    assert created.json()["status"] == "active"
    assert created.json()["replayed"] is False

    replayed = _register_rule(client, admin)
    assert replayed.status_code == 201
    assert replayed.json()["replayed"] is True
    assert replayed.json()["id"] == created.json()["id"]

    upgraded = _register_rule(client, admin, factor=0.006)
    assert upgraded.status_code == 201
    assert upgraded.json()["version"] == 2
    assert upgraded.json()["replayed"] is False

    rules = client.get("/api/sample-operations/conversion-rules", headers=admin["headers"])
    assert rules.status_code == 200
    versions = [(item["version"], item["status"]) for item in rules.json()]
    assert versions == [(1, "superseded"), (2, "active")]

    wrong_direction = client.post(
        "/api/sample-operations/conversion-rules",
        headers=admin["headers"],
        json={"rule_code": "ML-TO-G", "from_unit": "g", "to_unit": "mg", "factor": 1000},
    )
    assert wrong_direction.status_code == 422

    same_unit = client.post(
        "/api/sample-operations/conversion-rules",
        headers=admin["headers"],
        json={"rule_code": "SAME-UNIT", "from_unit": "g", "to_unit": "g", "factor": 1},
    )
    assert same_unit.status_code == 422


def test_freeze_dry_records_units_conversion_loss_and_version(client, admin):
    _, _, root = _bootstrap(client, admin)
    _register_rule(client, admin)
    response = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_code": "OP-FD-FIELDS",
            "operation_kind": "freeze_dry",
            "requested_quantity": 500,
            "output_unit": "g",
            "conversion_rule_code": "ML-TO-G",
            "loss_quantity": 0.1,
            "loss_reason": "冻干飞散损耗",
            "children": [{"sample_code": "FD-FIELDS", "quantity": 2.4}],
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["replayed"] is False
    assert body["parent"]["quantity"] == 0
    assert body["children"][0]["unit"] == "g"
    operation = body["operation"]
    assert operation["operation_kind"] == "freeze_dry"
    assert operation["input_unit"] == "mL"
    assert operation["output_unit"] == "g"
    assert operation["conversion_rule_code"] == "ML-TO-G"
    assert operation["conversion_rule_version"] == 1
    assert operation["conversion_factor"] == 0.005
    assert operation["loss_reason"] == "冻干飞散损耗"
    assert operation["tolerance_ratio"] == 0.005
    assert operation["version"] == 1


def test_unit_change_requires_conversion_rule(client, admin):
    _, _, root = _bootstrap(client, admin)
    missing = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_kind": "freeze_dry",
            "requested_quantity": 100,
            "output_unit": "g",
            "children": [{"sample_code": "FD-NORULE", "quantity": 0.5}],
        },
    )
    assert missing.status_code == 422

    unknown = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_kind": "freeze_dry",
            "requested_quantity": 100,
            "output_unit": "g",
            "conversion_rule_code": "NO-SUCH-RULE",
            "children": [{"sample_code": "FD-NORULE", "quantity": 0.5}],
        },
    )
    assert unknown.status_code == 422


def test_loss_requires_explainable_reason(client, admin):
    _, _, root = _bootstrap(client, admin)
    response = _aliquot(
        client,
        admin,
        root["id"],
        {
            "requested_quantity": 10,
            "loss_quantity": 1,
            "children": [{"sample_code": "LOSS-NO-REASON", "quantity": 9}],
        },
    )
    assert response.status_code == 422
    explained = _aliquot(
        client,
        admin,
        root["id"],
        {
            "requested_quantity": 10,
            "loss_quantity": 1,
            "loss_reason": "管壁残留",
            "children": [{"sample_code": "LOSS-WITH-REASON", "quantity": 9}],
        },
    )
    assert explained.status_code == 201, explained.text


def test_conservation_tolerance_enforced_and_customizable(client, admin):
    _, _, root = _bootstrap(client, admin)
    _register_rule(client, admin)
    payload = {
        "operation_kind": "freeze_dry",
        "requested_quantity": 500,
        "output_unit": "g",
        "conversion_rule_code": "ML-TO-G",
        "loss_quantity": 0.1,
        "loss_reason": "冻干飞散损耗",
        "children": [{"sample_code": "FD-TOL", "quantity": 2.0}],
    }
    exceeded = _aliquot(client, admin, root["id"], dict(payload))
    assert exceeded.status_code == 422
    relaxed = _aliquot(client, admin, root["id"], {**payload, "tolerance_ratio": 0.2})
    assert relaxed.status_code == 201, relaxed.text
    assert relaxed.json()["operation"]["tolerance_ratio"] == 0.2


def test_aliquot_replay_is_idempotent_by_operation_code(client, admin):
    _, _, root = _bootstrap(client, admin, quantity=100, unit="g", code="ROOT-REPLAY")
    payload = {
        "operation_code": "OP-REPLAY-1",
        "requested_quantity": 10,
        "children": [{"sample_code": "REPLAY-CHILD", "quantity": 10}],
    }
    first = _aliquot(client, admin, root["id"], dict(payload))
    assert first.status_code == 201, first.text
    second = _aliquot(client, admin, root["id"], dict(payload))
    assert second.status_code == 201
    assert second.json()["replayed"] is True
    assert [item["id"] for item in second.json()["children"]] == [
        item["id"] for item in first.json()["children"]
    ]
    assert second.json()["children"] == first.json()["children"]
    assert second.json()["operation"] == first.json()["operation"]
    assert second.json()["parent"]["quantity"] == 90

    conflicting = _aliquot(
        client,
        admin,
        root["id"],
        {**payload, "children": [{"sample_code": "REPLAY-OTHER", "quantity": 10}]},
    )
    assert conflicting.status_code == 409


def test_duplicate_child_codes_rejected(client, admin):
    _, _, root = _bootstrap(client, admin, quantity=100, unit="g", code="ROOT-DUP")
    in_request = _aliquot(
        client,
        admin,
        root["id"],
        {
            "requested_quantity": 20,
            "children": [
                {"sample_code": "DUP-CHILD", "quantity": 10},
                {"sample_code": "DUP-CHILD", "quantity": 10},
            ],
        },
    )
    assert in_request.status_code == 422
    _aliquot(
        client,
        admin,
        root["id"],
        {"requested_quantity": 10, "children": [{"sample_code": "DUP-CHILD", "quantity": 10}]},
    )
    existing = _aliquot(
        client,
        admin,
        root["id"],
        {"requested_quantity": 10, "children": [{"sample_code": "DUP-CHILD", "quantity": 10}]},
    )
    assert existing.status_code == 409


def test_rule_version_pin_conflict(client, admin):
    _, _, root = _bootstrap(client, admin)
    _register_rule(client, admin)
    _register_rule(client, admin, factor=0.006)
    stale = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_kind": "freeze_dry",
            "requested_quantity": 100,
            "output_unit": "g",
            "conversion_rule_code": "ML-TO-G",
            "conversion_rule_version": 1,
            "children": [{"sample_code": "FD-PIN", "quantity": 0.6}],
        },
    )
    assert stale.status_code == 409
    current = _aliquot(
        client,
        admin,
        root["id"],
        {
            "operation_kind": "freeze_dry",
            "requested_quantity": 100,
            "output_unit": "g",
            "conversion_rule_code": "ML-TO-G",
            "conversion_rule_version": 2,
            "children": [{"sample_code": "FD-PIN", "quantity": 0.6}],
        },
    )
    assert current.status_code == 201, current.text
    assert current.json()["operation"]["conversion_factor"] == 0.006


def test_ancestors_traces_upward_with_operation_snapshots(client, admin):
    root, fd, gd, leaves = _build_processed_tree(client, admin)
    response = client.get(
        f"/api/sample-operations/{leaves[0]['id']}/lineage/ancestors", headers=admin["headers"]
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cycle_detected"] is False
    chain = body["chain"]
    assert [item["sample"]["sample_code"] for item in chain] == ["ROOT-1", "FD-1", "GD-1", "A-1"]
    assert [item["depth"] for item in chain] == [0, 1, 2, 3]
    assert chain[0]["operation"] is None
    assert chain[1]["operation"]["operation_kind"] == "freeze_dry"
    assert chain[1]["operation"]["conversion_factor"] == 0.005
    assert chain[1]["operation"]["input_unit"] == "mL"
    assert chain[1]["operation"]["output_unit"] == "g"
    assert chain[2]["operation"]["operation_kind"] == "grind"
    assert chain[2]["operation"]["loss_reason"] == "研磨残留"
    assert chain[3]["operation"]["operation_kind"] == "aliquot"


def test_rollup_aggregates_inventory_and_cumulative_consumption(client, admin):
    root, _, _, leaves = _build_processed_tree(client, admin)
    consumed = client.post(
        f"/api/samples/{leaves[0]['id']}/consumptions",
        headers=admin["headers"],
        json={"experiment_code": "EXP-01", "quantity": 0.1, "idempotency_key": "rollup-consume-1"},
    )
    assert consumed.status_code == 201, consumed.text
    response = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unit"] == "mL"
    assert body["node_count"] == 5
    assert body["operation_count"] == 3
    assert body["remaining_by_unit"] == {"g": 2.2, "mL": 0.0}
    assert body["consumed_by_unit"] == {"g": 0.1}
    assert body["loss_by_unit"] == {"g": 0.2}
    normalized = body["normalized"]
    assert normalized["unit"] == "mL"
    assert normalized["initial_quantity"] == 500.0
    assert normalized["remaining_total"] == 440.0
    assert normalized["consumed_total"] == 20.0
    assert normalized["loss_total"] == 40.0
    assert normalized["accounted_total"] == 500.0
    assert normalized["unaccounted"] == 0.0
    assert normalized["conserved"] is True
    assert body["digest"]


def test_rollup_from_intermediate_node(client, admin):
    _, _, gd, _ = _build_processed_tree(client, admin)
    response = client.get(f"/api/sample-operations/{gd['id']}/lineage/rollup", headers=admin["headers"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["unit"] == "g"
    assert body["node_count"] == 3
    normalized = body["normalized"]
    assert normalized["initial_quantity"] == 2.3
    assert normalized["remaining_total"] == 2.3
    assert normalized["unaccounted"] == 0.0
    assert normalized["conserved"] is True


def test_rollup_includes_destroyed_quantity(client, admin):
    _, _, root = _bootstrap(client, admin, quantity=100, unit="g", code="ROOT-DESTROY")
    split = _aliquot(
        client,
        admin,
        root["id"],
        {"requested_quantity": 40, "children": [{"sample_code": "DESTROY-CHILD", "quantity": 40}]},
    )
    assert split.status_code == 201, split.text
    child_id = split.json()["children"][0]["id"]
    approvers = []
    for index in (1, 2):
        user = client.post(
            "/api/users",
            headers=admin["headers"],
            json={
                "username": f"approver.{index}",
                "password": "Approver!23456",
                "display_name": f"审批人{index}",
                "role_codes": ["approver"],
            },
        )
        assert user.status_code == 201, user.text
        login = client.post(
            "/api/auth/login",
            json={"username": f"approver.{index}", "password": "Approver!23456", "client_label": "tests"},
        )
        approvers.append({"id": user.json()["id"], "headers": {"Authorization": f"Bearer {login.json()['token']}"}})
    approval = client.post(
        "/api/samples/approvals",
        headers=admin["headers"],
        json={
            "action_type": "destruction",
            "resource_type": "sample",
            "resource_id": child_id,
            "payload": {"quantity": 10},
        },
    )
    assert approval.status_code == 201, approval.text
    for approver in approvers:
        decision = client.post(
            f"/api/samples/approvals/{approval.json()['id']}/decisions",
            headers=approver["headers"],
            json={"decision": "approve"},
        )
        assert decision.status_code == 200, decision.text
    executed = client.post(
        f"/api/sample-operations/destructions/{approval.json()['id']}",
        headers=admin["headers"],
        json={"method": "高温焚烧", "witness_one": approvers[0]["id"], "witness_two": approvers[1]["id"]},
    )
    assert executed.status_code == 201, executed.text
    rollup = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"])
    body = rollup.json()
    assert body["destroyed_by_unit"] == {"g": 10.0}
    normalized = body["normalized"]
    assert normalized["destroyed_total"] == 10.0
    assert normalized["remaining_total"] == 90.0
    assert normalized["unaccounted"] == 0.0
    assert normalized["conserved"] is True


def test_report_clean_tree_has_no_issues_and_stable_digest(client, admin):
    root, _, _, leaves = _build_processed_tree(client, admin)
    for sample_id in (root["id"], leaves[0]["id"]):
        response = client.get(
            f"/api/sample-operations/{sample_id}/lineage/report", headers=admin["headers"]
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["issues"] == []
        assert body["issue_counts"] == {}
        assert body["root_sample_id"] == root["id"]
        assert body["summary"]["normalized"]["conserved"] is True
    first = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    second = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    assert first == second
    assert first["digest"] == second["digest"]


def test_report_flags_broken_link_for_unlinked_child(client, admin):
    root, _, _, _ = _build_processed_tree(client, admin)
    _db_execute(
        """INSERT INTO samples(sample_code,batch_id,parent_sample_id,root_sample_id,sample_type,
               quantity,unit,lifecycle_state,lineage_depth,created_at,updated_at)
           VALUES('ORPHAN-1',?,?,?,'水样',5,'mL','available',1,'2026-09-25T00:00:00+00:00','2026-09-25T00:00:00+00:00')""",
        (root["batch_id"], root["id"], root["id"]),
    )
    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    broken = [issue for issue in report["issues"] if issue["kind"] == "broken_link"]
    assert any(issue["context"].get("sample_code") == "ORPHAN-1" for issue in broken)
    assert report["issue_counts"]["broken_link"] >= 1
    assert report["summary"]["normalized"]["conserved"] is False
    assert report["summary"]["normalized"]["unaccounted"] == -5.0


def test_rollup_marks_unnormalizable_nodes(client, admin):
    root, _, _, _ = _build_processed_tree(client, admin)
    _db_execute(
        """INSERT INTO samples(sample_code,batch_id,parent_sample_id,root_sample_id,sample_type,
               quantity,unit,lifecycle_state,lineage_depth,created_at,updated_at)
           VALUES('ORPHAN-UNIT',?,?,?,'冻干粉',5,'g','available',1,'2026-09-25T00:00:00+00:00','2026-09-25T00:00:00+00:00')""",
        (root["batch_id"], root["id"], root["id"]),
    )
    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    summary = report["summary"]
    orphan_id = next(
        issue["context"]["sample_id"]
        for issue in report["issues"]
        if issue["kind"] == "broken_link" and issue["context"].get("sample_code") == "ORPHAN-UNIT"
    )
    assert orphan_id in summary["unnormalizable_sample_ids"]
    assert summary["normalized"]["complete"] is False
    assert summary["normalized"]["conserved"] is False


def test_report_flags_cycle_and_ancestors_survives(client, admin):
    root, fd, _, _ = _build_processed_tree(client, admin)
    _db_execute("UPDATE samples SET parent_sample_id=? WHERE id=?", (fd["id"], root["id"]))
    report = client.get(f"/api/sample-operations/{fd['id']}/lineage/report", headers=admin["headers"])
    assert report.status_code == 200, report.text
    cycles = [issue for issue in report.json()["issues"] if issue["kind"] == "cycle"]
    assert cycles
    assert set(cycles[0]["context"]["sample_ids"]) == {root["id"], fd["id"]}
    ancestors = client.get(
        f"/api/sample-operations/{root['id']}/lineage/ancestors", headers=admin["headers"]
    )
    assert ancestors.status_code == 200
    assert ancestors.json()["cycle_detected"] is True


def test_report_flags_tolerance_exceeded_step(client, admin):
    root, _, _, _ = _build_processed_tree(client, admin)
    _db_execute("UPDATE aliquot_operations SET loss_quantity=loss_quantity+5 WHERE operation_code='OP-GRIND-1'")
    report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    exceeded = [issue for issue in report["issues"] if issue["kind"] == "tolerance_exceeded"]
    assert len(exceeded) == 1
    assert exceeded[0]["context"]["operation_code"] == "OP-GRIND-1"
    assert exceeded[0]["context"]["deviation"] > exceeded[0]["context"]["allowed_deviation"]
    assert report["summary"]["normalized"]["conserved"] is False


def test_detect_issues_flags_duplicate_codes():
    nodes = [
        {"id": 1, "sample_code": "DUP-S", "parent_sample_id": None, "root_sample_id": 1, "unit": "g", "quantity": 10},
        {"id": 2, "sample_code": "DUP-S", "parent_sample_id": 1, "root_sample_id": 1, "unit": "g", "quantity": 5},
    ]
    operations = [
        {
            "id": 1,
            "operation_code": "DUP-OP",
            "parent_sample_id": 1,
            "requested_quantity": 5,
            "produced_quantity": 5,
            "loss_quantity": 0,
            "input_unit": "g",
            "output_unit": "g",
            "conversion_rule_code": None,
            "conversion_factor": 1.0,
            "tolerance_ratio": 0.005,
        },
        {
            "id": 2,
            "operation_code": "DUP-OP",
            "parent_sample_id": 1,
            "requested_quantity": 5,
            "produced_quantity": 5,
            "loss_quantity": 0,
            "input_unit": "g",
            "output_unit": "g",
            "conversion_rule_code": None,
            "conversion_factor": 1.0,
            "tolerance_ratio": 0.005,
        },
    ]
    links = [{"id": 1, "operation_id": 1, "child_sample_id": 2, "quantity": 5}]
    issues = detect_issues(nodes, operations, links)
    duplicates = [issue for issue in issues if issue["kind"] == "duplicate_code"]
    assert any(issue["context"].get("sample_code") == "DUP-S" for issue in duplicates)
    assert any(issue["context"].get("operation_code") == "DUP-OP" for issue in duplicates)
    kinds = [issue["kind"] for issue in issues]
    assert kinds == sorted(kinds)


def test_history_not_rewritten_by_later_rule_versions(client, admin):
    root, fd, _, _ = _build_processed_tree(client, admin)
    before_rollup = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"]).json()
    before_report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    upgraded = _register_rule(client, admin, factor=0.006)
    assert upgraded.status_code == 201
    after_rollup = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"]).json()
    after_report = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    assert after_rollup == before_rollup
    assert after_report == before_report
    ancestors = client.get(f"/api/sample-operations/{fd['id']}/lineage/ancestors", headers=admin["headers"]).json()
    freeze_dry_step = [item for item in ancestors["chain"] if item["sample"]["sample_code"] == "FD-1"][0]
    assert freeze_dry_step["operation"]["conversion_factor"] == 0.005
    assert freeze_dry_step["operation"]["conversion_rule_version"] == 1


def test_report_and_rollup_outputs_are_deterministic(client, admin):
    root, _, _, _ = _build_processed_tree(client, admin)
    _db_execute(
        """INSERT INTO samples(sample_code,batch_id,parent_sample_id,root_sample_id,sample_type,
               quantity,unit,lifecycle_state,lineage_depth,created_at,updated_at)
           VALUES('ORPHAN-2',?,?,?,'冻干粉',3,'g','available',1,'2026-09-25T00:00:00+00:00','2026-09-25T00:00:00+00:00')""",
        (root["batch_id"], root["id"], root["id"]),
    )
    _db_execute("UPDATE aliquot_operations SET loss_quantity=loss_quantity+9 WHERE operation_code='OP-SPLIT-1'")
    first = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    second = client.get(f"/api/sample-operations/{root['id']}/lineage/report", headers=admin["headers"]).json()
    assert first == second
    kinds = [issue["kind"] for issue in first["issues"]]
    assert kinds == sorted(kinds)
    assert "broken_link" in kinds
    assert "tolerance_exceeded" in kinds
    rollup_first = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"]).json()
    rollup_second = client.get(f"/api/sample-operations/{root['id']}/lineage/rollup", headers=admin["headers"]).json()
    assert rollup_first == rollup_second
