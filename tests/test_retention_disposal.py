"""留存策略与处置任务测试。

覆盖：三类处置分离、策略版本固定、预览/批准/执行/核验 API、
部分失败重试、法律冻结实时复核、暂停恢复、重启续跑、
并发执行与并发查询、哈希链防篡改。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
from sqlalchemy import select, update

from app.models import Event
from app.retention import service
from app.retention.fingerprint import event_fingerprint
from tests.conftest import TestSessionLocal


def _checkin(eid, student, day="2024-03-15"):
    return {
        "event_id": eid,
        "event_type": "checkin",
        "student_id": student,
        "payload": {
            "activity_id": "A1",
            "activity_type": "regular",
            "check_in_at": f"{day}T08:00:00+08:00",
            "check_out_at": f"{day}T10:00:00+08:00",
        },
    }


POLICY = {
    "policy_version": "R-V1",
    "rules": [
        {"detail_type": "checkin", "retain_days": 30, "action": "delete"},
        {"detail_type": "mentor_confirm", "retain_days": 30,
         "action": "retain_fingerprint"},
        {"detail_type": "leave_correction", "retain_days": 3650, "action": "delete"},
    ],
}


@pytest.fixture
def scenario(client):
    """准备计划、四条签到（其中三条被回溯为旧数据）与一份冻结。"""
    client.post("/api/plans", json={
        "plan_version": "P1", "iana_timezone": "Asia/Shanghai",
        "required_seconds": 3600,
    })
    # E-01 先导入并立刻冻结，使其被已签发证明覆盖。
    client.post("/api/plans/P1/events", json={
        "events": [_checkin("E-01", "S-FROZEN")]
    })
    f = client.post("/api/plans/P1/freezes/F-01", json={})
    assert f.status_code == 201
    assert f.json()["event_cutoff_id"] == "E-01"

    client.post("/api/plans/P1/events", json={"events": [
        _checkin("E-02", "S-HELD"),
        _checkin("E-03", "S-DELETE"),
        _checkin("E-04", "S-NEW"),
    ]})

    # 将 E-01..E-03 的入库时间回溯到多年前（已过保留期），E-04 保持当前。
    old = datetime(2020, 1, 1, tzinfo=timezone.utc)
    session = TestSessionLocal()
    try:
        session.execute(
            update(Event)
            .where(Event.event_id.in_(["E-01", "E-02", "E-03"]))
            .values(created_at=old)
        )
        session.commit()
    finally:
        session.close()

    resp = client.post("/api/retention/policies", json=POLICY)
    assert resp.status_code == 201, resp.text
    client.post("/api/legal-holds/H-1", json={
        "student_id": "S-HELD", "reason": "labor dispute investigation",
    })
    return client


def _items_by_event(task_body: dict) -> dict:
    return {i["event_id"]: i for i in task_body["items"]}


def test_preview_separates_delete_fingerprint_and_legal_hold(scenario):
    client = scenario
    resp = client.post("/api/disposal-tasks/T-1/preview", json={
        "policy_version": "R-V1",
    })
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["state"] == "preview"
    assert body["dry_run"] is True
    assert body["policy_version"] == "R-V1"
    assert len(body["policy_checksum"]) == 64

    items = _items_by_event(body)
    # 新明细 E-04 未过保留期，不入清单。
    assert set(items) == {"E-01", "E-02", "E-03"}
    assert items["E-03"]["planned_action"] == "delete"
    # 被冻结证明引用：即使策略是 delete，也强制保留指纹。
    assert items["E-01"]["planned_action"] == "retain_fingerprint"
    # 法律冻结优先级最高：原样保留。
    assert items["E-02"]["planned_action"] == "legal_hold"

    # 预览不改变任何明细。
    session = TestSessionLocal()
    try:
        rows = session.scalars(select(Event).where(Event.plan_version == "P1")).all()
        assert len(rows) == 4
        assert all(not r.payload.get("redacted") for r in rows)
    finally:
        session.close()


def test_execute_requires_approval(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    resp = client.post("/api/disposal-tasks/T-1/execute", json={})
    assert resp.status_code == 409


def test_full_flow_execute_and_verify_three_dispositions(scenario):
    client = scenario
    # 先记录 E-01 原始内容指纹，执行后原件应被脱敏但指纹保留。
    session = TestSessionLocal()
    e01 = session.scalar(select(Event).where(Event.event_id == "E-01"))
    expected_e01_fp = event_fingerprint(
        event_id=e01.event_id, plan_version=e01.plan_version,
        student_id=e01.student_id, event_type=e01.event_type,
        payload=e01.payload,
        created_at_iso=e01.created_at.replace(tzinfo=timezone.utc)
        .isoformat().replace("+00:00", "Z"),
    )
    session.close()

    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    approved = client.post("/api/disposal-tasks/T-1/approve",
                           json={"approved_by": "dpo@school"}).json()
    assert approved["state"] == "approved"
    assert approved["dry_run"] is False

    result = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    assert result["state"] == "completed"
    assert result["counts"] == {
        "pending": 0, "failed": 0, "deleted": 1,
        "fingerprinted": 1, "held": 1, "skipped": 0,
    }

    items = _items_by_event(result)
    assert items["E-01"]["status"] == "fingerprinted"
    assert items["E-01"]["retained_fingerprint"] == expected_e01_fp
    assert items["E-02"]["status"] == "held"
    assert items["E-03"]["status"] == "deleted"
    # 每条都有哈希链记录，序号连续。
    assert [i["manifest_sequence"] for i in sorted(
        result["items"], key=lambda i: i["manifest_sequence"])] == [1, 2, 3]

    session = TestSessionLocal()
    try:
        remaining = {
            r.event_id: r for r in
            session.scalars(select(Event).where(Event.plan_version == "P1")).all()
        }
        assert "E-03" not in remaining  # 已删除
        assert "E-04" in remaining  # 未到期不动
        held = remaining["E-02"]
        assert held.student_id == "S-HELD"  # 法律冻结：原样保留
        assert held.payload["activity_id"] == "A1"
        redacted = remaining["E-01"]
        assert redacted.payload == {"redacted": True, "reason": "retention_disposal"}
        assert redacted.student_id.startswith("redacted:")
        # 重放不再包含已脱敏明细。
        from app.repository import load_events
        core_events = load_events(session, "P1")
        replayed_types = {(e.event_id, e.event_type) for e in core_events}
        assert ("E-01", "checkin") in replayed_types  # 行还在，但重放跳过
    finally:
        session.close()

    # 已签发证明仍可核验，内容不变。
    frozen = client.get("/api/plans/P1/freezes/F-01").json()
    assert frozen["students"][0]["total_seconds"] == 7200

    # 清单与核验。
    manifest = client.get("/api/disposal-tasks/T-1/manifest").json()
    assert manifest["chain_tip"] == result["manifest_hash"]
    assert [e["sequence"] for e in manifest["entries"]] == [1, 2, 3]
    assert manifest["policy_version"] == "R-V1"

    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is True
    assert verify["state_ok"] is True
    assert verify["entries_verified"] == 3


def test_policy_version_is_pinned_even_after_new_version(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    # 发布新版本不影响已生成的任务。
    v2 = client.post("/api/retention/policies", json={
        "policy_version": "R-V2",
        "rules": [
            {"detail_type": "checkin", "retain_days": 36500,
             "action": "retain_fingerprint"},
        ],
    })
    assert v2.status_code == 201
    # 同版本号不可覆盖。
    dup = client.post("/api/retention/policies", json=POLICY)
    assert dup.status_code == 409

    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})
    result = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    assert result["policy_version"] == "R-V1"
    manifest = client.get("/api/disposal-tasks/T-1/manifest").json()
    assert all(e["action"] in {"delete", "retain_fingerprint", "legal_hold"}
               for e in manifest["entries"])
    assert manifest["policy_checksum"] == result["policy_checksum"]


def test_partial_failure_is_isolated_and_retry_succeeds(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})

    calls = {"n": 0}

    def fault(item):
        # E-02（处置顺序第二条）首次处理时失败一次。
        if item.event_id == "E-02" and item.attempts == 0:
            calls["n"] += 1
            raise RuntimeError("simulated storage outage")

    service._fault_hook = fault
    try:
        first = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    finally:
        service._fault_hook = None

    assert calls["n"] == 1
    assert first["state"] == "completed_with_errors"
    assert first["counts"]["failed"] == 1
    assert first["counts"]["deleted"] == 1  # E-03 不受影响
    assert first["failed_this_run"] == 1
    items = _items_by_event(first)
    assert items["E-02"]["status"] == "failed"
    assert "simulated storage outage" in items["E-02"]["error"]
    assert items["E-02"]["attempts"] == 1
    # 失败项尚未入链，链中只有 2 条。
    assert first["manifest_hash"] is not None

    # 重试：只有失败项被处理，成功项不重复处置。
    second = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    assert second["state"] == "completed"
    assert second["counts"] == {
        "pending": 0, "failed": 0, "deleted": 1,
        "fingerprinted": 1, "held": 1, "skipped": 0,
    }
    assert second["processed_this_run"] == 1
    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is True
    assert verify["state_ok"] is True
    manifest = client.get("/api/disposal-tasks/T-1/manifest").json()
    # 重试成功的条目追加到链尾，序号为 3。
    last = manifest["entries"][-1]
    assert last["event_id"] == "E-02"
    assert last["sequence"] == 3


def test_pause_resume_and_limit_batching(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    # 批准后、执行前也可以暂停。
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})
    paused = client.post("/api/disposal-tasks/T-1/pause", json={}).json()
    assert paused["state"] == "paused"

    # 每批只处理 1 条：第一批恢复执行后应处理 E-01。
    first = client.post("/api/disposal-tasks/T-1/execute", json={"limit": 1}).json()
    assert first["state"] == "running"
    assert first["processed_this_run"] == 1
    assert first["counts"]["fingerprinted"] == 1

    # 运行中再暂停，随后继续。
    client.post("/api/disposal-tasks/T-1/pause", json={})
    second = client.post("/api/disposal-tasks/T-1/execute", json={"limit": 1}).json()
    assert second["processed_this_run"] == 1
    assert second["state"] == "running"

    # 不带 limit 跑完剩余条目。
    final = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    assert final["state"] == "completed"
    assert final["counts"]["held"] == 1
    assert final["counts"]["deleted"] == 1
    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is True


def test_restart_resumes_from_committed_positions(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})
    first = client.post("/api/disposal-tasks/T-1/execute", json={"limit": 2}).json()
    assert first["counts"]["pending"] == 1
    tip_after_two = first["manifest_hash"]

    # 模拟进程重启：用全新会话继续，终态条目跳过，剩余条目续跑。
    session = TestSessionLocal()
    try:
        resumed = service.execute_task(session, task_id="T-1")
    finally:
        session.close()
    assert resumed["state"] == "completed"
    assert resumed["counts"]["pending"] == 0
    # 链尖在原有基础上继续延伸而非重新开始。
    assert resumed["manifest_hash"] != tip_after_two
    assert len(resumed["manifest_hash"]) == 64

    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is True
    assert verify["state_ok"] is True
    assert verify["entries_verified"] == 3


def test_concurrent_executors_process_each_item_once(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})

    errors: list[Exception] = []

    def _execute():
        session = TestSessionLocal()
        try:
            service.execute_task(session, task_id="T-1")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    def _query():
        # 执行期间并发查询任务、清单与核验，不允许出现异常。
        session = TestSessionLocal()
        try:
            for _ in range(5):
                service.task_to_dict(session, "T-1")
                service.get_manifest(session, "T-1")
                service.verify_manifest(session, "T-1")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_execute) for _ in range(4)]
    threads += [threading.Thread(target=_query) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []

    session = TestSessionLocal()
    try:
        final = service.task_to_dict(session, "T-1")
        assert final["state"] == "completed"
        assert final["counts"] == {
            "pending": 0, "failed": 0, "deleted": 1,
            "fingerprinted": 1, "held": 1, "skipped": 0,
        }
        verify = service.verify_manifest(session, "T-1")
        assert verify["chain_ok"] is True
        assert verify["state_ok"] is True
        assert verify["entries_verified"] == 3
        # 没有任何条目被重复入链。
        manifest = service.get_manifest(session, "T-1")
        sequences = [e["sequence"] for e in manifest["entries"]]
        assert sequences == [1, 2, 3]
    finally:
        session.close()


def test_verify_detects_content_and_chain_tampering(scenario):
    client = scenario
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})
    client.post("/api/disposal-tasks/T-1/execute", json={})

    # 1) 篡改法律冻结明细内容 → 状态核验失败（指纹不再匹配）。
    session = TestSessionLocal()
    try:
        held = session.scalar(select(Event).where(Event.event_id == "E-02"))
        original_payload = dict(held.payload)
        held.payload = {"activity_id": "TAMPERED"}
        session.commit()
    finally:
        session.close()

    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is True  # 链本身完好
    assert verify["state_ok"] is False
    held_check = next(c for c in verify["state_checks"] if c["event_id"] == "E-02")
    assert held_check["state_ok"] is False

    # 还原后核验通过。
    session = TestSessionLocal()
    try:
        held = session.scalar(select(Event).where(Event.event_id == "E-02"))
        held.payload = original_payload
        session.commit()
    finally:
        session.close()
    assert client.get("/api/disposal-tasks/T-1/verify").json()["state_ok"] is True

    # 2) 篡改清单条目哈希 → 断链。
    session = TestSessionLocal()
    try:
        from app.models import DisposalItem
        item = session.scalar(
            select(DisposalItem).where(
                DisposalItem.task_id == "T-1", DisposalItem.event_id == "E-01"
            )
        )
        item.manifest_entry_hash = "f" * 64
        session.commit()
    finally:
        session.close()
    verify = client.get("/api/disposal-tasks/T-1/verify").json()
    assert verify["chain_ok"] is False
    assert verify["breaks"]


def test_legal_hold_reclassification_happens_at_execution(scenario):
    """预览后新增法律冻结，执行时实时复核为 held；解除后新任务可删除。"""
    client = scenario
    # E-03 没有冻结，预览为 delete。
    client.post("/api/disposal-tasks/T-1/preview", json={"policy_version": "R-V1"})
    client.post("/api/legal-holds/H-2", json={
        "student_id": "S-DELETE", "reason": "newly filed court order",
    })
    client.post("/api/disposal-tasks/T-1/approve", json={"approved_by": "dpo"})
    result = client.post("/api/disposal-tasks/T-1/execute", json={}).json()
    items = _items_by_event(result)
    assert items["E-03"]["planned_action"] == "legal_hold"
    assert items["E-03"]["status"] == "held"

    session = TestSessionLocal()
    try:
        still_there = session.scalar(select(Event).where(Event.event_id == "E-03"))
        assert still_there is not None
    finally:
        session.close()

    # 解除冻结后，新任务预览重新按策略判定为 delete。
    assert client.post("/api/legal-holds/H-2/release", json={}).json()["open"] is False
    second = client.post("/api/disposal-tasks/T-2/preview",
                         json={"policy_version": "R-V1"}).json()
    items2 = _items_by_event(second)
    assert items2["E-03"]["planned_action"] == "delete"


def test_invalid_policy_is_rejected(scenario):
    client = scenario
    # 通过 schema 校验但违反领域约束：同一明细类型规则重复。
    resp = client.post("/api/retention/policies", json={
        "policy_version": "BAD",
        "rules": [
            {"detail_type": "checkin", "retain_days": 1, "action": "delete"},
            {"detail_type": "checkin", "retain_days": 2, "action": "delete"},
        ],
    })
    assert resp.status_code == 409
    # schema 层对未知类型直接 422。
    bad_enum = client.post("/api/retention/policies", json={
        "policy_version": "BAD2",
        "rules": [{"detail_type": "unknown_type", "retain_days": 1,
                   "action": "delete"}],
    })
    assert bad_enum.status_code == 422
