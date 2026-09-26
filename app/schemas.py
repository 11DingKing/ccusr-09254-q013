"""服务端业务模块。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class PlanIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    iana_timezone: str = Field(..., min_length=1, max_length=64)
    required_seconds: int = Field(0, ge=0)


class PlanOut(BaseModel):
    plan_version: str
    iana_timezone: str
    required_seconds: int


class CheckinPayload(BaseModel):
    activity_id: str = ""
    activity_type: str = "regular"
    check_in_at: datetime
    check_out_at: datetime

    @model_validator(mode="after")
    def _check_order(self) -> "CheckinPayload":
        if self.check_out_at <= self.check_in_at:
            raise ValueError("check_out_at must be after check_in_at")
        return self

    @field_validator("check_in_at", "check_out_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("timestamps must be timezone-aware (RFC 3339)")
        return v


class MentorConfirmPayload(BaseModel):
    checkin_event_id: str


class LeaveCorrectionPayload(BaseModel):
    adjustment_seconds: int
    reason: str = ""


class EventIn(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=128)
    event_type: Literal["checkin", "mentor_confirm", "leave_correction"]
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]


class EventBatchIn(BaseModel):
    events: list[EventIn]


class EventOut(BaseModel):
    event_id: str
    plan_version: str
    event_type: str
    student_id: str
    payload: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


class ImportResult(BaseModel):
    accepted: int
    duplicates: list[str]
    rejected: list[dict[str, Any]]


class DailyTotal(BaseModel):
    academic_day: str
    seconds: int


class CheckinExplanation(BaseModel):
    event_id: str
    activity_id: str
    activity_type: str
    status: str
    counts: bool
    check_in_at_utc: str
    check_out_at_utc: str
    raw_seconds: int
    academic_days: list[dict[str, Any]]


class AdjustmentOut(BaseModel):
    event_id: str
    seconds: int
    reason: str


class StudentProgressOut(BaseModel):
    student_id: str
    confirmed_seconds: int
    pending_seconds: int
    adjustment_seconds: int
    total_seconds: int
    lesson_units: int
    pending_lesson_units: int
    meets_requirement: bool
    daily: list[DailyTotal]
    checkins: list[CheckinExplanation]
    adjustments: list[AdjustmentOut]


class SnapshotOut(BaseModel):
    plan_version: str
    freeze_id: str | None
    timezone: str
    required_seconds: int
    generated_at: str
    event_cutoff_id: str | None
    students: list[dict[str, Any]]


class FreezeIn(BaseModel):
    pass


class DiffOut(BaseModel):
    plan_version: str
    old_freeze_id: str | None
    new_freeze_id: str | None
    old_generated_at: str
    new_generated_at: str
    old_event_cutoff_id: str | None
    new_event_cutoff_id: str | None
    student_changes: list[dict[str, Any]]
    students_affected: int


# ---------------------------------------------------------------------------
# 保留策略与处置
# ---------------------------------------------------------------------------


class RetentionRuleIn(BaseModel):
    category: str = Field(..., min_length=1, max_length=64)
    retention_days: int = Field(..., ge=0)
    disposition: Literal["erase", "fingerprint"]


class RetentionPolicyIn(BaseModel):
    policy_id: str = Field(..., min_length=1, max_length=128)
    rules: list[RetentionRuleIn] = Field(..., min_length=1)
    activate: bool = False


class RetentionPolicyOut(BaseModel):
    policy_id: str
    version: int
    status: str
    rules: list[RetentionRuleIn]
    policy_hash: str
    created_at: datetime


class LegalHoldIn(BaseModel):
    hold_id: str = Field(..., min_length=1, max_length=128)
    plan_version: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1, max_length=512)


class LegalHoldOut(BaseModel):
    hold_id: str
    plan_version: str
    student_id: str
    reason: str
    active: bool
    created_at: datetime
    released_at: datetime | None


class DisposalPreviewIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)


class DisposalPreviewItem(BaseModel):
    item_key: str
    event_type: str
    category: str
    disposition: str
    reason: str


class DisposalPreviewOut(BaseModel):
    plan_version: str
    student_id: str
    policy_id: str
    policy_version: int
    policy_hash: str
    items: list[DisposalPreviewItem]
    summary: dict[str, int]


class DisposalTaskIn(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    student_id: str = Field(..., min_length=1, max_length=128)
    requested_by: str = Field(..., min_length=1, max_length=128)


class ApproveTaskIn(BaseModel):
    approved_by: str = Field(..., min_length=1, max_length=128)


class ExecuteTaskIn(BaseModel):
    max_items: int | None = Field(default=None, ge=1)


class DisposalItemOut(BaseModel):
    item_key: str
    disposition: str
    status: str
    fingerprint: str | None
    attempts: int
    last_error: str | None
    processed_at: datetime | None


class TaskCounts(BaseModel):
    total: int
    pending: int
    done: int
    failed: int
    held: int


class DisposalTaskOut(BaseModel):
    task_id: str
    plan_version: str
    student_id: str
    policy_id: str
    policy_version: int
    policy_hash: str
    status: str
    requested_by: str
    approved_by: str | None
    approved_at: datetime | None
    last_error: str | None
    counts: TaskCounts
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class DisposalTaskDetailOut(DisposalTaskOut):
    items: list[DisposalItemOut]


class ManifestEntryOut(BaseModel):
    seq: int
    item_key: str
    action: str
    fingerprint: str
    prev_hash: str
    entry_hash: str
    created_at: datetime


class TaskVerifyOut(BaseModel):
    task_id: str
    valid: bool
    checks: dict[str, bool]
    entries_checked: int
    detail: str


class CertificateVerifyOut(BaseModel):
    plan_version: str
    freeze_id: str
    valid: bool
    snapshot_hash: str
    referenced_records: int
    present_records: int
    fingerprinted_records: int
    missing_records: list[str]


class PresentedRecord(BaseModel):
    plan_version: str = Field(..., min_length=1, max_length=128)
    record_key: str = Field(..., min_length=1, max_length=128)
    record_type: str = Field(..., min_length=1, max_length=32)
    student_id: str = Field(..., min_length=1, max_length=128)
    payload: dict[str, Any]
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _ensure_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        return v


class FingerprintVerifyIn(BaseModel):
    records: list[PresentedRecord]


class FingerprintVerifyResult(BaseModel):
    plan_version: str
    record_key: str
    stored: bool
    match: bool


class FingerprintVerifyOut(BaseModel):
    results: list[FingerprintVerifyResult]
    all_matched: bool
