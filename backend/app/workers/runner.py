"""独立 worker 进程：领取 PostgreSQL 任务、续租、执行、fencing 提交、超时回收。"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai.provider import get_provider
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import (
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
)
from app.schemas import Anchor, MatchRow, SemanticReview
from app.services import catalog
from app.services import interviews as interviews_service
from app.services import jobs as jobs_service
from app.services.bank import content_digest, eligible_bank_questions, eligible_fact_cards
from app.services.materials import read_file
from app.services.planning import (
    SEMANTIC_DUPLICATE_THRESHOLD,
    build_fact_cards,
    build_internal_plan,
    publish_plan,
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
    validate_profile,
)

LOG = logging.getLogger("mockmate.worker")
# v2：槽位保存前执行本次期望类型、禁止题干与近似重复校验；旧缓存必须重新校验。
WORK_ITEM_VALIDATION_VERSION = "m1-work-item-v2"


def _now() -> datetime:
    return datetime.now(timezone.utc)


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
        while True:
            try:
                if time.monotonic() - last_reap > 15:
                    last_reap = time.monotonic()
                    db = SessionLocal()
                    try:
                        jobs_service.reap_expired_leases(db)
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
        }.get(code or "", "unknown")

    def _append_ai_record(
        self,
        job: Job,
        provider,
        *,
        stage: str,
        slot: str | None,
        outcome: str,
        error_code: str | None = None,
    ) -> None:
        last = getattr(provider, "last_call", None) or {}
        usage = last.get("usage")
        record = {
            "stage": stage,
            "slot": slot,
            "attempt": job.attempt,
            "provider": getattr(provider, "name", "unknown"),
            "model": getattr(provider, "model_id", ""),
            "request_id": last.get("request_id") or "",
            "usage": usage if isinstance(usage, dict) else None,
            "outcome": outcome,
            "result_known": outcome in {"success", "known_invalid", "auth_failed", "request_rejected"},
            "error_code": error_code,
        }
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
            )
            raise
        self._append_ai_record(
            job, provider, stage=stage, slot=slot, outcome="success"
        )
        return result

    def _fail(
        self, job: Job, code: str, message: str, details: list[str] | None = None
    ) -> None:
        db = SessionLocal()
        try:
            fenced = self._fence(db, job)
            if fenced is None:
                LOG.info("skip fail: fence lost id=%s", job.id)
                return
            jobs_service.mark_failed(fenced, code, message, details or [])
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

    # -- parse
    def _handle_parse(self, job: Job) -> None:
        provider = get_provider(self.settings)
        db = SessionLocal()
        try:
            draft = db.get(MaterialDraft, job.target_id)
            if draft is None or draft.owner_id != job.owner_id or draft.revision != job.input_revision:
                raise AppError("JOB_INPUT_CONFLICT", "材料版本已变化")
            if not draft.text:
                raise AppError("FILE_TEXT_UNAVAILABLE", "材料没有可解析文本")
            text = draft.text
            kind = draft.kind
            role_id = job.input_payload.get("role_id")
            level = job.input_payload.get("level")
            allowed_skill_codes = catalog.allowed_skill_codes(db)
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

        profile = self._call_ai(
            job,
            provider,
            stage="parse",
            slot=None,
            func=provider.parse_material,
            text=text,
            kind=kind,
            role_id=role_id,
            level=level,
        )
        validate_profile(
            profile,
            kind,
            text,
            allow_user_confirmed=False,
            allowed_skill_codes=allowed_skill_codes,
        )

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
            if draft.revision != job.input_revision or draft.profile is not None:
                jobs_service.supersede_job(fenced)
            else:
                draft.profile = profile
                draft.state = "parsed"
                draft.updated_at = _now()
                jobs_service.mark_succeeded(fenced, entity_type="material", entity_id=draft.id)
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
        validated = self._validate_match_rows(rows, resume, jd)

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

        generated: list[dict] = []
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
            )
            context_hash = generation_context_hash(context)
            allowed_ids = {card["id"] for card in context["fact_cards"]}

            reused = False
            cached = worked.get(slot)
            if (
                isinstance(cached, dict)
                and cached.get("validation_version") == WORK_ITEM_VALIDATION_VERSION
                and cached.get("context_hash") == context_hash
                and isinstance(cached.get("question"), dict)
            ):
                try:
                    self._validate_generated(
                        cached["question"],
                        resume,
                        jd,
                        allowed_fact_card_ids=allowed_ids,
                        expected_rubric=rubric,
                        expected_question_type=context.get("question_type"),
                        forbidden_stems=context.get("avoid_stems", []),
                    )
                    generated.append(cached["question"])
                    reused = True
                except AppError:
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
                )
                generated.append(question)
                package = {
                    "question": question,
                    "context_hash": context_hash,
                    "validation_version": WORK_ITEM_VALIDATION_VERSION,
                }
                if not self._save_slot(job, slot, package, content_digest(question)):
                    return

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
        test_only = test_only or fact_test_only

        limitations: list[str] = [
            "计划只包含面试准备信息；面试问答与评分在后续阶段开放",
        ]
        if test_only or provider.test_only:
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

        plan_id = uuid.uuid4()
        plan = build_internal_plan(
            plan_id=plan_id,
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
            test_only=test_only or provider.test_only,
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
                "key_points": [point.model_dump(mode="json") for point in question.evaluation.key_points],
                "anchors": [anchor.model_dump(mode="json") for anchor in question.anchors],
                "basis": [basis.model_dump(mode="json") for basis in question.evaluation.basis],
            }
            for question in plan.questions
        ]
        semantic = self._call_ai(
            job,
            provider,
            stage="plan_review",
            slot="semantic",
            func=provider.review_questions,
            questions=review_questions,
            material_texts=[resume.text, jd.text],
            fact_cards=[card.model_dump(mode="json") for card in fact_cards],
        )
        self._validate_semantic_output(semantic, {question.id for question in plan.questions})
        plan.semantic_review = SemanticReview(
            duplicate_pairs=semantic.get("duplicate_pairs", []),
            grounding_issues=semantic.get("grounding_issues", []),
            uncertain=bool(semantic.get("uncertain")),
            reviewed_at=_now(),
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
        except Exception as exc:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "追问判定输出结构不符合契约",
                details=[type(exc).__name__],
            )
        if model.decision == "advance":
            if model.prompt or model.answer_quote or model.target:
                raise AppError("AI_INVALID_OUTPUT", "无需追问时不能携带问题或引文")
            return
        prompt = (model.prompt or "").strip()
        if not (8 <= len(prompt) <= 220):
            raise AppError("AI_INVALID_OUTPUT", "追问长度必须在 8–220 码点之间")
        quote = model.answer_quote or ""
        if not quote or len(quote) > 240 or quote not in answer_text:
            raise AppError("AI_INVALID_OUTPUT", "追问引文必须逐字出现在本题主回答中")
        if not model.target:
            raise AppError("AI_INVALID_OUTPUT", "追问缺少考察点")
        if any(word in prompt for word in ("参考答案", "正确答案", "评分", "分数", "点评")):
            raise AppError("AI_INVALID_OUTPUT", "追问包含参考答案、评分或点评内容")
        if normalized_stem(prompt) == normalized_stem(question["stem"]):
            raise AppError("AI_INVALID_OUTPUT", "追问与主问题重复")
        if stem_similarity(prompt, question["stem"]) >= 0.85:
            raise AppError("AI_INVALID_OUTPUT", "追问与主问题过于相似")
        for exposed in exposed_prompts or []:
            if not exposed:
                continue
            if normalized_stem(prompt) == normalized_stem(exposed):
                raise AppError("AI_INVALID_OUTPUT", "追问与已曝光题目重复")
            if stem_similarity(prompt, exposed) >= 0.85:
                raise AppError("AI_INVALID_OUTPUT", "追问与已曝光题目过于相似")

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
        self._validate_follow_up_decision(
            decision, question_data, answer_text, exposed_prompts
        )

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
                question_turns = sorted(
                    [t for t in turns if t.question_id == question.id],
                    key=lambda t: (t.kind != "main", str(t.id)),
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
            if cached is not None and cached.content_hash == expected_hash:
                continue
            answers_payload = [
                {
                    "answer_id": str(question_answers[t.id].id),
                    "kind": t.kind,
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
            validated = interviews_service.validate_question_score(
                question, question_turns, question_answers, output
            )
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

    def _generate_slot(
        self,
        job: Job | None,
        provider,
        context: dict,
        resume: MaterialSnapshot,
        jd: MaterialSnapshot,
        rubric: dict,
        allowed_fact_card_ids: set[str],
    ) -> dict:
        """单槽初次输出 + 最多一次针对已知无效输出的纠正。"""
        last_error: AppError | None = None
        for attempt in range(2):
            call_context = dict(context)
            if last_error is not None:
                call_context["correction"] = {
                    "attempt": attempt + 1,
                    "message": last_error.message,
                    "details": list(last_error.details),
                }
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
                        slot=context.get("slot_key"),
                        func=provider.generate_questions,
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
                last_error = AppError(
                    "AI_INVALID_OUTPUT",
                    "模型每个槽位必须返回且只返回 1 道题",
                    [f"returned={len(items)}"],
                )
                continue
            try:
                self._validate_generated(
                    items[0],
                    resume,
                    jd,
                    allowed_fact_card_ids=allowed_fact_card_ids,
                    expected_rubric=rubric,
                    expected_question_type=context.get("question_type"),
                    forbidden_stems=context.get("avoid_stems", []),
                )
                return items[0]
            except AppError as exc:
                if exc.code != "AI_INVALID_OUTPUT":
                    raise
                last_error = exc
        raise AppError(
            "AI_INVALID_OUTPUT",
            "生成槽位在有限纠正后仍不合法",
            [
                context.get("slot_key", "generated"),
                last_error.message if last_error else "unknown",
                *((last_error.details if last_error else [])[:3]),
            ],
        )

    def _validate_semantic_output(self, result: dict, known_ids: set[str]) -> None:
        from app.schemas import DuplicateReviewOutput

        model = DuplicateReviewOutput.model_validate(result)
        seen_pairs: set[tuple[str, str]] = set()
        for pair in model.duplicate_pairs:
            left_id = pair.get("left_id")
            right_id = pair.get("right_id")
            if (
                not left_id
                or not right_id
                or left_id == right_id
                or left_id not in known_ids
                or right_id not in known_ids
            ):
                raise AppError(
                    "AI_INVALID_OUTPUT", "语义复核包含未知或非法题目引用"
                )
            key = tuple(sorted((str(left_id), str(right_id))))
            if key in seen_pairs:
                raise AppError("AI_INVALID_OUTPUT", "语义复核包含重复关系")
            seen_pairs.add(key)
        seen_issues: set[tuple[str, str]] = set()
        for issue in model.grounding_issues:
            question_id = issue.get("question_id")
            if not question_id or question_id not in known_ids:
                raise AppError("AI_INVALID_OUTPUT", "语义复核引用了未知题目")
            key = (str(question_id), str(issue.get("claim", "")))
            if key in seen_issues:
                raise AppError("AI_INVALID_OUTPUT", "语义复核包含重复无依据声明")
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
    ) -> None:
        from app.schemas import GeneratedQuestionOutput
        from app.services.evidence import profile_items, validate_anchor

        model = GeneratedQuestionOutput.model_validate(item)
        if expected_question_type is not None and model.question_type != expected_question_type:
            # 类型是本次槽位的确定性要求，不能靠模型自报其他类型绕开依据卡约束。
            raise AppError(
                "AI_INVALID_OUTPUT",
                "生成题类型与本次槽位要求不一致",
                [
                    f"类型不匹配：expected={expected_question_type}",
                    f"actual={model.question_type}",
                ],
            )
        stem_key = normalized_stem(model.stem)
        for forbidden in forbidden_stems or []:
            if not forbidden:
                continue
            if normalized_stem(forbidden) == stem_key or (
                stem_similarity(forbidden, model.stem) >= SEMANTIC_DUPLICATE_THRESHOLD
            ):
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "生成题与本次禁止题干重复",
                    [
                        f"重复题干：{model.stem[:40]}",
                        f"forbidden={forbidden[:40]}",
                    ],
                )
        snapshots = {str(resume.id): resume, str(jd.id): jd}
        items_by_snapshot = {
            str(resume.id): profile_items(resume.profile),
            str(jd.id): profile_items(jd.profile),
        }
        for anchor in model.anchors:
            validate_anchor(
                anchor,
                snapshots=snapshots,
                items_by_snapshot=items_by_snapshot,
                error_code="AI_INVALID_OUTPUT",
            )
        if model.evaluation.answer_status != "model_generated" or model.evaluation.trust != "provisional":
            raise AppError("AI_INVALID_OUTPUT", "生成题不能自称已核验")
        if model.evaluation.uncertainties:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "生成题包含未解决的不确定性，不能进入 ready 计划",
                model.evaluation.uncertainties[:2],
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
