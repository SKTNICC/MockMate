"""PostgreSQL 持久任务：创建、领取、租约续期、fencing 提交、失败、重试、回收。"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.diagnostics import (
    LEGACY_PARSE_SCHEMA_MESSAGE,
    PARSE_SCHEMA_ROOT_FIELDS,
    RECOVERABLE_PROVIDER_OUTPUT_REASONS,
    RETRYABLE_PARSE_AI_REASONS,
    RETRYABLE_PARSE_EVIDENCE_REASONS,
    is_legacy_parse_schema_detail,
    is_recoverable_parse_error,
    normalize_job_error_envelope,
    parse_detail_kv,
    resolve_job_error_envelope,
    sanitize_error_details_for_api,
    sanitize_job_error_code,
    sanitize_job_error_message,
)
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
    "SPEECH_PROTOCOL_ERROR",
}
VALID_GENERATED_SLOTS = {"generated-1", "generated-2", "generated-3"}
RETRYABLE_PLAN_FORMAT_REASONS = set(RECOVERABLE_PROVIDER_OUTPUT_REASONS)
RETRYABLE_PLAN_REVIEW_REASONS = set(RECOVERABLE_PROVIDER_OUTPUT_REASONS) | {
    "invalid_review_schema",
}
LEGACY_PLAN_SLOT_FAILURE_MESSAGE = "生成槽位在有限纠正后仍不合法"
LEGACY_PLAN_INVALID_JSON_DETAIL = "AI 返回内容不是合法 JSON"
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
    "AUDIO_CONSENT_REQUIRED": "未确认录音外发",
    "SPEECH_NOT_CONFIGURED": "语音服务未配置",
    "SPEECH_INPUT_INVALID": "语音内容不是受支持的音频格式",
    "SPEECH_INPUT_TOO_LARGE": "语音文件超过供应商限制",
    "SPEECH_TRANSCRIPT_TOO_LONG": "转写文本过长",
    "SPEECH_CONTENT_REJECTED": "播报文本未通过语音服务审核",
    "SPEECH_PROTOCOL_ERROR": "语音服务未返回完整结果",
    "AUDIO_FORMAT_INVALID": "录音格式不符合固定协议",
    "AUDIO_TOO_LARGE": "录音超过 120 秒上限",
    "AUDIO_HASH_MISMATCH": "录音摘要不一致",
    "AUDIO_TAKE_CONFLICT": "录音 take 冲突",
    "AUDIO_NOT_ACTIVE": "录音已不是当前 take",
    "AUDIO_EXPIRED": "录音已过期",
    "AUDIO_FILE_UNAVAILABLE": "录音文件不可用",
    "VOICE_REVISION_CONFLICT": "语音草稿版本已变化",
    "TRANSCRIPT_NOT_FOUND": "尚未创建转写任务",
    "TRANSCRIPT_NOT_READY": "转写尚未完成",
    "TRANSCRIPT_REVISION_CONFLICT": "转写版本已变化",
    "ANSWER_SOURCE_INVALID": "回答来源与录音不一致",
    "SPEECH_NOT_READY": "语音尚未生成完成",
    "SPEECH_INTEGRITY_FAILED": "语音文件校验失败",
    "SPEECH_NOT_AVAILABLE": "没有可播报内容",
    "SPEECH_SEGMENT_INVALID": "播报片段不合法",
    "INTERVIEW_MODE_TEXT": "该场是文字面试",
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
    if kind in ("follow_up", "interview_report"):
        from app.models import InterviewSession

        row = db.scalars(
            select(InterviewSession)
            .where(InterviewSession.id == target_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.owner_id != owner_id:
            raise AppError("NOT_FOUND", "面试不存在", status_code=404)
        if input_revision is not None:
            current = (
                row.follow_up_input_revision
                if kind == "follow_up"
                else row.ended_revision
            )
            if current != input_revision:
                raise AppError(
                    "REVISION_CONFLICT",
                    "面试任务输入已变化，请刷新后重试",
                    details=[f"current_revision={current}"],
                )
        return row
    if kind in ("audio_asr", "audio_tts"):
        from app.models import (
            AudioAsset,
            InterviewSession,
            InterviewTurn,
            SpeechAsset,
            VoiceDraft,
        )

        if kind == "audio_asr":
            probe = db.get(AudioAsset, target_id)
            if probe is None or probe.owner_id != owner_id:
                raise AppError("NOT_FOUND", "录音不存在", status_code=404)
            session = db.scalars(
                select(InterviewSession)
                .where(InterviewSession.id == probe.session_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if session is None or session.owner_id != owner_id:
                raise AppError("NOT_FOUND", "录音不存在", status_code=404)
            turn = db.scalars(
                select(InterviewTurn)
                .where(InterviewTurn.id == probe.turn_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if turn is None:
                raise AppError("NOT_FOUND", "录音不存在", status_code=404)
            draft = db.scalars(
                select(VoiceDraft)
                .where(VoiceDraft.turn_id == turn.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            row = db.scalars(
                select(AudioAsset)
                .where(AudioAsset.id == target_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if row is None or row.owner_id != owner_id:
                raise AppError("NOT_FOUND", "录音不存在", status_code=404)
            if input_revision is not None and row.voice_revision != input_revision:
                raise AppError(
                    "REVISION_CONFLICT",
                    "录音草稿版本已变化，请刷新后重试",
                    details=[f"current_revision={row.voice_revision}"],
                )
            if draft is None or draft.active_take_id != row.id:
                raise AppError("AUDIO_NOT_ACTIVE", "该录音已不是当前 take")
            return row
        probe = db.get(SpeechAsset, target_id)
        if probe is None or probe.owner_id != owner_id:
            raise AppError("NOT_FOUND", "语音不存在", status_code=404)
        session = db.scalars(
            select(InterviewSession)
            .where(InterviewSession.id == probe.session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if session is None or session.owner_id != owner_id:
            raise AppError("NOT_FOUND", "语音不存在", status_code=404)
        row = db.scalars(
            select(SpeechAsset)
            .where(SpeechAsset.id == target_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.owner_id != owner_id:
            raise AppError("NOT_FOUND", "语音不存在", status_code=404)
        if input_revision is not None and input_revision != 1:
            raise AppError("REVISION_CONFLICT", "语音任务输入已变化，请刷新后重试")
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
    elif kind == "follow_up":
        target.follow_up_job_id = job.id
    elif kind == "interview_report":
        target.report_job_id = job.id
    elif kind == "audio_tts":
        target.job_id = job.id
    return job, created


def has_structured_profile(profile: object) -> bool:
    return isinstance(profile, dict) and bool(profile.get("kind"))


def inputs_current(db: Session, job: Job) -> bool:
    if job.kind in ("extract", "parse"):
        draft = db.get(MaterialDraft, job.target_id)
        if draft is None or draft.owner_id != job.owner_id:
            return False
        if draft.revision != job.input_revision:
            return False
        if job.kind == "extract" and draft.state == "parsed":
            return False
        if job.kind == "parse":
            if not isinstance(draft.text, str) or not draft.text.strip():
                return False
            if has_structured_profile(draft.profile):
                return False
            from app.models import MaterialSnapshot

            confirmed = db.scalars(
                select(MaterialSnapshot.id).where(
                    MaterialSnapshot.material_id == draft.id,
                    MaterialSnapshot.source_revision == draft.revision,
                )
            ).first()
            if confirmed is not None:
                return False
        return True
    if job.kind in ("follow_up", "interview_report"):
        from app.models import InterviewSession

        session = db.get(InterviewSession, job.target_id)
        if session is None or session.owner_id != job.owner_id:
            return False
        if job.kind == "follow_up":
            return (
                session.follow_up_job_id == job.id
                and session.follow_up_input_revision == job.input_revision
                and session.phase == "deciding_follow_up"
                and session.status in ("active", "paused")
            )
        return (
            session.report_job_id == job.id
            and session.ended_revision == job.input_revision
            and session.status == "ended"
        )
    if job.kind == "audio_asr":
        from app.models import AudioAsset, VoiceDraft

        asset = db.get(AudioAsset, job.target_id)
        if asset is None or asset.owner_id != job.owner_id:
            return False
        if asset.voice_revision != job.input_revision or asset.status != "stored":
            return False
        draft = db.scalars(
            select(VoiceDraft).where(VoiceDraft.turn_id == asset.turn_id)
        ).one_or_none()
        return draft is not None and draft.active_take_id == asset.id
    if job.kind == "audio_tts":
        from app.models import SpeechAsset

        asset = db.get(SpeechAsset, job.target_id)
        return asset is not None and asset.owner_id == job.owner_id and asset.job_id == job.id
    preparation = db.get(Preparation, job.target_id)
    if preparation is None or preparation.owner_id != job.owner_id:
        return False
    return preparation.revision == job.input_revision


def _parse_detail_kv(details: object) -> dict[str, str]:
    if not isinstance(details, list):
        return {}
    kv: dict[str, str] = {}
    for entry in details:
        if isinstance(entry, str) and "=" in entry:
            key, _, value = entry.partition("=")
            kv[key.strip()] = value.strip()
    return kv


def _controlled_job_error(job: Job) -> dict[str, Any] | None:
    """重试策略与 API 投影共用的受控错误视图。

    可信错误列与历史 JSON 冲突时，旧 JSON 的消息和详情已被排除，不能用来授予
    格式型或证据型重试；错误列缺省的真实历史任务仍按白名单内的旧记录判断。
    """
    return resolve_job_error_envelope(
        job.error,
        trusted_code=job.error_code,
        job_id=job.id,
        terminal=job.state in ("failed", "interrupted"),
    )


def _is_retryable_plan_format_failure(job: Job) -> bool:
    """仅对计划出题槽位的明确输出/JSON 格式失败开放显式重试；业务校验失败保持不可重试。"""
    err = _controlled_job_error(job) or {}
    details = err.get("details") or []
    kv = _parse_detail_kv(details)
    if (
        kv.get("stage") == "plan"
        and kv.get("slot") in VALID_GENERATED_SLOTS
        and kv.get("reason") in RETRYABLE_PLAN_FORMAT_REASONS
    ):
        return True
    return (
        err.get("message") == LEGACY_PLAN_SLOT_FAILURE_MESSAGE
        and len(details) == 2
        and details[0] in VALID_GENERATED_SLOTS
        and details[1] == LEGACY_PLAN_INVALID_JSON_DETAIL
    )


def _is_retryable_plan_review_format_failure(job: Job) -> bool:
    err = _controlled_job_error(job) or {}
    kv = _parse_detail_kv(err.get("details"))
    return (
        kv.get("stage") == "plan_review"
        and kv.get("reason") in RETRYABLE_PLAN_REVIEW_REASONS
    )


def _is_legacy_parse_schema_detail(entry: object) -> bool:
    if not isinstance(entry, str) or ":" not in entry:
        return False
    field_path, _, msg = entry.partition(":")
    field_path = field_path.strip()
    if not field_path or not msg.strip():
        return False
    root = field_path.split(".", 1)[0]
    return root in PARSE_SCHEMA_ROOT_FIELDS


def _is_retryable_parse_failure(job: Job, code: str | None) -> bool:
    err = _controlled_job_error(job) or {}
    return is_recoverable_parse_error(
        err.get("code") or code,
        err.get("details"),
        message=err.get("message"),
        allow_legacy=True,
    )


def collect_job_server_ref_ids(
    db: Session,
    job: Job,
    details: object,
) -> dict[str, set[str]] | None:
    """按任务目标读取当前服务端对象允许的引用 ID，未命中时过滤自由或伪造 ID。"""
    if not isinstance(details, list) or not details:
        return None
    if job.kind in ("extract", "parse"):
        return {
            "item_ids": set(),
            "requirement_ids": set(),
            "dimension_ids": set(),
            "question_ids": set(),
        }
    needs_snap_refs = any(
        isinstance(e, str)
        and (
            e.startswith(("item_id=", "requirement_id="))
            or ("=" not in e and ":" not in e)
        )
        for e in details
    )
    needs_bank_refs = any(
        isinstance(e, str)
        and (
            e.startswith("conflict_question_id=")
            or ("=" not in e and ":" not in e)
        )
        for e in details
    )
    needs_dim_refs = any(
        isinstance(e, str) and e.startswith("dimension_id=") for e in details
    )
    if not (needs_snap_refs or needs_bank_refs or needs_dim_refs):
        return None

    refs: dict[str, set[str]] = {
        "item_ids": set(),
        "requirement_ids": set(),
        "dimension_ids": set(),
        "question_ids": set(),
    }
    if job.kind in ("match", "plan"):
        from app.models import BankQuestion, MaterialSnapshot
        from app.services.evidence import profile_items

        if needs_snap_refs:
            prep = db.get(Preparation, job.target_id)
            if prep is not None:
                if prep.resume_snapshot_id is not None:
                    resume_snap = db.get(MaterialSnapshot, prep.resume_snapshot_id)
                    if resume_snap is not None:
                        refs["item_ids"] = set(profile_items(resume_snap.profile).keys())
                if prep.jd_snapshot_id is not None:
                    jd_snap = db.get(MaterialSnapshot, prep.jd_snapshot_id)
                    if jd_snap is not None:
                        refs["requirement_ids"] = {
                            str(item["item_id"])
                            for item in ((jd_snap.profile or {}).get("requirements") or [])
                            if isinstance(item, dict) and item.get("item_id")
                        }
        if needs_bank_refs:
            refs["question_ids"] = {
                str(qid)
                for qid in db.scalars(select(BankQuestion.question_id)).all()
                if qid
            }
        return refs

    if job.kind in ("follow_up", "interview_report"):
        from app.models import InterviewQuestion

        if needs_dim_refs:
            dim_ids: set[str] = {"expression"}
            questions = db.scalars(
                select(InterviewQuestion).where(InterviewQuestion.session_id == job.target_id)
            ).all()
            for q in questions:
                rubric = q.rubric if isinstance(q.rubric, dict) else {}
                for dim in rubric.get("dimensions") or []:
                    if isinstance(dim, dict) and dim.get("id"):
                        dim_ids.add(str(dim["id"]))
            refs["dimension_ids"] = dim_ids
        return refs

    return refs


def is_retryable(db: Session, job: Job, max_attempts: int = 2) -> bool:
    if job.state not in ("failed", "interrupted"):
        return False
    if job.attempt >= max_attempts:
        return False
    err = normalize_job_error_envelope(
        job.error, fallback_code=job.error_code, job_id=job.id
    )
    code = (
        sanitize_job_error_code(job.error_code, fallback="")
        or (err["code"] if err is not None else "")
    )
    if job.kind == "plan" and code == "AI_INVALID_OUTPUT":
        if not (
            _is_retryable_plan_format_failure(job)
            or _is_retryable_plan_review_format_failure(job)
        ):
            return False
        return inputs_current(db, job)
    if job.kind == "parse" and code in ("AI_INVALID_OUTPUT", "EVIDENCE_INVALID", "PROFILE_INVALID"):
        if not _is_retryable_parse_failure(job, code):
            return False
        return inputs_current(db, job)
    retryable_codes = RETRYABLE_JOB_ERRORS
    if job.kind in ("match", "follow_up", "interview_report"):
        # 匹配分析、追问与报告允许在模型输出结构非法后显式重试；其余 M1 任务行为保持不变。
        retryable_codes = RETRYABLE_JOB_ERRORS | {"AI_INVALID_OUTPUT"}
    if code not in retryable_codes:
        return False
    return inputs_current(db, job)


def job_to_schema(db: Session, job: Job, max_attempts: int = 2) -> JobSchema:
    retryable = is_retryable(db, job, max_attempts)
    err = resolve_job_error_envelope(
        job.error,
        trusted_code=job.error_code,
        job_id=job.id,
        terminal=job.state in ("failed", "interrupted"),
    )
    error_info = None
    if err is not None:
        err_code = err["code"]
        raw_details = err["details"]
        allowed_refs = collect_job_server_ref_ids(db, job, raw_details)
        error_info = ErrorInfo(
            code=err_code,
            message=sanitize_job_error_message(
                err["message"], err_code, JOB_ERROR_MESSAGES
            ),
            retryable=retryable,
            details=sanitize_error_details_for_api(
                raw_details,
                kind=job.kind,
                code=err_code,
                allowed_refs=allowed_refs,
            ),
            request_id=err["request_id"],
        )
    result_ref = None
    if isinstance(job.result, dict) and job.state == "succeeded":
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
