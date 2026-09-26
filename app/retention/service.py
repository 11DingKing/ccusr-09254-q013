"""留存处置应用服务：策略发布、任务预览、批准、执行、暂停、重试与核验。

执行模型：
- 策略版本在任务创建时固定（版本号 + 内容校验值快照到任务行）；
- 每条明细一个独立事务，单项失败只标记该条，不影响其他条目；
- 处置清单为哈希链，任务行的 manifest_hash 作为链尖并做比较交换，
  杜绝并发执行产生分叉，任何篡改都会在核验时断链；
- 执行时按实时状态重新分类：法律冻结 > 已签发证明引用 > 策略规则；
- 终态条目（deleted/fingerprinted/held/skipped）重启后自动跳过，
  pending/failed 条目继续处理，实现崩溃后续跑。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import repository as repo
from .fingerprint import GENESIS, event_fingerprint, manifest_entry_hash
from .policy import (
    DisposalClass,
    PolicyError,
    classify,
    parse_rules,
    rules_checksum,
)
from ..models import DisposalItem, DisposalTask, Event

REDACTED_PAYLOAD = {"redacted": True, "reason": "retention_disposal"}


class RetentionError(ValueError):
    """请求与当前任务状态冲突。"""


class PolicyNotFoundError(LookupError):
    pass


class TaskNotFoundError(LookupError):
    pass


# 测试接缝：可注入单项失败以覆盖部分失败路径。
_fault_hook: Callable[[DisposalItem], None] | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _load_rules(db: Session, policy_version: str):
    row = repo.get_policy(db, policy_version)
    if row is None:
        raise PolicyNotFoundError(f"留存策略版本 '{policy_version}' 不存在")
    rules = parse_rules(row.rules)
    if rules_checksum(row.rules) != row.checksum:
        raise RetentionError("策略内容校验值不匹配，策略可能被篡改")
    return row, rules


# ---- 策略 ----

def publish_policy(
    db: Session, *, policy_version: str, rules: list[dict[str, Any]]
) -> dict[str, Any]:
    parsed = parse_rules(rules)  # 校验
    checksum = rules_checksum(rules)
    if not repo.insert_policy(
        db, policy_version=policy_version, rules=rules, checksum=checksum
    ):
        raise RetentionError(f"策略版本 '{policy_version}' 已存在，策略不可原地修改")
    return {
        "policy_version": policy_version,
        "rules": [r.to_dict() for r in parsed.values()],
        "checksum": checksum,
    }


def read_policy(db: Session, policy_version: str) -> dict[str, Any]:
    row, rules = _load_rules(db, policy_version)
    return {
        "policy_version": row.policy_version,
        "rules": [r.to_dict() for r in rules.values()],
        "checksum": row.checksum,
        "created_at": _iso(row.created_at),
    }


# ---- 法律冻结 ----

def create_hold(db: Session, *, hold_id: str, student_id: str, reason: str) -> dict[str, Any]:
    if not reason.strip():
        raise RetentionError("法律冻结必须记录原因")
    if not repo.insert_hold(
        db, hold_id=hold_id, student_id=student_id, reason=reason
    ):
        raise RetentionError(f"冻结令 '{hold_id}' 已存在")
    row = repo.get_hold(db, hold_id)
    assert row is not None
    return _hold_to_dict(row)


def release_hold(db: Session, *, hold_id: str) -> dict[str, Any]:
    row = repo.get_hold(db, hold_id)
    if row is None:
        raise RetentionError(f"冻结令 '{hold_id}' 不存在")
    if row.released_at is not None:
        return _hold_to_dict(row)
    now = _utcnow()
    if not repo.release_hold(db, hold_id, now):
        db.refresh(row)
        return _hold_to_dict(row)
    db.refresh(row)
    return _hold_to_dict(row)


def list_holds(db: Session) -> list[dict[str, Any]]:
    return [_hold_to_dict(r) for r in repo.list_holds(db, open_only=False)]


def _hold_to_dict(row) -> dict[str, Any]:
    return {
        "hold_id": row.hold_id,
        "student_id": row.student_id,
        "reason": row.reason,
        "created_at": _iso(row.created_at),
        "released_at": _iso(row.released_at) if row.released_at else None,
        "open": row.released_at is None,
    }


# ---- 预览（创建任务） ----

def preview_task(
    db: Session,
    *,
    task_id: str,
    policy_version: str,
    plan_version: str | None = None,
    student_id: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    instant = (now or _utcnow()).astimezone(timezone.utc)
    policy_row, rules = _load_rules(db, policy_version)

    task = repo.insert_task(
        db,
        task_id=task_id,
        policy_version=policy_version,
        policy_checksum=policy_row.checksum,
        plan_version=plan_version,
        student_id=student_id,
    )
    if task is None:
        raise RetentionError(f"处置任务 '{task_id}' 已存在")

    events = repo.load_candidate_events(
        db, plan_version=plan_version, student_id=student_id
    )
    referenced = repo.referenced_event_keys(db)

    for event in events:
        disposition = classify(
            detail_type=event.event_type,
            reference_time=_as_utc(event.created_at),
            now=instant,
            rules=rules,
            open_hold=repo.open_hold_exists(db, event.student_id),
            referenced_by_freeze=(event.plan_version, event.event_id) in referenced,
        )
        if disposition is None:
            continue
        action = (
            "legal_hold"
            if disposition is DisposalClass.LEGAL_HOLD
            else (
                "retain_fingerprint"
                if disposition is DisposalClass.RETAIN_FINGERPRINT
                else "delete"
            )
        )
        repo.insert_item(
            db,
            task_id=task_id,
            event_id=event.event_id,
            plan_version=event.plan_version,
            student_id=event.student_id,
            detail_type=event.event_type,
            action=action,
            status="pending",
        )
    db.commit()
    return task_to_dict(db, task_id, include_items=True)


# ---- 批准 ----

def approve_task(
    db: Session, *, task_id: str, approved_by: str
) -> dict[str, Any]:
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    actor = approved_by.strip()
    if not actor:
        raise RetentionError("批准必须记录操作人")
    if task.state == "approved":
        return task_to_dict(db, task_id)
    if task.state != "preview":
        raise RetentionError(f"任务处于 {task.state} 状态，不能批准")
    task.state = "approved"
    task.dry_run = False
    task.approved_by = actor
    task.approved_at = _utcnow()
    db.commit()
    return task_to_dict(db, task_id)


# ---- 暂停 ----

def pause_task(db: Session, *, task_id: str) -> dict[str, Any]:
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    if task.state in ("running", "approved"):
        task.state = "paused"
        db.commit()
    elif task.state != "paused":
        raise RetentionError(f"任务处于 {task.state} 状态，不能暂停")
    return task_to_dict(db, task_id)


def cancel_task(db: Session, *, task_id: str) -> dict[str, Any]:
    """撤回任务：预览、已批准、已暂停或部分失败时可取消；终态不可取消。"""
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    if task.state == "cancelled":
        return task_to_dict(db, task_id)
    if task.state not in ("preview", "approved", "paused", "completed_with_errors"):
        raise RetentionError(f"任务处于 {task.state} 状态，不能取消")
    task.state = "cancelled"
    db.commit()
    return task_to_dict(db, task_id)


def _is_paused(db: Session, task_id: str) -> bool:
    db.expire_all()
    row = repo.get_task(db, task_id)
    return row is not None and row.state == "paused"


# ---- 执行 / 重试 / 重启续跑 ----

_RUNNABLE_STATES = (
    "approved",
    "running",  # 进程崩溃后重启续跑
    "paused",  # 由 execute 显式恢复
    "completed",  # 幂等空跑
    "completed_with_errors",  # 重试 failed 条目
)


def execute_task(
    db: Session, *, task_id: str, limit: int | None = None
) -> dict[str, Any]:
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    if task.state not in _RUNNABLE_STATES:
        raise RetentionError(f"任务处于 {task.state} 状态，不能执行")
    if task.dry_run:
        raise RetentionError("任务尚未批准，只能预览，不能执行")
    if task.state != "running":
        repo.mark_running(db, task_id)
        db.refresh(task)

    # 固定本次扫描到的待处理条目：本次调用内不重复重试，失败留给下一次调用。
    pending = repo.list_items(db, task_id, status="pending")
    failed = repo.list_items(db, task_id, status="failed")
    work = sorted(pending + failed, key=lambda i: i.id)
    if limit is not None and limit >= 0:
        work = work[:limit]

    processed = 0
    failed_this_run = 0
    for candidate in work:
        if _is_paused(db, task_id):
            break
        outcome = _process_one(db, task, candidate.id)
        if outcome in ("processed",):
            processed += 1
        elif outcome == "failed":
            failed_this_run += 1
        # busy（链尖竞争）/stale（已被其他执行器处理）跳过，下轮续跑。

    _finalize_state(db, task_id)
    db.refresh(task)
    result = task_to_dict(db, task_id, include_items=True)
    result["processed_this_run"] = processed
    result["failed_this_run"] = failed_this_run
    return result


def _finalize_state(db: Session, task_id: str) -> None:
    task = repo.get_task(db, task_id)
    assert task is not None
    if task.state == "paused":
        return
    counts = repo.item_counts(db, task_id)
    failed_left = counts.get("failed", 0)
    pending_left = counts.get("pending", 0)
    if failed_left:
        task.state = "completed_with_errors"
    elif pending_left:
        # 还有未处理批次（limit 截断），保持 running 等待续跑。
        task.state = "running"
    else:
        task.state = "completed"
    db.commit()


def _process_one(db: Session, task: DisposalTask, item_id: int) -> str:
    """处理单条明细，整段在一个事务内。返回 processed/failed/busy/stale。"""
    try:
        item = db.get(DisposalItem, item_id)
        if item is None or item.status not in repo.RETRYABLE_STATUSES:
            return "stale"

        _, rules = _load_rules(db, task.policy_version)

        # 执行时实时复核分类（预览后可能新增冻结或冻结令解除）。
        event = db.execute(
            select(Event).where(
                Event.plan_version == item.plan_version,
                Event.event_id == item.event_id,
            )
        ).scalar_one_or_none()

        now = _utcnow()
        if event is None:
            disposition = None  # 明细已不存在（其他处置通道），跳过。
        else:
            disposition = classify(
                detail_type=item.detail_type,
                reference_time=_as_utc(event.created_at),
                now=now,
                rules=rules,
                open_hold=repo.open_hold_exists(db, item.student_id),
                referenced_by_freeze=(event.plan_version, event.event_id)
                in repo.referenced_event_keys(db),
            )

        if _fault_hook is not None:
            _fault_hook(item)

        if disposition is None:
            effective_action = "skip"
            status = "skipped"
            fingerprint = None
        elif disposition is DisposalClass.LEGAL_HOLD:
            effective_action = "legal_hold"
            status = "held"
            fingerprint = _fingerprint_of(event)
        else:
            fingerprint = _fingerprint_of(event)
            if disposition is DisposalClass.RETAIN_FINGERPRINT:
                effective_action = "retain_fingerprint"
                status = "fingerprinted"
                event.payload = dict(REDACTED_PAYLOAD)
                event.student_id = f"redacted:{fingerprint[:16]}"
            else:
                effective_action = "delete"
                status = "deleted"
                db.delete(event)

        # 追加哈希链清单：链上位置在提交时按已入链条目数确定，
        # 与链尖 CAS 同事务，失败重试不会插队或错位。
        prev_hash = task.manifest_hash or GENESIS
        sequence = repo.count_chained_items(db, task.task_id) + 1
        entry = {
            "sequence": sequence,
            "task_id": task.task_id,
            "policy_version": task.policy_version,
            "policy_checksum": task.policy_checksum,
            "event_id": item.event_id,
            "plan_version": item.plan_version,
            "student_id": item.student_id,
            "detail_type": item.detail_type,
            "action": effective_action,
            "status": status,
            "fingerprint": fingerprint,
            "processed_at": _iso(now),
        }
        entry_hash = manifest_entry_hash(prev_hash, entry)

        item.action = (
            "legal_hold"
            if effective_action == "legal_hold"
            else (
                "retain_fingerprint"
                if effective_action == "retain_fingerprint"
                else ("delete" if effective_action == "delete" else item.action)
            )
        )
        item.status = status
        item.attempts = item.attempts + 1
        item.error = None
        item.retained_fingerprint = fingerprint
        item.manifest_entry_hash = entry_hash
        item.manifest_sequence = sequence
        item.processed_at = now

        # 链尖比较交换：只允许接在当前链尖之后，杜绝分叉。
        advanced = repo.update_task_manifest_hash(
            db,
            task_id=task.task_id,
            expected_hash=prev_hash,
            new_hash=entry_hash,
        )
        if not advanced:
            db.rollback()
            return "busy"
        db.commit()
        return "processed"
    except PolicyError:
        raise
    except OperationalError as exc:
        # 数据库写锁竞争（如 SQLite）：条目未处置，下轮重试即可。
        db.rollback()
        if "locked" in str(exc).lower():
            return "busy"
        _record_failure(db, item_id, exc)
        return "failed"
    except Exception as exc:  # 单项失败隔离
        db.rollback()
        _record_failure(db, item_id, exc)
        return "failed"


def _record_failure(db: Session, item_id: int, exc: Exception) -> None:
    item = db.get(DisposalItem, item_id)
    if item is None or item.status not in repo.RETRYABLE_STATUSES:
        return
    item.status = "failed"
    item.attempts = item.attempts + 1
    item.error = f"{type(exc).__name__}: {exc}"[:1000]
    db.commit()


def _fingerprint_of(event: Event) -> str:
    return event_fingerprint(
        event_id=event.event_id,
        plan_version=event.plan_version,
        student_id=event.student_id,
        event_type=event.event_type,
        payload=event.payload,
        created_at_iso=_iso(event.created_at),
    )


# ---- 查询与核验 ----

def task_to_dict(
    db: Session, task_id: str, *, include_items: bool = False
) -> dict[str, Any]:
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    counts = repo.item_counts(db, task_id)
    data: dict[str, Any] = {
        "task_id": task.task_id,
        "policy_version": task.policy_version,
        "policy_checksum": task.policy_checksum,
        "plan_version": task.plan_version,
        "student_id": task.student_id,
        "state": task.state,
        "dry_run": task.dry_run,
        "approved_by": task.approved_by,
        "approved_at": _iso(task.approved_at) if task.approved_at else None,
        "last_error": task.last_error,
        "manifest_hash": task.manifest_hash,
        "created_at": _iso(task.created_at),
        "updated_at": _iso(task.updated_at),
        "counts": {
            "pending": counts.get("pending", 0),
            "failed": counts.get("failed", 0),
            "deleted": counts.get("deleted", 0),
            "fingerprinted": counts.get("fingerprinted", 0),
            "held": counts.get("held", 0),
            "skipped": counts.get("skipped", 0),
        },
    }
    if include_items:
        data["items"] = [
            {
                "event_id": i.event_id,
                "plan_version": i.plan_version,
                "student_id": i.student_id,
                "detail_type": i.detail_type,
                "planned_action": i.action,
                "status": i.status,
                "attempts": i.attempts,
                "error": i.error,
                "retained_fingerprint": i.retained_fingerprint,
                "manifest_entry_hash": i.manifest_entry_hash,
                "manifest_sequence": i.manifest_sequence,
            }
            for i in repo.list_items(db, task_id)
        ]
    return data


def get_manifest(db: Session, task_id: str) -> dict[str, Any]:
    task = repo.get_task(db, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    entries: list[dict[str, Any]] = []
    for item in repo.list_chain(db, task_id):
        entries.append(
            {
                "sequence": item.manifest_sequence,
                "event_id": item.event_id,
                "plan_version": item.plan_version,
                "student_id": item.student_id,
                "detail_type": item.detail_type,
                "action": _entry_action(item),
                "status": item.status,
                "fingerprint": item.retained_fingerprint,
                "processed_at": _iso(item.processed_at) if item.processed_at else None,
                "entry_hash": item.manifest_entry_hash,
            }
        )
    return {
        "task_id": task_id,
        "policy_version": task.policy_version,
        "policy_checksum": task.policy_checksum,
        "chain_tip": task.manifest_hash,
        "entries": entries,
    }


def verify_manifest(db: Session, task_id: str) -> dict[str, Any]:
    """重算哈希链并核对处置结果的当前数据库状态。"""
    if repo.get_task(db, task_id) is None:
        raise TaskNotFoundError(f"处置任务 '{task_id}' 不存在")
    previous = GENESIS
    breaks: list[dict[str, str]] = []
    state_checks: list[dict[str, Any]] = []

    items_with_hash = repo.list_chain(db, task_id)
    task_row = repo.get_task(db, task_id)
    assert task_row is not None
    for item in items_with_hash:
        position = item.manifest_sequence
        assert position is not None
        entry = {
            "sequence": position,
            "task_id": task_id,
            "policy_version": task_row.policy_version,
            "policy_checksum": task_row.policy_checksum,
            "event_id": item.event_id,
            "plan_version": item.plan_version,
            "student_id": item.student_id,
            "detail_type": item.detail_type,
            "action": _entry_action(item),
            "status": item.status,
            "fingerprint": item.retained_fingerprint,
            "processed_at": _iso(item.processed_at) if item.processed_at else None,
        }
        expected = manifest_entry_hash(previous, entry)
        ok = expected == item.manifest_entry_hash
        if not ok:
            breaks.append(
                {"sequence": str(position), "reason": "hash_mismatch",
                 "expected": expected, "actual": item.manifest_entry_hash or ""}
            )
        previous = item.manifest_entry_hash or previous

        event = db.execute(
            select(Event).where(
                Event.plan_version == item.plan_version,
                Event.event_id == item.event_id,
            )
        ).scalar_one_or_none()
        if item.status == "deleted":
            present_ok = event is None
            present_note = "event row absent"
        elif item.status == "fingerprinted":
            present_ok = (
                event is not None
                and event.payload.get("redacted") is True
                and event.student_id.startswith("redacted:")
            )
            present_note = "event row redacted"
        elif item.status == "held":
            present_ok = event is not None
            present_note = "event row preserved"
            if event is not None:
                current_fp = _fingerprint_of(event)
                present_ok = current_fp == item.retained_fingerprint
                present_note = (
                    "event row preserved, fingerprint matches"
                    if present_ok
                    else "event content changed, fingerprint mismatch"
                )
        else:  # skipped
            present_ok = True
            present_note = "not processed"
        state_checks.append(
            {
                "sequence": position,
                "event_id": item.event_id,
                "plan_version": item.plan_version,
                "status": item.status,
                "chain_ok": ok,
                "state_ok": present_ok,
                "note": present_note,
            }
        )

    task = repo.get_task(db, task_id)
    if previous == GENESIS:
        tip_ok = task.manifest_hash is None
    else:
        tip_ok = task.manifest_hash == previous
    if not tip_ok:
        breaks.append(
            {"reason": "chain_tip_mismatch",
             "expected": previous, "actual": task.manifest_hash or ""}
        )
    return {
        "task_id": task_id,
        "chain_ok": not breaks and tip_ok,
        "state_ok": all(c["state_ok"] for c in state_checks),
        "chain_tip": task.manifest_hash,
        "entries_verified": len(items_with_hash),
        "breaks": breaks,
        "state_checks": state_checks,
    }


def _entry_action(item: DisposalItem) -> str:
    mapping = {
        "deleted": "delete",
        "fingerprinted": "retain_fingerprint",
        "held": "legal_hold",
        "skipped": "skip",
    }
    return mapping.get(item.status, item.action)
