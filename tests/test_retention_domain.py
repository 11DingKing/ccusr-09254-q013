"""保留策略领域逻辑的纯函数测试。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.retention import (
    Disposition,
    ManifestAction,
    Rule,
    SourceRecord,
    categorize,
    genesis_hash,
    manifest_entry_hash,
    match_rule,
    plan_disposition,
    record_fingerprint,
    verify_chain,
)

CREATED = datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC)


def _record(**overrides) -> SourceRecord:
    base = {
        "record_key": "E-01",
        "record_type": "checkin",
        "student_id": "S1",
        "payload": {"activity_type": "regular"},
        "created_at": CREATED,
    }
    base.update(overrides)
    return SourceRecord(**base)


def test_categorize_splits_checkins_by_activity_type():
    assert categorize("checkin", {"activity_type": "internship"}) == "checkin:internship"
    assert categorize("checkin", {}) == "checkin:regular"
    assert categorize("leave_correction", {}) == "leave_correction"


def test_match_rule_prefers_most_specific_category():
    rules = [
        Rule("*", 365, Disposition.ERASE),
        Rule("checkin", 30, Disposition.ERASE),
        Rule("checkin:internship", 0, Disposition.FINGERPRINT),
    ]
    assert match_rule(rules, "checkin:internship").retention_days == 0
    assert match_rule(rules, "checkin:regular").retention_days == 30
    assert match_rule(rules, "leave_correction").retention_days == 365
    assert match_rule([], "checkin") is None


def test_plan_disposition_precedence():
    rules = [Rule("checkin:regular", 10, Disposition.ERASE)]
    record = _record()

    # 未到保留年限。
    early = plan_disposition(
        record,
        rules,
        covered_by_freeze=False,
        hold_active=False,
        now=CREATED + timedelta(days=5),
    )
    assert early.disposition == Disposition.KEEP

    # 保留期满可删除。
    eligible = plan_disposition(
        record,
        rules,
        covered_by_freeze=False,
        hold_active=False,
        now=CREATED + timedelta(days=15),
    )
    assert eligible.disposition == Disposition.ERASE

    # 已签发证明引用时升级为保留指纹。
    covered = plan_disposition(
        record,
        rules,
        covered_by_freeze=True,
        hold_active=False,
        now=CREATED + timedelta(days=15),
    )
    assert covered.disposition == Disposition.FINGERPRINT

    # 法律冻结优先级最高。
    held = plan_disposition(
        record,
        rules,
        covered_by_freeze=True,
        hold_active=True,
        now=CREATED + timedelta(days=15),
    )
    assert held.disposition == Disposition.HELD

    # 规则本身要求保留指纹时无需证明引用。
    fingerprint_rules = [Rule("checkin:regular", 10, Disposition.FINGERPRINT)]
    by_rule = plan_disposition(
        record,
        fingerprint_rules,
        covered_by_freeze=False,
        hold_active=False,
        now=CREATED + timedelta(days=15),
    )
    assert by_rule.disposition == Disposition.FINGERPRINT


def test_record_fingerprint_stable_across_naive_and_aware():
    naive = _record(created_at=datetime(2024, 1, 1, 8, 0, 0))
    aware = _record(created_at=datetime(2024, 1, 1, 8, 0, 0, tzinfo=UTC))
    assert record_fingerprint(naive) == record_fingerprint(aware)
    other = _record(payload={"activity_type": "internship"})
    assert record_fingerprint(other) != record_fingerprint(aware)


def _build_entries(task_id: str, policy_hash: str, n: int) -> list[dict]:
    entries = []
    prev = genesis_hash(task_id, policy_hash)
    for seq in range(1, n + 1):
        at = CREATED + timedelta(minutes=seq)
        entry_hash = manifest_entry_hash(
            task_id, seq, prev, f"E-{seq:02d}", ManifestAction.ERASED, "", at
        )
        entries.append(
            {
                "seq": seq,
                "item_key": f"E-{seq:02d}",
                "action": "erased",
                "fingerprint": "",
                "prev_hash": prev,
                "entry_hash": entry_hash,
                "created_at": at,
            }
        )
        prev = entry_hash
    return entries


def test_verify_chain_detects_tamper_gap_and_reorder():
    entries = _build_entries("DT-1", "ph", 3)
    ok = verify_chain("DT-1", "ph", entries)
    assert ok.valid and ok.entries_checked == 3

    tampered = [dict(e) for e in entries]
    tampered[1]["fingerprint"] = "f" * 64
    bad = verify_chain("DT-1", "ph", tampered)
    assert not bad.valid and bad.first_invalid_seq == 2

    gapped = [entries[0], entries[2]]
    bad_gap = verify_chain("DT-1", "ph", gapped)
    assert not bad_gap.valid and bad_gap.first_invalid_seq == 3

    reordered = [entries[1], entries[0], entries[2]]
    bad_order = verify_chain("DT-1", "ph", reordered)
    assert not bad_order.valid

    wrong_task = verify_chain("DT-2", "ph", entries)
    assert not wrong_task.valid
