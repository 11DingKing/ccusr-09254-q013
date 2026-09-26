"""保留策略与处置任务的接口及服务测试。"""

from __future__ import annotations

import threading

import pytest
from sqlalchemy import select

from app import retention_services as retention
from app.models import DisposalManifestEntry, ErasedFingerprint
from app.models import Event as EventModel
from app.retention import ItemFailure, ensure_utc
from tests.conftest import SHANGHAI_PLAN, TestSessionLocal


def _create_plan(client, plan=SHANGHAI_PLAN):
    resp = client.post("/api/plans", json=plan)
    assert resp.status_code == 201, resp.text


def _checkin(eid, student, start="2024-03-15T08:00:00+08:00", end="2024-03-15T10:00:00+08:00", activity_type="regular"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": activity_type,
            "check_in_at": start,
            "check_out_at": end,
        },
    }


def _leave(eid, student, seconds=3600, reason="make-up"):
    return {
        "event_id": eid,
        "event_type": "leave_correction",
        "student_id": student,
        "payload": {"adjustment_seconds": seconds, "reason": reason},
    }


def _post_events(client, events, plan=SHANGHAI_PLAN):
    resp = client.post(f"/api/plans/{plan['plan_version']}/events", json={"events": events})
    assert resp.status_code == 201, resp.text


def _create_policy(client, rules=None, policy_id="P-RET", activate=True):
    if rules is None:
        rules = [{"category": "*", "retention_days": 0, "disposition": "erase"}]
    resp = client.post(
        "/api/retention/policies",
        json={"policy_id": policy_id, "rules": rules, "activate": activate},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _create_task(client, student="S1", plan=SHANGHAI_PLAN):
    resp = client.post(
        "/api/retention/tasks",
        json={
            "plan_version": plan["plan_version"],
            "student_id": student,
            "requested_by": "privacy-office",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _approve(client, task_id):
    resp = client.post(
        f"/api/retention/tasks/{task_id}/approve", json={"approved_by": "compliance-lead"}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _execute(client, task_id, body=None):
    resp = client.post(f"/api/retention/tasks/{task_id}/execute", json=body or {})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _event_keys(db, plan_version):
    rows = db.execute(
        select(EventModel.event_id)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id)
    ).scalars()
    return list(rows)


# ---------------------------------------------------------------------------
# 策略管理
# ---------------------------------------------------------------------------


def test_policy_versions_and_activation(client):
    v1 = _create_policy(client, activate=False)
    assert v1["version"] == 1
    assert v1["status"] == "draft"

    v2 = _create_policy(
        client,
        rules=[{"category": "checkin", "retention_days": 30, "disposition": "fingerprint"}],
    )
    assert v2["version"] == 2
    assert v2["status"] == "active"

    # 激活 v1 后 v2 退役，全程只有一个激活策略。
    activated = client.post("/api/retention/policies/P-RET/1/activate")
    assert activated.status_code == 200
    assert activated.json()["status"] == "active"
    policies = client.get("/api/retention/policies").json()
    by_version = {p["version"]: p["status"] for p in policies}
    assert by_version == {1: "active", 2: "retired"}

    # 策略内容创建后不可变，哈希稳定。
    again = client.get("/api/retention/policies/P-RET/1").json()
    assert again["policy_hash"] == v1["policy_hash"]
    assert again["rules"] == v1["rules"]

    assert client.get("/api/retention/policies/P-RET/99").status_code == 404
    bad = client.post(
        "/api/retention/policies",
        json={"policy_id": "P-BAD", "rules": [{"category": "*", "retention_days": -1, "disposition": "erase"}]},
    )
    assert bad.status_code == 422


def test_preview_requires_active_policy(client):
    _create_plan(client)
    resp = client.post(
        "/api/retention/preview",
        json={"plan_version": SHANGHAI_PLAN["plan_version"], "student_id": "S1"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# 预览与分类
# ---------------------------------------------------------------------------


def test_preview_separates_erase_fingerprint_and_hold(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1"), _leave("E-02", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-1", json={})
    _post_events(client, [_checkin("E-03", "S1"), _checkin("E-04", "S2")])
    _create_policy(client)
    client.post(
        "/api/retention/holds",
        json={"hold_id": "H-1", "plan_version": pv, "student_id": "S2", "reason": "诉讼保全"},
    )

    preview = client.post(
        "/api/retention/preview", json={"plan_version": pv, "student_id": "S1"}
    ).json()
    dispositions = {item["item_key"]: item["disposition"] for item in preview["items"]}
    # 已签发证明引用的明细保留指纹，未引用的可删除。
    assert dispositions == {"E-01": "fingerprint", "E-02": "fingerprint", "E-03": "erase"}
    assert preview["summary"] == {"erase": 1, "fingerprint": 2, "held": 0, "keep": 0}

    held = client.post(
        "/api/retention/preview", json={"plan_version": pv, "student_id": "S2"}
    ).json()
    assert held["items"][0]["disposition"] == "held"
    assert "法律冻结" in held["items"][0]["reason"]


def test_retention_window_keeps_recent_records(client):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1")])
    _create_policy(client, rules=[{"category": "*", "retention_days": 365, "disposition": "erase"}])

    preview = client.post(
        "/api/retention/preview", json={"plan_version": pv, "student_id": "S1"}
    ).json()
    assert preview["items"][0]["disposition"] == "keep"

    # 未到保留年限的明细不进入任务，任务直接完成。
    task = _create_task(client)
    assert task["counts"]["total"] == 0
    _approve(client, task["task_id"])
    done = _execute(client, task["task_id"])
    assert done["status"] == "completed"
    assert client.get(f"/api/plans/{pv}/students/S1/progress").status_code == 200


# ---------------------------------------------------------------------------
# 任务工作流与证明核验
# ---------------------------------------------------------------------------


def _capture_originals(db, plan_version):
    rows = db.execute(
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .order_by(EventModel.event_id)
    ).scalars().all()
    return [
        {
            "plan_version": r.plan_version,
            "record_key": r.event_id,
            "record_type": r.event_type,
            "student_id": r.student_id,
            "payload": dict(r.payload),
            "created_at": ensure_utc(r.created_at).isoformat(),
        }
        for r in rows
    ]


def test_task_workflow_erases_and_preserves_certificate(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1"), _leave("E-02", "S1")])
    client.post(f"/api/plans/{pv}/freezes/F-1", json={})
    _post_events(client, [_checkin("E-03", "S1")])
    originals = _capture_originals(db, pv)
    _create_policy(client)

    task = _create_task(client)
    task_id = task["task_id"]
    assert task["status"] == "pending_approval"
    assert task["counts"]["total"] == 3

    _approve(client, task_id)
    done = _execute(client, task_id)
    assert done["status"] == "completed"
    assert done["counts"] == {"total": 3, "pending": 0, "done": 3, "failed": 0, "held": 0}

    # 明细原文全部删除，隐私要求满足。
    assert _event_keys(db, pv) == []
    assert client.get(f"/api/plans/{pv}/students/S1/progress").status_code == 404

    # 被证明引用的两条保留指纹，未引用的一条彻底删除。
    fingerprints = {
        row.record_key
        for row in db.execute(select(ErasedFingerprint)).scalars()
    }
    assert fingerprints == {"E-01", "E-02"}

    # 任务核验通过，清单不可篡改。
    verify = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert verify["valid"] is True
    assert verify["entries_checked"] == 3
    manifest = client.get(f"/api/retention/tasks/{task_id}/manifest").json()
    assert [m["action"] for m in manifest] == ["fingerprinted", "fingerprinted", "erased"]
    assert manifest[0]["fingerprint"] != ""

    # 已签发证明仍可读取且核验通过。
    snap = client.get(f"/api/plans/{pv}/freezes/F-1").json()
    assert snap["students"][0]["total_seconds"] == 7200 + 3600
    cert = client.get(f"/api/retention/certificates/{pv}/F-1/verify").json()
    assert cert["valid"] is True
    assert cert["referenced_records"] == 2
    assert cert["fingerprinted_records"] == 2
    assert cert["missing_records"] == []

    # 出示原始明细可比对指纹；篡改后比对失败。
    presented = [o for o in originals if o["record_key"] in ("E-01", "E-02")]
    res = client.post("/api/retention/fingerprints/verify", json={"records": presented}).json()
    assert res["all_matched"] is True
    tampered = dict(presented[0])
    tampered["payload"] = {**tampered["payload"], "activity_id": "FORGED"}
    res2 = client.post("/api/retention/fingerprints/verify", json={"records": [tampered]}).json()
    assert res2["all_matched"] is False
    assert res2["results"][0]["stored"] is True
    assert res2["results"][0]["match"] is False
    # 彻底删除的明细没有指纹留存。
    res3 = client.post(
        "/api/retention/fingerprints/verify",
        json={"records": [o for o in originals if o["record_key"] == "E-03"]},
    ).json()
    assert res3["results"][0]["stored"] is False


def test_policy_version_pinned_to_task(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1")])
    v1 = _create_policy(client, rules=[{"category": "*", "retention_days": 0, "disposition": "erase"}])

    task = _create_task(client)
    task_id = task["task_id"]
    assert task["policy_version"] == 1

    # 新版本把保留年限改为 10 年并激活。
    _create_policy(
        client,
        rules=[{"category": "*", "retention_days": 3650, "disposition": "erase"}],
    )
    preview = client.post(
        "/api/retention/preview", json={"plan_version": pv, "student_id": "S1"}
    ).json()
    assert preview["policy_version"] == 2
    assert preview["items"][0]["disposition"] == "keep"

    # 已创建的任务仍按 v1 执行删除。
    _approve(client, task_id)
    done = _execute(client, task_id)
    assert done["status"] == "completed"
    assert done["policy_version"] == 1
    assert done["policy_hash"] == v1["policy_hash"]
    assert _event_keys(db, pv) == []
    verify = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert verify["checks"]["policy_pinned"] is True


# ---------------------------------------------------------------------------
# 暂停、恢复与状态守卫
# ---------------------------------------------------------------------------


def test_pause_and_resume_execution(client):
    _create_plan(client)
    _post_events(client, [_checkin(f"E-0{i}", "S1") for i in range(1, 4)])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)

    first = _execute(client, task_id, {"max_items": 1})
    assert first["status"] == "running"
    assert first["counts"]["done"] == 1
    assert first["counts"]["pending"] == 2

    paused = client.post(f"/api/retention/tasks/{task_id}/pause").json()
    assert paused["status"] == "paused"

    # 暂停期间禁止执行。
    blocked = client.post(f"/api/retention/tasks/{task_id}/execute", json={})
    assert blocked.status_code == 409

    resumed = client.post(f"/api/retention/tasks/{task_id}/resume").json()
    assert resumed["status"] == "approved"

    done = _execute(client, task_id)
    assert done["status"] == "completed"
    assert done["counts"]["done"] == 3
    manifest = client.get(f"/api/retention/tasks/{task_id}/manifest").json()
    assert [m["seq"] for m in manifest] == [1, 2, 3]


def test_state_guards(client):
    _create_plan(client)
    _post_events(client, [_checkin("E-01", "S1")])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]

    # 未批准不能执行；重复创建进行中的任务被拒绝。
    assert client.post(f"/api/retention/tasks/{task_id}/execute", json={}).status_code == 409
    assert client.post(
        "/api/retention/tasks",
        json={
            "plan_version": SHANGHAI_PLAN["plan_version"],
            "student_id": "S1",
            "requested_by": "privacy-office",
        },
    ).status_code == 409

    _approve(client, task_id)
    assert client.post(
        f"/api/retention/tasks/{task_id}/approve", json={"approved_by": "again"}
    ).status_code == 409
    assert client.post(f"/api/retention/tasks/{task_id}/resume").status_code == 409

    _execute(client, task_id)
    # 完成后无可重试明细。
    assert client.post(f"/api/retention/tasks/{task_id}/retry").status_code == 409
    assert client.post(f"/api/retention/tasks/{task_id}/pause").status_code == 409
    assert client.get("/api/retention/tasks/DT-unknown").status_code == 404


# ---------------------------------------------------------------------------
# 部分失败与重试
# ---------------------------------------------------------------------------


def test_partial_failure_marks_item_and_retry_completes(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin(f"E-0{i}", "S1") for i in range(1, 4)])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)

    def flaky(session, *, task, item, event, disposition, now):
        if item.item_key == "E-02":
            raise ItemFailure("存储暂时不可用")
        return retention.default_item_processor(
            session, task=task, item=item, event=event, disposition=disposition, now=now
        )

    view = retention.execute_task(db, task_id, processor=flaky)
    assert view["status"] == "failed"
    assert view["counts"]["done"] == 2
    assert view["counts"]["failed"] == 1
    detail = retention.get_task_view(db, task_id)
    failed_item = next(i for i in detail["items"] if i["item_key"] == "E-02")
    assert failed_item["attempts"] == 1
    assert "存储暂时不可用" in failed_item["last_error"]

    # 失败也写入清单，成功明细的删除不会回滚。
    manifest = retention.get_manifest(db, task_id)
    assert [m["action"] for m in manifest] == ["erased", "failed", "erased"]
    assert _event_keys(db, pv) == ["E-02"]

    # 重试只处理失败明细，清单继续追加且链条完整。
    retried = client.post(f"/api/retention/tasks/{task_id}/retry").json()
    assert retried["status"] == "approved"
    assert retried["counts"]["pending"] == 1
    done = _execute(client, task_id)
    assert done["status"] == "completed"
    assert _event_keys(db, pv) == []
    verify = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert verify["valid"] is True
    assert verify["entries_checked"] == 4


# ---------------------------------------------------------------------------
# 中断后的重启续跑
# ---------------------------------------------------------------------------


def test_interrupted_execution_resumes_after_restart(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin(f"E-0{i}", "S1") for i in range(1, 4)])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)

    calls: list[str] = []

    def crashing(session, *, task, item, event, disposition, now):
        calls.append(item.item_key)
        if len(calls) == 2:
            raise RuntimeError("simulated crash")
        return retention.default_item_processor(
            session, task=task, item=item, event=event, disposition=disposition, now=now
        )

    with pytest.raises(RuntimeError):
        retention.execute_task(db, task_id, processor=crashing)

    # 已提交的明细保留成果，任务进入可恢复的暂停态。
    view = retention.get_task_view(db, task_id)
    assert view["status"] == "paused"
    assert view["counts"]["done"] == 1
    assert view["counts"]["pending"] == 2
    assert "执行中断" in view["last_error"]
    assert _event_keys(db, pv) == ["E-02", "E-03"]

    # 模拟服务重启：用全新会话恢复并续跑，不重复处理已完成明细。
    session2 = TestSessionLocal()
    try:
        retention.resume_task(session2, task_id)
        final = retention.execute_task(session2, task_id)
    finally:
        session2.close()

    assert final["status"] == "completed"
    assert calls == ["E-01", "E-02"]
    assert _event_keys(db, pv) == []
    verify = retention.verify_task(db, task_id)
    assert verify["valid"] is True
    assert verify["entries_checked"] == 3
    manifest = retention.get_manifest(db, task_id)
    assert [m["seq"] for m in manifest] == [1, 2, 3]
    assert all(m["action"] == "erased" for m in manifest)


# ---------------------------------------------------------------------------
# 法律冻结
# ---------------------------------------------------------------------------


def test_legal_hold_blocks_and_release_allows_retry(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1"), _leave("E-02", "S1")])
    _create_policy(client)
    client.post(
        "/api/retention/holds",
        json={"hold_id": "H-1", "plan_version": pv, "student_id": "S1", "reason": "法院调查取证"},
    )

    task = _create_task(client)
    task_id = task["task_id"]
    assert {i["disposition"] for i in task["items"]} == {"held"}
    _approve(client, task_id)
    done = _execute(client, task_id)
    assert done["status"] == "completed"
    assert done["counts"]["held"] == 2
    assert done["counts"]["done"] == 0

    # 冻结期间明细原封不动。
    assert _event_keys(db, pv) == ["E-01", "E-02"]
    assert client.get(f"/api/plans/{pv}/students/S1/progress").status_code == 200
    manifest = client.get(f"/api/retention/tasks/{task_id}/manifest").json()
    assert [m["action"] for m in manifest] == ["held", "held"]
    verify = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert verify["valid"] is True
    assert verify["checks"]["holds_respected"] is True

    # 解除冻结后重试，明细按策略删除。
    released = client.post("/api/retention/holds/H-1/release").json()
    assert released["active"] is False
    retried = client.post(f"/api/retention/tasks/{task_id}/retry").json()
    assert retried["counts"]["pending"] == 2
    final = _execute(client, task_id)
    assert final["status"] == "completed"
    assert final["counts"]["done"] == 2
    assert _event_keys(db, pv) == []
    verify = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert verify["valid"] is True
    assert verify["entries_checked"] == 4


def test_hold_placed_after_approval_still_blocks(client, db):
    _create_plan(client)
    pv = SHANGHAI_PLAN["plan_version"]
    _post_events(client, [_checkin("E-01", "S1")])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)

    # 批准之后、执行之前落下的冻结同样生效。
    client.post(
        "/api/retention/holds",
        json={"hold_id": "H-late", "plan_version": pv, "student_id": "S1", "reason": "监管检查"},
    )
    done = _execute(client, task_id)
    assert done["counts"]["held"] == 1
    assert _event_keys(db, pv) == ["E-01"]


# ---------------------------------------------------------------------------
# 并发查询
# ---------------------------------------------------------------------------


def test_concurrent_queries_during_execution(client, db):
    _create_plan(client)
    _post_events(client, [_checkin(f"E-{i:02d}", "S1") for i in range(1, 9)])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)

    errors: list[Exception] = []

    def reader():
        session = TestSessionLocal()
        try:
            for _ in range(20):
                view = retention.get_task_view(session, task_id)
                assert view["task_id"] == task_id
                retention.get_manifest(session, task_id)
                result = retention.verify_task(session, task_id)
                # 任意时刻读到的清单都应是完整前缀。
                assert result["checks"]["manifest_chain"] is True
                assert result["checks"]["policy_pinned"] is True
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()

    view = retention.execute_task(db, task_id, max_items=1)
    while view["counts"]["pending"] > 0:
        view = retention.execute_task(db, task_id, max_items=1)

    for t in threads:
        t.join()

    assert not errors
    assert view["status"] == "completed"
    final = retention.verify_task(db, task_id)
    assert final["valid"] is True
    assert final["entries_checked"] == 8


# ---------------------------------------------------------------------------
# 清单防篡改
# ---------------------------------------------------------------------------


def test_manifest_tamper_detected(client, db):
    _create_plan(client)
    _post_events(client, [_checkin("E-01", "S1"), _checkin("E-02", "S1")])
    _create_policy(client)
    task = _create_task(client)
    task_id = task["task_id"]
    _approve(client, task_id)
    _execute(client, task_id)

    before = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert before["valid"] is True

    entry = db.get(DisposalManifestEntry, (task_id, 1))
    assert entry is not None
    entry.fingerprint = "0" * 64
    db.commit()

    after = client.get(f"/api/retention/tasks/{task_id}/verify").json()
    assert after["valid"] is False
    assert after["checks"]["manifest_chain"] is False
