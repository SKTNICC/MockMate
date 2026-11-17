"""准备接口、匹配任务与匹配报告。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import MatchReport, User
from app.schemas import (
    AiJobInput,
    CreatePreparationInput,
    EditPreparationInput,
    Job as JobSchema,
    MatchReport as MatchReportSchema,
    Preparation as PreparationSchema,
    Preparations,
)
from app.services import idempotency as idem
from app.services import jobs as jobs_service
from app.services import preparations as preparations_service
from app.services.bank import digest

router = APIRouter(tags=["preparations"])


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
    "/preparations",
    response_model=PreparationSchema,
    status_code=201,
    operation_id="createPreparation",
)
def create_preparation(
    payload: CreatePreparationInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    prep = preparations_service.create_preparation(db, user.id, payload)
    body = preparations_service.preparation_to_schema(prep).model_dump(mode="json")
    return idem.complete(db, handle, 201, body)


@router.get("/preparations", response_model=Preparations, operation_id="listPreparations")
def list_preparations(
    db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Preparations:
    return Preparations(items=preparations_service.list_preparations(db, user.id))


@router.get(
    "/preparations/{preparation_id}",
    response_model=PreparationSchema,
    operation_id="getPreparation",
)
def get_preparation(
    preparation_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> PreparationSchema:
    prep = preparations_service.get_preparation(db, user.id, _uuid(preparation_id))
    return preparations_service.preparation_to_schema(prep)


@router.put(
    "/preparations/{preparation_id}",
    response_model=PreparationSchema,
    operation_id="editPreparation",
)
def edit_preparation(
    preparation_id: str,
    payload: EditPreparationInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, payload)
    if handle.replay is not None:
        return handle.replay
    prep = preparations_service.get_preparation(db, user.id, _uuid(preparation_id))
    prep = preparations_service.edit_preparation(db, prep, payload)
    body = preparations_service.preparation_to_schema(prep).model_dump(mode="json")
    return idem.complete(db, handle, 200, body)


@router.delete("/preparations/{preparation_id}", status_code=204, operation_id="deletePreparation")
def delete_preparation(
    preparation_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = _begin(db, user, request, None)
    if handle.replay is not None:
        return handle.replay
    prep = preparations_service.get_preparation(db, user.id, _uuid(preparation_id))
    preparations_service.delete_preparation(db, prep, request.url.path)
    return idem.complete(db, handle, 204, None)


@router.post(
    "/preparations/{preparation_id}/match",
    response_model=JobSchema,
    status_code=202,
    operation_id="matchPreparation",
)
def match_preparation(
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
        raise AppError("CONSENT_REQUIRED", "匹配会把材料文本发送给配置的外部模型，需要先确认")
    prep = preparations_service.get_preparation(db, user.id, _uuid(preparation_id))
    job = preparations_service.create_match_job(db, prep, payload, get_settings())
    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(mode="json")
    return idem.complete(db, handle, 202, body)


@router.get(
    "/match-reports/{report_id}", response_model=MatchReportSchema, operation_id="getMatchReport"
)
def get_match_report(
    report_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> MatchReportSchema:
    report = db.get(MatchReport, _uuid(report_id))
    if report is None:
        raise AppError("NOT_FOUND", "匹配报告不存在", status_code=404)
    prep = preparations_service.get_preparation(db, user.id, report.preparation_id)
    if prep.owner_id != user.id:
        raise AppError("NOT_FOUND", "匹配报告不存在", status_code=404)
    return MatchReportSchema(
        id=report.id,
        preparation_id=report.preparation_id,
        input_revision=report.input_revision,
        resume_snapshot_id=report.resume_snapshot_id,
        jd_snapshot_id=report.jd_snapshot_id,
        rows=report.payload.get("rows", []),
        created_at=report.created_at,
    )
