"""任务接口：查询与显式重试。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import Job, User
from app.schemas import Job as JobSchema
from app.schemas import RetryInput
from app.services import idempotency as idem
from app.services import jobs as jobs_service
from app.services.bank import digest

router = APIRouter(tags=["jobs"])


def _uuid(raw: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)


@router.get("/jobs/{job_id}", response_model=JobSchema, operation_id="getJob")
def get_job(
    job_id: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> JobSchema:
    job = db.get(Job, _uuid(job_id))
    if job is None or job.owner_id != user.id:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    return jobs_service.job_to_schema(db, job, get_settings().max_job_attempts)


@router.post(
    "/jobs/{job_id}/retry", response_model=JobSchema, status_code=202, operation_id="retryJob"
)
def retry_job(
    job_id: str,
    payload: RetryInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    handle = idem.begin(
        db,
        user_id=user.id,
        method=request.method,
        path=request.url.path,
        raw_key=request.headers.get("Idempotency-Key"),
        request_hash=digest(payload.model_dump(mode="json")),
    )
    if handle.replay is not None:
        return handle.replay
    job = db.get(Job, _uuid(job_id))
    if job is None or job.owner_id != user.id:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    # 与 worker／编辑统一锁序：先业务目标，再 Job。
    jobs_service.lock_job_target(
        db,
        owner_id=user.id,
        kind=job.kind,
        target_id=job.target_id,
        input_revision=None,
    )
    job = jobs_service.retry_job(db, job, payload.expected_attempt, get_settings().max_job_attempts)
    if job.kind in ("audio_asr", "audio_tts"):
        from app.services import voice as voice_service

        voice_service.on_retry_attempt(db, job)
    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(mode="json")
    return idem.complete(db, handle, 202, body)
