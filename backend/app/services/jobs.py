"""PostgreSQL 持久任务：创建、领取、租约续期、fencing 提交、失败、重试、回收。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Job, MaterialDraft, Preparation
from app.schemas import ErrorInfo, Job as JobSchema, ResultRef

LEASE_SECONDS = 120
HEARTBEAT_SECONDS = 20

RETRYABLE_JOB_ERRORS = {
    "AI_TIMEOUT",
    "AI_UNAVAILABLE",
    "AI_RATE_LIMITED",
    "JOB_INTERRUPTED",
}
JOB_ERROR_MESSAGES = {
    "AI_TIMEOUT": "AI 服务超时，结果未知",
    "AI_UNAVAILABLE": "AI 服务暂时不可用",
    "AI_RATE_LIMITED": "AI 服务限流，结果未知",
    "AI_AUTH_FAILED": "AI 服务认证失败，请检查服务端配置",
    "AI_REQUEST_REJECTED": "AI 服务拒绝了请求",
    "AI_INVALID_OUTPUT": "AI 输出不符合契约",
    "JOB_LEASE_LOST": "任务租约已失效",
    "BANK_INSUFFICIENT": "题库可用题不足",
    "AI_NOT_CONFIGURED": "AI 服务未配置",
    "FILE_TEXT_UNAVAILABLE": "文件无法提取文本",
    "JOB_INTERRUPTED": "任务执行中断，外部结果未知",
    "EVIDENCE_INVALID": "题目或匹配引用无依据",
    "EVALUATION_INVALID": "评价依据不完整",
    "DUPLICATE_QUESTION": "本场存在重复主问题",
    "DEDUP_UNCERTAIN": "语义去重不确定",
    "GROUNDING_INVALID": "存在无依据陈述",
    "CONSENT_REQUIRED": "未确认材料外发",
    "JD_LEVEL_CONFLICT": "JD 原文与当前岗位/级别设置冲突",
    "INTERNAL_ERROR": "任务执行失败",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_error(code: str, message: str | None = None, details: list[str] | None = None) -> dict:
    return {
        "code": code,
        "message": message or JOB_ERROR_MESSAGES.get(code, "任务执行失败"),
        "details": details or [],
        "request_id": str(uuid.uuid4()),
    }


def lock_job_target(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID,
    input_revision: int | None = None,
):
    """按统一锁顺序先锁业务目标，再锁 Job。

    worker 提交、编辑与任务创建都走这个入口，避免出现“job → target”的反向锁序。
    `populate_existing` 确保看到其他事务已提交的新 revision，而不是 Session 身份图里的旧对象。
    """
    if kind in ("extract", "parse"):
        row = db.scalars(
            select(MaterialDraft)
            .where(MaterialDraft.id == target_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.owner_id != owner_id:
            raise AppError("NOT_FOUND", "材料不存在", status_code=404)
        if input_revision is not None and row.revision != input_revision:
            raise AppError(
                "REVISION_CONFLICT",
                "材料版本已变化，请刷新后重试",
                details=[f"current_revision={row.revision}"],
            )
        return row
    if kind in ("match", "plan"):
        row = db.scalars(
            select(Preparation)
            .where(Preparation.id == target_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.owner_id != owner_id:
            raise AppError("NOT_FOUND", "准备不存在", status_code=404)
        if input_revision is not None and row.revision != input_revision:
            raise AppError(
                "REVISION_CONFLICT",
                "准备配置已变化，请刷新后重试",
                details=[f"current_revision={row.revision}"],
            )
        return row
    raise AppError("INVALID_INPUT", "未知任务类型")


def create_or_get_job(
    db: Session,
    *,
    owner_id: uuid.UUID,
    kind: str,
    target_id: uuid.UUID,
    input_revision: int,
    input_hash: str,
    input_payload: dict,
) -> tuple[Job, bool]:
    """同 target+kind+input_revision 最多一条 job；相同 key 不同输入返回 JOB_INPUT_CONFLICT。

    锁顺序固定为业务目标（MaterialDraft／Preparation）→ Job。调用方即使先读取过目标，
    这里也会重新锁并重读当前 revision，不能用先前 Session 中的旧对象代替。
    """
    target = lock_job_target(
        db,
        owner_id=owner_id,
        kind=kind,
        target_id=target_id,
        input_revision=input_revision,
    )
    stmt = (
        pg_insert(Job)
        .values(
            id=uuid.uuid4(),
            owner_id=owner_id,
            kind=kind,
            target_id=target_id,
            input_revision=input_revision,
            input_hash=input_hash,
            input_payload=input_payload,
            state="queued",
            attempt=1,
        )
        .on_conflict_do_nothing(constraint="uq_job_target_kind_revision")
        .returning(Job.id)
    )
    inserted_id = db.execute(stmt).scalar_one_or_none()
    created = inserted_id is not None
    job = db.scalars(
        select(Job)
        .where(
            Job.target_id == target_id,
            Job.kind == kind,
            Job.input_revision == input_revision,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one()
    if job.input_hash != input_hash:
        raise AppError(
            "JOB_INPUT_CONFLICT",
            "相同输入版本下已存在不同上下文的解析任务，请先编辑产生新版本后重试",
            details=["role_id", "level"],
        )
    if kind in ("extract", "parse"):
        target.latest_job_id = job.id
    return job, created


def inputs_current(db: Session, job: Job) -> bool:
    if job.kind in ("extract", "parse"):
        draft = db.get(MaterialDraft, job.target_id)
        if draft is None or draft.owner_id != job.owner_id:
            return False
        if draft.revision != job.input_revision:
            return False
        if job.kind == "extract" and draft.state == "parsed":
            return False
        return True
    preparation = db.get(Preparation, job.target_id)
    if preparation is None or preparation.owner_id != job.owner_id:
        return False
    return preparation.revision == job.input_revision


def is_retryable(db: Session, job: Job, max_attempts: int = 2) -> bool:
    if job.state not in ("failed", "interrupted"):
        return False
    if job.attempt >= max_attempts:
        return False
    code = job.error_code or (job.error or {}).get("code")
    if code not in RETRYABLE_JOB_ERRORS:
        return False
    return inputs_current(db, job)


def job_to_schema(db: Session, job: Job, max_attempts: int = 2) -> JobSchema:
    retryable = is_retryable(db, job, max_attempts)
    error_info = None
    if job.error:
        error_info = ErrorInfo(
            code=job.error.get("code", "INTERNAL_ERROR"),
            message=job.error.get("message", "任务执行失败"),
            retryable=retryable,
            details=list(job.error.get("details") or []),
            request_id=job.error.get("request_id") or str(job.id),
        )
    result_ref = None
    if job.result and job.state == "succeeded":
        result_ref = ResultRef(
            entity_type=job.result.get("entity_type"), id=job.result.get("id")
        )
    return JobSchema(
        id=job.id,
        kind=job.kind,
        target_id=job.target_id,
        input_revision=job.input_revision,
        state=job.state,
        attempt=job.attempt,
        retryable=retryable,
        result_ref=result_ref,
        error=error_info,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def retry_job(db: Session, job: Job, expected_attempt: int, max_attempts: int = 2) -> Job:
    # 调用方已先锁业务目标；这里再锁 Job 并重读当前状态。
    job = db.scalars(
        select(Job)
        .where(Job.id == job.id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one()
    if job.state not in ("failed", "interrupted"):
        raise AppError("RETRY_NOT_ALLOWED", "只有失败或中断的任务可以重试")
    if job.attempt != expected_attempt:
        raise AppError(
            "RETRY_NOT_ALLOWED",
            "任务尝试次数已变化，请刷新后重试",
            details=[f"current_attempt={job.attempt}"],
        )
    if not is_retryable(db, job, max_attempts):
        raise AppError("RETRY_NOT_ALLOWED", "该任务不可重试或输入已过期")
    job.attempt = job.attempt + 1
    job.state = "queued"
    job.error = None
    job.error_code = None
    job.lease_token = None
    job.lease_expires_at = None
    job.result = None
    db.flush()
    return job


def supersede_job(job: Job) -> None:
    job.state = "superseded"
    job.lease_token = None
    job.lease_expires_at = None
    job.result = None


def supersede_open_jobs(db: Session, *, owner_id: uuid.UUID, target_id: uuid.UUID | None, kinds: tuple[str, ...]) -> None:
    stmt = select(Job).where(
        Job.owner_id == owner_id,
        Job.kind.in_(kinds),
        Job.state.in_(("queued", "running")),
    )
    if target_id is not None:
        stmt = stmt.where(Job.target_id == target_id)
    for job in db.scalars(stmt).all():
        supersede_job(job)


def mark_failed(job: Job, code: str, message: str | None = None, details: list[str] | None = None) -> None:
    job.state = "failed"
    job.error_code = code
    job.error = job_error(code, message, details)
    job.lease_token = None
    job.lease_expires_at = None
    job.result = None


def mark_succeeded(job: Job, *, entity_type: str, entity_id: uuid.UUID) -> None:
    job.state = "succeeded"
    job.error = None
    job.error_code = None
    job.result = {"entity_type": entity_type, "id": str(entity_id)}
    job.lease_token = None
    job.lease_expires_at = None


# --------------------------------------------------------------------------- worker 侧


def claim_next_job(db: Session, worker_id: str) -> Job | None:
    job = db.scalars(
        select(Job)
        .where(Job.state == "queued")
        .order_by(Job.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
    ).one_or_none()
    if job is None:
        return None
    job.state = "running"
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = utcnow() + timedelta(seconds=LEASE_SECONDS)
    job.updated_at = utcnow()
    job.error = None
    db.commit()
    db.refresh(job)
    return job


def renew_lease(db: Session, job_id: uuid.UUID, attempt: int, lease_token: uuid.UUID) -> bool:
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(
            Job.id == job_id,
            Job.state == "running",
            Job.attempt == attempt,
            Job.lease_token == lease_token,
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at > now,
        )
        .values(lease_expires_at=now + timedelta(seconds=LEASE_SECONDS), updated_at=now)
    )
    db.commit()
    return bool(result.rowcount)


def fence_job(db: Session, job_id: uuid.UUID, attempt: int, lease_token: uuid.UUID) -> Job | None:
    job = db.scalars(select(Job).where(Job.id == job_id).with_for_update()).one_or_none()
    if job is None:
        return None
    if job.state != "running" or job.attempt != attempt:
        return None
    if job.lease_token != lease_token:
        return None
    if job.lease_expires_at is None or job.lease_expires_at <= utcnow():
        return None
    return job


def reap_expired_leases(db: Session) -> int:
    """原子回收过期租约；续租成功与回收竞争时以数据库当前条件为准。"""
    now = utcnow()
    result = db.execute(
        update(Job)
        .where(
            Job.state == "running",
            Job.lease_expires_at.is_not(None),
            Job.lease_expires_at < now,
        )
        .values(
            state="interrupted",
            error_code="JOB_INTERRUPTED",
            error=job_error(
                "JOB_INTERRUPTED",
                "任务执行中断，外部结果未知；不会自动重发外部调用",
            ),
            lease_token=None,
            lease_expires_at=None,
            result=None,
            updated_at=now,
        )
        .returning(Job.id)
    )
    reaped = len(result.scalars().all())
    db.commit()
    return reaped


def fenced_job_for_target(db: Session, job: Job) -> Job | None:
    """锁业务目标后重核 job fence；调用方必须先调用 `lock_job_target` 取得目标行锁。"""
    return fence_job(db, job.id, job.attempt, job.lease_token)


def save_work_item(
    db: Session,
    job: Job,
    *,
    slot_key: str,
    payload: dict,
    content_hash: str,
) -> None:
    """在锁目标并重核 job fence 后保存已验证槽位，旧 attempt/token 不能写入。"""
    from app.models import JobWorkItem

    lock_job_target(
        db,
        owner_id=job.owner_id,
        kind=job.kind,
        target_id=job.target_id,
        input_revision=job.input_revision,
    )
    fenced = fenced_job_for_target(db, job)
    if fenced is None:
        raise AppError("JOB_LEASE_LOST", "任务租约已失效，不能写入槽位结果")
    row = db.scalars(
        select(JobWorkItem)
        .where(JobWorkItem.job_id == job.id, JobWorkItem.slot_key == slot_key)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if row is None:
        db.add(
            JobWorkItem(
                job_id=job.id,
                slot_key=slot_key,
                payload=payload,
                content_hash=content_hash,
                validated=True,
            )
        )
    else:
        row.payload = payload
        row.content_hash = content_hash
        row.validated = True
    db.flush()
