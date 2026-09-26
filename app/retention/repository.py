"""留存处置模块的数据库访问。"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..models import (
    DisposalItem,
    DisposalTask,
    Event,
    Freeze,
    LegalHold,
    RetentionPolicy,
)
from .fingerprint import GENESIS

RETRYABLE_STATUSES = ("pending", "failed")


# ---- 策略 ----

def insert_policy(
    db: Session, *, policy_version: str, rules: list[dict[str, Any]], checksum: str
) -> bool:
    """插入策略版本；版本号已存在返回 False（策略不可原地修改）。"""
    stmt = sqlite_insert(RetentionPolicy).values(
        policy_version=policy_version, rules=rules, checksum=checksum
    ).on_conflict_do_nothing(index_elements=["policy_version"])
    inserted = db.execute(stmt).rowcount
    db.commit()
    return bool(inserted)


def get_policy(db: Session, policy_version: str) -> RetentionPolicy | None:
    return db.get(RetentionPolicy, policy_version)


# ---- 法律冻结 ----

def insert_hold(db: Session, *, hold_id: str, student_id: str, reason: str) -> bool:
    stmt = sqlite_insert(LegalHold).values(
        hold_id=hold_id, student_id=student_id, reason=reason
    ).on_conflict_do_nothing(index_elements=["hold_id"])
    inserted = db.execute(stmt).rowcount
    db.commit()
    return bool(inserted)


def get_hold(db: Session, hold_id: str) -> LegalHold | None:
    return db.get(LegalHold, hold_id)


def release_hold(db: Session, hold_id: str, released_at: datetime) -> bool:
    stmt = (
        update(LegalHold)
        .where(LegalHold.hold_id == hold_id)
        .where(LegalHold.released_at.is_(None))
        .values(released_at=released_at)
    )
    changed = db.execute(stmt).rowcount
    db.commit()
    return bool(changed)


def list_holds(db: Session, *, open_only: bool) -> list[LegalHold]:
    stmt = select(LegalHold)
    if open_only:
        stmt = stmt.where(LegalHold.released_at.is_(None))
    return list(db.execute(stmt.order_by(LegalHold.hold_id)).scalars().all())


def open_hold_exists(db: Session, student_id: str) -> bool:
    stmt = select(LegalHold.hold_id).where(
        LegalHold.student_id == student_id,
        LegalHold.released_at.is_(None),
    )
    return db.execute(stmt.limit(1)).scalar_one_or_none() is not None


# ---- 事件与冻结引用 ----

def load_candidate_events(
    db: Session, *, plan_version: str | None, student_id: str | None
) -> list[Event]:
    stmt = select(Event)
    if plan_version is not None:
        stmt = stmt.where(Event.plan_version == plan_version)
    if student_id is not None:
        stmt = stmt.where(Event.student_id == student_id)
    rows = list(db.execute(stmt.order_by(Event.id)).scalars().all())
    # 已脱敏的明细已经完成处置，不再重复入选新任务。
    return [r for r in rows if not r.payload.get("redacted")]


def referenced_event_keys(db: Session) -> set[tuple[str, str]]:
    """被任意已签发冻结快照覆盖的事件 (plan_version, event_id)。

    冻结快照按 event_id 截止重放（与 core.replay 一致），因此引用集合
    等于 event_id <= 某个冻结 cutoff 的全部事件。
    """
    stmt = (
        select(Event.plan_version, Event.event_id)
        .join(Freeze, Event.plan_version == Freeze.plan_version)
        .where(Freeze.event_cutoff_id.is_not(None))
        .where(Event.event_id <= Freeze.event_cutoff_id)
        .distinct()
    )
    return set(db.execute(stmt).all())


# ---- 处置任务 ----

def insert_task(
    db: Session, *, task_id: str, policy_version: str, policy_checksum: str,
    plan_version: str | None, student_id: str | None,
) -> DisposalTask | None:
    stmt = sqlite_insert(DisposalTask).values(
        task_id=task_id,
        policy_version=policy_version,
        policy_checksum=policy_checksum,
        plan_version=plan_version,
        student_id=student_id,
        state="preview",
        dry_run=True,
    ).on_conflict_do_nothing(index_elements=["task_id"])
    if db.execute(stmt).rowcount == 0:
        db.rollback()
        return None
    task = db.get(DisposalTask, task_id)
    assert task is not None
    return task


def insert_item(db: Session, **values: Any) -> None:
    db.add(DisposalItem(**values))


def get_task(db: Session, task_id: str) -> DisposalTask | None:
    return db.get(DisposalTask, task_id)


def list_items(
    db: Session, task_id: str, *, status: str | None = None
) -> list[DisposalItem]:
    stmt = select(DisposalItem).where(DisposalItem.task_id == task_id)
    if status is not None:
        stmt = stmt.where(DisposalItem.status == status)
    return list(db.execute(stmt.order_by(DisposalItem.id)).scalars().all())


def list_chain(db: Session, task_id: str) -> list[DisposalItem]:
    """按哈希链上的先后顺序返回已入链条目。"""
    stmt = (
        select(DisposalItem)
        .where(DisposalItem.task_id == task_id)
        .where(DisposalItem.manifest_sequence.is_not(None))
        .order_by(DisposalItem.manifest_sequence)
    )
    return list(db.execute(stmt).scalars().all())


def item_counts(db: Session, task_id: str) -> dict[str, int]:
    items = list_items(db, task_id)
    counts: dict[str, int] = {}
    for item in items:
        counts[item.status] = counts.get(item.status, 0) + 1
    return counts


def count_chained_items(db: Session, task_id: str) -> int:
    stmt = (
        select(func.count(DisposalItem.id))
        .where(DisposalItem.task_id == task_id)
        .where(DisposalItem.manifest_sequence.is_not(None))
    )
    return int(db.execute(stmt).scalar_one())


def update_task_state(
    db: Session, *, task_id: str, expected: tuple[str, ...], target: str
) -> bool:
    """状态比较与交换；仅当当前状态属于 expected 时生效。"""
    stmt = (
        update(DisposalTask)
        .where(DisposalTask.task_id == task_id)
        .where(DisposalTask.state.in_(expected))
        .values(state=target)
    )
    changed = db.execute(stmt).rowcount
    db.commit()
    return bool(changed)


def mark_running(db: Session, task_id: str) -> bool:
    """approved/paused/completed_with_errors → running（暂停后恢复同样适用）。"""
    return update_task_state(
        db,
        task_id=task_id,
        expected=("approved", "paused", "completed_with_errors"),
        target="running",
    )


def update_task_manifest_hash(
    db: Session, *, task_id: str, expected_hash: str, new_hash: str
) -> bool:
    """链尖比较交换：expected_hash 为创世值时任务行必须还没有链尖。

    仅在 running 状态下可推进；执行期间被暂停时本更新落空，
    调用方回滚该条目，暂停状态不被覆盖。
    """
    stmt = (
        update(DisposalTask)
        .where(DisposalTask.task_id == task_id)
        .where(DisposalTask.state == "running")
        .values(manifest_hash=new_hash)
    )
    if expected_hash == GENESIS:
        stmt = stmt.where(DisposalTask.manifest_hash.is_(None))
    else:
        stmt = stmt.where(DisposalTask.manifest_hash == expected_hash)
    return db.execute(stmt).rowcount > 0
