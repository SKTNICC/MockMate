"""M2 文字模拟面试：开场、回合推进、暂停/跳过/早退、追问与报告发布。

所有写操作在行锁内计算许可并一次提交；公开 DTO 只由这里构造，禁止把内部评分快照
或未曝光题干直接返回客户端。
"""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Iterable

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.core.db import read_snapshot_session
from app.core.diagnostics import safe_validation_details, sanitize_job_error_code
from app.core.errors import AppError
from app.models import (
    InterviewAnswer,
    InterviewQuestion,
    InterviewReport,
    InterviewSession,
    InterviewTurn,
    Job,
    JobWorkItem,
    Plan,
    Preparation,
    Role,
)
from app.schemas import (
    InterviewAnswerView,
    CachedScoreSlotPayload,
    InterviewCoverage,
    InterviewDetail,
    InterviewDimensionResult,
    InterviewEvidence,
    InterviewFinding,
    InterviewHistory,
    InterviewHistoryItem,
    InterviewJobSummary,
    InterviewMaterialVersion,
    InterviewPlanSummary,
    InterviewReceipt,
    InterviewReport as InterviewReportSchema,
    InterviewReportQuestion,
    InterviewTurnView,
    ScoreQuestionOutput,
)
from app.services import jobs as jobs_service
from app.services import planning

CONSENT_VERSION = "m2-external-processing-v1"
AUDIO_CONSENT_VERSION = "m3-voice-processing-v1"
OPENING_VERSION = "m2-opening-v1"
CLOSING_VERSION = "m2-closing-v1"
RUBRIC_POLICY_VERSION = "m2-rubric-policy-v1"
FOLLOW_UP_PROMPT_VERSION = "m2-follow-up-v2"
SCORE_PROMPT_VERSION = "m2-score-v3"
MAX_ANSWER_CODEPOINTS = 4000
EXPRESSION_DIMENSION_ID = "expression_structure"
EXPRESSION_DIMENSION_NAME = "表达结构"
EXPRESSION_DIMENSION_VERSION = "expression_structure_v1"
EXPRESSION_WEIGHT = 0.0001  # 独立展示；不参与单题加权分
MAX_QUOTE_CODEPOINTS = 240


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _codepoint_len(value: str) -> int:
    return len(value)


def _require_text(raw: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise AppError("TEXT_INVALID", "回答正文不能为空白")
    if _codepoint_len(raw) > MAX_ANSWER_CODEPOINTS:
        raise AppError(
            "TEXT_INVALID",
            f"回答正文不能超过 {MAX_ANSWER_CODEPOINTS} 个字符",
            details=[f"codepoints={_codepoint_len(raw)}"],
        )
    return raw


# --------------------------------------------------------------------------- 开场文本


def opening_text(main_question_count: int, mode: str = "text") -> str:
    half = main_question_count // 2
    if mode == "voice":
        return (
            f"本次语音模拟面试共 {main_question_count} 道主问题，题库与现场生成各 {half} 道。"
            "你会先听到题干，再开始录音；每次回答最多 120 秒，停止后先核对转写再提交。"
            "播题和识别等待不计入作答时限。每道主问题至多有一次追问；你可以暂停、跳过或提前结束。"
            "录音与必要上下文会由配置的语音和文字 AI 处理，用于转写、追问判定与面后评分。"
        )
    return (
        f"本次文字模拟面试共 {main_question_count} 道主问题，题库与现场生成各 {half} 道。"
        "每道主问题至多有一次相关追问；你可以暂停、跳过当前题或提前结束。"
        "你的回答及必要上下文会由配置的 AI 处理，用于追问判定与面后评分。"
    )


def closing_text(mode: str = "text") -> str:
    if mode == "voice":
        return "本次语音面试已结束，问答已锁定。报告只依据你确认提交的文字与原话证据生成，不评价口音、音色或情绪。"
    return "本次文字面试已结束，问答已锁定。报告会基于你的原话证据生成，可稍后在面试记录中重开。"


def _now_iso() -> str:
    return utcnow().isoformat()


# --------------------------------------------------------------------------- 查询辅助


def _session_for_owner(db: Session, owner_id: uuid.UUID, session_id: uuid.UUID) -> InterviewSession:
    row = db.get(InterviewSession, session_id)
    if row is None or row.owner_id != owner_id:
        raise AppError("NOT_FOUND", "面试不存在", status_code=404)
    return row


def _lock_session(db: Session, owner_id: uuid.UUID, session_id: uuid.UUID) -> InterviewSession:
    row = db.scalars(
        select(InterviewSession)
        .where(InterviewSession.id == session_id, InterviewSession.owner_id == owner_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "面试不存在", status_code=404)
    return row


def _questions(db: Session, session_id: uuid.UUID) -> list[InterviewQuestion]:
    return list(
        db.scalars(
            select(InterviewQuestion)
            .where(InterviewQuestion.session_id == session_id)
            .order_by(InterviewQuestion.position)
        ).all()
    )


def _turns_with_questions(
    db: Session, session_id: uuid.UUID
) -> list[tuple[InterviewTurn, InterviewQuestion]]:
    return list(
        db.execute(
            select(InterviewTurn, InterviewQuestion)
            .join(InterviewQuestion, InterviewTurn.question_id == InterviewQuestion.id)
            .where(InterviewQuestion.session_id == session_id)
            .order_by(InterviewQuestion.position, InterviewTurn.kind.desc())
        ).all()
    )


def _answers_by_turn(db: Session, turn_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, InterviewAnswer]:
    ids = list(turn_ids)
    if not ids:
        return {}
    rows = db.scalars(select(InterviewAnswer).where(InterviewAnswer.turn_id.in_(ids))).all()
    return {row.turn_id: row for row in rows}


def _turn_view(
    turn: InterviewTurn,
    question: InterviewQuestion,
    answer: InterviewAnswer | None,
) -> InterviewTurnView:
    return InterviewTurnView(
        turn_id=turn.id,
        question_id=question.id,
        position=question.position,
        kind=turn.kind,
        prompt=turn.prompt,
        status=turn.status,
        answer=(
            InterviewAnswerView(
                answer_id=answer.id,
                text=answer.text,
                submitted_at=answer.submitted_at,
                input_source=answer.input_source or "text",
                audio_asset_id=answer.audio_asset_id,
                transcript_revision=answer.transcript_revision,
            )
            if answer is not None
            else None
        ),
    )


def _job_summary(db: Session, job_id: uuid.UUID | None, max_attempts: int) -> InterviewJobSummary | None:
    if job_id is None:
        return None
    job = db.get(Job, job_id)
    if job is None:
        return None
    return InterviewJobSummary(
        id=job.id,
        state=job.state,
        attempt=job.attempt,
        retryable=jobs_service.is_retryable(db, job, max_attempts),
        error_code=(
            sanitize_job_error_code(job.error_code)
            if job.error_code is not None
            else None
        ),
    )


def report_state(db: Session, session: InterviewSession) -> str:
    if db.scalars(
        select(InterviewReport.id).where(InterviewReport.session_id == session.id)
    ).first() is not None:
        return "ready"
    job = db.get(Job, session.report_job_id) if session.report_job_id else None
    if job is None:
        return "not_requested"
    if job.state in ("failed", "interrupted"):
        return "failed"
    return "pending"


def _allowed_actions(
    db: Session,
    session: InterviewSession,
    *,
    max_attempts: int,
) -> list[str]:
    if session.status == "ended":
        actions: list[str] = []
        if report_state(db, session) == "ready":
            return actions
        job = db.get(Job, session.report_job_id) if session.report_job_id else None
        if job is not None and job.state in ("failed", "interrupted"):
            actions.append("create_basic_report")
            if jobs_service.is_retryable(db, job, max_attempts):
                actions.append("retry_report")
        return actions

    actions = []
    if session.status == "active":
        actions.append("pause")
    else:
        actions.append("resume")
    actions.append("finish")

    if session.phase == "awaiting_answer" and session.current_turn_id is not None and session.status == "active":
        actions.extend(["answer", "skip"])

    if session.phase == "deciding_follow_up":
        job = db.get(Job, session.follow_up_job_id) if session.follow_up_job_id else None
        if job is not None and job.state in ("failed", "interrupted"):
            actions.append("continue_without_follow_up")
            if jobs_service.is_retryable(db, job, max_attempts):
                actions.append("retry_follow_up")
    return actions


def _plan_summary(session: InterviewSession) -> InterviewPlanSummary:
    snapshot = session.plan_snapshot
    return InterviewPlanSummary(
        plan_id=session.plan_id,
        main_question_count=snapshot["main_question_count"],
        source_counts=snapshot["source_counts"],
        material_versions=[
            InterviewMaterialVersion(**item) for item in snapshot["material_versions"]
        ],
        sample_only=bool(snapshot.get("sample_only", False)),
    )


def _current_turn(
    db: Session,
    session: InterviewSession,
    turns: list[tuple[InterviewTurn, InterviewQuestion]],
    answers: dict[uuid.UUID, InterviewAnswer],
) -> InterviewTurnView | None:
    if session.current_turn_id is None:
        return None
    for turn, question in turns:
        if turn.id == session.current_turn_id and turn.status == "pending":
            return _turn_view(turn, question, answers.get(turn.id))
    return None


# --------------------------------------------------------------------------- 开始


def find_session_for_plan(
    db: Session, owner_id: uuid.UUID, plan_id: uuid.UUID
) -> InterviewSession | None:
    return db.scalars(
        select(InterviewSession).where(
            InterviewSession.owner_id == owner_id, InterviewSession.plan_id == plan_id
        )
    ).one_or_none()


def start_interview(
    db: Session,
    *,
    owner_id: uuid.UUID,
    plan_id: uuid.UUID,
    plan_revision: int,
    external_processing_consent: bool,
    allow_test_fixtures: bool,
    mode: str = "text",
    audio_processing_consent: bool = False,
    speech_available: bool = False,
) -> tuple[InterviewSession, bool]:
    """返回 (session, created)。同 owner + plan 只创建一场，重复进入只读打开。"""
    existing = find_session_for_plan(db, owner_id, plan_id)
    if existing is not None:
        return existing, False

    if mode not in ("text", "voice"):
        raise AppError("VALIDATION_ERROR", "面试模式不合法", status_code=422)
    if mode == "voice" and not speech_available:
        raise AppError("SPEECH_NOT_CONFIGURED", "语音服务未配置，不能开始语音面试", status_code=503)
    if not external_processing_consent:
        raise AppError(
            "CONSENT_REQUIRED",
            "面试会把回答及必要上下文发送给配置的外部模型，需要先确认本场处理说明",
        )
    if mode == "voice" and not audio_processing_consent:
        raise AppError(
            "AUDIO_CONSENT_REQUIRED",
            "语音面试会把录音发送给配置的语音服务转写，需要单独确认音频处理说明",
        )

    candidate = db.get(Plan, plan_id)
    if candidate is None or candidate.owner_id != owner_id:
        raise AppError("NOT_FOUND", "计划不存在", status_code=404)
    preparation = db.scalars(
        select(Preparation)
        .where(
            Preparation.id == candidate.preparation_id,
            Preparation.owner_id == owner_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if preparation is None:
        raise AppError("NOT_FOUND", "计划不存在", status_code=404)
    plan = db.scalars(
        select(Plan)
        .where(Plan.id == plan_id, Plan.owner_id == owner_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if plan is None:
        raise AppError("NOT_FOUND", "计划不存在", status_code=404)

    # 并发开始：取得 Preparation/Plan 锁后重查。
    existing = find_session_for_plan(db, owner_id, plan_id)
    if existing is not None:
        return existing, False

    if plan.state != "confirmed":
        raise AppError("PLAN_STATE_INVALID", "只有已确认的计划可以开始面试")
    if plan.revision != plan_revision:
        raise AppError(
            "REVISION_CONFLICT",
            "计划版本已变化，请刷新后重试",
            details=[f"current_revision={plan.revision}"],
        )
    if preparation.current_plan_id != plan.id:
        raise AppError("PLAN_STALE", "当前计划已不是该准备的确认计划，请重新确认")
    if preparation.revision != plan.input_revision:
        raise AppError("PLAN_STALE", "准备配置已变化，该计划不能再开始")

    internal = planning.parse_internal_plan(plan.internal_payload)
    if internal.id != plan.id or internal.preparation_id != preparation.id:
        raise AppError("PLAN_STALE", "计划内容与数据库记录不一致")
    if internal.state != "confirmed":
        raise AppError("PLAN_STATE_INVALID", "计划内容尚未确认")
    if str(internal.resume_snapshot_id) != str(preparation.resume_snapshot_id) or str(
        internal.jd_snapshot_id
    ) != str(preparation.jd_snapshot_id):
        raise AppError("PLAN_STALE", "计划内的材料版本与当前准备不一致，请重新制定计划")
    snapshots = {str(snap.id): snap for snap in internal.material_snapshots}
    if set(snapshots) != {str(internal.resume_snapshot_id), str(internal.jd_snapshot_id)}:
        raise AppError("EVIDENCE_INVALID", "计划材料快照不完整")
    if snapshots[str(internal.resume_snapshot_id)].kind != "resume":
        raise AppError("EVIDENCE_INVALID", "计划简历快照类型不匹配")
    if snapshots[str(internal.jd_snapshot_id)].kind != "jd":
        raise AppError("EVIDENCE_INVALID", "计划 JD 快照类型不匹配")

    planning.validate_internal_plan(
        internal,
        planning.PlanValidationContext(allow_test_fixtures=allow_test_fixtures, db=db),
    )
    planning.revalidate_current_content(
        db, internal, allow_test_fixtures=allow_test_fixtures
    )

    role = db.get(Role, internal.role_id)
    material_versions = [
        {
            "kind": snap.kind,
            "snapshot_id": str(snap.id),
            "material_id": str(snap.material_id),
            "title": snap.title,
            "source_revision": snap.source_revision,
        }
        for snap in internal.material_snapshots
    ]
    plan_snapshot = {
        "plan_id": str(internal.id),
        "plan_revision": plan.revision,
        "preparation_id": str(preparation.id),
        "role_id": internal.role_id,
        "level": internal.level,
        "main_question_count": internal.main_question_count,
        "source_counts": internal.source_counts.model_dump(mode="json"),
        "material_versions": material_versions,
        "focus_areas": list(internal.focus_areas),
        "limitations": list(internal.limitations),
        "test_only": bool(internal.test_only),
        "sample_only": bool(role.sample_only) if role is not None else False,
        "bank_source_refs": [
            {
                "question_id": candidate.question_id,
                "version": candidate.version,
                "content_sha256": candidate.content_sha256,
            }
            for candidate in internal.bank_source_snapshots
        ],
        "fact_cards": [
            {"id": card.id, "version": card.version, "content_sha256": card.content_sha256}
            for card in internal.fact_cards
        ],
        "opening_version": OPENING_VERSION,
        "closing_version": CLOSING_VERSION,
    }

    opening_version = "m3-opening-voice-v1" if mode == "voice" else OPENING_VERSION
    plan_snapshot["opening_version"] = opening_version
    now = utcnow()
    session = InterviewSession(
        id=uuid.uuid4(),
        owner_id=owner_id,
        plan_id=plan.id,
        preparation_id=preparation.id,
        plan_revision=plan.revision,
        role_id=internal.role_id,
        level=internal.level,
        mode=mode,
        status="active",
        phase="awaiting_answer",
        revision=1,
        opening_text=opening_text(internal.main_question_count, mode),
        closing_text=None,
        plan_snapshot=plan_snapshot,
        external_processing_consented_at=now,
        consent_version=CONSENT_VERSION,
        audio_processing_consented_at=now if mode == "voice" else None,
        audio_consent_version=AUDIO_CONSENT_VERSION if mode == "voice" else None,
        started_at=now,
    )
    db.add(session)
    db.flush()

    first_question: InterviewQuestion | None = None
    for item in internal.questions:
        question = InterviewQuestion(
            id=uuid.uuid4(),
            session_id=session.id,
            position=item.position,
            plan_question_id=item.id,
            source=item.source,
            stem=item.stem,
            bank_ref=item.bank_ref.model_dump(mode="json") if item.bank_ref else None,
            anchors=[anchor.model_dump(mode="json") for anchor in item.anchors],
            evaluation=item.evaluation.model_dump(mode="json"),
            status="planned",
            follow_up_outcome=None,
        )
        db.add(question)
        if first_question is None or question.position < first_question.position:
            first_question = question
    if first_question is None:
        raise AppError("INTERVIEW_STATE_INVALID", "计划没有可用主问题")
    db.flush()

    first_question.status = "current"
    turn = InterviewTurn(
        id=uuid.uuid4(),
        question_id=first_question.id,
        kind="main",
        prompt=first_question.stem,
        status="pending",
        presented_at=now,
    )
    db.add(turn)
    db.flush()
    session.current_turn_id = turn.id
    db.flush()
    return session, True


# --------------------------------------------------------------------------- 详情


def build_detail(db: Session, session: InterviewSession, max_attempts: int) -> InterviewDetail:
    questions = _questions(db, session.id)
    turns = _turns_with_questions(db, session.id)
    answers = _answers_by_turn(db, [turn.id for turn, _ in turns])
    transcript = [_turn_view(turn, question, answers.get(turn.id)) for turn, question in turns]
    current = _current_turn(db, session, turns, answers)
    current_question = next((q for q in questions if q.status == "current"), None)
    return InterviewDetail(
        session_id=session.id,
        plan_id=session.plan_id,
        preparation_id=session.preparation_id,
        role_id=session.role_id,
        level=session.level,
        mode=session.mode,
        status=session.status,
        phase=session.phase,
        revision=session.revision,
        started_at=session.started_at,
        ended_at=session.ended_at,
        end_reason=session.end_reason,
        plan_summary=_plan_summary(session),
        opening_text=session.opening_text,
        closing_text=session.closing_text,
        current_main_position=(
            None
            if session.status == "ended" or current_question is None
            else current_question.position
        ),
        answered_main_count=sum(1 for q in questions if q.status == "answered"),
        skipped_main_count=sum(1 for q in questions if q.status == "skipped"),
        current_turn=current,
        allowed_actions=_allowed_actions(db, session, max_attempts=max_attempts),
        transcript=transcript,
        follow_up_job=_job_summary(db, session.follow_up_job_id, max_attempts),
        report_job=_job_summary(db, session.report_job_id, max_attempts),
        report_state=report_state(db, session),
    )


def get_detail(
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    max_attempts: int,
) -> InterviewDetail:
    """所有详情字段从同一个 REPEATABLE READ 只读快照生成。

    路由先结束鉴权只读事务并归还连接，这里再建立独立只读快照连接，
    从归属检查开始保持一致读视图，worker 在读取子表期间提交的新状态不会混入本响应。
    """
    with read_snapshot_session() as snapshot:
        session = _session_for_owner(snapshot, owner_id, session_id)
        return build_detail(snapshot, session, max_attempts)


# --------------------------------------------------------------------------- 历史记录


def _encode_cursor(started_at: datetime, session_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"started_at": started_at.astimezone(timezone.utc).isoformat(), "id": str(session_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii")


def _decode_cursor(raw: str) -> tuple[datetime, uuid.UUID]:
    try:
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
        started_at = datetime.fromisoformat(payload["started_at"])
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        return started_at, uuid.UUID(payload["id"])
    except Exception:
        raise AppError("VALIDATION_ERROR", "cursor 无效", status_code=422, details=["cursor"])


def _counts_for_sessions(
    db: Session, session_ids: list[uuid.UUID]
) -> tuple[dict[uuid.UUID, dict[str, int]], dict[uuid.UUID, str], dict[uuid.UUID, Job]]:
    if not session_ids:
        return {}, {}, {}
    question_counts: dict[uuid.UUID, dict[str, int]] = {sid: {} for sid in session_ids}
    rows = db.execute(
        select(
            InterviewQuestion.session_id,
            InterviewQuestion.status,
            func.count(InterviewQuestion.id),
        )
        .where(InterviewQuestion.session_id.in_(session_ids))
        .group_by(InterviewQuestion.session_id, InterviewQuestion.status)
    ).all()
    for session_id, status, count in rows:
        question_counts.setdefault(session_id, {})[status] = int(count)

    report_states: dict[uuid.UUID, str] = {}
    report_ids = set(
        db.scalars(
            select(InterviewReport.session_id).where(
                InterviewReport.session_id.in_(session_ids)
            )
        ).all()
    )
    sessions = db.scalars(
        select(InterviewSession).where(InterviewSession.id.in_(session_ids))
    ).all()
    job_ids = [s.report_job_id for s in sessions if s.report_job_id]
    jobs: dict[uuid.UUID, Job] = {}
    if job_ids:
        for job in db.scalars(select(Job).where(Job.id.in_(job_ids))).all():
            jobs[job.id] = job
    for session in sessions:
        if session.id in report_ids:
            report_states[session.id] = "ready"
        else:
            job = jobs.get(session.report_job_id) if session.report_job_id else None
            if job is None:
                report_states[session.id] = "not_requested"
            elif job.state in ("failed", "interrupted"):
                report_states[session.id] = "failed"
            else:
                report_states[session.id] = "pending"
    return question_counts, report_states, jobs


def list_history(
    db: Session,
    *,
    owner_id: uuid.UUID,
    limit: int,
    cursor: str | None,
    plan_id: uuid.UUID | None,
) -> InterviewHistory:
    if limit < 1 or limit > 50:
        raise AppError("VALIDATION_ERROR", "limit 必须在 1–50 之间", status_code=422)
    stmt = select(InterviewSession).where(InterviewSession.owner_id == owner_id)
    if plan_id is not None:
        stmt = stmt.where(InterviewSession.plan_id == plan_id)
    if cursor:
        started_at, session_id = _decode_cursor(cursor)
        stmt = stmt.where(
            or_(
                InterviewSession.started_at < started_at,
                and_(
                    InterviewSession.started_at == started_at,
                    InterviewSession.id < session_id,
                ),
            )
        )
    rows = list(
        db.scalars(
            stmt.order_by(
                InterviewSession.started_at.desc(), InterviewSession.id.desc()
            ).limit(limit)
        ).all()
    )
    counts, states, _jobs = _counts_for_sessions(db, [row.id for row in rows])
    items = [
        InterviewHistoryItem(
            session_id=row.id,
            plan_id=row.plan_id,
            preparation_id=row.preparation_id,
            role_id=row.role_id,
            level=row.level,
            mode=row.mode,
            status=row.status,
            started_at=row.started_at,
            ended_at=row.ended_at,
            end_reason=row.end_reason,
            main_question_count=row.plan_snapshot["main_question_count"],
            answered_main_count=counts.get(row.id, {}).get("answered", 0),
            report_state=states.get(row.id, "not_requested"),
        )
        for row in rows
    ]
    next_cursor = (
        _encode_cursor(rows[-1].started_at, rows[-1].id) if len(rows) == limit and rows else None
    )
    return InterviewHistory(items=items, next_cursor=next_cursor)


# --------------------------------------------------------------------------- 写操作


def _check_revision(session: InterviewSession, revision: int) -> None:
    if session.revision != revision:
        raise AppError(
            "REVISION_CONFLICT",
            "面试版本已变化，请刷新后重试",
            details=[f"current_revision={session.revision}"],
        )


def _current_turn_locked(
    db: Session, session: InterviewSession, turn_id: uuid.UUID
) -> tuple[InterviewTurn, InterviewQuestion]:
    if session.current_turn_id != turn_id:
        raise AppError("TURN_NOT_CURRENT", "该回合已不是当前待作答回合，请刷新")
    turn = db.scalars(
        select(InterviewTurn)
        .where(InterviewTurn.id == turn_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if turn is None:
        raise AppError("NOT_FOUND", "回合不存在", status_code=404)
    question = db.scalars(
        select(InterviewQuestion)
        .where(
            InterviewQuestion.id == turn.question_id,
            InterviewQuestion.session_id == session.id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if question is None:
        raise AppError("NOT_FOUND", "回合不存在", status_code=404)
    if turn.status != "pending":
        raise AppError("TURN_NOT_CURRENT", "该回合已解决，请刷新")
    return turn, question


def _create_main_turn(
    db: Session, session: InterviewSession, question: InterviewQuestion, now: datetime
) -> InterviewTurn:
    question.status = "current"
    turn = InterviewTurn(
        id=uuid.uuid4(),
        question_id=question.id,
        kind="main",
        prompt=question.stem,
        status="pending",
        presented_at=now,
    )
    db.add(turn)
    db.flush()
    session.current_turn_id = turn.id
    session.phase = "awaiting_answer"
    return turn


def _finish_session_locked(
    db: Session,
    session: InterviewSession,
    *,
    end_reason: str,
) -> InterviewSession:
    """调用方已递增 revision 并持有 session 行锁；这里只做一次性收尾。"""
    now = utcnow()
    session.status = "ended"
    session.phase = "closed"
    session.ended_at = now
    session.ended_revision = session.revision
    session.end_reason = end_reason
    session.closing_text = closing_text(session.mode)

    questions = _questions(db, session.id)
    turns = list(
        db.scalars(
            select(InterviewTurn)
            .where(
                InterviewTurn.question_id.in_([q.id for q in questions])
            )
            .execution_options(populate_existing=True)
        ).all()
    )
    answers = _answers_by_turn(db, [t.id for t in turns])
    answered_question_ids = {
        t.question_id for t in turns if t.kind == "main" and t.id in answers
    }
    follow_up_turns = {t.question_id: t for t in turns if t.kind == "follow_up"}

    for question in questions:
        if question.status == "planned":
            question.status = "not_reached"
        elif question.status == "current":
            if question.id in answered_question_ids:
                question.status = "answered"
                if question.follow_up_outcome is None:
                    follow_up = follow_up_turns.get(question.id)
                    question.follow_up_outcome = (
                        "asked" if follow_up is not None else "cancelled"
                    )
            else:
                question.status = "unanswered"
        if question.status in ("answered", "skipped", "unanswered"):
            follow_up = follow_up_turns.get(question.id)
            if follow_up is not None and follow_up.status == "pending":
                follow_up.status = "unanswered"
                follow_up.resolved_at = now

    if session.current_turn_id is not None:
        current = next((t for t in turns if t.id == session.current_turn_id), None)
        if current is not None and current.status == "pending":
            current.status = "unanswered"
            current.resolved_at = now
    session.current_turn_id = None

    if session.follow_up_job_id is not None:
        job = db.get(Job, session.follow_up_job_id)
        if job is not None and job.state in ("queued", "running"):
            jobs_service.supersede_job(job)

    scored_question_ids = [q.id for q in questions if q.id in answered_question_ids]
    db.flush()
    if not scored_question_ids:
        _publish_report_row(
            db,
            session,
            questions=questions,
            turns=turns,
            answers=answers,
            quality="no_answers",
            dimensions_by_question={},
            rubric_policy_version=RUBRIC_POLICY_VERSION,
            prompt_version="",
            model_id="",
        )
        return session

    input_payload = {
        "session_id": str(session.id),
        "ended_revision": session.ended_revision,
        "question_ids": [str(qid) for qid in scored_question_ids],
        "rubric_policy_version": RUBRIC_POLICY_VERSION,
        "prompt_version": SCORE_PROMPT_VERSION,
    }
    input_hash = _sha256(
        "|".join(
            [
                str(session.id),
                str(session.ended_revision),
                *sorted(str(qid) for qid in scored_question_ids),
                RUBRIC_POLICY_VERSION,
                SCORE_PROMPT_VERSION,
            ]
        )
    )
    job, _created = jobs_service.create_or_get_job(
        db,
        owner_id=session.owner_id,
        kind="interview_report",
        target_id=session.id,
        input_revision=session.ended_revision,
        input_hash=input_hash,
        input_payload=input_payload,
    )
    session.report_job_id = job.id
    db.flush()
    return session


def _advance_after_question(
    db: Session, session: InterviewSession, question: InterviewQuestion
) -> None:
    """调用方已递增 revision；这里发放下一题或在最后一题后收尾。"""
    next_question = db.scalars(
        select(InterviewQuestion)
        .where(
            InterviewQuestion.session_id == session.id,
            InterviewQuestion.position > question.position,
            InterviewQuestion.status == "planned",
        )
        .order_by(InterviewQuestion.position)
        .limit(1)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if next_question is None:
        _finish_session_locked(db, session, end_reason="completed")
        return
    session.current_turn_id = None
    _create_main_turn(db, session, next_question, utcnow())
    db.flush()


def submit_answer(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    revision: int,
    turn_id: uuid.UUID,
    text: str,
    max_attempts: int,
    input_source: str = "text",
    audio_asset_id: uuid.UUID | None = None,
    transcript_revision: int | None = None,
) -> tuple[InterviewReceipt, str]:
    session = _lock_session(db, owner_id, session_id)
    if session.status != "active":
        raise AppError("INTERVIEW_STATE_INVALID", "当前状态不能提交回答")
    if session.phase not in ("awaiting_answer", "deciding_follow_up"):
        raise AppError("INTERVIEW_STATE_INVALID", "当前状态不能提交回答")
    _check_revision(session, revision)
    turn, question = _current_turn_locked(db, session, turn_id)
    body = _require_text(text)
    if input_source not in ("text", "asr", "asr_corrected"):
        raise AppError("ANSWER_SOURCE_INVALID", "回答来源不合法")
    from app.services import voice as voice_service

    frozen_asset_id, frozen_transcript_revision = voice_service.validate_answer_source(
        db,
        owner_id=owner_id,
        session=session,
        turn=turn,
        input_source=input_source,
        audio_asset_id=audio_asset_id,
        transcript_revision=transcript_revision,
        text=body,
    )
    now = utcnow()

    answer = InterviewAnswer(
        id=uuid.uuid4(),
        turn_id=turn.id,
        text=body,
        input_source=input_source,
        audio_asset_id=frozen_asset_id,
        transcript_revision=frozen_transcript_revision,
        submitted_at=now,
    )
    db.add(answer)
    turn.status = "answered"
    turn.resolved_at = now
    session.current_turn_id = None

    if turn.kind == "main":
        session.phase = "deciding_follow_up"
        session.revision += 1
        session.follow_up_input_revision = session.revision
        db.flush()
        input_payload = {
            "session_id": str(session.id),
            "question_id": str(question.id),
            "answer_id": str(answer.id),
            "input_revision": session.follow_up_input_revision,
            "source": question.source,
            "position": question.position,
            "prompt_version": FOLLOW_UP_PROMPT_VERSION,
            "rubric_policy_version": RUBRIC_POLICY_VERSION,
        }
        input_hash = _sha256(
            "|".join(
                [
                    str(session.id),
                    str(question.id),
                    str(answer.id),
                    _sha256(body),
                    question.source,
                    FOLLOW_UP_PROMPT_VERSION,
                    RUBRIC_POLICY_VERSION,
                ]
            )
        )
        job, _created = jobs_service.create_or_get_job(
            db,
            owner_id=owner_id,
            kind="follow_up",
            target_id=session.id,
            input_revision=session.follow_up_input_revision,
            input_hash=input_hash,
            input_payload=input_payload,
        )
        session.follow_up_job_id = job.id
        db.flush()
        return (
            InterviewReceipt(
                session_id=session.id,
                revision=session.revision,
                answer_id=answer.id,
                job_id=job.id,
            ),
            "main",
        )

    # 追问回答：直接结清该主问题。
    question.status = "answered"
    if question.follow_up_outcome is None:
        question.follow_up_outcome = "asked"
    session.revision += 1
    db.flush()
    _advance_after_question(db, session, question)
    db.flush()
    return (
        InterviewReceipt(
            session_id=session.id,
            revision=session.revision,
            answer_id=answer.id,
            job_id=session.report_job_id,
        ),
        "follow_up",
    )


def skip_turn(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    revision: int,
    turn_id: uuid.UUID,
    max_attempts: int,
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status != "active" or session.phase != "awaiting_answer":
        raise AppError("INTERVIEW_STATE_INVALID", "当前状态不能跳过")
    _check_revision(session, revision)
    turn, question = _current_turn_locked(db, session, turn_id)
    now = utcnow()
    turn.status = "skipped"
    turn.resolved_at = now
    session.current_turn_id = None
    session.revision += 1

    if turn.kind == "main":
        question.status = "skipped"
        question.follow_up_outcome = None
    else:
        question.status = "answered"
        if question.follow_up_outcome is None:
            question.follow_up_outcome = "asked"
    db.flush()
    _advance_after_question(db, session, question)
    db.flush()
    return InterviewReceipt(
        session_id=session.id,
        revision=session.revision,
        job_id=session.report_job_id,
    )


def pause_interview(
    db: Session, *, owner_id: uuid.UUID, session_id: uuid.UUID, revision: int
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status != "active":
        raise AppError("INTERVIEW_STATE_INVALID", "只有进行中的面试可以暂停")
    _check_revision(session, revision)
    session.status = "paused"
    session.revision += 1
    db.flush()
    return InterviewReceipt(session_id=session.id, revision=session.revision)


def resume_interview(
    db: Session, *, owner_id: uuid.UUID, session_id: uuid.UUID, revision: int
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status != "paused":
        raise AppError("INTERVIEW_STATE_INVALID", "只有已暂停的面试可以继续")
    _check_revision(session, revision)
    session.status = "active"
    session.revision += 1
    db.flush()
    return InterviewReceipt(session_id=session.id, revision=session.revision)


def continue_without_follow_up(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    revision: int,
    job_id: uuid.UUID,
    max_attempts: int,
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status not in ("active", "paused") or session.phase != "deciding_follow_up":
        raise AppError("INTERVIEW_STATE_INVALID", "当前没有待处理的追问判定")
    _check_revision(session, revision)
    if session.follow_up_job_id != job_id:
        raise AppError("TURN_NOT_CURRENT", "追问任务已变化，请刷新")
    job = db.scalars(
        select(Job)
        .where(Job.id == job_id, Job.owner_id == owner_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if job is None:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    if job.state not in ("failed", "interrupted"):
        raise AppError("RETRY_NOT_ALLOWED", "该追问任务仍可重试或正在处理，不能跳过")
    question = db.scalars(
        select(InterviewQuestion)
        .where(
            InterviewQuestion.session_id == session.id,
            InterviewQuestion.status == "current",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if question is None:
        raise AppError("INTERVIEW_STATE_INVALID", "当前主问题已结清，请刷新")
    question.status = "answered"
    question.follow_up_outcome = "unavailable"
    jobs_service.supersede_job(job)
    session.current_turn_id = None
    session.revision += 1
    db.flush()
    _advance_after_question(db, session, question)
    db.flush()
    return InterviewReceipt(
        session_id=session.id,
        revision=session.revision,
        job_id=session.report_job_id,
    )


def finish_interview(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    revision: int,
    max_attempts: int,
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status == "ended":
        raise AppError("INTERVIEW_STATE_INVALID", "面试已结束")
    _check_revision(session, revision)
    session.revision += 1
    _finish_session_locked(db, session, end_reason="early_finish")
    db.flush()
    return InterviewReceipt(
        session_id=session.id,
        revision=session.revision,
        job_id=session.report_job_id,
    )


# --------------------------------------------------------------------------- 追问判定（worker）


def apply_follow_up_decision(
    db: Session,
    *,
    session: InterviewSession,
    job: Job,
    decision: dict,
    max_attempts: int,
) -> None:
    """在 worker 已锁 session 且通过 fence 的事务内应用追问判定。"""
    if session.status == "ended" or session.phase != "deciding_follow_up":
        raise AppError("INTERVIEW_STATE_INVALID", "面试已不在追问判定阶段")
    if session.follow_up_job_id != job.id:
        raise AppError("TURN_NOT_CURRENT", "追问任务已不是当前任务")
    if job.input_revision != session.follow_up_input_revision:
        raise AppError("TURN_NOT_CURRENT", "追问任务输入已过期")
    question = db.scalars(
        select(InterviewQuestion)
        .where(
            InterviewQuestion.session_id == session.id,
            InterviewQuestion.status == "current",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if question is None:
        raise AppError("INTERVIEW_STATE_INVALID", "当前主问题已结清")
    main_turn = db.scalars(
        select(InterviewTurn).where(
            InterviewTurn.question_id == question.id, InterviewTurn.kind == "main"
        )
    ).one_or_none()
    if main_turn is None:
        raise AppError("INTERVIEW_STATE_INVALID", "当前主问题缺少主回合")

    if decision.get("decision") == "ask":
        existing = db.scalars(
            select(InterviewTurn).where(
                InterviewTurn.question_id == question.id,
                InterviewTurn.kind == "follow_up",
            )
        ).one_or_none()
        if existing is not None:
            raise AppError("AI_INVALID_OUTPUT", "同一主问题不能生成第二个追问回合")
        prompt = decision["prompt"]
        turn = InterviewTurn(
            id=uuid.uuid4(),
            question_id=question.id,
            kind="follow_up",
            prompt=prompt,
            status="pending",
            presented_at=utcnow(),
        )
        db.add(turn)
        db.flush()
        question.follow_up_outcome = "asked"
        session.current_turn_id = turn.id
        session.phase = "awaiting_answer"
        session.revision += 1
        jobs_service.mark_succeeded(
            job, entity_type="interview_session", entity_id=session.id
        )
        return

    question.status = "answered"
    question.follow_up_outcome = "not_needed"
    session.current_turn_id = None
    session.revision += 1
    db.flush()
    _advance_after_question(db, session, question)
    jobs_service.mark_succeeded(job, entity_type="interview_session", entity_id=session.id)


# --------------------------------------------------------------------------- 评分与报告


def _expected_dimensions(question: InterviewQuestion) -> list[dict]:
    rubric = (question.evaluation or {}).get("rubric") or {}
    dims = []
    for dim in rubric.get("dimensions") or []:
        dims.append(
            {
                "id": dim["id"],
                "name": dim["name"],
                "weight": float(dim["weight"]),
                "low_anchor": dim["low_anchor"],
                "high_anchor": dim["high_anchor"],
            }
        )
    dims.append(
        {
            "id": EXPRESSION_DIMENSION_ID,
            "name": EXPRESSION_DIMENSION_NAME,
            "weight": EXPRESSION_WEIGHT,
            "low_anchor": "文字缺少因果或步骤组织，难以跟随",
            "high_anchor": "文字按因果或步骤组织，结论与依据可跟随",
        }
    )
    return dims


def _ordered_question_turns(
    turns: list[InterviewTurn],
) -> list[InterviewTurn]:
    """按实际曝光顺序排列同一主问题的回合；时间相同则主问题优先。"""
    fallback = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(
        turns,
        key=lambda turn: (
            turn.presented_at or fallback,
            turn.kind != "main",
            str(turn.id),
        ),
    )


def question_slot_hash(question: InterviewQuestion, turns: list[InterviewTurn], answers: dict) -> str:
    """评分槽位的稳定内容标识。

    题干、回合顺序、回答标识与原文摘要、评分口径和提示词版本全部纳入确定性 JSON；
    worker 查缓存与发布事务重核使用同一规则。旧版本槽位不会与新 hash 命中。
    """
    ordered = _ordered_question_turns(turns)
    payload = {
        "question_id": str(question.id),
        "position": question.position,
        "source": question.source,
        "stem": question.stem,
        "dimensions": _expected_dimensions(question),
        "rubric_policy_version": RUBRIC_POLICY_VERSION,
        "score_prompt_version": SCORE_PROMPT_VERSION,
        "turns": [
            {
                "order": index,
                "turn_id": str(turn.id),
                "kind": turn.kind,
                "prompt": turn.prompt,
                "answer_id": str(answers[turn.id].id) if turn.id in answers else None,
                "answer_sha256": (
                    _sha256(answers[turn.id].text) if turn.id in answers else None
                ),
            }
            for index, turn in enumerate(ordered)
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha256(canonical)


class CachedScoreSlotError(AppError):
    """仅表示已持久化的评分槽位载荷或哈希未通过确定性校验。"""


def validate_question_score(
    question: InterviewQuestion,
    turns: list[InterviewTurn],
    answers: dict[uuid.UUID, InterviewAnswer],
    output: dict,
) -> list[InterviewDimensionResult]:
    try:
        model = ScoreQuestionOutput.model_validate(output)
    except ValidationError as exc:
        raise AppError(
            "AI_INVALID_OUTPUT",
            "评分输出结构不符合契约",
            details=[
                "stage=interview_score",
                "reason=invalid_score_schema",
                *safe_validation_details(exc, ScoreQuestionOutput),
            ],
        ) from exc
    except Exception as exc:
        raise AppError(
            "AI_INVALID_OUTPUT",
            "评分输出结构不符合契约",
            details=["stage=interview_score", "reason=invalid_score_schema", type(exc).__name__],
        )
    if model.question_id != question.id:
        raise AppError("AI_INVALID_OUTPUT", "评分输出引用了其他题目")
    expected = {dim["id"]: dim for dim in _expected_dimensions(question)}
    seen: set[str] = set()
    results: list[InterviewDimensionResult] = []
    for item in model.dimensions:
        dim = expected.get(item.dimension_id)
        if dim is None:
            unknown_count = sum(
                1 for candidate in model.dimensions if candidate.dimension_id not in expected
            )
            raise AppError(
                "AI_INVALID_OUTPUT",
                "评分输出包含未知维度",
                [
                    "stage=interview_score",
                    "reason=unknown_dimension_id",
                    "field=dimension_id",
                    f"unknown_count={unknown_count}",
                ],
            )
        if item.dimension_id in seen:
            raise AppError(
                "AI_INVALID_OUTPUT",
                "评分输出包含重复维度",
                ["stage=interview_score", f"dimension_id={item.dimension_id}"],
            )
        seen.add(item.dimension_id)
        evidence: list[InterviewEvidence] = []
        if item.verdict == "scored":
            if item.score is None:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "scored 维度必须给出 0–4 分",
                    ["stage=interview_score", f"dimension_id={item.dimension_id}"],
                )
            if not item.evidence:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "scored 维度必须引用原话",
                    ["stage=interview_score", f"dimension_id={item.dimension_id}"],
                )
            valid = True
            for anchor in item.evidence:
                answer = None
                for turn in turns:
                    candidate = answers.get(turn.id)
                    if candidate is not None and candidate.id == anchor.answer_id:
                        answer = candidate
                        break
                if answer is None:
                    valid = False
                    break
                if (
                    not anchor.quote
                    or not anchor.quote.strip()
                    or _codepoint_len(anchor.quote) > MAX_QUOTE_CODEPOINTS
                    or anchor.quote not in answer.text
                ):
                    valid = False
                    break
                evidence.append(
                    InterviewEvidence(answer_id=anchor.answer_id, quote=anchor.quote)
                )
            if not valid:
                # 无效引文降为受限维度，清掉分数、引文和依赖失效引文的结论与建议。
                results.append(
                    InterviewDimensionResult(
                        dimension_id=item.dimension_id,
                        name=dim["name"],
                        weight=dim["weight"],
                        verdict="uncertain",
                        score=None,
                        evidence=[],
                        explanation="原回答无法定位引文，已降为受限结果；该维度的建议一并撤回。",
                        improvement="",
                    )
                )
                continue
        else:
            if item.score is not None:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "非 scored 维度不能携带分数",
                    ["stage=interview_score", f"dimension_id={item.dimension_id}"],
                )
            if item.evidence:
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "非 scored 维度不能携带引文",
                    ["stage=interview_score", f"dimension_id={item.dimension_id}"],
                )
        results.append(
            InterviewDimensionResult(
                dimension_id=item.dimension_id,
                name=dim["name"],
                weight=dim["weight"],
                verdict=item.verdict,
                score=item.score,
                evidence=evidence,
                explanation=item.explanation,
                improvement=item.improvement,
            )
        )
    missing = set(expected) - seen
    if missing:
        raise AppError(
            "AI_INVALID_OUTPUT", "评分输出缺少维度", sorted(missing)
        )
    return results


def validate_cached_question_score(
    item: JobWorkItem | None,
    question: InterviewQuestion,
    turns: list[InterviewTurn],
    answers: dict[uuid.UUID, InterviewAnswer],
    expected_hash: str,
) -> list[InterviewDimensionResult]:
    """统一读取并严格校验报告槽位缓存；Worker 跳过与报告发布共用同一入口。"""
    if item is None or not getattr(item, "validated", False):
        raise CachedScoreSlotError(
            "AI_INVALID_OUTPUT",
            "已保存的评分结果无法通过校验",
            details=["stage=interview_report", "reason=invalid_cached_score_slot", f"slot={question.id}"],
        )
    if item.content_hash != expected_hash or not isinstance(item.payload, dict):
        raise CachedScoreSlotError(
            "AI_INVALID_OUTPUT",
            "已保存的评分结果无法通过校验",
            details=["stage=interview_report", "reason=invalid_cached_score_slot", f"slot={question.id}"],
        )
    try:
        slot_payload = CachedScoreSlotPayload.model_validate(item.payload)
    except Exception as exc:
        raise CachedScoreSlotError(
            "AI_INVALID_OUTPUT",
            "已保存的评分结果无法通过校验",
            details=[
                "stage=interview_report",
                "reason=invalid_cached_score_slot",
                f"slot={question.id}",
                type(exc).__name__,
            ],
        ) from exc
    if slot_payload.question_id != question.id or slot_payload.content_hash != expected_hash:
        raise CachedScoreSlotError(
            "AI_INVALID_OUTPUT",
            "已保存的评分结果无法通过校验",
            details=["stage=interview_report", "reason=invalid_cached_score_slot", f"slot={question.id}"],
        )
    expected_by_id = {dim["id"]: dim for dim in _expected_dimensions(question)}
    for dim in slot_payload.dimensions:
        expected_dim = expected_by_id.get(dim.dimension_id)
        if (
            expected_dim is None
            or dim.name != expected_dim["name"]
            or abs(dim.weight - expected_dim["weight"]) > 1e-6
        ):
            raise CachedScoreSlotError(
                "AI_INVALID_OUTPUT",
                "已保存的评分结果无法通过校验",
                details=["stage=interview_report", "reason=invalid_cached_score_slot", f"slot={question.id}"],
            )
    raw_dimensions = [
        {
            "dimension_id": dim.dimension_id,
            "verdict": dim.verdict,
            "score": dim.score,
            "evidence": [
                {"answer_id": str(ev.answer_id), "quote": ev.quote}
                for ev in dim.evidence
            ],
            "explanation": dim.explanation,
            "improvement": dim.improvement,
        }
        for dim in slot_payload.dimensions
    ]
    try:
        return validate_question_score(
            question,
            turns,
            answers,
            {"question_id": str(question.id), "dimensions": raw_dimensions},
        )
    except AppError as exc:
        raise CachedScoreSlotError(
            "AI_INVALID_OUTPUT",
            "已保存的评分结果无法通过校验",
            details=["stage=interview_report", "reason=invalid_cached_score_slot", f"slot={question.id}"],
        ) from exc


def question_score(
    question: InterviewQuestion, dimensions: list[InterviewDimensionResult]
) -> int | None:
    rubric_dims = [
        dim for dim in _expected_dimensions(question) if dim["id"] != EXPRESSION_DIMENSION_ID
    ]
    by_id = {dim.dimension_id: dim for dim in dimensions}
    total_weight = 0.0
    weighted = 0.0
    for dim in rubric_dims:
        result = by_id.get(dim["id"])
        if result is None or result.verdict != "scored" or result.score is None:
            return None
        total_weight += dim["weight"]
        weighted += dim["weight"] * result.score
    if total_weight <= 0:
        return None
    return int((25 * weighted / total_weight) + 0.5)


def _report_questions(
    questions: list[InterviewQuestion],
    turns: list[InterviewTurn],
    answers: dict[uuid.UUID, InterviewAnswer],
    dimensions_by_question: dict[uuid.UUID, list[InterviewDimensionResult]],
) -> list[InterviewReportQuestion]:
    turns_by_question: dict[uuid.UUID, list[InterviewTurn]] = {}
    for turn in turns:
        turns_by_question.setdefault(turn.question_id, []).append(turn)
    items: list[InterviewReportQuestion] = []
    for question in questions:
        question_turns = _ordered_question_turns(turns_by_question.get(question.id, []))
        outcome = question.status
        if outcome == "current":
            outcome = "unanswered"
        evaluation = question.evaluation or {}
        rubric = evaluation.get("rubric") or {}
        dims = dimensions_by_question.get(question.id, [])
        items.append(
            InterviewReportQuestion(
                question_id=question.id,
                position=question.position,
                source=question.source,
                outcome=outcome,
                prompt=None if outcome == "not_reached" else question.stem,
                bank_version=(
                    (question.bank_ref or {}).get("version")
                    if question.source == "bank"
                    else None
                ),
                turns=(
                    []
                    if outcome == "not_reached"
                    else [
                        _turn_view(turn, question, answers.get(turn.id))
                        for turn in question_turns
                    ]
                ),
                assessment_trust=evaluation.get("trust", "provisional"),
                rubric_version=f"{rubric.get('id', 'unknown')}@{rubric.get('version', 1)}",
                question_score=question_score(question, dims) if dims else None,
                dimensions=dims,
                follow_up_outcome=question.follow_up_outcome,
            )
        )
    return items


def _coverage(
    questions: list[InterviewQuestion],
    report_questions: list[InterviewReportQuestion],
) -> InterviewCoverage:
    presented = sum(1 for q in questions if q.status != "not_reached")
    answered = sum(1 for q in questions if q.status == "answered")
    skipped = sum(1 for q in questions if q.status == "skipped")
    unanswered = sum(1 for q in questions if q.status == "unanswered")
    not_reached = sum(1 for q in questions if q.status == "not_reached")
    scored = sum(1 for item in report_questions if item.question_score is not None)
    bank_planned = sum(1 for q in questions if q.source == "bank")
    generated_planned = sum(1 for q in questions if q.source == "generated")
    bank_answered = sum(
        1 for q in questions if q.source == "bank" and q.status == "answered"
    )
    generated_answered = sum(
        1 for q in questions if q.source == "generated" and q.status == "answered"
    )
    follow_up_turns = [
        turn
        for item in report_questions
        for turn in item.turns
        if turn.kind == "follow_up"
    ]
    return InterviewCoverage(
        planned=len(questions),
        presented=presented,
        answered=answered,
        skipped=skipped,
        unanswered=unanswered,
        not_reached=not_reached,
        scored=scored,
        bank_planned=bank_planned,
        bank_answered=bank_answered,
        generated_planned=generated_planned,
        generated_answered=generated_answered,
        follow_up_presented=len(follow_up_turns),
        follow_up_answered=sum(1 for turn in follow_up_turns if turn.status == "answered"),
    )


def _findings(
    report_questions: list[InterviewReportQuestion],
    *,
    strengths: bool,
) -> list[InterviewFinding]:
    findings: list[InterviewFinding] = []
    for item in report_questions:
        for dim in item.dimensions:
            if dim.verdict != "scored" or dim.score is None:
                continue
            if strengths and dim.score < 3:
                continue
            if not strengths and dim.score > 1:
                continue
            text = (
                f"{item.position}. {dim.name}：{dim.explanation or '达到该维度的正向锚点。'}"
                if strengths
                else f"{item.position}. {dim.name}：{dim.improvement or dim.explanation or '需要补充具体依据。'}"
            )
            findings.append(
                InterviewFinding(
                    question_id=item.question_id,
                    position=item.position,
                    dimension_id=dim.dimension_id,
                    text=text,
                    evidence=list(dim.evidence),
                )
            )
            if len(findings) >= 3:
                return findings
    return findings


def build_report_payload(
    *,
    session: InterviewSession,
    report_id: uuid.UUID,
    questions: list[InterviewQuestion],
    turns: list[InterviewTurn],
    answers: dict[uuid.UUID, InterviewAnswer],
    quality: str,
    dimensions_by_question: dict[uuid.UUID, list[InterviewDimensionResult]],
    rubric_policy_version: str,
    created_at: datetime,
) -> dict:
    report_questions = _report_questions(questions, turns, answers, dimensions_by_question)
    coverage = _coverage(questions, report_questions)
    material_versions = [
        InterviewMaterialVersion(**item).model_dump(mode="json")
        for item in session.plan_snapshot["material_versions"]
    ]
    payload = {
        "report_id": str(report_id),
        "session_id": str(session.id),
        "role_id": session.role_id,
        "level": session.level,
        "material_versions": material_versions,
        "quality": quality,
        "rubric_policy_version": rubric_policy_version,
        "created_at": created_at.isoformat(),
        "end_reason": session.end_reason,
        "coverage": coverage.model_dump(mode="json"),
        "strengths": [item.model_dump(mode="json") for item in _findings(report_questions, strengths=True)],
        "improvements": [
            item.model_dump(mode="json") for item in _findings(report_questions, strengths=False)
        ],
        "questions": [item.model_dump(mode="json") for item in report_questions],
    }
    # 用公开 DTO 反向校验，避免内部字段混入持久报告。
    InterviewReportSchema.model_validate(payload)
    return payload


def _publish_report_row(
    db: Session,
    session: InterviewSession,
    *,
    questions: list[InterviewQuestion],
    turns: list[InterviewTurn],
    answers: dict[uuid.UUID, InterviewAnswer],
    quality: str,
    dimensions_by_question: dict[uuid.UUID, list[InterviewDimensionResult]],
    rubric_policy_version: str,
    prompt_version: str,
    model_id: str,
) -> InterviewReport:
    existing = db.scalars(
        select(InterviewReport).where(InterviewReport.session_id == session.id)
    ).one_or_none()
    if existing is not None:
        return existing
    report_id = uuid.uuid4()
    created_at = utcnow()
    payload = build_report_payload(
        session=session,
        report_id=report_id,
        questions=questions,
        turns=turns,
        answers=answers,
        quality=quality,
        dimensions_by_question=dimensions_by_question,
        rubric_policy_version=rubric_policy_version,
        created_at=created_at,
    )
    row = InterviewReport(
        id=report_id,
        session_id=session.id,
        rubric_policy_version=rubric_policy_version,
        prompt_version=prompt_version,
        model_id=model_id,
        quality=quality,
        payload=payload,
        created_at=created_at,
    )
    db.add(row)
    db.flush()
    return row


def publish_report_from_work(
    db: Session,
    *,
    session: InterviewSession,
    job: Job,
    max_attempts: int,
) -> InterviewReport:
    """在 worker 已锁 session 且通过 fence 的事务内发布报告。"""
    if session.status != "ended" or session.ended_revision is None:
        raise AppError("INTERVIEW_STATE_INVALID", "面试尚未结束")
    if session.report_job_id != job.id or job.input_revision != session.ended_revision:
        raise AppError("TURN_NOT_CURRENT", "报告任务已不是当前任务")
    existing = db.scalars(
        select(InterviewReport).where(InterviewReport.session_id == session.id)
    ).one_or_none()
    if existing is not None:
        jobs_service.mark_succeeded(
            job, entity_type="interview_report", entity_id=existing.id
        )
        return existing

    questions = _questions(db, session.id)
    turns = list(
        db.scalars(
            select(InterviewTurn).where(
                InterviewTurn.question_id.in_([q.id for q in questions])
            )
        ).all()
    )
    answers = _answers_by_turn(db, [t.id for t in turns])
    work_items = {
        item.slot_key: item
        for item in db.scalars(
            select(JobWorkItem).where(
                JobWorkItem.job_id == job.id, JobWorkItem.validated.is_(True)
            )
        ).all()
    }
    dimensions_by_question: dict[uuid.UUID, list[InterviewDimensionResult]] = {}
    for question in questions:
        if question.status != "answered":
            continue
        item = work_items.get(str(question.id))
        if item is None:
            raise AppError("INTERVIEW_STATE_INVALID", "评分结果不完整，不能发布报告")
        question_turns = _ordered_question_turns(
            [t for t in turns if t.question_id == question.id]
        )
        question_answers = {
            t.id: answers[t.id] for t in question_turns if t.id in answers
        }
        expected_hash = question_slot_hash(question, question_turns, question_answers)
        if item.content_hash != expected_hash:
            raise AppError(
                "INTERVIEW_STATE_INVALID", "评分结果与当前问答内容不一致，不能发布报告"
            )
        try:
            dims = validate_cached_question_score(
                item,
                question,
                question_turns,
                question_answers,
                expected_hash,
            )
        except CachedScoreSlotError as exc:
            raise AppError(exc.code, exc.message, details=exc.details) from exc
        dimensions_by_question[question.id] = dims

    answered_questions = [q for q in questions if q.status == "answered"]
    full = True
    for question in answered_questions:
        dims = dimensions_by_question.get(question.id) or []
        expected_ids = {dim["id"] for dim in _expected_dimensions(question)}
        scored_ids = {
            dim.dimension_id
            for dim in dims
            if dim.verdict == "scored" and dim.score is not None
        }
        if not expected_ids.issubset(scored_ids):
            full = False
            break
    quality = "full" if full else "partial"
    return _publish_report_row(
        db,
        session,
        questions=questions,
        turns=turns,
        answers=answers,
        quality=quality,
        dimensions_by_question=dimensions_by_question,
        rubric_policy_version=RUBRIC_POLICY_VERSION,
        prompt_version=SCORE_PROMPT_VERSION,
        model_id=(job.ai_call_records or [{}])[-1].get("model", "")
        if job.ai_call_records
        else "",
    )


def get_report(
    db: Session, *, owner_id: uuid.UUID, session_id: uuid.UUID
) -> InterviewReportSchema:
    session = _session_for_owner(db, owner_id, session_id)
    if session.status != "ended":
        raise AppError("REPORT_NOT_AVAILABLE", "面试尚未结束，不能查看报告")
    row = db.scalars(
        select(InterviewReport).where(InterviewReport.session_id == session.id)
    ).one_or_none()
    if row is not None:
        return InterviewReportSchema.model_validate(row.payload)
    job = db.get(Job, session.report_job_id) if session.report_job_id else None
    if job is None:
        raise AppError("REPORT_NOT_AVAILABLE", "该场次没有可用的报告")
    if job.state in ("failed", "interrupted"):
        raise AppError("REPORT_FAILED", "报告生成失败，可重试或生成基础报告")
    raise AppError("REPORT_PENDING", "报告仍在生成中，请稍后再试")


def create_basic_report(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    revision: int,
    job_id: uuid.UUID,
    max_attempts: int,
) -> InterviewReceipt:
    session = _lock_session(db, owner_id, session_id)
    if session.status != "ended":
        raise AppError("REPORT_NOT_AVAILABLE", "面试尚未结束，不能生成报告")
    _check_revision(session, revision)
    if session.report_job_id != job_id:
        raise AppError("REPORT_NOT_AVAILABLE", "报告任务已变化，请刷新")
    existing = db.scalars(
        select(InterviewReport).where(InterviewReport.session_id == session.id)
    ).one_or_none()
    if existing is not None:
        raise AppError("REPORT_NOT_AVAILABLE", "该场次已有报告")
    job = db.scalars(
        select(Job)
        .where(Job.id == job_id, Job.owner_id == owner_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if job is None:
        raise AppError("NOT_FOUND", "任务不存在", status_code=404)
    if job.state not in ("failed", "interrupted"):
        raise AppError("REPORT_PENDING", "报告任务仍在处理中，不能生成基础报告")
    jobs_service.supersede_job(job)

    questions = _questions(db, session.id)
    turns = list(
        db.scalars(
            select(InterviewTurn).where(
                InterviewTurn.question_id.in_([q.id for q in questions])
            )
        ).all()
    )
    answers = _answers_by_turn(db, [t.id for t in turns])
    _publish_report_row(
        db,
        session,
        questions=questions,
        turns=turns,
        answers=answers,
        quality="unscored",
        dimensions_by_question={},
        rubric_policy_version=RUBRIC_POLICY_VERSION,
        prompt_version="",
        model_id="",
    )
    session.revision += 1
    db.flush()
    return InterviewReceipt(session_id=session.id, revision=session.revision)
