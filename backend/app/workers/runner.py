"""独立 worker 进程：领取 PostgreSQL 任务、续租、执行、fencing 提交、超时回收。"""

from __future__ import annotations

import inspect
import hashlib
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.ai.speech import get_speech_provider
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.diagnostics import (
    SAFE_REASONS,
    SAFE_RECORD_DETAIL_KEYS,
    SAFE_TOKEN_RE,
    is_recoverable_parse_error,
    safe_field_paths,
    safe_validation_details,
    sanitize_error_details_for_api,
    sanitize_error_details_for_records,
    sanitize_external_request_id,
    sanitize_usage,
)
from app.core.errors import AppError
from app.models import (
    AudioAsset,
    AudioTranscript,
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
    InterviewTurn,
    Job,
    JobWorkItem,
    MaterialDraft,
    MaterialSnapshot,
    MatchReport,
    Plan,
    Preparation,
    SpeechAsset,
    VoiceDraft,
)
from app.schemas import Anchor, DuplicateReviewOutput, MatchRow, SemanticReview
from app.services import catalog
from app.services import interviews as interviews_service
from app.services import jobs as jobs_service
from app.services import voice as voice_service
from app.services.bank import content_digest, eligible_bank_questions, eligible_fact_cards
from app.services.materials import read_file
from app.services.planning import (
    SEMANTIC_DUPLICATE_THRESHOLD,
    build_fact_cards,
    build_internal_plan,
    publish_plan,
    semantic_review_failure_details,
    stem_similarity,
    to_api_error,
)
from app.services.preparations import (
    build_generation_context,
    generation_context_hash,
    prepare_plan_inputs,
)
from app.services.text import (
    detect_jd_level_conflict,
    extract_text_from_file,
    normalized_stem,
    sanitize_profile_skill_codes,
    sanitize_question_skill_codes,
    validate_profile,
)

LOG = logging.getLogger("mockmate.worker")
# v2：槽位保存前执行本次期望类型、禁止题干与近似重复校验；旧缓存必须重新校验。
WORK_ITEM_VALIDATION_VERSION = "m1-work-item-v2"
ALLOWED_FINISH_REASONS = {
    "stop",
    "length",
    "content_filter",
    "aborted",
    "insufficient_system_resource",
    "tool_calls",
    "unknown",
}
SAFE_FIELD_PATH_RE = SAFE_TOKEN_RE


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _summarize_generated_question(slot_key: str, question: dict) -> dict:
    anchor_refs: list[str] = []
    for anchor in question.get("anchors") or []:
        if isinstance(anchor, dict) and anchor.get("snapshot_id") and anchor.get("item_id"):
            ref = f"{anchor['snapshot_id']}/{anchor['item_id']}"
            if ref not in anchor_refs:
                anchor_refs.append(ref)
    return {
        "slot": slot_key,
        "question_type": str(question.get("question_type") or ""),
        "focus": str(question.get("focus") or "")[:60],
        "goal": str((question.get("evaluation") or {}).get("goal") or "")[:80],
        "topic_key": str(question.get("topic_key") or ""),
        "anchor_refs": anchor_refs,
    }


class Heartbeat:
    def __init__(self, job_id: uuid.UUID, attempt: int, lease_token: uuid.UUID) -> None:
        self._job_id = job_id
        self._attempt = attempt
        self._token = lease_token
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "Heartbeat":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.wait(jobs_service.HEARTBEAT_SECONDS):
            db = SessionLocal()
            try:
                jobs_service.renew_lease(db, self._job_id, self._attempt, self._token)
            except Exception:  # pragma: no cover - 租约续期失败让任务自然过期
                LOG.warning("lease renew failed job=%s", self._job_id)
            finally:
                db.close()
            if self._stop.is_set():
                return


class JobWorker:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.worker_id = f"worker-{os.getpid()}-{uuid.uuid4().hex[:8]}"

    # -- 主循环
    def run_forever(self, poll_interval: float = 1.0) -> None:  # pragma: no cover - 长驻进程
        LOG.info("worker started id=%s", self.worker_id)
        last_reap = 0.0
        last_cleanup = 0.0
        while True:
            try:
                if time.monotonic() - last_reap > 15:
                    last_reap = time.monotonic()
                    db = SessionLocal()
                    try:
                        jobs_service.reap_expired_leases(db)
                    finally:
                        db.close()
                if time.monotonic() - last_cleanup > 900:
                    last_cleanup = time.monotonic()
                    db = SessionLocal()
                    try:
                        voice_service.cleanup_expired(db, self.settings)
                        db.commit()
                    except Exception:
                        db.rollback()
                        LOG.exception("audio cleanup failed")
                    finally:
                        db.close()
                processed = self.run_once()
                if not processed:
                    time.sleep(poll_interval)
            except KeyboardInterrupt:
                LOG.info("worker interrupted")
                return
            except Exception:  # pragma: no cover
                LOG.exception("worker loop error")
                time.sleep(2)

    def run_once(self) -> bool:
        db = SessionLocal()
        try:
            job = jobs_service.claim_next_job(db, self.worker_id)
        finally:
            db.close()
        if job is None:
            return False
        self._process(job)
        return True

    # -- 单任务
    def _process(self, job: Job) -> None:
        LOG.info("job claimed id=%s kind=%s attempt=%s", job.id, job.kind, job.attempt)
        with Heartbeat(job.id, job.attempt, job.lease_token):
            try:
                handler = {
                    "extract": self._handle_extract,
                    "parse": self._handle_parse,
                    "match": self._handle_match,
                    "plan": self._handle_plan,
                    "follow_up": self._handle_follow_up,
                    "interview_report": self._handle_interview_report,
                    "audio_asr": self._handle_audio_asr,
                    "audio_tts": self._handle_audio_tts,
                }[job.kind]
                handler(job)
            except AppError as exc:
                api_error = to_api_error(exc)
                self._fail(job, api_error.code, api_error.message, api_error.details)
            except Exception as exc:  # pragma: no cover - 兜底，不泄露内部细节
                # 只记录 job 标识与异常类型；不把模型原文或材料正文写入日志。
                LOG.error(
                    "job failed id=%s error_type=%s", job.id, type(exc).__name__
                )
                self._fail(job, "INTERNAL_ERROR", "任务执行失败", [])

    def _fence(self, db: Session, job: Job) -> Job | None:
        return jobs_service.fence_job(db, job.id, job.attempt, job.lease_token)

    @staticmethod
    def _ai_outcome(code: str | None) -> str:
        return {
            "AI_TIMEOUT": "timeout",
            "AI_UNAVAILABLE": "unavailable",
            "AI_RATE_LIMITED": "rate_limited",
            "AI_AUTH_FAILED": "auth_failed",
            "AI_REQUEST_REJECTED": "request_rejected",
            "AI_INVALID_OUTPUT": "known_invalid",
            "SPEECH_PROTOCOL_ERROR": "known_invalid",
            "SPEECH_CONTENT_REJECTED": "request_rejected",
            "SPEECH_INPUT_INVALID": "request_rejected",
        }.get(code or "", "unknown")

    @staticmethod
    def _safe_error_reason(details: list[str] | None) -> str | None:
        for entry in details or []:
            if isinstance(entry, str) and entry.startswith("reason="):
                val = entry.partition("=")[2].strip()
                if val in SAFE_REASONS:
                    return val
        return None

    @staticmethod
    def _safe_field_paths(details: list[str] | None) -> list[str]:
        return safe_field_paths(details)

    @staticmethod
    def _safe_feedback_details(
        details: list[str] | None,
        *,
        kind: str | None = None,
        allowed_refs: dict[str, set[str]] | None = None,
    ) -> list[str]:
        return sanitize_error_details_for_records(
            details, kind=kind, allowed_refs=allowed_refs
        )

    def _append_ai_record(
        self,
        job: Job,
        provider,
        *,
        stage: str,
        slot: str | None,
        outcome: str,
        error_code: str | None = None,
        error_details: list[str] | None = None,
        is_correction: bool = False,
        correction_attempt: int = 1,
    ) -> None:
        last = getattr(provider, "last_call", None) or {}
        usage = sanitize_usage(last.get("usage"))
        request_id = sanitize_external_request_id(last.get("request_id"))
        raw_finish = last.get("finish_reason")
        stripped_finish = (
            raw_finish.strip()
            if isinstance(raw_finish, str) and raw_finish.strip()
            else "unknown"
        )
        finish_reason = (
            stripped_finish if stripped_finish in ALLOWED_FINISH_REASONS else "other"
        )
        error_reason = self._safe_error_reason(error_details)
        field_paths = self._safe_field_paths(error_details)
        thinking_fallback = bool(last.get("thinking_fallback", False))
        http_requests = int(last.get("http_requests") or 1)
        record = {
            "job_id": str(job.id),
            "stage": stage,
            "slot": slot,
            "attempt": job.attempt,
            "correction_attempt": correction_attempt,
            "is_correction": bool(is_correction),
            "thinking_fallback": thinking_fallback,
            "http_requests": http_requests,
            "provider": getattr(provider, "name", "unknown"),
            "model": getattr(provider, "model_id", ""),
            "request_id": request_id,
            "usage": usage,
            "finish_reason": finish_reason,
            "outcome": outcome,
            "result_known": outcome in {"success", "known_invalid", "auth_failed", "request_rejected"},
            "error_code": error_code,
            "error_reason": error_reason,
            "field_paths": field_paths,
        }
        LOG.info(
            "ai_call job=%s attempt=%s stage=%s slot=%s correction=%s fallback=%s finish_reason=%s outcome=%s error_code=%s error_reason=%s",
            job.id,
            job.attempt,
            stage,
            slot or "-",
            is_correction,
            thinking_fallback,
            finish_reason,
            outcome,
            error_code or "-",
            error_reason or "-",
        )
        db = SessionLocal()
        try:
            fenced = self._fence(db, job)
            if fenced is None:
                return
            fenced.ai_call_records = [*(fenced.ai_call_records or []), record]
            db.commit()
        except Exception as exc:  # pragma: no cover - 记录失败不能掩盖主错误
            db.rollback()
            LOG.error("ai record failed id=%s error_type=%s", job.id, type(exc).__name__)
        finally:
            db.close()

    def _call_ai(
        self,
        job: Job,
        provider,
        *,
        stage: str,
        slot: str | None,
        func,
        is_correction: bool = False,
        correction_attempt: int = 1,
        **kwargs,
    ):
        try:
            result = func(**kwargs)
        except AppError as exc:
            self._append_ai_record(
                job,
                provider,
                stage=stage,
                slot=slot,
                outcome=self._ai_outcome(exc.code),
                error_code=exc.code,
                error_details=exc.details,
                is_correction=is_correction,
                correction_attempt=correction_attempt,
            )
            raise
        except Exception:
            self._append_ai_record(
                job,
                provider,
                stage=stage,
                slot=slot,
                outcome="unknown",
                error_code="INTERNAL_ERROR",
                is_correction=is_correction,
                correction_attempt=correction_attempt,
            )
            raise
        self._append_ai_record(
            job,
            provider,
            stage=stage,
            slot=slot,
            outcome="success",
            is_correction=is_correction,
            correction_attempt=correction_attempt,
        )
        return result

    def _annotate_last_ai_record(
        self,
        job: Job,
        *,
        stage: str,
        slot: str | None,
        validation_outcome: str,
        validation_details: list[str] | None = None,
    ) -> None:
        safe_details = sanitize_error_details_for_records(
            validation_details, kind=job.kind
        )
        LOG.info(
            "ai_validation job=%s attempt=%s stage=%s slot=%s validation_outcome=%s details=%s",
            job.id,
            job.attempt,
            stage,
            slot or "-",
            validation_outcome,
            ",".join(safe_details) or "-",
        )
        db = SessionLocal()
        try:
            fenced = self._fence(db, job)
            if fenced is None:
                return
            records = list(fenced.ai_call_records or [])
            for idx in range(len(records) - 1, -1, -1):
                rec = records[idx]
                if (
                    isinstance(rec, dict)
                    and rec.get("stage") == stage
                    and rec.get("slot") == slot
                    and rec.get("attempt") == job.attempt
                ):
                    updated = dict(rec)
                    updated["validation_outcome"] = validation_outcome
                    if safe_details:
                        updated["validation_details"] = list(safe_details)
                    records[idx] = updated
                    fenced.ai_call_records = records
                    db.commit()
                    return
        except Exception as exc:  # pragma: no cover - 诊断附注失败不能掩盖主错误
            db.rollback()
            LOG.error("ai record annotate failed id=%s error_type=%s", job.id, type(exc).__name__)
        finally:
            db.close()

    def _fail(
        self, job: Job, code: str, message: str, details: list[str] | None = None
    ) -> None:
        db = SessionLocal()
        try:
            fenced = self._fence(db, job)
            if fenced is None:
                LOG.info("skip fail: fence lost id=%s", job.id)
                return
            safe_details = sanitize_error_details_for_api(
                details or [],
                kind=job.kind,
                code=code,
                allowed_refs=jobs_service.collect_job_server_ref_ids(
                    db, fenced, details or []
                ),
            )
            jobs_service.mark_failed(fenced, code, message, safe_details)
            db.commit()
        except Exception as exc:  # pragma: no cover - 错误处理自身不能再抛出
            db.rollback()
            LOG.error(
                "fail handler error id=%s error_type=%s", job.id, type(exc).__name__
            )
        finally:
            db.close()

    # -- extract
    def _handle_extract(self, job: Job) -> None:
        db = SessionLocal()
        try:
            draft = db.get(MaterialDraft, job.target_id)
            if draft is None or draft.owner_id != job.owner_id or draft.revision != job.input_revision:
                raise AppError("JOB_INPUT_CONFLICT", "材料版本已变化")
            extension = os.path.splitext(draft.file_name or "")[1]
            data = read_file(self.settings, draft)
        finally:
            db.close()
        text = extract_text_from_file(data, extension)

        db = SessionLocal()
        try:
            # 锁顺序：先材料，再 Job；锁后重读 revision，不能用先前读取的对象提交。
            draft = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            if draft.revision != job.input_revision:
                jobs_service.supersede_job(fenced)
            else:
                draft.text = text
                draft.profile = None
                draft.state = "text_ready"
                draft.updated_at = _now()
                jobs_service.mark_succeeded(fenced, entity_type="material", entity_id=draft.id)
            db.commit()
        finally:
            db.close()

    @staticmethod
    def _provider_supports_parse_correction(provider) -> bool:
        try:
            sig = inspect.signature(provider.parse_material)
        except (TypeError, ValueError):
            return False
        if "correction" in sig.parameters:
            return True
        return any(
            param.kind == inspect.Parameter.VAR_KEYWORD
            for param in sig.parameters.values()
        )

    @staticmethod
    def _ensure_stage_details(
        details: list[str], *, stage: str, default_reason: str | None = None
    ) -> list[str]:
        merged = [f"stage={stage}"]
        if default_reason and not any(
            isinstance(item, str) and item.startswith("reason=") for item in details
        ):
            merged.append(f"reason={default_reason}")
        for entry in details:
            if not isinstance(entry, str) or entry.startswith("stage="):
                continue
            if entry not in merged:
                merged.append(entry)
        return merged

    # -- parse
    def _handle_parse(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            draft = db.get(MaterialDraft, job.target_id)
            if draft is None or draft.owner_id != job.owner_id or draft.revision != job.input_revision:
                raise AppError("JOB_INPUT_CONFLICT", "材料版本已变化")
            if not draft.text or not draft.text.strip():
                raise AppError("FILE_TEXT_UNAVAILABLE", "材料没有可解析文本")
            text = draft.text
            kind = draft.kind
            role_id = job.input_payload.get("role_id")
            level = job.input_payload.get("level")
            enabled_skills = catalog.list_skill_codes(db)
            skill_catalog = [
                {"code": item.code, "label": item.label} for item in enabled_skills
            ]
            allowed_skill_codes = {item.code for item in enabled_skills}
        finally:
            db.close()

        if kind == "jd":
            conflicts = detect_jd_level_conflict(text, level)
            if conflicts:
                raise AppError(
                    "JD_LEVEL_CONFLICT",
                    "JD 原文明确要求资深或多年经验，不能自动改写为实习/初级；请修改 JD 或手动核对结构",
                    details=conflicts,
                )

        supports_correction = self._provider_supports_parse_correction(provider)
        profile: dict | None = None
        removed_tags = 0
        last_error: AppError | None = None
        for attempt in range(2):
            call_kwargs: dict = {
                "text": text,
                "kind": kind,
                "role_id": role_id,
                "level": level,
                "skill_catalog": skill_catalog,
            }
            if attempt > 0:
                if not supports_correction or last_error is None:
                    break
                call_kwargs["correction"] = {
                    "attempt": attempt + 1,
                    "error_code": last_error.code,
                    "reason": self._safe_error_reason(last_error.details) or "invalid_output",
                    "details": self._safe_feedback_details(last_error.details, kind="parse"),
                }
            try:
                candidate = self._call_ai(
                    job,
                    provider,
                    stage="parse",
                    slot=None,
                    func=provider.parse_material,
                    is_correction=(attempt > 0),
                    correction_attempt=attempt + 1,
                    **call_kwargs,
                )
            except AppError as exc:
                if exc.code == "AI_INVALID_OUTPUT":
                    last_error = AppError(
                        exc.code,
                        exc.message,
                        details=self._ensure_stage_details(
                            exc.details,
                            stage="parse",
                            default_reason="invalid_profile_schema",
                        ),
                    )
                    if attempt == 0 and supports_correction:
                        continue
                    raise last_error
                raise

            try:
                removed_tags = sanitize_profile_skill_codes(candidate, allowed_skill_codes)
                validate_profile(
                    candidate,
                    kind,
                    text,
                    allow_user_confirmed=False,
                    allowed_skill_codes=allowed_skill_codes,
                )
                if kind == "jd":
                    with SessionLocal() as check_db:
                        try:
                            catalog.ensure_jd_profile_compatible(check_db, candidate)
                        except AppError as exc:
                            if exc.code == "ROLE_LEVEL_MISMATCH":
                                raise AppError(
                                    "AI_INVALID_OUTPUT",
                                    "JD 解析出的岗位或级别不在服务端目录中",
                                    details=[
                                        "stage=parse",
                                        "reason=invalid_jd_catalog_role",
                                        "field=role_id",
                                        "source=provider_output",
                                    ],
                                ) from exc
                            raise
                self._annotate_last_ai_record(
                    job,
                    stage="parse",
                    slot=None,
                    validation_outcome="passed",
                )
                profile = candidate
                break
            except AppError as exc:
                self._annotate_last_ai_record(
                    job,
                    stage="parse",
                    slot=None,
                    validation_outcome="rejected",
                    validation_details=self._validation_record_details(exc),
                )
                can_correct = (
                    attempt == 0
                    and supports_correction
                    and is_recoverable_parse_error(exc.code, self._ensure_stage_details(exc.details, stage="parse"))
                )
                enriched = AppError(
                    exc.code,
                    exc.message,
                    details=self._ensure_stage_details(exc.details, stage="parse"),
                )
                if can_correct:
                    last_error = enriched
                    continue
                raise enriched

        if profile is None:
            assert last_error is not None
            raise last_error

        db = SessionLocal()
        try:
            draft = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            from app.models import MaterialSnapshot

            confirmed = db.scalars(
                select(MaterialSnapshot.id).where(
                    MaterialSnapshot.material_id == draft.id,
                    MaterialSnapshot.source_revision == draft.revision,
                )
            ).first()
            if (
                draft.revision != job.input_revision
                or jobs_service.has_structured_profile(draft.profile)
                or confirmed is not None
            ):
                jobs_service.supersede_job(fenced)
            else:
                latest_allowed = catalog.allowed_skill_codes(db)
                removed_tags += sanitize_profile_skill_codes(profile, latest_allowed)
                validate_profile(
                    profile,
                    kind,
                    text,
                    allow_user_confirmed=False,
                    allowed_skill_codes=latest_allowed,
                )
                if kind == "jd":
                    try:
                        catalog.ensure_jd_profile_compatible(db, profile)
                    except AppError as exc:
                        if exc.code == "ROLE_LEVEL_MISMATCH":
                            raise AppError(
                                "AI_INVALID_OUTPUT",
                                "JD 解析出的岗位或级别不在服务端目录中",
                                details=[
                                    "stage=parse",
                                    "reason=invalid_jd_catalog_role",
                                    "field=role_id",
                                    "source=provider_output",
                                ],
                            ) from exc
                        raise
                if removed_tags > 0:
                    LOG.info(
                        "parse sanitized skill tags job=%s removed=%d",
                        job.id,
                        removed_tags,
                    )
                draft.profile = profile
                draft.state = "parsed"
                draft.updated_at = _now()
                jobs_service.mark_succeeded(fenced, entity_type="material", entity_id=draft.id)
                LOG.info(
                    "job published id=%s kind=parse attempt=%s target=%s revision=%s",
                    job.id,
                    job.attempt,
                    draft.id,
                    draft.revision,
                )
            db.commit()
        finally:
            db.close()

    # -- match
    def _handle_match(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            prep = db.get(Preparation, job.target_id)
            if prep is None or prep.owner_id != job.owner_id:
                raise AppError("NOT_FOUND", "准备已删除")
            resume = db.get(MaterialSnapshot, prep.resume_snapshot_id)
            jd = db.get(MaterialSnapshot, prep.jd_snapshot_id)
            if resume is None or jd is None:
                raise AppError("MATERIALS_REQUIRED", "材料版本已失效")
        finally:
            db.close()

        rows = self._call_ai(
            job,
            provider,
            stage="match",
            slot=None,
            func=provider.match_requirements,
            resume_snapshot=resume,
            jd_snapshot=jd,
        )
        try:
            validated = self._validate_match_rows(rows, resume, jd)
            self._annotate_last_ai_record(
                job,
                stage="match",
                slot=None,
                validation_outcome="passed",
            )
        except AppError as exc:
            self._annotate_last_ai_record(
                job,
                stage="match",
                slot=None,
                validation_outcome="rejected",
                validation_details=self._validation_record_details(exc),
            )
            raise

        db = SessionLocal()
        try:
            # 锁顺序：先 Preparation，再 Job。
            prep = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            current_resume = db.get(MaterialSnapshot, prep.resume_snapshot_id)
            current_jd = db.get(MaterialSnapshot, prep.jd_snapshot_id)
            if (
                prep.revision != job.input_revision
                or current_resume is None
                or current_jd is None
                or current_resume.id != resume.id
                or current_jd.id != jd.id
            ):
                jobs_service.supersede_job(fenced)
                db.commit()
                return
            report = db.scalars(
                select(MatchReport)
                .where(
                    MatchReport.preparation_id == prep.id,
                    MatchReport.input_revision == prep.revision,
                )
                .with_for_update()
            ).one_or_none()
            if report is None:
                report = MatchReport(
                    preparation_id=prep.id,
                    input_revision=prep.revision,
                    resume_snapshot_id=resume.id,
                    jd_snapshot_id=jd.id,
                    payload={"rows": validated},
                )
                db.add(report)
                db.flush()
            prep.match_report_id = report.id
            prep.updated_at = _now()
            jobs_service.mark_succeeded(fenced, entity_type="match_report", entity_id=report.id)
            db.commit()
        finally:
            db.close()

    def _validate_match_rows(self, rows: list[dict], resume: MaterialSnapshot, jd: MaterialSnapshot) -> list[dict]:
        from app.services.evidence import validate_match_rows

        return validate_match_rows(rows, resume, jd)

    # -- plan
    def _handle_plan(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            prep = db.get(Preparation, job.target_id)
            if prep is None or prep.owner_id != job.owner_id:
                raise AppError("NOT_FOUND", "准备已删除")
            resume, jd, report, bank_sources, fact_cards, fact_reviews, test_only = prepare_plan_inputs(
                db, self.settings, prep
            )
            half = prep.main_question_count // 2
            all_bank = eligible_bank_questions(db, self.settings, prep.role_id, prep.level)
            all_fact_cards = eligible_fact_cards(db, self.settings)
            allowed_skill_codes = catalog.allowed_skill_codes(db)
            worked = {
                item.slot_key: item.payload
                for item in db.scalars(
                    select(JobWorkItem).where(
                        JobWorkItem.job_id == job.id, JobWorkItem.validated.is_(True)
                    )
                ).all()
            }
            rubric = (bank_sources[0].content.get("evaluation") or {}).get("rubric")
        finally:
            db.close()

        bank_forbidden = [
            {
                "stem": item.content.get("stem", ""),
                "source": "bank",
                "ref": item.question.question_id,
            }
            for item in all_bank
            if item.content.get("stem")
        ]
        generated: list[dict] = []
        generated_summaries: list[dict] = []
        generated_forbidden: list[dict] = []
        for index in range(1, half + 1):
            slot = f"generated-{index}"
            question_type = "knowledge" if index % 2 == 0 and all_fact_cards else "experience"
            context = build_generation_context(
                slot_key=slot,
                slot_index=index,
                question_type=question_type,
                resume=resume,
                jd=jd,
                report=report,
                preparation=prep,
                bank_sources=bank_sources,
                all_bank=all_bank,
                fact_cards=all_fact_cards,
                rubric=rubric,
                previous_stems=[item["stem"] for item in generated],
                previous_summaries=generated_summaries,
            )
            context_hash = generation_context_hash(context)
            allowed_ids = {card["id"] for card in context["fact_cards"]}
            forbidden_entries = [*generated_forbidden, *bank_forbidden]

            reused = False
            cached = worked.get(slot)
            if (
                isinstance(cached, dict)
                and cached.get("validation_version") == WORK_ITEM_VALIDATION_VERSION
                and isinstance(cached.get("question"), dict)
            ):
                saved_hash = cached.get("context_hash")
                hash_matches = saved_hash == context_hash
                # 兼容新增 previous_summaries 与槽位差异化字段前写入的旧槽位缓存键；仍需在下方重新校验题目本身。
                if (
                    isinstance(saved_hash, str)
                    and not hash_matches
                    and "previous_summaries" in context
                ):
                    legacy_context = dict(context)
                    del legacy_context["previous_summaries"]
                    hash_matches = saved_hash == generation_context_hash(legacy_context)
                if isinstance(saved_hash, str) and not hash_matches:
                    for dropped in (
                        {"bank_summaries", "slot_angle", "preferred_anchor_ids"},
                        {"previous_summaries", "bank_summaries", "slot_angle", "preferred_anchor_ids"},
                    ):
                        legacy_v1_context = {
                            k: v for k, v in context.items() if k not in dropped
                        }
                        if saved_hash == generation_context_hash(legacy_v1_context):
                            hash_matches = True
                            break
                if hash_matches:
                    try:
                        candidate_cached = dict(cached["question"])
                        sanitize_question_skill_codes(candidate_cached, allowed_skill_codes)
                        self._validate_generated(
                            candidate_cached,
                            resume,
                            jd,
                            allowed_fact_card_ids=allowed_ids,
                            expected_rubric=rubric,
                            expected_question_type=context.get("question_type"),
                            forbidden_stems=context.get("avoid_stems", []),
                            forbidden_entries=forbidden_entries,
                            allowed_anchors=context.get("allowed_anchors"),
                            allowed_skill_codes=allowed_skill_codes,
                        )
                        generated.append(candidate_cached)
                        reused = True
                    except (AppError, ValidationError):
                        reused = False

            if not reused:
                question = self._generate_slot(
                    job,
                    provider,
                    context,
                    resume,
                    jd,
                    rubric,
                    allowed_ids,
                    forbidden_entries=forbidden_entries,
                    allowed_skill_codes=allowed_skill_codes,
                )
                generated.append(question)
                package = {
                    "question": question,
                    "context_hash": context_hash,
                    "validation_version": WORK_ITEM_VALIDATION_VERSION,
                }
                if not self._save_slot(job, slot, package, content_digest(question)):
                    return
            else:
                question = generated[-1]
            generated_summaries.append(_summarize_generated_question(slot, question))
            generated_forbidden.append(
                {"stem": question["stem"], "source": "prior_generated", "ref": slot}
            )

        base_test_only = test_only
        fact_cards, fact_reviews, effective_test_only, plan, review_questions = (
            self._assemble_plan_and_review_payload(
                prep=prep,
                resume=resume,
                jd=jd,
                bank_sources=bank_sources,
                generated=generated,
                provider=provider,
                base_test_only=base_test_only,
            )
        )
        semantic = self._run_semantic_review(
            job=job,
            provider=provider,
            plan=plan,
            review_questions=review_questions,
            resume=resume,
            jd=jd,
            fact_cards=fact_cards,
            is_correction=False,
            correction_attempt=1,
        )

        # 仅当语义复核仅报告重复对且无不确定性或依据问题时，执行至多一轮定向重生成 + 复核。
        if (
            semantic["duplicate_pairs"]
            and not semantic["grounding_issues"]
            and not semantic["uncertain"]
        ):
            repair_targets = self._select_semantic_duplicate_repair_targets(
                plan, semantic["duplicate_pairs"]
            )
            if repair_targets:
                rejected_stems = {
                    idx: generated[idx - 1]["stem"] for idx in repair_targets
                }
                repair_succeeded = True
                for idx in sorted(repair_targets):
                    slot = f"generated-{idx}"
                    question_type = (
                        "knowledge" if idx % 2 == 0 and all_fact_cards else "experience"
                    )
                    active_others = [
                        (j, generated[j - 1])
                        for j in range(1, half + 1)
                        if j != idx and (j not in repair_targets or j < idx)
                    ]
                    other_stems = [q["stem"] for _, q in active_others] + list(
                        rejected_stems.values()
                    )
                    other_summaries = [
                        _summarize_generated_question(f"generated-{j}", q)
                        for j, q in active_others
                    ]
                    repair_context = build_generation_context(
                        slot_key=slot,
                        slot_index=idx,
                        question_type=question_type,
                        resume=resume,
                        jd=jd,
                        report=report,
                        preparation=prep,
                        bank_sources=bank_sources,
                        all_bank=all_bank,
                        fact_cards=all_fact_cards,
                        rubric=rubric,
                        previous_stems=other_stems,
                        previous_summaries=other_summaries,
                    )
                    repair_allowed_ids = {
                        card["id"] for card in repair_context["fact_cards"]
                    }
                    repair_forbidden_entries = (
                        [
                            {
                                "stem": q["stem"],
                                "source": "prior_generated",
                                "ref": f"generated-{j}",
                            }
                            for j, q in active_others
                        ]
                        + [
                            {
                                "stem": r_stem,
                                "source": "prior_generated",
                                "ref": f"generated-{r_idx}",
                            }
                            for r_idx, r_stem in rejected_stems.items()
                        ]
                        + bank_forbidden
                    )
                    target_info = repair_targets[idx]
                    corr_details = [
                        "stage=plan_review",
                        f"slot={slot}",
                        "reason=semantic_duplicate",
                        f"conflict_source={target_info['conflict_source']}",
                    ]
                    if target_info.get("conflict_slot"):
                        corr_details.append(
                            f"conflict_slot={target_info['conflict_slot']}"
                        )
                    if target_info.get("conflict_question_id"):
                        corr_details.append(
                            f"conflict_question_id={target_info['conflict_question_id']}"
                        )
                    initial_correction = {
                        "message": "语义去重复核发现重复题目",
                        "details": corr_details,
                    }
                    try:
                        repaired_question = self._generate_slot(
                            job,
                            provider,
                            repair_context,
                            resume,
                            jd,
                            rubric,
                            repair_allowed_ids,
                            forbidden_entries=repair_forbidden_entries,
                            allowed_skill_codes=allowed_skill_codes,
                            initial_correction=initial_correction,
                        )
                    except AppError as exc:
                        if (
                            exc.code == "AI_INVALID_OUTPUT"
                            and "reason=duplicate_stem" in exc.details
                        ):
                            repair_succeeded = False
                            break
                        raise
                    generated[idx - 1] = repaired_question

                if repair_succeeded:
                    for persist_idx in range(min(repair_targets), half + 1):
                        persist_slot = f"generated-{persist_idx}"
                        persist_type = (
                            "knowledge"
                            if persist_idx % 2 == 0 and all_fact_cards
                            else "experience"
                        )
                        persist_ctx = build_generation_context(
                            slot_key=persist_slot,
                            slot_index=persist_idx,
                            question_type=persist_type,
                            resume=resume,
                            jd=jd,
                            report=report,
                            preparation=prep,
                            bank_sources=bank_sources,
                            all_bank=all_bank,
                            fact_cards=all_fact_cards,
                            rubric=rubric,
                            previous_stems=[
                                generated[k]["stem"] for k in range(persist_idx - 1)
                            ],
                            previous_summaries=[
                                _summarize_generated_question(
                                    f"generated-{k + 1}", generated[k]
                                )
                                for k in range(persist_idx - 1)
                            ],
                        )
                        persist_package = {
                            "question": generated[persist_idx - 1],
                            "context_hash": generation_context_hash(persist_ctx),
                            "validation_version": WORK_ITEM_VALIDATION_VERSION,
                        }
                        if not self._save_slot(
                            job,
                            persist_slot,
                            persist_package,
                            content_digest(generated[persist_idx - 1]),
                        ):
                            return

                    fact_cards, fact_reviews, effective_test_only, plan, review_questions = (
                        self._assemble_plan_and_review_payload(
                            prep=prep,
                            resume=resume,
                            jd=jd,
                            bank_sources=bank_sources,
                            generated=generated,
                            provider=provider,
                            base_test_only=base_test_only,
                        )
                    )
                    self._run_semantic_review(
                        job=job,
                        provider=provider,
                        plan=plan,
                        review_questions=review_questions,
                        resume=resume,
                        jd=jd,
                        fact_cards=fact_cards,
                        is_correction=True,
                        correction_attempt=2,
                    )

        db = SessionLocal()
        try:
            # 锁顺序：先 Preparation，再 Job；发布事务内重核当前输入和内容准入。
            prep = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            if prep.revision != job.input_revision:
                jobs_service.supersede_job(fenced)
                db.commit()
                return
            if prep.match_report_id is None or str(prep.match_report_id) != job.input_payload.get(
                "match_report_id"
            ):
                jobs_service.supersede_job(fenced)
                db.commit()
                return

            current = prepare_plan_inputs(db, self.settings, prep)
            current_resume, current_jd, current_report = current[0], current[1], current[2]
            current_bank = current[3]
            if (
                current_resume.id != resume.id
                or current_jd.id != jd.id
                or current_report.id != report.id
            ):
                jobs_service.supersede_job(fenced)
                db.commit()
                return
            current_bank_keys = {
                (item.question.question_id, item.question.version, content_digest(item.content))
                for item in current_bank
            }
            for used in bank_sources:
                key = (
                    used.question.question_id,
                    used.question.version,
                    content_digest(used.content),
                )
                if key not in current_bank_keys:
                    raise AppError(
                        "BANK_INSUFFICIENT",
                        "生成期间题库内容或审核准入已变化，不能发布旧计划",
                        details=[used.question.question_id],
                    )
            current_fact_ids = {
                card.card.id for card in eligible_fact_cards(db, self.settings)
            }
            if any(card.id not in current_fact_ids for card in fact_cards):
                raise AppError(
                    "EVALUATION_INVALID",
                    "生成期间依据卡审核状态已变化，不能发布旧计划",
                )
            current_all_bank = eligible_bank_questions(
                db, self.settings, prep.role_id, prep.level
            )
            candidate_stems = [
                item.content.get("stem", "") for item in current_all_bank
            ]

            existing = db.scalars(
                select(Plan)
                .where(
                    Plan.preparation_id == prep.id, Plan.input_revision == prep.revision
                )
                .with_for_update()
            ).one_or_none()
            if existing is not None:
                jobs_service.mark_succeeded(fenced, entity_type="plan", entity_id=existing.id)
                db.commit()
                return
            published = publish_plan(
                db,
                preparation=prep,
                plan=plan,
                job_id=job.id,
                allow_test_fixtures=self.settings.allow_test_fixtures,
                candidate_stems=candidate_stems,
            )
            jobs_service.mark_succeeded(fenced, entity_type="plan", entity_id=published.id)
            LOG.info(
                "job published id=%s kind=plan attempt=%s target=%s plan_id=%s",
                job.id,
                job.attempt,
                prep.id,
                published.id,
            )
            db.commit()
        finally:
            db.close()

    # -- M2 追问判定

    def _validate_follow_up_decision(
        self,
        decision: dict,
        question: dict,
        answer_text: str,
        exposed_prompts: list[str] | None = None,
    ) -> None:
        from app.schemas import FollowUpDecisionOutput

        try:
            model = FollowUpDecisionOutput.model_validate(decision)
        except ValidationError as exc:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问判定输出结构不符合契约",
                details=[
                    "stage=follow_up",
                    "reason=invalid_follow_up_schema",
                    *safe_validation_details(exc, FollowUpDecisionOutput),
                ],
            ) from exc
        except Exception as exc:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问判定输出结构不符合契约",
                details=["stage=follow_up", "reason=invalid_follow_up_schema", type(exc).__name__],
            )
        if model.decision == "advance":
            if (
                model.prompt is not None
                or model.answer_quote is not None
                or model.target is not None
            ):
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "无需追问时不能携带问题或引文",
                    details=["stage=follow_up", "reason=invalid_follow_up_advance"],
                )
            return
        prompt = (model.prompt or "").strip()
        if not prompt or not (8 <= len(prompt) <= 220):
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问长度必须在 8–220 码点之间",
                details=["stage=follow_up", "reason=invalid_follow_up_prompt", "field=prompt"],
            )
        quote = model.answer_quote or ""
        if not quote or not quote.strip() or len(quote) > 240 or quote not in answer_text:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问引文必须逐字出现在本题主回答中",
                details=["stage=follow_up", "reason=invalid_follow_up_quote", "field=answer_quote"],
            )
        target = (model.target or "").strip()
        if not target:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问缺少考察点",
                details=["stage=follow_up", "reason=invalid_follow_up_target", "field=target"],
            )
        if any(word in prompt for word in ("参考答案", "正确答案", "评分", "分数", "点评")):
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问包含参考答案、评分或点评内容",
                details=["stage=follow_up", "reason=invalid_follow_up_prompt", "field=prompt"],
            )
        if normalized_stem(prompt) == normalized_stem(question["stem"]):
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问与主问题重复",
                details=["stage=follow_up", "reason=duplicate_follow_up", "field=prompt"],
            )
        if stem_similarity(prompt, question["stem"]) >= 0.85:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问与主问题过于相似",
                details=["stage=follow_up", "reason=duplicate_follow_up", "field=prompt"],
            )
        for exposed in exposed_prompts or []:
            if not exposed:
                continue
            if normalized_stem(prompt) == normalized_stem(exposed):
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "追问与已曝光题目重复",
                    details=["stage=follow_up", "reason=duplicate_follow_up", "field=prompt"],
                )
            if stem_similarity(prompt, exposed) >= 0.85:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "追问与已曝光题目过于相似",
                    details=["stage=follow_up", "reason=duplicate_follow_up", "field=prompt"],
                )

    def _handle_follow_up(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            session = db.get(InterviewSession, job.target_id)
            if session is None or session.owner_id != job.owner_id:
                raise AppError("NOT_FOUND", "面试不存在")
            if session.status == "ended" or session.phase != "deciding_follow_up":
                return
            if session.follow_up_job_id != job.id:
                return
            question = db.scalars(
                select(InterviewQuestion).where(
                    InterviewQuestion.session_id == session.id,
                    InterviewQuestion.status == "current",
                )
            ).one_or_none()
            if question is None:
                return
            main_turn = db.scalars(
                select(InterviewTurn).where(
                    InterviewTurn.question_id == question.id,
                    InterviewTurn.kind == "main",
                )
            ).one_or_none()
            if main_turn is None:
                return
            answer = db.scalars(
                select(InterviewAnswer).where(InterviewAnswer.turn_id == main_turn.id)
            ).one_or_none()
            if answer is None:
                return
            first_position = db.scalar(
                select(func.min(InterviewQuestion.position)).where(
                    InterviewQuestion.session_id == session.id,
                    InterviewQuestion.source == question.source,
                )
            )
            question_data = {
                "id": str(question.id),
                "position": question.position,
                "source": question.source,
                "stem": question.stem,
                "focus": (question.evaluation or {}).get("goal", ""),
                "evaluation": question.evaluation,
                "anchors": question.anchors,
                "bank_ref": question.bank_ref,
            }
            answer_text = answer.text
            is_first = first_position == question.position
            exposed_prompts = list(
                db.scalars(
                    select(InterviewTurn.prompt)
                    .join(
                        InterviewQuestion,
                        InterviewTurn.question_id == InterviewQuestion.id,
                    )
                    .where(InterviewQuestion.session_id == session.id)
                ).all()
            )
        finally:
            db.close()

        decision = self._call_ai(
            job,
            provider,
            stage="follow_up",
            slot=str(question_data["id"]),
            func=provider.decide_follow_up,
            question=question_data,
            answer_text=answer_text,
            context={"is_first_of_source": is_first},
        )
        try:
            self._validate_follow_up_decision(
                decision, question_data, answer_text, exposed_prompts
            )
            self._annotate_last_ai_record(
                job,
                stage="follow_up",
                slot=str(question_data["id"]),
                validation_outcome="passed",
            )
        except AppError as exc:
            self._annotate_last_ai_record(
                job,
                stage="follow_up",
                slot=str(question_data["id"]),
                validation_outcome="rejected",
                validation_details=self._validation_record_details(exc),
            )
            raise

        db = SessionLocal()
        try:
            session = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
                input_revision=job.input_revision,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            if (
                session.status == "ended"
                or session.phase != "deciding_follow_up"
                or session.follow_up_job_id != job.id
            ):
                jobs_service.supersede_job(fenced)
                db.commit()
                return
            interviews_service.apply_follow_up_decision(
                db,
                session=session,
                job=fenced,
                decision=decision,
                max_attempts=self.settings.max_job_attempts,
            )
            db.commit()
        finally:
            db.close()

    # -- M2 面后评分与报告

    def _handle_interview_report(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            session = db.get(InterviewSession, job.target_id)
            if session is None or session.owner_id != job.owner_id:
                raise AppError("NOT_FOUND", "面试不存在")
            if session.status != "ended" or session.report_job_id != job.id:
                return
            questions = list(
                db.scalars(
                    select(InterviewQuestion)
                    .where(InterviewQuestion.session_id == session.id)
                    .order_by(InterviewQuestion.position)
                ).all()
            )
            turns = list(
                db.scalars(
                    select(InterviewTurn).where(
                        InterviewTurn.question_id.in_([q.id for q in questions])
                    )
                ).all()
            )
            answers = {
                answer.turn_id: answer
                for answer in db.scalars(
                    select(InterviewAnswer).where(
                        InterviewAnswer.turn_id.in_([t.id for t in turns])
                    )
                ).all()
            }
            work_items = {
                item.slot_key: item
                for item in db.scalars(
                    select(JobWorkItem).where(
                        JobWorkItem.job_id == job.id, JobWorkItem.validated.is_(True)
                    )
                ).all()
            }
            planned: list[tuple[InterviewQuestion, list[InterviewTurn], dict]] = []
            for question in questions:
                if question.status != "answered":
                    continue
                question_turns = interviews_service._ordered_question_turns(
                    [t for t in turns if t.question_id == question.id]
                )
                question_answers = {
                    t.id: answers[t.id] for t in question_turns if t.id in answers
                }
                if not question_answers:
                    continue
                planned.append((question, question_turns, question_answers))
        finally:
            db.close()

        for question, question_turns, question_answers in planned:
            expected_hash = interviews_service.question_slot_hash(
                question, question_turns, question_answers
            )
            cached = work_items.get(str(question.id))
            if cached is not None:
                try:
                    interviews_service.validate_cached_question_score(
                        cached,
                        question,
                        question_turns,
                        question_answers,
                        expected_hash,
                    )
                    continue
                except interviews_service.CachedScoreSlotError:
                    LOG.info(
                        "report slot cache invalidated job=%s slot=%s",
                        job.id,
                        question.id,
                    )
            answers_payload = [
                {
                    "answer_id": str(question_answers[t.id].id),
                    "kind": t.kind,
                    "prompt": t.prompt,
                    "text": question_answers[t.id].text,
                }
                for t in question_turns
                if t.id in question_answers
            ]
            question_data = {
                "id": str(question.id),
                "position": question.position,
                "source": question.source,
                "stem": question.stem,
                "focus": (question.evaluation or {}).get("goal", ""),
                "evaluation": question.evaluation,
                "anchors": question.anchors,
            }
            dimensions = interviews_service._expected_dimensions(question)
            output = self._call_ai(
                job,
                provider,
                stage="interview_score",
                slot=str(question.id),
                func=provider.score_interview_question,
                question=question_data,
                answers=answers_payload,
                dimensions=dimensions,
            )
            try:
                validated = interviews_service.validate_question_score(
                    question, question_turns, question_answers, output
                )
                self._annotate_last_ai_record(
                    job,
                    stage="interview_score",
                    slot=str(question.id),
                    validation_outcome="passed",
                )
            except AppError as exc:
                self._annotate_last_ai_record(
                    job,
                    stage="interview_score",
                    slot=str(question.id),
                    validation_outcome="rejected",
                    validation_details=self._validation_record_details(exc),
                )
                raise
            package = {
                "question_id": str(question.id),
                "content_hash": expected_hash,
                "dimensions": [dim.model_dump(mode="json") for dim in validated],
            }
            if not self._save_slot(job, str(question.id), package, expected_hash):
                return

        db = SessionLocal()
        try:
            session = jobs_service.lock_job_target(
                db,
                owner_id=job.owner_id,
                kind=job.kind,
                target_id=job.target_id,
                input_revision=job.input_revision,
            )
            fenced = self._fence(db, job)
            if fenced is None:
                return
            if session.status != "ended" or session.report_job_id != job.id:
                jobs_service.supersede_job(fenced)
                db.commit()
                return
            report = interviews_service.publish_report_from_work(
                db,
                session=session,
                job=fenced,
                max_attempts=self.settings.max_job_attempts,
            )
            jobs_service.mark_succeeded(
                fenced, entity_type="interview_report", entity_id=report.id
            )
            LOG.info(
                "job published id=%s kind=interview_report attempt=%s session=%s report_id=%s",
                job.id,
                job.attempt,
                session.id,
                report.id,
            )
            db.commit()
        finally:
            db.close()

    def _supersede_if_fenced(self, job: Job) -> None:
        db = SessionLocal()
        try:
            fenced = self._fence(db, job)
            if fenced is not None:
                jobs_service.supersede_job(fenced)
                db.commit()
        finally:
            db.close()

    # -- M3 语音转写与播报

    def _handle_audio_asr(self, job: Job) -> None:
        provider = get_speech_provider(self.settings)
        db = SessionLocal()
        try:
            asset = db.get(AudioAsset, job.target_id)
            if asset is None or asset.owner_id != job.owner_id:
                return
            if not jobs_service.inputs_current(db, job):
                self._supersede_if_fenced(job)
                return
            existing = db.scalars(
                select(AudioTranscript).where(
                    AudioTranscript.audio_asset_id == asset.id,
                    AudioTranscript.revision == voice_service.TRANSCRIPT_REVISION,
                )
            ).one_or_none()
            if existing is not None and existing.status in ("ready", "empty"):
                self._finish_asr_job(job, asset)
                return
            wav = voice_service._safe_read(
                self.settings.private_file_root, asset.storage_path
            )
        finally:
            db.close()

        try:
            result = self._call_ai(
                job,
                provider,
                stage="audio_asr",
                slot=str(asset.id),
                func=provider.transcribe,
                wav_bytes=wav,
            )
        except AppError as exc:
            self._record_transcript_failure(job, asset.id, exc.code)
            raise

        db = SessionLocal()
        try:
            locked = self._lock_audio_context(db, asset)
            if locked is None:
                return
            fenced = self._fence(db, job)
            if fenced is None:
                return
            voice_service.publish_transcription(
                db,
                asset=locked,
                job=fenced,
                text=result.text,
                request_id=result.request_id,
                provider=provider.name,
            )
            jobs_service.mark_succeeded(
                fenced, entity_type="audio_asset", entity_id=locked.id
            )
            db.commit()
            LOG.info(
                "job published id=%s kind=audio_asr attempt=%s asset=%s status=%s",
                job.id,
                job.attempt,
                locked.id,
                "empty" if not result.text.strip() else "ready",
            )
        finally:
            db.close()

    def _handle_audio_tts(self, job: Job) -> None:
        db = SessionLocal()
        try:
            asset = db.get(SpeechAsset, job.target_id)
            if asset is None or asset.owner_id != job.owner_id or asset.job_id != job.id:
                return
            provider = get_speech_provider(self.settings, provider_name=asset.provider)
            if asset.status == "ready" and asset.storage_path:
                jobs_service.mark_succeeded(
                    job, entity_type="speech_asset", entity_id=asset.id
                )
                db.commit()
                return
            if (
                hashlib.sha256(asset.text.encode("utf-8")).hexdigest()
                != asset.content_hash
            ):
                raise AppError("SPEECH_PROTOCOL_ERROR", "播报缓存输入摘要不一致")
            text = asset.text
        finally:
            db.close()

        try:
            result = self._call_ai(
                job,
                provider,
                stage="audio_tts",
                slot=str(asset.id),
                func=provider.synthesize,
                text=text,
            )
        except AppError as exc:
            self._mark_speech_failure(job, asset.id, exc.code)
            raise

        db = SessionLocal()
        path: str | None = None
        try:
            session = db.scalars(
                select(InterviewSession)
                .where(InterviewSession.id == asset.session_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if session is None or session.owner_id != job.owner_id:
                return
            locked = db.scalars(
                select(SpeechAsset)
                .where(SpeechAsset.id == asset.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if locked is None or locked.job_id != job.id:
                return
            fenced = self._fence(db, job)
            if fenced is None:
                return
            path, sha, duration_ms = voice_service.publish_speech(
                db,
                settings=self.settings,
                asset=locked,
                pcm=result.pcm,
                request_id=result.request_id,
            )
            locked.status = "ready"
            locked.storage_path = path
            locked.sha256 = sha
            locked.duration_ms = duration_ms
            locked.error_code = None
            locked.updated_at = voice_service.utcnow()
            jobs_service.mark_succeeded(
                fenced, entity_type="speech_asset", entity_id=locked.id
            )
            db.commit()
            LOG.info(
                "job published id=%s kind=audio_tts attempt=%s asset=%s duration_ms=%s",
                job.id,
                job.attempt,
                locked.id,
                duration_ms,
            )
        except Exception:
            db.rollback()
            if path:
                voice_service._delete_private(self.settings.private_file_root, path)
            raise
        finally:
            db.close()

    def _record_transcript_failure(
        self, job: Job, asset_id: uuid.UUID, code: str
    ) -> None:
        db = SessionLocal()
        try:
            asset = db.get(AudioAsset, asset_id)
            if asset is None:
                return
            locked = self._lock_audio_context(db, asset)
            if locked is None:
                return
            fenced = self._fence(db, job)
            if fenced is None:
                return
            row = db.scalars(
                select(AudioTranscript).where(
                    AudioTranscript.audio_asset_id == locked.id,
                    AudioTranscript.revision == voice_service.TRANSCRIPT_REVISION,
                )
            ).one_or_none()
            if row is None:
                row = AudioTranscript(
                    id=uuid.uuid4(),
                    audio_asset_id=locked.id,
                    revision=voice_service.TRANSCRIPT_REVISION,
                    text="",
                    status="failed",
                    provider="speech",
                    request_id=None,
                    error_code=code,
                )
                db.add(row)
            else:
                row.status = "failed"
                row.error_code = code
                row.updated_at = voice_service.utcnow()
            db.commit()
        except Exception:
            db.rollback()
            LOG.error("transcript failure record failed job=%s", job.id)
        finally:
            db.close()

    def _mark_speech_failure(self, job: Job, asset_id: uuid.UUID, code: str) -> None:
        db = SessionLocal()
        try:
            asset = db.get(SpeechAsset, asset_id)
            if asset is None:
                return
            session = db.scalars(
                select(InterviewSession)
                .where(InterviewSession.id == asset.session_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if session is None:
                return
            locked = db.scalars(
                select(SpeechAsset)
                .where(SpeechAsset.id == asset_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            ).one_or_none()
            if locked is None or locked.job_id != job.id:
                return
            fenced = self._fence(db, job)
            if fenced is None:
                return
            voice_service.mark_speech_failed(db, locked, code)
            db.commit()
        except Exception:
            db.rollback()
            LOG.error("speech failure record failed job=%s", job.id)
        finally:
            db.close()

    def _finish_asr_job(self, job: Job, asset: AudioAsset) -> None:
        db = SessionLocal()
        try:
            locked = self._lock_audio_context(db, asset)
            if locked is None:
                return
            fenced = self._fence(db, job)
            if fenced is None:
                return
            jobs_service.mark_succeeded(
                fenced, entity_type="audio_asset", entity_id=locked.id
            )
            db.commit()
        finally:
            db.close()

    def _lock_audio_context(self, db: Session, asset: AudioAsset) -> AudioAsset | None:
        session = db.scalars(
            select(InterviewSession)
            .where(InterviewSession.id == asset.session_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if session is None:
            return None
        turn = db.scalars(
            select(InterviewTurn)
            .where(InterviewTurn.id == asset.turn_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if turn is None:
            return None
        draft = db.scalars(
            select(VoiceDraft)
            .where(VoiceDraft.turn_id == turn.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if draft is None or draft.active_take_id != asset.id:
            return None
        locked = db.scalars(
            select(AudioAsset)
            .where(AudioAsset.id == asset.id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        return locked

    def _save_slot(
        self, job: Job, slot_key: str, package: dict, content_hash: str
    ) -> bool:
        db = SessionLocal()
        try:
            jobs_service.save_work_item(
                db,
                job,
                slot_key=slot_key,
                payload=package,
                content_hash=content_hash,
            )
            db.commit()
            return True
        except AppError as exc:
            db.rollback()
            if exc.code == "REVISION_CONFLICT":
                self._supersede_if_fenced(job)
                return False
            if exc.code == "JOB_LEASE_LOST":
                return False
            raise
        finally:
            db.close()

    @staticmethod
    def _validation_record_details(exc: AppError) -> list[str]:
        structured = JobWorker._safe_feedback_details(exc.details)
        if structured:
            return structured[:6]
        return ["reason=slot_validation_failed"]

    def _assemble_plan_and_review_payload(
        self,
        *,
        prep: Preparation,
        resume: MaterialSnapshot,
        jd: MaterialSnapshot,
        bank_sources: list,
        generated: list[dict],
        provider,
        base_test_only: bool,
    ) -> tuple[list, list, bool, object, list[dict]]:
        # 生成题技术结论可能引用不在 bank 依据集中的已审卡片；计划必须把实际使用的卡片
        # 一并带入审核快照，而不是只保留题库题引用过的那几张。
        used_fact_card_ids: set[str] = set()
        for item in bank_sources:
            for basis in (item.content.get("evaluation") or {}).get("basis") or []:
                if basis.get("kind") == "fact_card":
                    used_fact_card_ids.add(basis.get("ref_id"))
        for item in generated:
            for basis in item["evaluation"]["basis"]:
                if basis.get("kind") == "fact_card":
                    used_fact_card_ids.add(basis["ref_id"])
        with SessionLocal() as db:
            fact_cards, fact_reviews, fact_test_only = build_fact_cards(
                db, self.settings, used_fact_card_ids
            )
        effective_test_only = base_test_only or fact_test_only

        limitations: list[str] = [
            "计划只包含面试准备信息；面试问答与评分在后续阶段开放",
        ]
        if effective_test_only or provider.test_only:
            limitations.insert(
                0,
                "测试数据流程：本计划使用 test_only 合成样例与测试 provider，"
                "不代表真实 AI 输出或人工审核通过",
            )
        focus_areas: list[str] = []
        for item in bank_sources:
            focus = (item.content.get("evaluation") or {}).get("goal") or item.content.get("focus")
            if focus and focus not in focus_areas:
                focus_areas.append(focus)
        for item in generated:
            focus = item["evaluation"]["goal"]
            if focus not in focus_areas:
                focus_areas.append(focus)
        focus_areas = focus_areas[:4]

        plan = build_internal_plan(
            plan_id=uuid.uuid4(),
            preparation=prep,
            resume_snapshot=resume,
            jd_snapshot=jd,
            bank_sources=bank_sources,
            generated_questions=generated,
            fact_cards=fact_cards,
            fact_card_reviews=fact_reviews,
            semantic_review=SemanticReview(
                duplicate_pairs=[], grounding_issues=[], uncertain=False, reviewed_at=_now()
            ),
            model_id=provider.model_id,
            prompt_version=provider.prompt_version,
            test_only=effective_test_only or provider.test_only,
            focus_areas=focus_areas,
            limitations=limitations,
        )

        # 复核使用计划内真实题目 ID，并把题干、考察目标、参考思路、评分要点、
        # 锚点、评价依据和 fact cards 一并交给模型。
        review_questions = [
            {
                "id": question.id,
                "position": question.position,
                "source": question.source,
                "question_type": question.question_type,
                "stem": question.stem,
                "focus": question.focus,
                "topic_key": question.topic_key,
                "skill_codes": list(question.skill_codes),
                "reference_approach": question.evaluation.reference_approach,
                "key_points": [
                    point.model_dump(mode="json") for point in question.evaluation.key_points
                ],
                "anchors": [anchor.model_dump(mode="json") for anchor in question.anchors],
                "basis": [basis.model_dump(mode="json") for basis in question.evaluation.basis],
            }
            for question in plan.questions
        ]
        return fact_cards, fact_reviews, effective_test_only, plan, review_questions

    def _run_semantic_review(
        self,
        *,
        job: Job,
        provider,
        plan,
        review_questions: list[dict],
        resume: MaterialSnapshot,
        jd: MaterialSnapshot,
        fact_cards: list,
        is_correction: bool,
        correction_attempt: int,
    ) -> dict:
        semantic = self._call_ai(
            job,
            provider,
            stage="plan_review",
            slot="semantic",
            func=provider.review_questions,
            is_correction=is_correction,
            correction_attempt=correction_attempt,
            questions=review_questions,
            material_texts=[resume.text, jd.text],
            fact_cards=[card.model_dump(mode="json") for card in fact_cards],
        )
        try:
            self._validate_semantic_output(semantic, {question.id for question in plan.questions})
        except AppError as exc:
            self._annotate_last_ai_record(
                job,
                stage="plan_review",
                slot="semantic",
                validation_outcome="rejected",
                validation_details=self._validation_record_details(exc),
            )
            raise
        plan.semantic_review = SemanticReview(
            duplicate_pairs=semantic["duplicate_pairs"],
            grounding_issues=semantic["grounding_issues"],
            uncertain=semantic["uncertain"],
            reviewed_at=_now(),
        )
        review_clean = (
            not semantic["duplicate_pairs"]
            and not semantic["grounding_issues"]
            and not semantic["uncertain"]
        )
        self._annotate_last_ai_record(
            job,
            stage="plan_review",
            slot="semantic",
            validation_outcome="passed" if review_clean else "rejected",
            validation_details=None if review_clean else semantic_review_failure_details(plan),
        )
        return semantic

    @staticmethod
    def _select_semantic_duplicate_repair_targets(
        plan, duplicate_pairs: list[dict]
    ) -> dict[int, dict]:
        """从语义重复对中选出需要重生成的生成槽位；题库彼此冲突时返回空字典。"""
        question_meta: dict[str, dict] = {}
        gen_idx = 0
        for q in sorted(plan.questions, key=lambda item: item.position):
            if q.source == "bank":
                question_meta[q.id] = {
                    "source": "bank",
                    "question_id": q.bank_ref.question_id if q.bank_ref else "",
                }
            else:
                gen_idx += 1
                question_meta[q.id] = {
                    "source": "generated",
                    "slot_index": gen_idx,
                    "slot": f"generated-{gen_idx}",
                }

        resolved_pairs: list[tuple[dict, dict]] = []
        for pair in duplicate_pairs:
            if not isinstance(pair, dict):
                continue
            left_meta = question_meta.get(str(pair.get("left_id")))
            right_meta = question_meta.get(str(pair.get("right_id")))
            if left_meta is None or right_meta is None:
                return {}
            if left_meta["source"] == "bank" and right_meta["source"] == "bank":
                return {}
            resolved_pairs.append((left_meta, right_meta))

        targets: dict[int, dict] = {}
        for left_meta, right_meta in resolved_pairs:
            if left_meta["source"] == "generated" and right_meta["source"] == "bank":
                targets.setdefault(
                    left_meta["slot_index"],
                    {
                        "conflict_source": "bank",
                        "conflict_question_id": right_meta["question_id"],
                    },
                )
            elif right_meta["source"] == "generated" and left_meta["source"] == "bank":
                targets.setdefault(
                    right_meta["slot_index"],
                    {
                        "conflict_source": "bank",
                        "conflict_question_id": left_meta["question_id"],
                    },
                )

        for left_meta, right_meta in resolved_pairs:
            if left_meta["source"] == "generated" and right_meta["source"] == "generated":
                if left_meta["slot_index"] <= right_meta["slot_index"]:
                    earlier, later = left_meta, right_meta
                else:
                    earlier, later = right_meta, left_meta
                if earlier["slot_index"] in targets or later["slot_index"] in targets:
                    continue
                targets[later["slot_index"]] = {
                    "conflict_source": "prior_generated",
                    "conflict_slot": earlier["slot"],
                }

        return targets

    def _generate_slot(
        self,
        job: Job | None,
        provider,
        context: dict,
        resume: MaterialSnapshot,
        jd: MaterialSnapshot,
        rubric: dict,
        allowed_fact_card_ids: set[str],
        *,
        forbidden_entries: list[dict] | None = None,
        allowed_skill_codes: set[str] | None = None,
        initial_correction: dict | None = None,
    ) -> dict:
        """单槽初次输出 + 最多一次针对已知无效输出的纠正。"""
        last_error: AppError | None = None
        slot_key = context.get("slot_key")
        bank_ref_ids = {
            str(entry["ref"])
            for entry in (forbidden_entries or [])
            if isinstance(entry, dict) and entry.get("source") == "bank" and entry.get("ref")
        } | {
            str(item["question_id"])
            for item in (context.get("bank_summaries") or [])
            if isinstance(item, dict) and item.get("question_id")
        }
        slot_allowed_refs = {"question_ids": bank_ref_ids} if bank_ref_ids else None
        for attempt in range(2):
            call_context = dict(context)
            is_corr = attempt > 0
            corr_attempt = attempt + 1
            if last_error is not None:
                call_context["correction"] = {
                    "attempt": attempt + 1,
                    "message": last_error.message,
                    "details": self._safe_feedback_details(
                        last_error.details, allowed_refs=slot_allowed_refs
                    ),
                }
            elif attempt == 0 and isinstance(initial_correction, dict):
                call_context["correction"] = {
                    "attempt": 1,
                    "message": str(initial_correction.get("message") or ""),
                    "details": self._safe_feedback_details(
                        initial_correction.get("details"), allowed_refs=slot_allowed_refs
                    ),
                }
                is_corr = True
                corr_attempt = 1
            try:
                if job is None:
                    items = provider.generate_questions(
                        count=1,
                        resume_snapshot=resume,
                        jd_snapshot=jd,
                        rubric=rubric,
                        avoid_stems=context.get("avoid_stems", []),
                        context=call_context,
                    )
                else:
                    items = self._call_ai(
                        job,
                        provider,
                        stage="plan",
                        slot=slot_key,
                        func=provider.generate_questions,
                        is_correction=is_corr,
                        correction_attempt=corr_attempt,
                        count=1,
                        resume_snapshot=resume,
                        jd_snapshot=jd,
                        rubric=rubric,
                        avoid_stems=context.get("avoid_stems", []),
                        context=call_context,
                    )
            except AppError as exc:
                if exc.code != "AI_INVALID_OUTPUT":
                    raise
                last_error = exc
                continue
            if len(items) != 1:
                count_details = ["reason=invalid_question_count", f"returned={len(items)}"]
                if job is not None:
                    self._annotate_last_ai_record(
                        job,
                        stage="plan",
                        slot=slot_key,
                        validation_outcome="rejected",
                        validation_details=count_details,
                    )
                last_error = AppError(
                    "AI_INVALID_OUTPUT",
                    "模型每个槽位必须返回且只返回 1 道题",
                    count_details,
                )
                continue
            candidate_item = dict(items[0]) if isinstance(items[0], dict) else items[0]
            if allowed_skill_codes is not None and isinstance(candidate_item, dict):
                removed = sanitize_question_skill_codes(candidate_item, allowed_skill_codes)
                if removed > 0 and job is not None:
                    LOG.info(
                        "plan sanitized generated skill tags job=%s slot=%s removed=%d",
                        job.id,
                        slot_key,
                        removed,
                    )
            try:
                self._validate_generated(
                    candidate_item,
                    resume,
                    jd,
                    allowed_fact_card_ids=allowed_fact_card_ids,
                    expected_rubric=rubric,
                    expected_question_type=context.get("question_type"),
                    forbidden_stems=context.get("avoid_stems", []),
                    forbidden_entries=forbidden_entries,
                    allowed_anchors=context.get("allowed_anchors"),
                    allowed_skill_codes=allowed_skill_codes,
                )
                if job is not None:
                    self._annotate_last_ai_record(
                        job,
                        stage="plan",
                        slot=slot_key,
                        validation_outcome="passed",
                    )
                return candidate_item
            except AppError as exc:
                if exc.code != "AI_INVALID_OUTPUT":
                    raise
                if job is not None:
                    self._annotate_last_ai_record(
                        job,
                        stage="plan",
                        slot=slot_key,
                        validation_outcome="rejected",
                        validation_details=self._validation_record_details(exc),
                    )
                last_error = exc
        slot_key = str(context.get("slot_key") or "generated")
        safe_last_details = sanitize_error_details_for_records(
            list(last_error.details) if last_error else []
        )
        structured: list[str] = ["stage=plan", f"slot={slot_key}"]
        extra: list[str] = []
        for entry in safe_last_details:
            if not isinstance(entry, str) or entry.startswith(("stage=", "slot=")):
                continue
            if "=" in entry:
                if entry not in structured:
                    structured.append(entry)
            elif entry not in extra:
                extra.append(entry)
        # last_error.message 仅来自服务端固定业务消息，保留用于诊断与现有契约一致。
        last_msg = last_error.message if last_error else "unknown"
        if last_msg and last_msg not in extra:
            extra.insert(0, last_msg)
        raise AppError(
            "AI_INVALID_OUTPUT",
            "生成槽位在有限纠正后仍不合法",
            [*structured, *extra[:4]],
        )

    def _validate_semantic_output(self, result: dict, known_ids: set[str]) -> None:
        try:
            model = DuplicateReviewOutput.model_validate(result)
        except ValidationError as exc:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "语义复核输出结构不符合契约",
                details=[
                    "stage=plan_review",
                    "slot=semantic",
                    "reason=invalid_review_schema",
                    "source=provider_output",
                    *safe_validation_details(exc, DuplicateReviewOutput),
                ],
            ) from exc
        seen_pairs: set[tuple[str, str]] = set()
        for pair in model.duplicate_pairs:
            left_id = pair.left_id
            right_id = pair.right_id
            if (
                left_id == right_id
                or left_id not in known_ids
                or right_id not in known_ids
            ):
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "语义复核包含未知或非法题目引用",
                    details=[
                        "stage=plan_review",
                        "slot=semantic",
                        "reason=invalid_review_reference",
                        "field=duplicate_pairs",
                    ],
                )
            key = tuple(sorted((left_id, right_id)))
            if key in seen_pairs:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "语义复核包含重复关系",
                    details=[
                        "stage=plan_review",
                        "slot=semantic",
                        "reason=invalid_review_reference",
                        "field=duplicate_pairs",
                    ],
                )
            seen_pairs.add(key)
        seen_issues: set[tuple[str, str]] = set()
        for issue in model.grounding_issues:
            question_id = issue.question_id
            if question_id not in known_ids:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "语义复核引用了未知题目",
                    details=[
                        "stage=plan_review",
                        "slot=semantic",
                        "reason=invalid_review_reference",
                        "field=grounding_issues",
                    ],
                )
            key = (question_id, issue.claim)
            if key in seen_issues:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "语义复核包含重复无依据声明",
                    details=[
                        "stage=plan_review",
                        "slot=semantic",
                        "reason=invalid_review_reference",
                        "field=grounding_issues",
                    ],
                )
            seen_issues.add(key)

    def _validate_generated(
        self,
        item: dict,
        resume: MaterialSnapshot,
        jd: MaterialSnapshot,
        *,
        allowed_fact_card_ids: set[str] | None = None,
        expected_rubric: dict | None = None,
        expected_question_type: str | None = None,
        forbidden_stems: list[str] | None = None,
        forbidden_entries: list[dict] | None = None,
        allowed_anchors: list[dict] | None = None,
        allowed_skill_codes: set[str] | None = None,
    ) -> None:
        from app.schemas import GeneratedQuestionOutput
        from app.services.evidence import profile_items, validate_anchor

        try:
            model = GeneratedQuestionOutput.model_validate(item)
        except ValidationError as exc:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "AI 输出不符合契约结构",
                details=[
                    "reason=invalid_question_schema",
                    *safe_validation_details(exc, GeneratedQuestionOutput),
                ],
            ) from exc
        if expected_question_type is not None and model.question_type != expected_question_type:
            # 类型是本次槽位的确定性要求，不能靠模型自报其他类型绕开依据卡约束。
            raise AppError(
                "AI_INVALID_OUTPUT",
                "生成题类型与本次槽位要求不一致",
                [
                    "reason=question_type_mismatch",
                    f"expected={expected_question_type}",
                    f"actual={model.question_type}",
                ],
            )
        stem_key = normalized_stem(model.stem)
        candidates: list[dict] = []
        seen_forbidden_keys: set[str] = set()
        for entry in forbidden_entries or []:
            if not isinstance(entry, dict):
                continue
            stem_val = entry.get("stem")
            if not stem_val:
                continue
            key = normalized_stem(stem_val)
            seen_forbidden_keys.add(key)
            candidates.append(entry)
        for forbidden in forbidden_stems or []:
            if not forbidden:
                continue
            key = normalized_stem(forbidden)
            if key in seen_forbidden_keys:
                continue
            seen_forbidden_keys.add(key)
            candidates.append({"stem": forbidden, "source": None, "ref": None})

        for entry in candidates:
            forbidden = entry["stem"]
            is_exact = normalized_stem(forbidden) == stem_key
            similarity = stem_similarity(forbidden, model.stem)
            if is_exact or similarity >= SEMANTIC_DUPLICATE_THRESHOLD:
                duplicate_kind = "exact" if is_exact else "near"
                details = [
                    "reason=duplicate_stem",
                    f"duplicate_kind={duplicate_kind}",
                ]
                if duplicate_kind == "near":
                    details.extend(
                        [
                            f"similarity={similarity:.2f}",
                            f"threshold={SEMANTIC_DUPLICATE_THRESHOLD:.2f}",
                        ]
                    )
                source = entry.get("source")
                ref = entry.get("ref")
                if source == "prior_generated":
                    details.append("conflict_source=prior_generated")
                    if ref:
                        details.append(f"conflict_slot={ref}")
                elif source == "bank":
                    details.append("conflict_source=bank")
                    if ref:
                        details.append(f"conflict_question_id={ref}")
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "生成题与本次禁止题干重复",
                    details,
                )
        snapshots = {str(resume.id): resume, str(jd.id): jd}
        items_by_snapshot = {
            str(resume.id): profile_items(resume.profile),
            str(jd.id): profile_items(jd.profile),
        }
        allowed_anchor_map: dict[tuple[str, str], str] | None = None
        if allowed_anchors is not None:
            allowed_anchor_map = {
                (str(a["snapshot_id"]), str(a["item_id"])): str(a["quote"])
                for a in allowed_anchors
                if isinstance(a, dict)
                and a.get("snapshot_id")
                and a.get("item_id")
                and a.get("quote")
            }
        for anchor in model.anchors:
            validate_anchor(
                anchor,
                snapshots=snapshots,
                items_by_snapshot=items_by_snapshot,
                error_code="AI_INVALID_OUTPUT",
            )
            if allowed_anchor_map is not None:
                key = (str(anchor.snapshot_id), anchor.item_id)
                expected_quote = allowed_anchor_map.get(key)
                if expected_quote is None or anchor.quote != expected_quote:
                    raise AppError(
                        "AI_INVALID_OUTPUT",
                        "生成题锚点不在本次允许锚点列表中",
                        ["reason=invalid_anchor_selection", f"item_id={anchor.item_id}"],
                    )
        if allowed_skill_codes is not None:
            unknown_skills = [
                code for code in model.skill_codes if code not in allowed_skill_codes
            ]
            if unknown_skills:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "生成题包含未启用的技能编码",
                    ["reason=unknown_skill_code", "field=skill_codes"],
                )
        if model.evaluation.answer_status != "model_generated" or model.evaluation.trust != "provisional":
            raise AppError("AI_INVALID_OUTPUT", "生成题不能自称已核验")
        if model.evaluation.uncertainties:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "生成题包含未解决的不确定性，不能进入 ready 计划",
                [f"uncertainties_count={len(model.evaluation.uncertainties)}"],
            )
        if not model.evaluation.acceptable_alternatives:
            raise AppError("AI_INVALID_OUTPUT", "生成题缺少合理替代解")
        point_total = sum(point.weight for point in model.evaluation.key_points)
        if abs(point_total - 1.0) > 0.001:
            raise AppError("AI_INVALID_OUTPUT", "评分要点权重之和不为 1")
        rubric_total = sum(dim.weight for dim in model.evaluation.rubric.dimensions)
        if abs(rubric_total - 1.0) > 0.001:
            raise AppError("AI_INVALID_OUTPUT", "评分量表维度权重之和不为 1")
        if expected_rubric is not None:
            if model.evaluation.rubric.model_dump(mode="json") != expected_rubric:
                raise AppError("AI_INVALID_OUTPUT", "生成题评分量表与本次提供的不一致")

        allowed_cards = allowed_fact_card_ids or set()
        material_basis = 0
        fact_card_basis = 0
        for basis in model.evaluation.basis:
            if basis.kind == "material":
                snapshot_id, separator, item_id = basis.ref_id.partition("/")
                if (
                    not separator
                    or snapshot_id not in snapshots
                    or item_id not in items_by_snapshot.get(snapshot_id, {})
                ):
                    raise AppError(
                        "AI_INVALID_OUTPUT", "生成题评价依据引用了不存在的材料条目"
                    )
                if allowed_anchor_map is not None and (snapshot_id, item_id) not in allowed_anchor_map:
                    raise AppError(
                        "AI_INVALID_OUTPUT",
                        "生成题评价依据引用了不在本次允许锚点列表中的材料条目",
                        ["reason=invalid_anchor_selection", f"item_id={item_id}"],
                    )
                material_basis += 1
            elif basis.kind == "fact_card":
                if basis.ref_id not in allowed_cards:
                    raise AppError(
                        "AI_INVALID_OUTPUT", "生成题引用了本次未提供的依据卡"
                    )
                fact_card_basis += 1
        if model.question_type == "knowledge" and fact_card_basis < 1:
            raise AppError(
                "AI_INVALID_OUTPUT", "技术知识生成题必须引用本次提供的已审依据卡"
            )
        if model.question_type == "experience" and material_basis < 1:
            raise AppError("AI_INVALID_OUTPUT", "经历／推理生成题必须引用本场材料")


def main() -> None:  # pragma: no cover - 入口
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = get_settings()
    settings.ensure_directories()
    worker = JobWorker(settings)
    worker.run_forever()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
