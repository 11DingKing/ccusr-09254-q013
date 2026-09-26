"""留存处置 API 的请求与响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class RetentionRuleIn(BaseModel):
    detail_type: Literal["checkin", "mentor_confirm", "leave_correction", "*"]
    retain_days: int = Field(..., ge=0)
    action: Literal["delete", "retain_fingerprint"]


class PolicyIn(BaseModel):
    policy_version: str = Field(..., min_length=1, max_length=64)
    rules: list[RetentionRuleIn] = Field(..., min_length=1)


class PolicyOut(BaseModel):
    policy_version: str
    rules: list[dict[str, Any]]
    checksum: str
    created_at: str | None = None


class HoldIn(BaseModel):
    student_id: str = Field(..., min_length=1, max_length=128)
    reason: str = Field(..., min_length=1)


class HoldOut(BaseModel):
    hold_id: str
    student_id: str
    reason: str
    created_at: str
    released_at: str | None
    open: bool


class DisposalPreviewIn(BaseModel):
    policy_version: str = Field(..., min_length=1, max_length=64)
    plan_version: str | None = Field(None, max_length=128)
    student_id: str | None = Field(None, max_length=128)


class ApprovalIn(BaseModel):
    approved_by: str = Field(..., min_length=1, max_length=128)


class DisposalItemOut(BaseModel):
    event_id: str
    plan_version: str
    student_id: str
    detail_type: str
    planned_action: str
    status: str
    attempts: int = 0
    error: str | None = None
    retained_fingerprint: str | None = None
    manifest_entry_hash: str | None = None
    manifest_sequence: int | None = None


class DisposalTaskOut(BaseModel):
    task_id: str
    policy_version: str
    policy_checksum: str
    plan_version: str | None
    student_id: str | None
    state: str
    dry_run: bool
    approved_by: str | None
    approved_at: str | None
    last_error: str | None
    manifest_hash: str | None
    created_at: str
    updated_at: str
    counts: dict[str, int]
    items: list[DisposalItemOut] | None = None
    processed_this_run: int | None = None
    failed_this_run: int | None = None


class ExecuteIn(BaseModel):
    limit: int | None = Field(None, ge=0)


class ManifestEntryOut(BaseModel):
    sequence: int
    event_id: str
    plan_version: str
    student_id: str
    detail_type: str
    action: str
    status: str
    fingerprint: str | None
    processed_at: str | None
    entry_hash: str


class ManifestOut(BaseModel):
    task_id: str
    policy_version: str
    policy_checksum: str
    chain_tip: str | None
    entries: list[ManifestEntryOut]


class ManifestVerifyOut(BaseModel):
    task_id: str
    chain_ok: bool
    state_ok: bool
    chain_tip: str | None
    entries_verified: int
    breaks: list[dict[str, Any]]
    state_checks: list[dict[str, Any]]
