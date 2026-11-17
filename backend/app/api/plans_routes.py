"""计划接口：生成任务、公开摘要、确认。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import Plan, User
from app.schemas import AiJobInput, Job as JobSchema, PublicPlan, RevisionInput
from app.services import idempotency as idem
from app.services import jobs as jobs_service
from app.services import planning
from app.services import preparations as preparations_service
from app.services.bank import digest

router = APIRouter(tags=["plans"])


def _begin(db: Session, user: User, request: Request, payload) -> idem.IdempotencyHandle:
    request_hash = digest(payload.model_dump(mode="json")) if payload is not None else digest({})
    return idem.begin(
        db,
        user_id=user.id,
        method=request.method,
        path=request.url.path,
        raw_key=request.headers.get("Idempotency-Key"),
        request_hash=request_hash,
    )


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError("NOT_FOUND", "对象不存在", status_code=404)


@router.post(
    "/preparations/{preparation_id}/generate-plan",
    response_model=JobSchema,
    status_code=202,
    operation_id="generatePlan",
)
def generate_plan(
    preparation_id: str,
    payload: AiJobInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    if not payload.external_processing_consent:
        raise AppError("CONSENT_REQUIRED", "生成计划会把材料文本发送给配置的外部模型，需要先确认")
    prep = preparations_service.get_preparation(db, user.id, _uuid(preparation_id))
    job = preparations_service.create_plan_job(db, prep, payload, get_settings())
    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(mode="json")
    return idem.complete(db, handle, 202, body)


@router.get("/plans/{plan_id}", response_model=PublicPlan, operation_id="getPlan")
def get_plan(
    plan_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> PublicPlan:
    plan = db.get(Plan, _uuid(plan_id))
    if plan is None or plan.owner_id != user.id:
        raise AppError("NOT_FOUND", "计划不存在", status_code=404)
    return planning.plan_to_public(plan)


@router.post(
    "/plans/{plan_id}/confirm", response_model=PublicPlan, status_code=200, operation_id="confirmPlan"
)
def confirm_plan(
    plan_id: str,
    payload: RevisionInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    plan = planning.confirm_plan_for_owner(
        db, owner_id=user.id, plan_id=_uuid(plan_id), revision=payload.revision
    )
    body = planning.plan_to_public(plan).model_dump(mode="json")
    return idem.complete(db, handle, 200, body)
