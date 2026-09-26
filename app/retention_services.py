"""保留策略与处置任务的数据库服务。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, Callable, Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (
    DisposalItem,
    DisposalManifestEntry,
    DisposalTask,
    ErasedFingerprint,
    Event as EventModel,
    Freeze,
    LegalHold,
    Plan,
    RetentionPolicy,
)
from .retention import (
    TERMINAL_TASK_STATES,
    Disposition,
    ItemFailure,
    ItemStatus,
    ManifestAction,
    PlannedItem,
    PolicyStatus,
    Rule,
    SourceRecord,
    TaskStatus,
    categorize,
    content_fingerprint,
    ensure_utc,
    genesis_hash,
    manifest_entry_hash,
    plan_disposition,
    policy_hash,
    record_fingerprint,
    verify_chain,
)


class RetentionServiceError(Exception):
    """服务层基础异常。"""


class PlanNotFoundError(RetentionServiceError):
    pass


class PolicyNotFoundError(RetentionServiceError):
    pass


class NoActivePolicyError(RetentionServiceError):
    pass


class PolicyValidationError(RetentionServiceError):
    pass


class TaskNotFoundError(RetentionServiceError):
    pass


class InvalidTaskStateError(RetentionServiceError):
    pass


class DuplicateActiveTaskError(RetentionServiceError):
    pass


class HoldNotFoundError(RetentionServiceError):
    pass


class FreezeNotFoundError(RetentionServiceError):
    pass


ItemProcessor = Callable[..., str]


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


def _normalize_rules(rules: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in rules:
        category = str(raw.get("category", "")).strip()
        if not category:
            raise PolicyValidationError("规则类别不能为空")
        if category in seen:
            raise PolicyValidationError(f"规则类别重复: {category}")
        seen.add(category)
        try:
            retention_days = int(raw.get("retention_days"))
        except (TypeError, ValueError) as exc:
            raise PolicyValidationError("保留年限必须是非负整数") from exc
        if retention_days < 0:
            raise PolicyValidationError("保留年限必须是非负整数")
        disposition = str(raw.get("disposition", "")).strip()
        if disposition not in (Disposition.ERASE.value, Disposition.FINGERPRINT.value):
            raise PolicyValidationError("处置方式只能是 erase 或 fingerprint")
        normalized.append(
            {
                "category": category,
                "retention_days": retention_days,
                "disposition": disposition,
            }
        )
    if not normalized:
        raise PolicyValidationError("至少配置一条保留规则")
    return normalized


def _rules_from_snapshot(snapshot: Sequence[dict[str, Any]]) -> list[Rule]:
    return [
        Rule(
            category=r["category"],
            retention_days=int(r["retention_days"]),
            disposition=Disposition(r["disposition"]),
        )
        for r in snapshot
    ]


def create_policy(
    db: Session,
    *,
    policy_id: str,
    rules: Sequence[dict[str, Any]],
    activate: bool = False,
    now: datetime | None = None,
) -> RetentionPolicy:
    """创建新的策略版本，版本号在同一 policy_id 下自增。"""
    policy_id = policy_id.strip()
    if not policy_id:
        raise PolicyValidationError("策略标识不能为空")
    instant = now or _now()
    normalized = _normalize_rules(rules)
    current_max = db.execute(
        select(func.max(RetentionPolicy.version)).where(
            RetentionPolicy.policy_id == policy_id
        )
    ).scalar()
    version = (current_max or 0) + 1
    row = RetentionPolicy(
        policy_id=policy_id,
        version=version,
        status=PolicyStatus.DRAFT.value,
        rules=normalized,
        policy_hash=policy_hash(normalized),
        created_at=instant,
    )
    db.add(row)
    if activate:
        _activate(db, row)
    db.commit()
    return row


def _activate(db: Session, row: RetentionPolicy) -> None:
    others = db.execute(
        select(RetentionPolicy).where(RetentionPolicy.status == PolicyStatus.ACTIVE.value)
    ).scalars()
    for other in others:
        other.status = PolicyStatus.RETIRED.value
    row.status = PolicyStatus.ACTIVE.value


def activate_policy(db: Session, policy_id: str, version: int) -> RetentionPolicy:
    row = db.get(RetentionPolicy, (policy_id, version))
    if row is None:
        raise PolicyNotFoundError(f"策略 {policy_id} 版本 {version} 不存在")
    _activate(db, row)
    db.commit()
    return row


def get_policy(db: Session, policy_id: str, version: int) -> RetentionPolicy:
    row = db.get(RetentionPolicy, (policy_id, version))
    if row is None:
        raise PolicyNotFoundError(f"策略 {policy_id} 版本 {version} 不存在")
    return row


def list_policies(db: Session) -> list[RetentionPolicy]:
    stmt = select(RetentionPolicy).order_by(
        RetentionPolicy.policy_id, RetentionPolicy.version
    )
    return list(db.execute(stmt).scalars())


def active_policy(db: Session) -> RetentionPolicy:
    row = db.execute(
        select(RetentionPolicy).where(
            RetentionPolicy.status == PolicyStatus.ACTIVE.value
        )
    ).scalar_one_or_none()
    if row is None:
        raise NoActivePolicyError("没有已激活的保留策略")
    return row


def policy_view(row: RetentionPolicy) -> dict[str, Any]:
    return {
        "policy_id": row.policy_id,
        "version": row.version,
        "status": row.status,
        "rules": [dict(r) for r in row.rules],
        "policy_hash": row.policy_hash,
        "created_at": ensure_utc(row.created_at),
    }


# ---------------------------------------------------------------------------
# 法律冻结
# ---------------------------------------------------------------------------


def place_hold(
    db: Session,
    *,
    hold_id: str,
    plan_version: str,
    student_id: str,
    reason: str,
    now: datetime | None = None,
) -> LegalHold:
    hold_id = hold_id.strip()
    reason = reason.strip()
    if not hold_id or not reason:
        raise PolicyValidationError("冻结标识与原因不能为空")
    if db.get(LegalHold, hold_id) is not None:
        raise PolicyValidationError(f"冻结 {hold_id} 已存在")
    row = LegalHold(
        hold_id=hold_id,
        plan_version=plan_version,
        student_id=student_id,
        reason=reason,
        active=True,
        created_at=now or _now(),
    )
    db.add(row)
    db.commit()
    return row


def release_hold(db: Session, hold_id: str, *, now: datetime | None = None) -> LegalHold:
    row = db.get(LegalHold, hold_id)
    if row is None:
        raise HoldNotFoundError(f"冻结 {hold_id} 不存在")
    if row.active:
        row.active = False
        row.released_at = now or _now()
        db.commit()
    return row


def list_holds(
    db: Session, *, plan_version: str | None = None, student_id: str | None = None
) -> list[LegalHold]:
    stmt = select(LegalHold).order_by(LegalHold.hold_id)
    if plan_version is not None:
        stmt = stmt.where(LegalHold.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(LegalHold.student_id == student_id)
    return list(db.execute(stmt).scalars())


def hold_view(row: LegalHold) -> dict[str, Any]:
    return {
        "hold_id": row.hold_id,
        "plan_version": row.plan_version,
        "student_id": row.student_id,
        "reason": row.reason,
        "active": bool(row.active),
        "created_at": ensure_utc(row.created_at),
        "released_at": ensure_utc(row.released_at) if row.released_at else None,
    }


def _hold_active(db: Session, plan_version: str, student_id: str) -> bool:
    stmt = (
        select(LegalHold.hold_id)
        .where(LegalHold.plan_version == plan_version)
        .where(LegalHold.student_id == student_id)
        .where(LegalHold.active.is_(True))
        .limit(1)
    )
    return db.execute(stmt).first() is not None


# ---------------------------------------------------------------------------
# 处置规划
# ---------------------------------------------------------------------------


def _to_source_record(row: EventModel) -> SourceRecord:
    return SourceRecord(
        record_key=row.event_id,
        record_type=row.event_type,
        student_id=row.student_id,
        payload=dict(row.payload),
        created_at=ensure_utc(row.created_at),
    )


def _covered_by_freeze(db: Session, plan_version: str, record_key: str) -> bool:
    """事件是否已被任一冻结快照（已签发证明）引用。"""
    cutoffs = db.execute(
        select(Freeze.event_cutoff_id).where(Freeze.plan_version == plan_version)
    ).scalars()
    return any(c is not None and record_key <= c for c in cutoffs)


def _load_student_events(
    db: Session, plan_version: str, student_id: str
) -> list[EventModel]:
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.student_id == student_id)
        .order_by(EventModel.event_id)
    )
    return list(db.execute(stmt).scalars())


def _plan_for_record(
    db: Session,
    plan_version: str,
    record: SourceRecord,
    rules: Sequence[Rule],
    now: datetime,
) -> PlannedItem:
    return plan_disposition(
        record,
        rules,
        covered_by_freeze=_covered_by_freeze(db, plan_version, record.record_key),
        hold_active=_hold_active(db, plan_version, record.student_id),
        now=now,
    )


def preview_disposal(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按当前激活策略预演处置结果，不落库。"""
    if db.get(Plan, plan_version) is None:
        raise PlanNotFoundError(f"培养方案 {plan_version} 未注册")
    instant = now or _now()
    policy = active_policy(db)
    rules = _rules_from_snapshot(policy.rules)
    items: list[dict[str, Any]] = []
    summary = {d.value: 0 for d in Disposition}
    for row in _load_student_events(db, plan_version, student_id):
        record = _to_source_record(row)
        planned = _plan_for_record(db, plan_version, record, rules, instant)
        summary[planned.disposition.value] += 1
        items.append(
            {
                "item_key": record.record_key,
                "event_type": record.record_type,
                "category": categorize(record.record_type, record.payload),
                "disposition": planned.disposition.value,
                "reason": planned.reason,
            }
        )
    return {
        "plan_version": plan_version,
        "student_id": student_id,
        "policy_id": policy.policy_id,
        "policy_version": policy.version,
        "policy_hash": policy.policy_hash,
        "items": items,
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# 处置任务
# ---------------------------------------------------------------------------


def _require_task(db: Session, task_id: str) -> DisposalTask:
    task = db.get(DisposalTask, task_id)
    if task is None:
        raise TaskNotFoundError(f"处置任务 {task_id} 不存在")
    return task


def _item_counts(db: Session, task_id: str) -> dict[str, int]:
    rows = db.execute(
        select(DisposalItem.status, func.count())
        .where(DisposalItem.task_id == task_id)
        .group_by(DisposalItem.status)
    ).all()
    counts = {status.value: 0 for status in ItemStatus}
    for status_value, count in rows:
        counts[status_value] = count
    counts["total"] = sum(counts.values())
    return counts


def _item_view(row: DisposalItem) -> dict[str, Any]:
    return {
        "item_key": row.item_key,
        "disposition": row.disposition,
        "status": row.status,
        "fingerprint": row.fingerprint,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "processed_at": ensure_utc(row.processed_at) if row.processed_at else None,
    }


def task_view(db: Session, task: DisposalTask, *, with_items: bool = False) -> dict[str, Any]:
    view = {
        "task_id": task.task_id,
        "plan_version": task.plan_version,
        "student_id": task.student_id,
        "policy_id": task.policy_id,
        "policy_version": task.policy_version,
        "policy_hash": task.policy_hash,
        "status": task.status,
        "requested_by": task.requested_by,
        "approved_by": task.approved_by,
        "approved_at": ensure_utc(task.approved_at) if task.approved_at else None,
        "last_error": task.last_error,
        "counts": _item_counts(db, task.task_id),
        "created_at": ensure_utc(task.created_at),
        "updated_at": ensure_utc(task.updated_at),
        "finished_at": ensure_utc(task.finished_at) if task.finished_at else None,
    }
    if with_items:
        items = db.execute(
            select(DisposalItem)
            .where(DisposalItem.task_id == task.task_id)
            .order_by(DisposalItem.item_key)
        ).scalars()
        view["items"] = [_item_view(i) for i in items]
    return view


def create_task(
    db: Session,
    *,
    plan_version: str,
    student_id: str,
    requested_by: str,
    now: datetime | None = None,
) -> DisposalTask:
    """创建处置任务并把当前激活策略的版本与规则快照固定到任务。"""
    if db.get(Plan, plan_version) is None:
        raise PlanNotFoundError(f"培养方案 {plan_version} 未注册")
    requested_by = requested_by.strip()
    if not requested_by:
        raise PolicyValidationError("请求人不能为空")
    instant = now or _now()
    policy = active_policy(db)
    non_terminal = [s.value for s in TaskStatus if s not in TERMINAL_TASK_STATES]
    existing = db.execute(
        select(DisposalTask.task_id)
        .where(DisposalTask.plan_version == plan_version)
        .where(DisposalTask.student_id == student_id)
        .where(DisposalTask.status.in_(non_terminal))
        .limit(1)
    ).first()
    if existing is not None:
        raise DuplicateActiveTaskError("该学员已有进行中的处置任务")

    rules = _rules_from_snapshot(policy.rules)
    task = DisposalTask(
        task_id=f"DT-{uuid.uuid4().hex[:16]}",
        plan_version=plan_version,
        student_id=student_id,
        policy_id=policy.policy_id,
        policy_version=policy.version,
        policy_snapshot=[dict(r) for r in policy.rules],
        policy_hash=policy.policy_hash,
        status=TaskStatus.PENDING_APPROVAL.value,
        requested_by=requested_by,
        created_at=instant,
        updated_at=instant,
    )
    db.add(task)
    for row in _load_student_events(db, plan_version, student_id):
        record = _to_source_record(row)
        planned = _plan_for_record(db, plan_version, record, rules, instant)
        if planned.disposition == Disposition.KEEP:
            continue
        db.add(
            DisposalItem(
                task_id=task.task_id,
                item_key=record.record_key,
                disposition=planned.disposition.value,
                status=ItemStatus.PENDING.value,
            )
        )
    db.commit()
    return task


def list_tasks(
    db: Session, *, plan_version: str | None = None, student_id: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(DisposalTask).order_by(DisposalTask.created_at, DisposalTask.task_id)
    if plan_version is not None:
        stmt = stmt.where(DisposalTask.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(DisposalTask.student_id == student_id)
    return [task_view(db, t) for t in db.execute(stmt).scalars()]


def get_task_view(db: Session, task_id: str, *, with_items: bool = True) -> dict[str, Any]:
    return task_view(db, _require_task(db, task_id), with_items=with_items)


def approve_task(
    db: Session, task_id: str, *, approved_by: str, now: datetime | None = None
) -> dict[str, Any]:
    task = _require_task(db, task_id)
    approved_by = approved_by.strip()
    if not approved_by:
        raise PolicyValidationError("批准人不能为空")
    if task.status != TaskStatus.PENDING_APPROVAL.value:
        raise InvalidTaskStateError(f"任务状态 {task.status} 不允许批准")
    instant = now or _now()
    task.status = TaskStatus.APPROVED.value
    task.approved_by = approved_by
    task.approved_at = instant
    task.updated_at = instant
    db.commit()
    return task_view(db, task)


def pause_task(db: Session, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    task = _require_task(db, task_id)
    if task.status not in (TaskStatus.APPROVED.value, TaskStatus.RUNNING.value):
        raise InvalidTaskStateError(f"任务状态 {task.status} 不允许暂停")
    task.status = TaskStatus.PAUSED.value
    task.updated_at = now or _now()
    db.commit()
    return task_view(db, task)


def resume_task(db: Session, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    task = _require_task(db, task_id)
    if task.status != TaskStatus.PAUSED.value:
        raise InvalidTaskStateError(f"任务状态 {task.status} 不允许恢复")
    task.status = TaskStatus.APPROVED.value
    task.updated_at = now or _now()
    db.commit()
    return task_view(db, task)


def retry_task(db: Session, task_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """把失败与被法律冻结跳过的明细重新置为待处理。"""
    task = _require_task(db, task_id)
    if task.status not in (TaskStatus.FAILED.value, TaskStatus.COMPLETED.value):
        raise InvalidTaskStateError(f"任务状态 {task.status} 不允许重试")
    resettable = db.execute(
        select(DisposalItem).where(
            DisposalItem.task_id == task_id,
            DisposalItem.status.in_([ItemStatus.FAILED.value, ItemStatus.HELD.value]),
        )
    ).scalars().all()
    if not resettable:
        raise InvalidTaskStateError("没有可重试的明细")
    instant = now or _now()
    for item in resettable:
        item.status = ItemStatus.PENDING.value
        item.last_error = None
        item.processed_at = None
    task.status = TaskStatus.APPROVED.value
    task.last_error = None
    task.finished_at = None
    task.updated_at = instant
    db.commit()
    return task_view(db, task)


# ---------------------------------------------------------------------------
# 执行与清单
# ---------------------------------------------------------------------------


def default_item_processor(
    db: Session,
    *,
    task: DisposalTask,
    item: DisposalItem,
    event: EventModel | None,
    disposition: Disposition,
    now: datetime,
) -> str:
    """默认处置器：物理删除明细，必要时先保留指纹。返回指纹或空串。"""
    if event is None:
        return ""
    fingerprint = ""
    if disposition == Disposition.FINGERPRINT:
        fingerprint = record_fingerprint(_to_source_record(event))
        db.add(
            ErasedFingerprint(
                plan_version=task.plan_version,
                record_key=item.item_key,
                student_id=task.student_id,
                record_type=event.event_type,
                fingerprint=fingerprint,
                task_id=task.task_id,
                erased_at=now,
            )
        )
    db.delete(event)
    return fingerprint


def _append_manifest(
    db: Session,
    task: DisposalTask,
    item_key: str,
    action: ManifestAction,
    fingerprint: str,
    now: datetime,
) -> None:
    last = db.execute(
        select(DisposalManifestEntry)
        .where(DisposalManifestEntry.task_id == task.task_id)
        .order_by(DisposalManifestEntry.seq.desc())
        .limit(1)
    ).scalar_one_or_none()
    seq = last.seq + 1 if last is not None else 1
    prev_hash = (
        last.entry_hash
        if last is not None
        else genesis_hash(task.task_id, task.policy_hash)
    )
    entry_hash = manifest_entry_hash(
        task.task_id, seq, prev_hash, item_key, action, fingerprint, now
    )
    db.add(
        DisposalManifestEntry(
            task_id=task.task_id,
            seq=seq,
            item_key=item_key,
            action=action.value,
            fingerprint=fingerprint,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
            created_at=now,
        )
    )


def _get_event(db: Session, plan_version: str, event_id: str) -> EventModel | None:
    stmt = (
        select(EventModel)
        .where(EventModel.plan_version == plan_version)
        .where(EventModel.event_id == event_id)
    )
    return db.execute(stmt).scalar_one_or_none()


def execute_task(
    db: Session,
    task_id: str,
    *,
    max_items: int | None = None,
    processor: ItemProcessor | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """逐条执行处置，每条独立提交，支持分片、暂停与重启续跑。"""
    instant = now or _now()
    task = _require_task(db, task_id)
    if task.status not in (TaskStatus.APPROVED.value, TaskStatus.RUNNING.value):
        raise InvalidTaskStateError(f"任务状态 {task.status} 不允许执行")
    if processor is None:
        processor = default_item_processor
    task.status = TaskStatus.RUNNING.value
    task.updated_at = instant
    db.commit()

    plan_version = task.plan_version
    rules = _rules_from_snapshot(task.policy_snapshot)
    stmt = (
        select(DisposalItem)
        .where(DisposalItem.task_id == task_id)
        .where(DisposalItem.status == ItemStatus.PENDING.value)
        .order_by(DisposalItem.item_key)
    )
    if max_items is not None:
        stmt = stmt.limit(max_items)
    items = list(db.execute(stmt).scalars())

    for item in items:
        item_key = item.item_key
        try:
            event = _get_event(db, plan_version, item_key)
            if event is None:
                planned = Disposition.ERASE
            else:
                planned = _plan_for_record(
                    db,
                    plan_version,
                    _to_source_record(event),
                    rules,
                    instant,
                ).disposition
            if planned == Disposition.HELD:
                action = ManifestAction.HELD
                fingerprint = ""
                item.status = ItemStatus.HELD.value
                item.disposition = Disposition.HELD.value
            elif planned == Disposition.KEEP:
                action = ManifestAction.KEPT
                fingerprint = ""
                item.status = ItemStatus.DONE.value
                item.disposition = Disposition.KEEP.value
            else:
                fingerprint = processor(
                    db,
                    task=task,
                    item=item,
                    event=event,
                    disposition=planned,
                    now=instant,
                )
                action = (
                    ManifestAction.FINGERPRINTED
                    if planned == Disposition.FINGERPRINT
                    else ManifestAction.ERASED
                )
                item.status = ItemStatus.DONE.value
                item.disposition = planned.value
            item.fingerprint = fingerprint or None
            item.last_error = None
            item.processed_at = instant
            _append_manifest(db, task, item_key, action, fingerprint, instant)
            db.commit()
        except ItemFailure as exc:
            # 先回滚处理器可能留下的半成品，再记录失败，保证可安全重试。
            db.rollback()
            item = db.get(DisposalItem, (task_id, item_key))
            assert item is not None
            item.status = ItemStatus.FAILED.value
            item.attempts += 1
            item.last_error = str(exc)
            item.processed_at = instant
            _append_manifest(db, task, item_key, ManifestAction.FAILED, "", instant)
            db.commit()
            continue
        except Exception as exc:
            db.rollback()
            _mark_interrupted(db, task_id, exc)
            raise

    return _finalize(db, task_id, instant)


def _mark_interrupted(db: Session, task_id: str, exc: Exception) -> None:
    """异常中断后把任务置为暂停，等待恢复续跑。"""
    task = db.get(DisposalTask, task_id)
    assert task is not None
    task.status = TaskStatus.PAUSED.value
    task.last_error = f"执行中断: {type(exc).__name__}"
    task.updated_at = _now()
    db.commit()


def _finalize(db: Session, task_id: str, now: datetime) -> dict[str, Any]:
    task = db.get(DisposalTask, task_id)
    assert task is not None
    counts = _item_counts(db, task_id)
    if counts[ItemStatus.PENDING.value] == 0:
        task.status = (
            TaskStatus.FAILED.value
            if counts[ItemStatus.FAILED.value] > 0
            else TaskStatus.COMPLETED.value
        )
        task.finished_at = now
    task.updated_at = now
    db.commit()
    return task_view(db, task)


# ---------------------------------------------------------------------------
# 核验
# ---------------------------------------------------------------------------


def get_manifest(db: Session, task_id: str) -> list[dict[str, Any]]:
    _require_task(db, task_id)
    entries = db.execute(
        select(DisposalManifestEntry)
        .where(DisposalManifestEntry.task_id == task_id)
        .order_by(DisposalManifestEntry.seq)
    ).scalars()
    return [
        {
            "seq": e.seq,
            "item_key": e.item_key,
            "action": e.action,
            "fingerprint": e.fingerprint,
            "prev_hash": e.prev_hash,
            "entry_hash": e.entry_hash,
            "created_at": ensure_utc(e.created_at),
        }
        for e in entries
    ]


def verify_task(db: Session, task_id: str) -> dict[str, Any]:
    """核验任务：策略固定未被替换、清单哈希链完整、处置结果与记录一致。"""
    task = _require_task(db, task_id)
    checks: dict[str, bool] = {}

    checks["policy_pinned"] = policy_hash(task.policy_snapshot) == task.policy_hash

    entries = get_manifest(db, task_id)
    chain = verify_chain(task_id, task.policy_hash, entries)
    checks["manifest_chain"] = chain.valid

    items = db.execute(
        select(DisposalItem).where(DisposalItem.task_id == task_id)
    ).scalars().all()

    fingerprints_ok = True
    erased_ok = True
    held_ok = True
    for item in items:
        if item.status == ItemStatus.DONE.value:
            if item.disposition == Disposition.FINGERPRINT.value:
                stored = db.get(
                    ErasedFingerprint, (task.plan_version, item.item_key)
                )
                if (
                    stored is None
                    or stored.fingerprint != (item.fingerprint or "")
                ):
                    fingerprints_ok = False
            if item.disposition in (
                Disposition.ERASE.value,
                Disposition.FINGERPRINT.value,
            ):
                if _get_event(db, task.plan_version, item.item_key) is not None:
                    erased_ok = False
        elif item.status == ItemStatus.HELD.value:
            if _get_event(db, task.plan_version, item.item_key) is None:
                held_ok = False
    checks["fingerprints_retained"] = fingerprints_ok
    checks["records_erased"] = erased_ok
    checks["holds_respected"] = held_ok

    return {
        "task_id": task_id,
        "valid": all(checks.values()),
        "checks": checks,
        "entries_checked": chain.entries_checked,
        "detail": chain.detail,
    }


def verify_certificate(
    db: Session, plan_version: str, freeze_id: str
) -> dict[str, Any]:
    """核验已签发证明：快照完整且被引用的明细要么仍在、要么留有指纹。"""
    freeze = db.get(Freeze, (plan_version, freeze_id))
    if freeze is None:
        raise FreezeNotFoundError(f"冻结快照 {freeze_id} 不存在")
    snapshot = dict(freeze.snapshot)
    snapshot_hash = content_fingerprint(snapshot)

    referenced: set[str] = set()
    for student in snapshot.get("students", []):
        for checkin in student.get("checkins", []):
            referenced.add(str(checkin["event_id"]))
        for adjustment in student.get("adjustments", []):
            referenced.add(str(adjustment["event_id"]))

    present: list[str] = []
    fingerprinted: list[str] = []
    missing: list[str] = []
    for record_key in sorted(referenced):
        if _get_event(db, plan_version, record_key) is not None:
            present.append(record_key)
        elif db.get(ErasedFingerprint, (plan_version, record_key)) is not None:
            fingerprinted.append(record_key)
        else:
            missing.append(record_key)

    return {
        "plan_version": plan_version,
        "freeze_id": freeze_id,
        "valid": not missing,
        "snapshot_hash": snapshot_hash,
        "referenced_records": len(referenced),
        "present_records": len(present),
        "fingerprinted_records": len(fingerprinted),
        "missing_records": missing,
    }


def verify_presented_records(
    db: Session, records: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """核验外部出示的原始明细是否与保留指纹一致。"""
    results: list[dict[str, Any]] = []
    for raw in records:
        record = SourceRecord(
            record_key=str(raw["record_key"]),
            record_type=str(raw["record_type"]),
            student_id=str(raw["student_id"]),
            payload=dict(raw["payload"]),
            created_at=ensure_utc(raw["created_at"]),
        )
        stored = db.get(ErasedFingerprint, (str(raw["plan_version"]), record.record_key))
        match = stored is not None and stored.fingerprint == record_fingerprint(record)
        results.append(
            {
                "plan_version": str(raw["plan_version"]),
                "record_key": record.record_key,
                "stored": stored is not None,
                "match": match,
            }
        )
    return {
        "results": results,
        "all_matched": all(r["match"] for r in results) if results else True,
    }
