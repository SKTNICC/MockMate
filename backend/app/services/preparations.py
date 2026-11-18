"""Preparation 生命周期：配置校验、版本栅栏、匹配与计划任务入队、删除语义。"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Text, cast, delete, select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError, materials_required
from app.models import (
    IdempotencyRecord,
    Job,
    MaterialSnapshot,
    MatchReport,
    Plan,
    Preparation,
)
from app.schemas import Preparation as PreparationSchema
from app.services import catalog
from app.services.bank import (
    EligibleBankSource,
    EligibleFactCard,
    digest as canonical_digest,
    eligible_bank_questions,
    eligible_fact_card,
    select_bank_sources,
)
from app.services.jobs import create_or_get_job, supersede_job
from app.services.text import sha256_text


def preparation_to_schema(prep: Preparation) -> PreparationSchema:
    return PreparationSchema(
        id=prep.id,
        revision=prep.revision,
        role_id=prep.role_id,
        level=prep.level,
        main_question_count=prep.main_question_count,
        resume_snapshot_id=prep.resume_snapshot_id,
        jd_snapshot_id=prep.jd_snapshot_id,
        match_report_id=prep.match_report_id,
        current_plan_id=prep.current_plan_id,
        created_at=prep.created_at,
        updated_at=prep.updated_at,
    )


def get_preparation(
    db: Session,
    owner_id: uuid.UUID,
    preparation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> Preparation:
    if for_update:
        prep = db.scalars(
            select(Preparation)
            .where(Preparation.id == preparation_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
    else:
        prep = db.get(Preparation, preparation_id)
    if prep is None or prep.owner_id != owner_id:
        raise AppError("NOT_FOUND", "准备不存在", status_code=404)
    return prep


def list_preparations(db: Session, owner_id: uuid.UUID) -> list[PreparationSchema]:
    rows = db.scalars(
        select(Preparation)
        .where(Preparation.owner_id == owner_id)
        .order_by(Preparation.updated_at.desc())
        .limit(100)
    ).all()
    return [preparation_to_schema(row) for row in rows]


def _load_snapshot(db: Session, owner_id: uuid.UUID, snapshot_id: uuid.UUID, kind: str) -> MaterialSnapshot:
    row = db.get(MaterialSnapshot, snapshot_id)
    if row is None or row.owner_id != owner_id:
        raise AppError("NOT_FOUND", "材料版本不存在", status_code=404)
    if row.kind != kind:
        raise AppError(
            "MATERIALS_REQUIRED",
            "材料类型与字段不匹配",
            details=[f"expected_{kind}"],
        )
    return row


def validate_configuration(
    db: Session,
    owner_id: uuid.UUID,
    *,
    role_id: str,
    level: str,
    main_question_count: int,
    resume_snapshot_id: uuid.UUID | None,
    jd_snapshot_id: uuid.UUID | None,
    require_enabled_role: bool = True,
) -> tuple[MaterialSnapshot | None, MaterialSnapshot | None]:
    catalog.require_role_level(db, role_id, level, require_enabled=require_enabled_role)
    if main_question_count not in (2, 4, 6):
        raise AppError("INVALID_INPUT", "题量必须是 2、4、6")
    resume = _load_snapshot(db, owner_id, resume_snapshot_id, "resume") if resume_snapshot_id else None
    jd = _load_snapshot(db, owner_id, jd_snapshot_id, "jd") if jd_snapshot_id else None
    if jd is not None:
        profile = jd.profile or {}
        if profile.get("role_id") != role_id or profile.get("level") != level:
            raise AppError(
                "ROLE_LEVEL_MISMATCH",
                "JD 的岗位或级别与本次准备设置不一致，请修改设置或更换 JD",
                details=["role_id", "level"],
            )
    return resume, jd


def create_preparation(db: Session, owner_id: uuid.UUID, payload) -> Preparation:
    validate_configuration(
        db,
        owner_id,
        role_id=payload.role_id,
        level=payload.level,
        main_question_count=payload.main_question_count,
        resume_snapshot_id=payload.resume_snapshot_id,
        jd_snapshot_id=payload.jd_snapshot_id,
    )
    prep = Preparation(
        owner_id=owner_id,
        revision=1,
        role_id=payload.role_id,
        level=payload.level,
        main_question_count=payload.main_question_count,
        resume_snapshot_id=payload.resume_snapshot_id,
        jd_snapshot_id=payload.jd_snapshot_id,
    )
    db.add(prep)
    db.flush()
    return prep


def edit_preparation(db: Session, prep: Preparation, payload) -> Preparation:
    # 以数据库当前 revision 为准，不能只比较先前 Session 中的对象。
    locked = get_preparation(db, prep.owner_id, prep.id, for_update=True)
    if locked.revision != payload.revision:
        raise AppError(
            "REVISION_CONFLICT",
            "准备配置已变化，请刷新后重试",
            details=[f"current_revision={locked.revision}"],
        )
    validate_configuration(
        db,
        locked.owner_id,
        role_id=payload.role_id,
        level=payload.level,
        main_question_count=payload.main_question_count,
        resume_snapshot_id=payload.resume_snapshot_id,
        jd_snapshot_id=payload.jd_snapshot_id,
    )
    changed = (
        locked.role_id != payload.role_id
        or locked.level != payload.level
        or locked.main_question_count != payload.main_question_count
        or locked.resume_snapshot_id != payload.resume_snapshot_id
        or locked.jd_snapshot_id != payload.jd_snapshot_id
    )
    if not changed:
        return locked

    locked.role_id = payload.role_id
    locked.level = payload.level
    locked.main_question_count = payload.main_question_count
    locked.resume_snapshot_id = payload.resume_snapshot_id
    locked.jd_snapshot_id = payload.jd_snapshot_id
    locked.revision = locked.revision + 1
    locked.match_report_id = None
    locked.current_plan_id = None
    locked.updated_at = datetime.now(timezone.utc)
    # 锁顺序：先 Preparation，再其 Job。
    for job in db.scalars(
        select(Job).where(
            Job.target_id == locked.id,
            Job.kind.in_(("match", "plan")),
            Job.state.in_(("queued", "running")),
        )
    ).all():
        supersede_job(job)
    db.flush()
    return locked


def delete_preparation(db: Session, prep: Preparation, current_delete_path: str | None = None) -> None:
    # 先锁准备，再删除 Job／报告／计划，避免与 worker 发布形成反向锁序。
    prep = get_preparation(db, prep.owner_id, prep.id, for_update=True)
    preparation_id = prep.id
    db.execute(delete(Job).where(Job.target_id == preparation_id, Job.owner_id == prep.owner_id))
    db.execute(delete(MatchReport).where(MatchReport.preparation_id == preparation_id))
    db.execute(delete(Plan).where(Plan.preparation_id == preparation_id))
    marker = str(preparation_id)
    purge = delete(IdempotencyRecord).where(
        IdempotencyRecord.user_id == prep.owner_id,
        (IdempotencyRecord.path.like(f"%{marker}%"))
        | (cast(IdempotencyRecord.response, Text).like(f"%{marker}%")),
    )
    if current_delete_path:
        purge = purge.where(
            ~(
                (IdempotencyRecord.method == "DELETE")
                & (IdempotencyRecord.path == current_delete_path)
            )
        )
    db.execute(purge)
    db.delete(prep)
    db.flush()


def ensure_materials_ready(prep: Preparation) -> None:
    missing: list[str] = []
    if prep.resume_snapshot_id is None:
        missing.append("resume_snapshot_id")
    if prep.jd_snapshot_id is None:
        missing.append("jd_snapshot_id")
    if missing:
        raise materials_required(missing)


def load_snapshots(db: Session, prep: Preparation) -> tuple[MaterialSnapshot, MaterialSnapshot]:
    ensure_materials_ready(prep)
    resume = _load_snapshot(db, prep.owner_id, prep.resume_snapshot_id, "resume")
    jd = _load_snapshot(db, prep.owner_id, prep.jd_snapshot_id, "jd")
    return resume, jd


def create_match_job(db: Session, prep: Preparation, payload, settings: Settings) -> Job:
    from app.ai.provider import get_provider

    if prep.revision != payload.revision:
        raise AppError("REVISION_CONFLICT", "准备配置已变化，请刷新后重试")
    resume, jd = load_snapshots(db, prep)
    profile = jd.profile or {}
    if profile.get("role_id") != prep.role_id or profile.get("level") != prep.level:
        raise AppError(
            "ROLE_LEVEL_MISMATCH",
            "JD 的岗位或级别与本次准备设置不一致",
            details=["role_id", "level"],
        )
    get_provider(settings)  # 未配置时在入队前明确拒绝
    input_hash = sha256_text(
        "|".join(
            [
                str(resume.id),
                str(jd.id),
                prep.role_id,
                prep.level,
                str(prep.revision),
            ]
        )
    )
    job, _ = create_or_get_job(
        db,
        owner_id=prep.owner_id,
        kind="match",
        target_id=prep.id,
        input_revision=prep.revision,
        input_hash=input_hash,
        input_payload={
            "preparation_id": str(prep.id),
            "resume_snapshot_id": str(resume.id),
            "jd_snapshot_id": str(jd.id),
            "role_id": prep.role_id,
            "level": prep.level,
        },
    )
    return job


def prepare_plan_inputs(
    db: Session, settings: Settings, prep: Preparation
) -> tuple[
    MaterialSnapshot,
    MaterialSnapshot,
    MatchReport,
    list[EligibleBankSource],
    list,
    list,
    bool,
]:
    """校验生成前提并返回题库/依据卡输入；不足时抛业务错误。"""
    resume, jd = load_snapshots(db, prep)
    if prep.match_report_id is None:
        raise AppError("MATCH_REQUIRED", "请先完成一次匹配分析再生成计划")
    report = db.get(MatchReport, prep.match_report_id)
    if report is None or report.preparation_id != prep.id:
        raise AppError("MATCH_REQUIRED", "当前匹配报告不属于该准备")
    if report.input_revision != prep.revision:
        raise AppError("MATCH_REQUIRED", "匹配报告对应的是旧版本配置，请重新分析")
    if (
        report.resume_snapshot_id != resume.id
        or report.jd_snapshot_id != jd.id
    ):
        raise AppError("MATCH_REQUIRED", "匹配报告与当前材料不一致，请重新分析")

    half = prep.main_question_count // 2
    eligible = eligible_bank_questions(db, settings, prep.role_id, prep.level)
    bank_sources = select_bank_sources(eligible, half, resume=resume, jd=jd)

    fact_card_ids: set[str] = set()
    for item in bank_sources:
        for basis in (item.content.get("evaluation") or {}).get("basis") or []:
            if basis.get("kind") == "fact_card":
                fact_card_ids.add(basis.get("ref_id"))
    from app.services.planning import build_fact_cards

    fact_cards, fact_reviews, fact_test_only = build_fact_cards(db, settings, fact_card_ids)
    test_only = any(item.test_only for item in bank_sources) or fact_test_only
    return resume, jd, report, bank_sources, fact_cards, fact_reviews, test_only


def _resume_anchors(snapshot: MaterialSnapshot) -> list[dict]:
    anchors: list[dict] = []
    profile = snapshot.profile or {}
    for section in ("work_experience", "projects", "skills", "education"):
        for item in profile.get(section) or []:
            quote = (item.get("evidence") or {}).get("quote")
            if not quote:
                continue
            anchors.append(
                {
                    "snapshot_id": str(snapshot.id),
                    "item_id": item["item_id"],
                    "quote": quote,
                    "source_kind": "resume",
                    "label": item.get("name")
                    or item.get("organization")
                    or item.get("major")
                    or item.get("degree")
                    or item.get("item_id"),
                }
            )
    return anchors


def _jd_anchors(snapshot: MaterialSnapshot) -> list[dict]:
    anchors: list[dict] = []
    profile = snapshot.profile or {}
    for item in profile.get("requirements") or []:
        quote = (item.get("evidence") or {}).get("quote")
        if not quote:
            continue
        anchors.append(
            {
                "snapshot_id": str(snapshot.id),
                "item_id": item["item_id"],
                "quote": quote,
                "source_kind": "jd",
                "label": item.get("text", item["item_id"]),
                "skill_codes": list(item.get("skill_codes") or []),
            }
        )
    return anchors


def build_generation_context(
    *,
    slot_key: str,
    slot_index: int,
    question_type: str,
    resume: MaterialSnapshot,
    jd: MaterialSnapshot,
    report: MatchReport,
    preparation: Preparation,
    bank_sources: list[EligibleBankSource],
    all_bank: list[EligibleBankSource],
    fact_cards: list[EligibleFactCard],
    rubric: dict,
    previous_stems: list[str],
    previous_summaries: list[dict] | None = None,
) -> dict:
    """构造单个生成槽位的显式上下文；context_hash 覆盖所有会影响结果的输入。"""
    rows = list((report.payload or {}).get("rows") or [])
    requirement_by_id = {
        item["item_id"]: item for item in (jd.profile.get("requirements") or [])
    }
    status_rank = {"to_verify": 0, "supported": 1, "not_evidenced": 2}
    ordered_rows = sorted(
        rows, key=lambda row: (status_rank.get(row.get("status"), 3), row.get("requirement_id", ""))
    )
    target_skill_codes: list[str] = []
    match_focus: list[str] = []
    for row in ordered_rows:
        requirement = requirement_by_id.get(row.get("requirement_id"))
        if requirement:
            for code in requirement.get("skill_codes") or []:
                if code not in target_skill_codes:
                    target_skill_codes.append(code)
        focus = row.get("suggested_focus")
        if focus and focus not in match_focus:
            match_focus.append(focus)

    resume_anchors = _resume_anchors(resume)
    jd_anchors = _jd_anchors(jd)
    anchors = resume_anchors + jd_anchors
    card_payload = [
        {
            "id": card.card.id,
            "version": card.card.version,
            "summary": card.content.get("summary", ""),
            "url": card.content.get("url", ""),
            "content_sha256": card.card.content_sha256,
        }
        for card in fact_cards
    ]
    bank_stems = [item.content.get("stem", "") for item in all_bank]
    avoid_stems: list[str] = []
    for stem in [*bank_stems, *previous_stems]:
        if stem and stem not in avoid_stems:
            avoid_stems.append(stem)

    context = {
        "slot_key": slot_key,
        "slot_index": slot_index,
        "question_type": question_type,
        "target_skill_codes": target_skill_codes,
        "match_focus": match_focus[:6],
        "prefer_jd_scenario": not (
            (resume.profile or {}).get("projects")
            or (resume.profile or {}).get("work_experience")
        ),
        "allowed_anchors": anchors,
        "fact_cards": card_payload,
        "rubric": rubric,
        "avoid_stems": avoid_stems,
        "previous_summaries": list(previous_summaries or []),
        "resume_snapshot_id": str(resume.id),
        "jd_snapshot_id": str(jd.id),
        "preparation_revision": preparation.revision,
        "main_question_count": preparation.main_question_count,
        "match_report_id": str(report.id),
        "bank_source_digests": [
            {
                "question_id": item.question.question_id,
                "version": item.question.version,
                "content_sha256": item.question.source_digest,
            }
            for item in bank_sources
        ],
    }
    return context


def generation_context_hash(context: dict) -> str:
    return canonical_digest(context)


def create_plan_job(db: Session, prep: Preparation, payload, settings: Settings) -> Job:
    from app.ai.provider import get_provider

    if prep.revision != payload.revision:
        raise AppError("REVISION_CONFLICT", "准备配置已变化，请刷新后重试")
    get_provider(settings)
    prepare_plan_inputs(db, settings, prep)  # 审核题不足、匹配缺失在调用 AI 前同步阻断

    input_hash = sha256_text(
        "|".join(
            [
                str(prep.match_report_id),
                str(prep.resume_snapshot_id),
                str(prep.jd_snapshot_id),
                prep.role_id,
                prep.level,
                str(prep.main_question_count),
                str(prep.revision),
            ]
        )
    )
    job, _ = create_or_get_job(
        db,
        owner_id=prep.owner_id,
        kind="plan",
        target_id=prep.id,
        input_revision=prep.revision,
        input_hash=input_hash,
        input_payload={
            "preparation_id": str(prep.id),
            "match_report_id": str(prep.match_report_id),
            "resume_snapshot_id": str(prep.resume_snapshot_id),
            "jd_snapshot_id": str(prep.jd_snapshot_id),
            "role_id": prep.role_id,
            "level": prep.level,
            "main_question_count": prep.main_question_count,
        },
    )
    return job
