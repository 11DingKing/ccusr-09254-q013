"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Plan(Base):
    __tablename__ = "plans"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    iana_timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    required_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        CheckConstraint("required_seconds >= 0", name="ck_plans_required_nonneg"),
    )


class Event(Base):
    __tablename__ = "events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("event_id", "plan_version", name="uq_events_event_id_plan"),
        Index("ix_events_plan_student", "plan_version", "student_id"),
    )


class Freeze(Base):
    __tablename__ = "freezes"

    plan_version: Mapped[str] = mapped_column(String(128), primary_key=True)
    freeze_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    event_cutoff_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class RetentionPolicy(Base):
    """留存策略：不同明细类型有不同保留年限与处置方式。

    策略内容不可原地修改，只能发布新版本；已生成的处置任务固定
    创建时的策略版本号与内容哈希。
    """

    __tablename__ = "retention_policies"

    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    rules: Mapped[list] = mapped_column(JSON, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )


class LegalHold(Base):
    """法律冻结：命中的学时明细禁止删除或脱敏，直到解除。"""

    __tablename__ = "legal_holds"

    hold_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("ix_legal_holds_student_open", "student_id", "released_at"),
    )


class DisposalTask(Base):
    """删除处置任务：策略版本在创建时固定，执行可暂停/重试/重启续跑。"""

    __tablename__ = "disposal_tasks"

    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    plan_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    student_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="preview")
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=_utcnow,
        onupdate=_utcnow,
        server_default=func.now(),
    )

    __table_args__ = (
        CheckConstraint(
            "state in ('preview','approved','running','paused','completed',"
            "'completed_with_errors','cancelled')",
            name="ck_disposal_tasks_state",
        ),
        Index("ix_disposal_tasks_state", "state"),
    )


class DisposalItem(Base):
    """处置任务中的单条明细；单项一个事务，失败不影响其他项。"""

    __tablename__ = "disposal_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    plan_version: Mapped[str] = mapped_column(String(128), nullable=False)
    student_id: Mapped[str] = mapped_column(String(128), nullable=False)
    detail_type: Mapped[str] = mapped_column(String(32), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    retained_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_entry_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("task_id", "event_id", "plan_version", name="uq_disposal_items_event"),
        CheckConstraint(
            "action in ('delete','retain_fingerprint','legal_hold')",
            name="ck_disposal_items_action",
        ),
        CheckConstraint(
            "status in ('pending','deleted','fingerprinted','held','skipped',"
            "'failed','superseded')",
            name="ck_disposal_items_status",
        ),
        Index("ix_disposal_items_task_status", "task_id", "status"),
    )
