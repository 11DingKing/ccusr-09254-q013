"""留存策略与处置任务 API：策略、预览、批准、执行、核验。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from ..db import get_db
from . import service
from .policy import PolicyError
from .schemas import (
    ApprovalIn,
    DisposalPreviewIn,
    DisposalTaskOut,
    ExecuteIn,
    HoldIn,
    HoldOut,
    ManifestOut,
    ManifestVerifyOut,
    PolicyIn,
    PolicyOut,
)

router = APIRouter(tags=["retention"])

_CONFLICT = (service.RetentionError, PolicyError)
_NOT_FOUND = (service.PolicyNotFoundError, service.TaskNotFoundError)


@router.post(
    "/retention/policies",
    response_model=PolicyOut,
    status_code=status.HTTP_201_CREATED,
)
def post_policy(body: PolicyIn, db: Session = Depends(get_db)) -> Any:
    try:
        return service.publish_policy(
            db,
            policy_version=body.policy_version,
            rules=[r.model_dump() for r in body.rules],
        )
    except _CONFLICT as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/retention/policies/{policy_version}", response_model=PolicyOut)
def get_policy(policy_version: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.read_policy(db, policy_version)
    except service.PolicyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/legal-holds/{hold_id}",
    response_model=HoldOut,
    status_code=status.HTTP_201_CREATED,
)
def post_hold(hold_id: str, body: HoldIn, db: Session = Depends(get_db)) -> Any:
    try:
        return service.create_hold(
            db,
            hold_id=hold_id,
            student_id=body.student_id,
            reason=body.reason,
        )
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/legal-holds/{hold_id}/release", response_model=HoldOut)
def post_release_hold(hold_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.release_hold(db, hold_id=hold_id)
    except service.RetentionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/legal-holds", response_model=list[HoldOut])
def get_holds(db: Session = Depends(get_db)) -> Any:
    return service.list_holds(db)


@router.post(
    "/disposal-tasks/{task_id}/preview",
    response_model=DisposalTaskOut,
    status_code=status.HTTP_201_CREATED,
)
def post_preview(
    task_id: str, body: DisposalPreviewIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.preview_task(
            db,
            task_id=task_id,
            policy_version=body.policy_version,
            plan_version=body.plan_version,
            student_id=body.student_id,
        )
    except service.PolicyNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/disposal-tasks/{task_id}", response_model=DisposalTaskOut)
def get_task(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.task_to_dict(db, task_id, include_items=True)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/disposal-tasks/{task_id}/approve", response_model=DisposalTaskOut)
def post_approve(
    task_id: str, body: ApprovalIn, db: Session = Depends(get_db)
) -> Any:
    try:
        return service.approve_task(db, task_id=task_id, approved_by=body.approved_by)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/disposal-tasks/{task_id}/pause", response_model=DisposalTaskOut)
def post_pause(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.pause_task(db, task_id=task_id)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/disposal-tasks/{task_id}/cancel", response_model=DisposalTaskOut)
def post_cancel(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.cancel_task(db, task_id=task_id)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/disposal-tasks/{task_id}/execute", response_model=DisposalTaskOut)
def post_execute(
    task_id: str, body: ExecuteIn | None = None, db: Session = Depends(get_db)
) -> Any:
    limit = body.limit if body is not None else None
    try:
        return service.execute_task(db, task_id=task_id, limit=limit)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except service.RetentionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get(
    "/disposal-tasks/{task_id}/manifest",
    response_model=ManifestOut,
)
def get_manifest(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.get_manifest(db, task_id)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get(
    "/disposal-tasks/{task_id}/verify",
    response_model=ManifestVerifyOut,
)
def get_verify(task_id: str, db: Session = Depends(get_db)) -> Any:
    try:
        return service.verify_manifest(db, task_id)
    except service.TaskNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
