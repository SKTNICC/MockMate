"""计划规则：各半配额、审核准入、去重、证据与评价依据、发布投影。

服务端独立执行；不依赖 Prompt 或客户端自述。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import Plan, Preparation
from app.schemas import (
    BankCandidate,
    BankRef,
    FactCard,
    InternalPlan,
    InternalQuestion,
    PublicPlan,
    ReviewDecision,
    SemanticReview,
    Snapshot,
)
from app.services.bank import EligibleBankSource, EligibleFactCard, content_digest
from app.services.evidence import profile_items, validate_anchor
from app.services.text import normalized_stem, normalized_text

WEIGHT_EPSILON = 0.001
SEMANTIC_DUPLICATE_THRESHOLD = 0.70
GENERATED_REUSE_THRESHOLD = 0.70

# 内部规则 code -> 对外业务 code
API_ERROR_MAP = {
    "SCHEMA_INVALID": ("EVALUATION_INVALID", 409),
    "SOURCE_QUOTA": ("BANK_INSUFFICIENT", 409),
    "BANK_INELIGIBLE": ("BANK_INSUFFICIENT", 409),
    "DUPLICATE_QUESTION": ("DUPLICATE_QUESTION", 409),
    "DEDUP_UNCERTAIN": ("DUPLICATE_QUESTION", 409),
    "EVALUATION_INVALID": ("EVALUATION_INVALID", 409),
    "EVIDENCE_INVALID": ("EVIDENCE_INVALID", 409),
    "GROUNDING_INVALID": ("EVIDENCE_INVALID", 409),
    "PLAN_STATE_INVALID": ("PLAN_STALE", 409),
    "GENERATED_REUSE": ("DUPLICATE_QUESTION", 409),
}


def rule_error(code: str, message: str, details: list[str] | None = None) -> AppError:
    return AppError(code, message, details=details or [])


def to_api_error(exc: AppError) -> AppError:
    mapped = API_ERROR_MAP.get(exc.code)
    if mapped is None:
        return exc
    code, status = mapped
    return AppError(code, exc.message, details=exc.details, status_code=status)


# --------------------------------------------------------------------------- 相似度


def _normalize_for_similarity(value: str) -> str:
    return normalized_text(value)


def _bigrams(value: str) -> set[str]:
    text = _normalize_for_similarity(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[i : i + 2] for i in range(len(text) - 1)}


def stem_similarity(left: str, right: str) -> float:
    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# --------------------------------------------------------------------------- 材料辅助


def _profile_dict(profile) -> dict:
    if hasattr(profile, "model_dump"):
        return profile.model_dump(mode="json")
    return profile or {}


def snapshot_items(profile) -> dict[str, dict]:
    profile = _profile_dict(profile)
    items: dict[str, dict] = {}
    kind = profile.get("kind")
    sections = (
        ("education", "work_experience", "projects", "skills")
        if kind == "resume"
        else ("responsibilities", "requirements")
    )
    for section in sections:
        for item in profile.get(section) or []:
            item_id = item.get("item_id")
            if item_id:
                items[item_id] = item
    return items


def snapshot_to_schema(row) -> Snapshot:
    return Snapshot(
        id=row.id,
        material_id=row.material_id,
        source_revision=row.source_revision,
        kind=row.kind,
        title=row.title,
        text=row.text,
        profile=row.profile,
        template_id=row.template_id,
        template_version=row.template_version,
        confirmed_at=row.confirmed_at,
    )


# --------------------------------------------------------------------------- 校验


@dataclass
class PlanValidationContext:
    allow_test_fixtures: bool
    db: Session | None = None
    extra_fact_cards: dict[str, dict] = field(default_factory=dict)
    # 同一岗位／级别下未入选的合格候选题干，防止生成题只避开本场已选题目。
    candidate_stems: list[str] = field(default_factory=list)
    allowed_skill_codes: set[str] | None = None


def semantic_review_failure_details(plan: InternalPlan) -> list[str]:
    """生成语义复核失败的安全结构化明细，绝不包含题干原文或模型自由文本。"""
    question_meta: dict[str, dict] = {}
    gen_idx = 0
    for q in sorted(plan.questions, key=lambda item: item.position):
        if q.source == "bank":
            question_meta[q.id] = {
                "source": "bank",
                "ref": q.bank_ref.question_id if q.bank_ref else "",
            }
        else:
            gen_idx += 1
            question_meta[q.id] = {
                "source": "generated",
                "slot": f"generated-{gen_idx}",
                "index": gen_idx,
            }

    if plan.semantic_review.duplicate_pairs:
        details = [
            "stage=plan_review",
            "reason=semantic_duplicate",
            f"duplicate_count={len(plan.semantic_review.duplicate_pairs)}",
        ]
        first = plan.semantic_review.duplicate_pairs[0]
        left_id = first.get("left_id") if isinstance(first, dict) else None
        right_id = first.get("right_id") if isinstance(first, dict) else None
        left_meta = question_meta.get(str(left_id)) if left_id else None
        right_meta = question_meta.get(str(right_id)) if right_id else None
        if left_meta and right_meta:
            if left_meta["source"] == "generated" and right_meta["source"] == "bank":
                g_meta, b_meta = left_meta, right_meta
                details.extend([f"slot={g_meta['slot']}", "conflict_source=bank"])
                if b_meta["ref"]:
                    details.append(f"conflict_question_id={b_meta['ref']}")
            elif right_meta["source"] == "generated" and left_meta["source"] == "bank":
                g_meta, b_meta = right_meta, left_meta
                details.extend([f"slot={g_meta['slot']}", "conflict_source=bank"])
                if b_meta["ref"]:
                    details.append(f"conflict_question_id={b_meta['ref']}")
            elif left_meta["source"] == "generated" and right_meta["source"] == "generated":
                if left_meta["index"] >= right_meta["index"]:
                    target, other = left_meta, right_meta
                else:
                    target, other = right_meta, left_meta
                details.extend(
                    [
                        f"slot={target['slot']}",
                        "conflict_source=prior_generated",
                        f"conflict_slot={other['slot']}",
                    ]
                )
            elif left_meta["source"] == "bank" and right_meta["source"] == "bank":
                details.extend(["slot=semantic", "conflict_source=bank"])
                if left_meta["ref"]:
                    details.append(f"conflict_question_id={left_meta['ref']}")
            else:
                details.append("slot=semantic")
        else:
            details.append("slot=semantic")
        return details

    if plan.semantic_review.uncertain:
        return ["stage=plan_review", "slot=semantic", "reason=semantic_uncertain"]

    if plan.semantic_review.grounding_issues:
        first = plan.semantic_review.grounding_issues[0]
        qid = first.get("question_id") if isinstance(first, dict) else None
        meta = question_meta.get(str(qid)) if qid else None
        slot_token = (
            f"slot={meta['slot']}"
            if meta and meta["source"] == "generated"
            else "slot=semantic"
        )
        return ["stage=plan_review", slot_token, "reason=grounding_issue"]

    return []


def parse_internal_plan(raw: dict) -> InternalPlan:
    try:
        return InternalPlan.model_validate(raw)
    except ValidationError as exc:
        details = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]]
        raise rule_error("SCHEMA_INVALID", "计划结构不符合契约", details)


def validate_internal_plan(plan: InternalPlan, ctx: PlanValidationContext) -> None:
    n = plan.main_question_count
    if n not in (2, 4, 6):
        raise rule_error("SCHEMA_INVALID", "题量必须是 2、4、6", [f"count={n}"])
    if plan.state == "ready" and plan.confirmed_at is not None:
        raise rule_error("PLAN_STATE_INVALID", "ready 计划不能带确认时间")
    if plan.state == "confirmed" and plan.confirmed_at is None:
        raise rule_error("PLAN_STATE_INVALID", "confirmed 计划必须有确认时间")

    snapshots = {str(snap.id): snap for snap in plan.material_snapshots}
    resume_id, jd_id = str(plan.resume_snapshot_id), str(plan.jd_snapshot_id)
    if set(snapshots) != {resume_id, jd_id}:
        raise rule_error("EVIDENCE_INVALID", "计划必须嵌入且只嵌入本场的简历与 JD 快照")
    if snapshots[resume_id].kind != "resume" or snapshots[jd_id].kind != "jd":
        raise rule_error("EVIDENCE_INVALID", "简历/JD 快照类型不匹配")
    items_by_snapshot = {
        str(snap.id): profile_items(snap.profile) for snap in plan.material_snapshots
    }
    text_by_snapshot = {str(snap.id): snap.text for snap in plan.material_snapshots}
    available_rubrics = {
        (candidate.evaluation.rubric.id, candidate.evaluation.rubric.version): (
            candidate.evaluation.rubric.model_dump(mode="json")
        )
        for candidate in plan.bank_source_snapshots
    }

    half = n // 2
    actual_bank = sum(1 for q in plan.questions if q.source == "bank")
    actual_generated = sum(1 for q in plan.questions if q.source == "generated")
    if not (
        len(plan.questions) == n
        and actual_bank == half
        and actual_generated == half
        and plan.source_counts.bank == half
        and plan.source_counts.generated == half
    ):
        raise rule_error(
            "SOURCE_QUOTA",
            f"主问题必须题库与生成严格各半（{half}+{half}）",
            [
                f"questions={len(plan.questions)}",
                f"bank={actual_bank}",
                f"generated={actual_generated}",
                f"declared={plan.source_counts.bank}+{plan.source_counts.generated}",
            ],
        )

    positions = sorted(q.position for q in plan.questions)
    if positions != list(range(1, n + 1)):
        raise rule_error("POSITION_INVALID", "题序必须从 1 连续到 N", [str(positions)])

    bank_by_id: dict[tuple[str, int], dict] = {}
    test_source_present = False
    for candidate in plan.bank_source_snapshots:
        key = (candidate.question_id, candidate.version)
        bank_by_id[key] = candidate.model_dump(mode="json")
        digest = content_digest(candidate.model_dump(mode="json"))
        if digest != candidate.content_sha256:
            raise rule_error(
                "BANK_INELIGIBLE",
                "题库内容与审核绑定的摘要不一致",
                [candidate.question_id],
            )
        if candidate.status != "approved" or candidate.answer_status != "verified":
            raise rule_error("BANK_INELIGIBLE", "题库题目未处于已审核可用状态", [candidate.question_id])
        if candidate.license_status != "clear":
            raise rule_error("BANK_INELIGIBLE", "题库题目来源许可不明确", [candidate.question_id])
        if candidate.provenance != "authored":
            raise rule_error("BANK_INELIGIBLE", "题库题目来源非法", [candidate.question_id])
        if not candidate.source_urls:
            raise rule_error("BANK_INELIGIBLE", "题库题目缺少来源链接", [candidate.question_id])

    review_approvers: dict[tuple[str, int, str], set[str]] = {}
    for review in plan.content_reviews:
        decision_map = review_approvers.setdefault(
            (review.resource_type, review.resource_id, review.content_sha256), set()
        )
        if review.decision == "approve":
            decision_map.add(review.reviewer_id)
        elif review.decision == "reject":
            raise rule_error(
                "BANK_INELIGIBLE",
                "存在未通过的审核记录",
                [review.resource_id],
            )
    synthetic_reviewers: set[str] = set()
    if ctx.db is not None:
        from app.models import ContentReview

        for review in plan.content_reviews:
            row = ctx.db.scalars(
                select(ContentReview).where(
                    ContentReview.resource_type == review.resource_type,
                    ContentReview.resource_id == review.resource_id,
                    ContentReview.version == review.version,
                    ContentReview.reviewer_id == review.reviewer_id,
                    ContentReview.content_sha256 == review.content_sha256,
                )
            ).first()
            if row is not None and row.reviewer_kind == "synthetic_test":
                synthetic_reviewers.add(review.reviewer_id)

    fact_cards: dict[str, FactCard] = {card.id: card for card in plan.fact_cards}
    for card in plan.fact_cards:
        if card.status != "approved":
            raise rule_error("EVALUATION_INVALID", "计划引用了未审核的依据卡", [card.id])
        if card.license_status != "clear":
            raise rule_error("EVALUATION_INVALID", "计划引用了来源许可未确认的依据卡", [card.id])
        digest = content_digest(card.model_dump(mode="json"))
        if digest != card.content_sha256:
            raise rule_error("EVALUATION_INVALID", "依据卡内容与审核摘要不一致", [card.id])
        approvers = review_approvers.get(("fact_card", card.id, card.content_sha256), set())
        if len(approvers) < 2:
            raise rule_error("EVALUATION_INVALID", "依据卡缺少两位审核者的通过记录", [card.id])
        if ctx.db is not None:
            from app.models import FactCard as FactCardRow

            row = ctx.db.scalars(
                select(FactCardRow).where(
                    FactCardRow.id == card.id, FactCardRow.version == card.version
                )
            ).first()
            if row is None or row.content_sha256 != card.content_sha256 or row.status != "approved":
                raise rule_error("EVALUATION_INVALID", "依据卡与数据库审核状态不一致", [card.id])

    allowed_skill_codes = ctx.allowed_skill_codes
    if allowed_skill_codes is None and ctx.db is not None:
        from app.services import catalog

        allowed_skill_codes = catalog.allowed_skill_codes(ctx.db)

    used_fact_cards: set[str] = set()
    for question in plan.questions:
        _validate_question(
            question,
            plan=plan,
            snapshots=snapshots,
            items_by_snapshot=items_by_snapshot,
            text_by_snapshot=text_by_snapshot,
            bank_by_id=bank_by_id,
            review_approvers=review_approvers,
            fact_cards=fact_cards,
            used_fact_cards=used_fact_cards,
            available_rubrics=available_rubrics,
            allowed_skill_codes=allowed_skill_codes,
        )

    # 去重：canonical、归一化题干、近似改写、生成题复用
    seen_canonical: dict[str, int] = {}
    seen_stem: dict[str, int] = {}
    for question in plan.questions:
        canonical = question.canonical_id
        if canonical in seen_canonical:
            raise rule_error(
                "DUPLICATE_QUESTION",
                "本场存在相同 canonical 的主问题",
                [question.id, canonical],
            )
        seen_canonical[canonical] = question.position

        stem_key = normalized_stem(question.stem)
        if stem_key in seen_stem:
            raise rule_error(
                "DUPLICATE_QUESTION",
                "本场存在相同问法的主问题",
                [question.id, "reason=duplicate_normalized_stem"],
            )
        seen_stem[stem_key] = question.position

    for i, left in enumerate(plan.questions):
        for right in plan.questions[i + 1 :]:
            similarity = stem_similarity(left.stem, right.stem)
            if similarity >= SEMANTIC_DUPLICATE_THRESHOLD:
                raise rule_error(
                    "DUPLICATE_QUESTION",
                    "本场存在同义改写或相同问法的主问题",
                    [left.id, right.id, f"similarity={similarity:.2f}"],
                )

    # 生成题还要避开未入选的同岗位合格候选池，不能只与本场 bank 题比较。
    if ctx.candidate_stems:
        for question in plan.questions:
            if question.source != "generated":
                continue
            for candidate_stem in ctx.candidate_stems:
                similarity = stem_similarity(question.stem, candidate_stem)
                if similarity >= GENERATED_REUSE_THRESHOLD:
                    raise rule_error(
                        "DUPLICATE_QUESTION",
                        "生成题复用了同岗位题库候选，不算新生成",
                        [question.id, "reason=candidate_stem_reused", f"similarity={similarity:.2f}"],
                    )

    known_question_ids = {question.id for question in plan.questions}
    seen_pairs: set[tuple[str, str]] = set()
    for pair in plan.semantic_review.duplicate_pairs:
        left_id = pair.get("left_id") if isinstance(pair, dict) else None
        right_id = pair.get("right_id") if isinstance(pair, dict) else None
        if (
            not left_id
            or not right_id
            or left_id == right_id
            or left_id not in known_question_ids
            or right_id not in known_question_ids
        ):
            raise rule_error("DEDUP_UNCERTAIN", "语义去重结果包含未知或非法题目引用")
        key = tuple(sorted((str(left_id), str(right_id))))
        if key in seen_pairs:
            raise rule_error("DEDUP_UNCERTAIN", "语义去重结果包含重复关系")
        seen_pairs.add(key)
    seen_grounding: set[tuple[str, str]] = set()
    for issue in plan.semantic_review.grounding_issues:
        question_id = issue.get("question_id") if isinstance(issue, dict) else None
        if not question_id or question_id not in known_question_ids:
            raise rule_error("GROUNDING_INVALID", "语义复核引用了未知题目")
        key = (str(question_id), str(issue.get("claim", "")))
        if key in seen_grounding:
            raise rule_error("GROUNDING_INVALID", "语义复核包含重复无依据声明")
        seen_grounding.add(key)

    if plan.semantic_review.duplicate_pairs:
        raise rule_error(
            "DUPLICATE_QUESTION",
            "语义去重复核发现重复题目",
            semantic_review_failure_details(plan),
        )
    if plan.semantic_review.uncertain:
        raise rule_error(
            "DEDUP_UNCERTAIN",
            "语义去重结果不确定，不能发布 ready 计划",
            semantic_review_failure_details(plan),
        )
    if plan.semantic_review.grounding_issues:
        raise rule_error(
            "GROUNDING_INVALID",
            "语义复核发现无依据陈述",
            semantic_review_failure_details(plan),
        )

    if synthetic_reviewers:
        if not plan.test_only:
            raise rule_error(
                "BANK_INELIGIBLE", "使用合成审核记录的题库内容必须把计划标记为 test_only"
            )
        test_source_present = True
    if (test_source_present or plan.test_only) and not ctx.allow_test_fixtures:
        raise rule_error(
            "BANK_INELIGIBLE", "生产环境不能使用 test_only 内容或合成审核记录"
        )


def _validate_question(
    question: InternalQuestion,
    *,
    plan: InternalPlan,
    snapshots: dict[str, Snapshot],
    items_by_snapshot: dict[str, dict[str, dict]],
    text_by_snapshot: dict[str, str],
    bank_by_id: dict[tuple[str, int], dict],
    review_approvers: dict[tuple[str, str, str], set[str]],
    fact_cards: dict[str, FactCard],
    used_fact_cards: set[str],
    available_rubrics: dict[tuple[str, int], dict],
    allowed_skill_codes: set[str] | None = None,
) -> None:
    if question.source == "generated" and question.bank_ref is not None:
        raise rule_error("GENERATED_REUSE", "生成题不能带题库引用", [question.id])
    if question.source == "bank":
        if question.bank_ref is None:
            raise rule_error("BANK_INELIGIBLE", "题库题缺少审核引用", [question.id])
        ref = question.bank_ref
        candidate = bank_by_id.get((ref.question_id, ref.version))
        if candidate is None:
            raise rule_error("BANK_INELIGIBLE", "题库题不在本场题库快照中", [ref.question_id])
        if candidate.get("canonical_id") != ref.canonical_id:
            raise rule_error("BANK_INELIGIBLE", "题库题 canonical 与快照不一致", [ref.question_id])
        if ref.content_sha256 != candidate.get("content_sha256"):
            raise rule_error("BANK_INELIGIBLE", "题库题摘要与快照不一致", [ref.question_id])
        if ref.license_status != "clear" or ref.review_status != "approved":
            raise rule_error("BANK_INELIGIBLE", "题库题许可或审核状态非法", [ref.question_id])
        if len(set(ref.reviewer_ids)) < 2:
            raise rule_error("BANK_INELIGIBLE", "题库题缺少两位审核者", [ref.question_id])
        approvers = review_approvers.get(
            ("bank_question", ref.question_id, ref.content_sha256), set()
        )
        if not set(ref.reviewer_ids).issubset(approvers) or len(approvers) < 2:
            raise rule_error("BANK_INELIGIBLE", "题库题审核记录不完整", [ref.question_id])
        if question.evaluation.answer_status != "verified" or question.evaluation.trust != "reviewed":
            raise rule_error("EVALUATION_INVALID", "题库题评价依据未核验", [question.id])

    evaluation = question.evaluation
    point_total = sum(point.weight for point in evaluation.key_points)
    if abs(point_total - 1.0) > WEIGHT_EPSILON:
        raise rule_error(
            "EVALUATION_INVALID", "评分要点权重之和必须为 1", [question.id, f"sum={point_total:.3f}"]
        )
    rubric_total = sum(dim.weight for dim in evaluation.rubric.dimensions)
    if abs(rubric_total - 1.0) > WEIGHT_EPSILON:
        raise rule_error(
            "EVALUATION_INVALID", "量表维度权重之和必须为 1", [question.id, f"sum={rubric_total:.3f}"]
        )
    if evaluation.uncertainties:
        raise rule_error(
            "EVALUATION_INVALID",
            "题目存在未解决的不确定性，不能发布 ready 计划",
            [question.id, f"uncertainties_count={len(evaluation.uncertainties)}"],
        )
    if not evaluation.basis:
        raise rule_error("EVALUATION_INVALID", "题目缺少评价依据", [question.id])

    rubric_key = (evaluation.rubric.id, evaluation.rubric.version)
    expected_rubric = available_rubrics.get(rubric_key)
    if expected_rubric is None:
        raise rule_error("EVALUATION_INVALID", "题目引用了本场不可用的评分量表", [question.id])
    if evaluation.rubric.model_dump(mode="json") != expected_rubric:
        raise rule_error("EVALUATION_INVALID", "题目评分量表与已审核量表不一致", [question.id])

    # 题库题实际使用的正文、能力标签与评价必须逐字段对应审核快照；
    # 只核对 bank_ref 不能阻止“引用对、题干换掉”。
    if question.source == "bank":
        candidate = bank_by_id.get(
            (question.bank_ref.question_id, question.bank_ref.version)
        )
        assert candidate is not None
        if (
            question.canonical_id != candidate.get("canonical_id")
            or question.stem != candidate.get("stem")
            or question.focus != candidate.get("focus")
            or question.topic_key != candidate.get("topic_key")
            or list(question.skill_codes) != list(candidate.get("skill_codes") or [])
            or question.evaluation.model_dump(mode="json") != candidate.get("evaluation")
        ):
            raise rule_error(
                "BANK_INELIGIBLE",
                "题库题实际内容／评价与审核快照不一致",
                [question.bank_ref.question_id],
            )

    for anchor in question.anchors:
        validate_anchor(
            anchor,
            snapshots=snapshots,
            items_by_snapshot=items_by_snapshot,
            error_code="EVIDENCE_INVALID",
        )

    if question.source == "generated":
        if evaluation.answer_status != "model_generated" or evaluation.trust != "provisional":
            raise rule_error(
                "EVALUATION_INVALID", "生成题不能自称已验证或已审核", [question.id]
            )
        if not evaluation.acceptable_alternatives:
            raise rule_error("EVALUATION_INVALID", "生成题缺少合理替代解", [question.id])
        if allowed_skill_codes is not None:
            unknown_skills = [
                code for code in question.skill_codes if code not in allowed_skill_codes
            ]
            if unknown_skills:
                raise rule_error(
                    "EVALUATION_INVALID",
                    "生成题包含未启用的技能编码",
                    [question.id, "reason=unknown_skill_code", *unknown_skills[:5]],
                )

    material_basis = 0
    fact_card_basis = 0
    for basis in evaluation.basis:
        if basis.kind == "material":
            snapshot_id, separator, item_id = basis.ref_id.partition("/")
            if (
                not separator
                or snapshot_id not in snapshots
                or item_id not in items_by_snapshot.get(snapshot_id, {})
            ):
                raise rule_error("EVIDENCE_INVALID", "评价依据引用材料不存在", [question.id, basis.ref_id])
            material_basis += 1
        elif basis.kind == "fact_card":
            card = fact_cards.get(basis.ref_id)
            if card is None:
                raise rule_error("EVALUATION_INVALID", "引用了未知的依据卡", [question.id, basis.ref_id])
            if card.status != "approved":
                raise rule_error("EVALUATION_INVALID", "引用了未审核的依据卡", [basis.ref_id])
            approvers = review_approvers.get(("fact_card", card.id, card.content_sha256), set())
            if len(approvers) < 2:
                raise rule_error("EVALUATION_INVALID", "依据卡缺少两位审核者", [basis.ref_id])
            used_fact_cards.add(basis.ref_id)
            fact_card_basis += 1

    if question.source == "generated":
        if question.question_type == "knowledge" and fact_card_basis < 1:
            raise rule_error(
                "EVALUATION_INVALID",
                "技术知识生成题至少引用一张本次提供的已审依据卡",
                [question.id],
            )
        if question.question_type == "experience" and material_basis < 1:
            raise rule_error(
                "EVALUATION_INVALID",
                "经历／推理生成题至少引用一条本场材料依据",
                [question.id],
            )


# --------------------------------------------------------------------------- 构建


def build_fact_cards(
    db: Session, settings, basis_refs: set[str]
) -> tuple[list[FactCard], list[ReviewDecision], bool]:
    from app.services.bank import eligible_fact_card

    cards: list[FactCard] = []
    reviews: list[ReviewDecision] = []
    test_only = False
    for card_id in sorted(basis_refs):
        eligible = eligible_fact_card(db, settings, card_id)
        if eligible is None:
            raise AppError("EVALUATION_INVALID", "评价依据使用的依据卡不可用", details=[card_id])
        test_only = test_only or eligible.test_only
        cards.append(
            FactCard(
                id=eligible.card.id,
                version=eligible.card.version,
                status="approved",
                reviewer_ids=eligible.reviewer_ids,
                url=eligible.content.get("url", ""),
                summary=eligible.content.get("summary", ""),
                license_status=eligible.content.get("license_status", "pending_owner_review"),
                content_sha256=eligible.card.content_sha256,
            )
        )
        for review in eligible.reviews:
            if review.decision != "approve":
                continue
            reviews.append(
                ReviewDecision(
                    resource_type="fact_card",
                    resource_id=eligible.card.id,
                    version=eligible.card.version,
                    content_sha256=review.content_sha256,
                    reviewer_id=review.reviewer_id,
                    decision="approve",
                    reviewed_at=review.reviewed_at,
                    note=review.note or "审核通过",
                )
            )
    return cards, reviews, test_only


def bank_source_to_candidate(item: EligibleBankSource) -> BankCandidate:
    content = dict(item.content)
    return BankCandidate.model_validate(content)


def build_internal_plan(
    *,
    plan_id: uuid.UUID,
    preparation: Preparation,
    resume_snapshot,
    jd_snapshot,
    bank_sources: list[EligibleBankSource],
    generated_questions: list[dict],
    fact_cards: list[FactCard],
    fact_card_reviews: list[ReviewDecision],
    semantic_review: SemanticReview,
    model_id: str,
    prompt_version: str,
    test_only: bool,
    focus_areas: list[str],
    limitations: list[str],
    created_at: datetime | None = None,
) -> InternalPlan:
    now = created_at or datetime.now(timezone.utc)
    half = preparation.main_question_count // 2
    questions: list[InternalQuestion] = []
    position = 1
    bank_candidates: list[BankCandidate] = []
    reviews: list[ReviewDecision] = []
    from app.schemas import Anchor

    requirement_items = list(jd_snapshot.profile.get("requirements") or [])
    requirement_by_id = {item["item_id"]: item for item in requirement_items}
    resume_items = profile_items(resume_snapshot.profile)
    if not requirement_items:
        raise AppError("BANK_INSUFFICIENT", "JD 缺少显式技能要求，无法建立题库主问题的依据")
    for item in bank_sources:
        candidate = bank_source_to_candidate(item)
        bank_candidates.append(candidate)
        if item.jd_requirement_id:
            requirement = requirement_by_id.get(item.jd_requirement_id)
            if requirement is None:
                raise AppError(
                    "BANK_INSUFFICIENT",
                    "题库题绑定的 JD 要求已不在当前材料中",
                    [candidate.question_id],
                )
            bank_anchors = [
                Anchor(
                    snapshot_id=jd_snapshot.id,
                    item_id=requirement["item_id"],
                    quote=requirement["evidence"]["quote"],
                )
            ]
        elif item.resume_item_id:
            resume_item = resume_items.get(item.resume_item_id)
            if resume_item is None:
                raise AppError(
                    "BANK_INSUFFICIENT",
                    "题库题绑定的简历条目已不在当前材料中",
                    [candidate.question_id],
                )
            bank_anchors = [
                Anchor(
                    snapshot_id=resume_snapshot.id,
                    item_id=resume_item["item_id"],
                    quote=resume_item["evidence"]["quote"],
                )
            ]
        else:
            raise AppError(
                "BANK_INSUFFICIENT",
                "题库题无法绑定当前 JD 要求或简历条目",
                [candidate.question_id],
            )
        questions.append(
            InternalQuestion(
                id=str(uuid.uuid4()),
                position=position,
                source="bank",
                question_type="knowledge",
                bank_ref=BankRef(
                    question_id=candidate.question_id,
                    version=candidate.version,
                    canonical_id=candidate.canonical_id,
                    review_status="approved",
                    answer_status="verified",
                    reviewer_ids=item.reviewer_ids,
                    content_sha256=candidate.content_sha256,
                    license_status="clear",
                ),
                canonical_id=candidate.canonical_id,
                stem=candidate.stem,
                focus=candidate.focus,
                topic_key=candidate.topic_key,
                skill_codes=list(candidate.skill_codes),
                anchors=bank_anchors,
                evaluation=candidate.evaluation,
            )
        )
        for review in item.reviews:
            if review.decision != "approve":
                continue
            reviews.append(
                ReviewDecision(
                    resource_type="bank_question",
                    resource_id=candidate.question_id,
                    version=candidate.version,
                    content_sha256=review.content_sha256,
                    reviewer_id=review.reviewer_id,
                    decision="approve",
                    reviewed_at=review.reviewed_at,
                    note=review.note or "审核通过",
                )
            )
        position += 1

    for generated in generated_questions:
        questions.append(
            InternalQuestion(
                id=str(uuid.uuid4()),
                position=position,
                source="generated",
                bank_ref=None,
                question_type=generated["question_type"],
                canonical_id=generated["canonical_id"],
                stem=generated["stem"],
                focus=generated["focus"],
                topic_key=generated["topic_key"],
                skill_codes=list(generated.get("skill_codes", [])),
                anchors=[a for a in generated["anchors"]],
                evaluation=generated["evaluation"],
            )
        )
        position += 1

    if len(questions) != preparation.main_question_count or len(bank_sources) != half:
        raise AppError("BANK_INSUFFICIENT", "题库与生成题数量不满足各半要求")

    return InternalPlan(
        id=plan_id,
        preparation_id=preparation.id,
        input_revision=preparation.revision,
        revision=1,
        state="ready",
        role_id=preparation.role_id,
        level=preparation.level,
        main_question_count=preparation.main_question_count,
        source_counts={"bank": half, "generated": half},
        resume_snapshot_id=resume_snapshot.id,
        jd_snapshot_id=jd_snapshot.id,
        focus_areas=focus_areas,
        limitations=limitations,
        created_at=now,
        confirmed_at=None,
        test_only=test_only,
        material_snapshots=[snapshot_to_schema(resume_snapshot), snapshot_to_schema(jd_snapshot)],
        bank_source_snapshots=bank_candidates,
        fact_cards=fact_cards,
        content_reviews=reviews + fact_card_reviews,
        questions=questions,
        prompt_version=prompt_version,
        model_id=model_id,
        semantic_review=semantic_review,
    )


# --------------------------------------------------------------------------- 投影与保存


def plan_internal_payload(plan: InternalPlan) -> dict:
    return plan.model_dump(mode="json")


def plan_to_public(plan: Plan) -> PublicPlan:
    payload = plan.internal_payload
    return PublicPlan(
        id=plan.id,
        preparation_id=plan.preparation_id,
        input_revision=plan.input_revision,
        revision=plan.revision,
        state=plan.state,
        role_id=payload["role_id"],
        level=payload["level"],
        main_question_count=payload["main_question_count"],
        source_counts=payload["source_counts"],
        resume_snapshot_id=payload["resume_snapshot_id"],
        jd_snapshot_id=payload["jd_snapshot_id"],
        focus_areas=payload.get("focus_areas", []),
        limitations=payload.get("limitations", []),
        created_at=plan.created_at,
        confirmed_at=plan.confirmed_at,
    )


def revalidate_current_content(
    db: Session, plan: InternalPlan, *, allow_test_fixtures: bool
) -> None:
    """发布事务内重新锁定并核对当前题库、依据卡和审核决定。

    只检查计划里嵌入的快照不足以阻止“生成后题被弃用／撤审／改内容”。这里按稳定 ID
    顺序 `FOR UPDATE` 当前内容行，保证发布提交前状态没有被并发改掉。
    """
    from app.models import BankQuestion, ContentReview, FactCard as FactCardRow

    if plan.test_only and not allow_test_fixtures:
        raise rule_error("BANK_INELIGIBLE", "生产环境不能发布 test_only 计划")

    for candidate in sorted(
        plan.bank_source_snapshots, key=lambda item: (item.question_id, item.version)
    ):
        row = db.scalars(
            select(BankQuestion)
            .where(
                BankQuestion.question_id == candidate.question_id,
                BankQuestion.version == candidate.version,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.status != "approved":
            raise rule_error(
                "BANK_INSUFFICIENT",
                "题库题目当前已不可用（可能已弃用或撤审）",
                [candidate.question_id],
            )
        if row.test_only and not allow_test_fixtures:
            raise rule_error("BANK_INELIGIBLE", "生产环境不能使用 test_only 题库", [candidate.question_id])
        current_digest = content_digest(row.content or {})
        if (
            current_digest != row.source_digest
            or current_digest != candidate.content_sha256
            or row.canonical_id != candidate.canonical_id
        ):
            raise rule_error(
                "BANK_INELIGIBLE",
                "题库内容或摘要已变化，不能按旧审核快照发布",
                [candidate.question_id],
            )
        reviews = db.scalars(
            select(ContentReview)
            .where(
                ContentReview.resource_type == "bank_question",
                ContentReview.resource_id == candidate.question_id,
                ContentReview.version == candidate.version,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
        if any(review.decision == "reject" for review in reviews):
            raise rule_error("BANK_INELIGIBLE", "题库题目存在拒绝审核记录", [candidate.question_id])
        if not allow_test_fixtures and any(
            review.reviewer_kind != "human" for review in reviews
        ):
            raise rule_error("BANK_INELIGIBLE", "生产内容不能混入合成审核记录", [candidate.question_id])
        approvers = {
            review.reviewer_id
            for review in reviews
            if review.decision == "approve" and review.content_sha256 == current_digest
        }
        if len(approvers) < 2:
            raise rule_error("BANK_INELIGIBLE", "题库题目当前审核通过者不足两位", [candidate.question_id])
        embedded = {
            review.reviewer_id
            for review in plan.content_reviews
            if review.resource_type == "bank_question"
            and review.resource_id == candidate.question_id
            and review.version == candidate.version
            and review.content_sha256 == current_digest
            and review.decision == "approve"
        }
        if len(embedded & approvers) < 2:
            raise rule_error("BANK_INELIGIBLE", "题库题审核快照与当前审核记录不一致", [candidate.question_id])

    for card in sorted(plan.fact_cards, key=lambda item: (item.id, item.version)):
        row = db.scalars(
            select(FactCardRow)
            .where(FactCardRow.id == card.id, FactCardRow.version == card.version)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if row is None or row.status != "approved":
            raise rule_error("EVALUATION_INVALID", "依据卡当前已不可用", [card.id])
        if row.test_only and not allow_test_fixtures:
            raise rule_error("EVALUATION_INVALID", "生产环境不能使用 test_only 依据卡", [card.id])
        current_digest = content_digest(row.content or {})
        if current_digest != row.content_sha256 or current_digest != card.content_sha256:
            raise rule_error("EVALUATION_INVALID", "依据卡内容或摘要已变化", [card.id])
        reviews = db.scalars(
            select(ContentReview)
            .where(
                ContentReview.resource_type == "fact_card",
                ContentReview.resource_id == card.id,
                ContentReview.version == card.version,
            )
            .execution_options(populate_existing=True)
            .with_for_update()
        ).all()
        if any(review.decision == "reject" for review in reviews):
            raise rule_error("EVALUATION_INVALID", "依据卡存在拒绝审核记录", [card.id])
        if not allow_test_fixtures and any(
            review.reviewer_kind != "human" for review in reviews
        ):
            raise rule_error("EVALUATION_INVALID", "生产依据卡不能混入合成审核记录", [card.id])
        approvers = {
            review.reviewer_id
            for review in reviews
            if review.decision == "approve" and review.content_sha256 == current_digest
        }
        if len(approvers) < 2:
            raise rule_error("EVALUATION_INVALID", "依据卡当前审核通过者不足两位", [card.id])


def publish_plan(
    db: Session,
    *,
    preparation: Preparation,
    plan: InternalPlan,
    job_id: uuid.UUID | None,
    allow_test_fixtures: bool,
    candidate_stems: list[str] | None = None,
) -> Plan:
    """在调用方事务内写入计划、题项并把 current_plan_id 指向它。"""
    from app.models import PlanItem

    if plan.preparation_id != preparation.id or plan.input_revision != preparation.revision:
        raise rule_error("PLAN_STATE_INVALID", "计划对应的准备配置已变化")
    revalidate_current_content(db, plan, allow_test_fixtures=allow_test_fixtures)
    validate_internal_plan(
        plan,
        PlanValidationContext(
            allow_test_fixtures=allow_test_fixtures,
            db=db,
            candidate_stems=candidate_stems or [],
        ),
    )

    row = Plan(
        id=plan.id,
        owner_id=preparation.owner_id,
        preparation_id=preparation.id,
        input_revision=preparation.revision,
        revision=1,
        state="ready",
        internal_payload=plan_internal_payload(plan),
        confirmed_at=None,
    )
    db.add(row)
    db.flush()
    for question in plan.questions:
        db.add(
            PlanItem(
                plan_id=row.id,
                position=question.position,
                source=question.source,
                question_id=question.bank_ref.question_id if question.bank_ref else None,
                version=question.bank_ref.version if question.bank_ref else None,
                canonical_id=question.canonical_id,
                normalized_hash=normalized_stem(question.stem),
            )
        )
    preparation.current_plan_id = row.id
    db.flush()
    return row


def confirm_plan(
    db: Session, plan: Plan, preparation: Preparation, revision: int
) -> Plan:
    if plan.owner_id != preparation.owner_id:
        raise AppError("NOT_FOUND", "计划不存在", status_code=404)
    if plan.input_revision != preparation.revision or preparation.current_plan_id != plan.id:
        raise AppError("PLAN_STALE", "材料或准备配置已变化，该计划不能再确认")
    if plan.state == "confirmed":
        return plan
    if plan.revision != revision:
        raise AppError("REVISION_CONFLICT", "计划版本已变化，请刷新后重试")
    plan.state = "confirmed"
    plan.revision = plan.revision + 1
    plan.confirmed_at = datetime.now(timezone.utc)
    payload = dict(plan.internal_payload)
    payload["state"] = "confirmed"
    payload["confirmed_at"] = plan.confirmed_at.isoformat()
    plan.internal_payload = payload
    db.flush()
    return plan


def confirm_plan_for_owner(
    db: Session, *, owner_id: uuid.UUID, plan_id: uuid.UUID, revision: int
) -> Plan:
    """确认计划时固定锁顺序：先父 Preparation，再 Plan。"""
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
    return confirm_plan(db, plan, preparation, revision)
