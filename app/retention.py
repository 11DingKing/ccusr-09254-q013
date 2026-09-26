"""保留策略与处置的领域逻辑，纯函数实现便于确定性测试。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
from typing import Any, Iterable, Mapping, Sequence


class PolicyStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    RETIRED = "retired"


class Disposition(StrEnum):
    ERASE = "erase"  # 可删除明细，物理删除
    FINGERPRINT = "fingerprint"  # 删除原文但保留指纹
    HELD = "held"  # 法律冻结，禁止触碰
    KEEP = "keep"  # 未到保留年限，暂保留


class ItemStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    HELD = "held"


class TaskStatus(StrEnum):
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_TASK_STATES = frozenset({TaskStatus.COMPLETED, TaskStatus.CANCELLED})


class ManifestAction(StrEnum):
    ERASED = "erased"
    FINGERPRINTED = "fingerprinted"
    HELD = "held"
    KEPT = "kept"
    FAILED = "failed"


class RetentionError(ValueError):
    """封装领域状态与业务约束。"""


class ItemFailure(Exception):
    """单条明细处置失败，任务可继续处理其余明细。"""


@dataclass(frozen=True)
class Rule:
    category: str
    retention_days: int
    disposition: Disposition


@dataclass(frozen=True)
class SourceRecord:
    """待处置的学时明细（事件行）。"""

    record_key: str
    record_type: str
    student_id: str
    payload: Mapping[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class PlannedItem:
    item_key: str
    disposition: Disposition
    reason: str


def ensure_utc(value: datetime) -> datetime:
    """数据库读出的时间可能不带时区，统一按 UTC 归一。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def canonical_json(value: Any) -> str:
    """确定性序列化，用于指纹与哈希链。"""

    def _default(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return ensure_utc(obj).isoformat()
        raise TypeError(f"cannot canonicalize {type(obj)!r}")

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def content_fingerprint(value: Any) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def record_fingerprint(record: SourceRecord) -> str:
    """对单条明细原文计算指纹，删除原文后仍可核验。"""
    return content_fingerprint(
        {
            "record_key": record.record_key,
            "record_type": record.record_type,
            "student_id": record.student_id,
            "payload": dict(record.payload),
            "created_at": ensure_utc(record.created_at).isoformat(),
        }
    )


def policy_hash(rules: Sequence[Mapping[str, Any]]) -> str:
    return content_fingerprint([dict(r) for r in rules])


def categorize(record_type: str, payload: Mapping[str, Any]) -> str:
    """明细类别：签到按活动类型细分，其余按事件类型。"""
    if record_type == "checkin":
        activity = str(payload.get("activity_type", "regular"))
        return f"checkin:{activity}"
    return record_type


def match_rule(rules: Sequence[Rule], category: str) -> Rule | None:
    """最具体的类别优先，通配符 * 兜底。"""
    candidates = [category]
    if ":" in category:
        candidates.append(category.split(":", 1)[0])
    candidates.append("*")
    for key in candidates:
        for rule in rules:
            if rule.category == key:
                return rule
    return None


def plan_disposition(
    record: SourceRecord,
    rules: Sequence[Rule],
    *,
    covered_by_freeze: bool,
    hold_active: bool,
    now: datetime,
) -> PlannedItem:
    """决定单条明细的处置方式：法律冻结 > 保留年限 > 证明指纹 > 删除。"""
    if hold_active:
        return PlannedItem(record.record_key, Disposition.HELD, "法律冻结期间禁止处置")
    rule = match_rule(rules, categorize(record.record_type, record.payload))
    if rule is None:
        return PlannedItem(record.record_key, Disposition.KEEP, "无匹配策略规则")
    eligible_at = ensure_utc(record.created_at) + timedelta(days=rule.retention_days)
    if ensure_utc(now) < eligible_at:
        return PlannedItem(record.record_key, Disposition.KEEP, "未到保留年限")
    if rule.disposition == Disposition.FINGERPRINT or covered_by_freeze:
        return PlannedItem(
            record.record_key,
            Disposition.FINGERPRINT,
            "已签发证明引用，删除原文并保留指纹",
        )
    return PlannedItem(record.record_key, Disposition.ERASE, "保留期满，可删除")


def genesis_hash(task_id: str, pinned_policy_hash: str) -> str:
    return content_fingerprint({"task_id": task_id, "policy_hash": pinned_policy_hash})


def manifest_entry_hash(
    task_id: str,
    seq: int,
    prev_hash: str,
    item_key: str,
    action: ManifestAction,
    fingerprint: str,
    processed_at: datetime,
) -> str:
    return content_fingerprint(
        {
            "task_id": task_id,
            "seq": seq,
            "prev_hash": prev_hash,
            "item_key": item_key,
            "action": action.value,
            "fingerprint": fingerprint,
            "processed_at": ensure_utc(processed_at).isoformat(),
        }
    )


@dataclass(frozen=True)
class ChainVerification:
    valid: bool
    entries_checked: int
    first_invalid_seq: int | None
    detail: str


def verify_chain(
    task_id: str,
    pinned_policy_hash: str,
    entries: Iterable[Mapping[str, Any]],
) -> ChainVerification:
    """重放哈希链，任何篡改、缺页或乱序都会使校验失败。"""
    expected_seq = 1
    prev = genesis_hash(task_id, pinned_policy_hash)
    checked = 0
    for entry in entries:
        seq = int(entry["seq"])
        if seq != expected_seq:
            return ChainVerification(False, checked, seq, "清单序号不连续")
        if entry["prev_hash"] != prev:
            return ChainVerification(False, checked, seq, "哈希链断裂")
        recomputed = manifest_entry_hash(
            task_id,
            seq,
            prev,
            str(entry["item_key"]),
            ManifestAction(str(entry["action"])),
            str(entry["fingerprint"]),
            ensure_utc(entry["created_at"]),
        )
        if recomputed != entry["entry_hash"]:
            return ChainVerification(False, checked, seq, "条目哈希不匹配")
        prev = entry["entry_hash"]
        expected_seq += 1
        checked += 1
    return ChainVerification(True, checked, None, "清单完整")
