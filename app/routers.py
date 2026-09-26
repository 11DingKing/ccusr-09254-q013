"""服务端业务模块。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from . import retention_services as retention
from . import services
from .db import get_db
from .schemas import (
    ApproveTaskIn,
    CertificateVerifyOut,
    DiffOut,
    DisposalPreviewIn,
    DisposalPreviewOut,
    DisposalTaskDetailOut,
    DisposalTaskIn,
    DisposalTaskOut,
    EventBatchIn,
    ExecuteTaskIn,
    FingerprintVerifyIn,
    FingerprintVerifyOut,
    FreezeIn,
    ImportResult,
    LegalHoldIn,
    LegalHoldOut,
    ManifestEntryOut,
    PlanIn,
    PlanOut,
    RetentionPolicyIn,
    RetentionPolicyOut,
    SnapshotOut,
    StudentProgressOut,
    TaskVerifyOut,
)

router = APIRouter(prefix="/api")


@router.post("/plans", response_model=PlanOut, status_code=status.HTTP_201_CREATED)
def create_plan(body: PlanIn, db: Session = Depends(get_db)) -> Any:
    return services.ensure_plan(
        db,
        plan_version=body.plan_version,
        iana_timezone=body.iana_timezone,
        required_seconds=body.required_seconds,
    )


@router.get("/plans/{plan_version}", response_model=PlanOut)
def read_plan(plan_version: str, db: Session = Depends(get_db)) -> Any:
    plan = services.get_plan_plain(db, plan_version)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan not found")
    return plan


@router.post(
    "/plans/{plan_version}/events",
    response_model=ImportResult,
    status_code=status.HTTP_201_CREATED,
)
def post_events(
    plan_version: str, body: EventBatchIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return services.import_events(
            db,
            plan_version=plan_version,
            events=[e.model_dump() for e in body.events],
        )
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/snapshot",
    response_model=SnapshotOut,
)
def get_snapshot(plan_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        snap = services.current_snapshot(db, plan_version)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/students/{student_id}/progress",
    response_model=StudentProgressOut,
)
def get_progress(
    plan_version: str, student_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        result = services.student_progress(db, plan_version, student_id)
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.post(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
)
def post_freeze(
    plan_version: str,
    freeze_id: str,
    body: FreezeIn,
    db: Session = Depends(get_db),
) -> Any:
    try:
        snap, _ = services.freeze_semester(
            db, plan_version=plan_version, freeze_id=freeze_id
        )
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}",
    response_model=SnapshotOut,
)
def get_freeze(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        snap = services.get_frozen_snapshot(db, plan_version, freeze_id)
        return snap.to_dict()
    except services.PlanNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except services.FreezeNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/explain/{student_id}",
    response_model=StudentProgressOut,
)
def explain_freeze_student(
    plan_version: str,
    freeze_id: str,
    student_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        result = services.explain_frozen_student(
            db, plan_version, freeze_id, student_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if result is None:
        raise HTTPException(status_code=404, detail="student not found")
    return result


@router.get(
    "/plans/{plan_version}/freezes/{freeze_id}/diff/{other_freeze_id}",
    response_model=DiffOut,
)
def get_diff(
    plan_version: str,
    freeze_id: str,
    other_freeze_id: str,
    db: Session = Depends(get_db),
) -> Any:
    try:
        return services.diff_freezes(
            db, plan_version, freeze_id, other_freeze_id
        )
    except (services.PlanNotFoundError, services.FreezeNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# 保留策略与处置
# ---------------------------------------------------------------------------

_NOT_FOUND = (
    retention.PlanNotFoundError,
    retention.PolicyNotFoundError,
    retention.TaskNotFoundError,
    retention.HoldNotFoundError,
    retention.FreezeNotFoundError,
)
_CONFLICT = (
    retention.NoActivePolicyError,
    retention.InvalidTaskStateError,
    retention.DuplicateActiveTaskError,
    retention.PolicyValidationError,
)


def _retention_errors(exc: Exception) -> HTTPException:
    if isinstance(exc, _NOT_FOUND):
        return HTTPException(status_code=404, detail=str(exc))
    return HTTPException(status_code=409, detail=str(exc))


@router.post(
    "/retention/policies",
    response_model=RetentionPolicyOut,
    status_code=status.HTTP_201_CREATED,
)
def create_retention_policy(body: RetentionPolicyIn, db: Session = Depends(get_db)) -> Any:
    try:
        row = retention.create_policy(
            db,
            policy_id=body.policy_id,
            rules=[r.model_dump() for r in body.rules],
            activate=body.activate,
        )
        return retention.policy_view(row)
    except _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.get("/retention/policies", response_model=list[RetentionPolicyOut])
def list_retention_policies(db: Session = Depends(get_db)) -> Any:
    return [retention.policy_view(r) for r in retention.list_policies(db)]


@router.get(
    "/retention/policies/{policy_id}/{version}",
    response_model=RetentionPolicyOut,
)
def get_retention_policy(policy_id: str, version: int, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.policy_view(retention.get_policy(db, policy_id, version))
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.post(
    "/retention/policies/{policy_id}/{version}/activate",
    response_model=RetentionPolicyOut,
)
def activate_retention_policy(
    policy_id: str, version: int, db: Session = Depends(get_db)
) -> Any:
    try:
        return retention.policy_view(retention.activate_policy(db, policy_id, version))
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.post(
    "/retention/holds",
    response_model=LegalHoldOut,
    status_code=status.HTTP_201_CREATED,
)
def place_legal_hold(body: LegalHoldIn, db: Session = Depends(get_db)) -> Any:
    try:
        row = retention.place_hold(
            db,
            hold_id=body.hold_id,
            plan_version=body.plan_version,
            student_id=body.student_id,
            reason=body.reason,
        )
        return retention.hold_view(row)
    except _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.get("/retention/holds", response_model=list[LegalHoldOut])
def list_legal_holds(
    plan_version: str | None = None,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return [
        retention.hold_view(r)
        for r in retention.list_holds(
            db, plan_version=plan_version, student_id=student_id
        )
    ]


@router.post("/retention/holds/{hold_id}/release", response_model=LegalHoldOut)
def release_legal_hold(hold_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.hold_view(retention.release_hold(db, hold_id))
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/preview", response_model=DisposalPreviewOut)
def preview_disposal(body: DisposalPreviewIn, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.preview_disposal(
            db, plan_version=body.plan_version, student_id=body.student_id
        )
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.post(
    "/retention/tasks",
    response_model=DisposalTaskDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def create_disposal_task(body: DisposalTaskIn, db: Session = Depends(get_db)) -> Any:
    try:
        task = retention.create_task(
            db,
            plan_version=body.plan_version,
            student_id=body.student_id,
            requested_by=body.requested_by,
        )
        return retention.task_view(db, task, with_items=True)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.get("/retention/tasks", response_model=list[DisposalTaskOut])
def list_disposal_tasks(
    plan_version: str | None = None,
    student_id: str | None = None,
    db: Session = Depends(get_db),
) -> Any:
    return retention.list_tasks(db, plan_version=plan_version, student_id=student_id)


@router.get("/retention/tasks/{task_id}", response_model=DisposalTaskDetailOut)
def get_disposal_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.get_task_view(db, task_id, with_items=True)
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/tasks/{task_id}/approve", response_model=DisposalTaskOut)
def approve_disposal_task(
    task_id: str, body: ApproveTaskIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return retention.approve_task(db, task_id, approved_by=body.approved_by)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/tasks/{task_id}/execute", response_model=DisposalTaskOut)
def execute_disposal_task(
    task_id: str, body: ExecuteTaskIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return retention.execute_task(db, task_id, max_items=body.max_items)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/tasks/{task_id}/pause", response_model=DisposalTaskOut)
def pause_disposal_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.pause_task(db, task_id)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/tasks/{task_id}/resume", response_model=DisposalTaskOut)
def resume_disposal_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.resume_task(db, task_id)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/tasks/{task_id}/retry", response_model=DisposalTaskOut)
def retry_disposal_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.retry_task(db, task_id)
    except _NOT_FOUND + _CONFLICT as exc:
        raise _retention_errors(exc) from exc


@router.get(
    "/retention/tasks/{task_id}/manifest",
    response_model=list[ManifestEntryOut],
)
def get_disposal_manifest(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.get_manifest(db, task_id)
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.get("/retention/tasks/{task_id}/verify", response_model=TaskVerifyOut)
def verify_disposal_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return retention.verify_task(db, task_id)
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.get(
    "/retention/certificates/{plan_version}/{freeze_id}/verify",
    response_model=CertificateVerifyOut,
)
def verify_certificate(
    plan_version: str, freeze_id: str, db: Session = Depends(get_db)
) -> Any:
    try:
        return retention.verify_certificate(db, plan_version, freeze_id)
    except _NOT_FOUND as exc:
        raise _retention_errors(exc) from exc


@router.post("/retention/fingerprints/verify", response_model=FingerprintVerifyOut)
def verify_presented_records(
    body: FingerprintVerifyIn, db: Session = Depends(get_db)
) -> Any:
    return retention.verify_presented_records(
        db, [r.model_dump() for r in body.records]
    )
