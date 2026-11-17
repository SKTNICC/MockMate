"""题库准入、来源绑定与 fact card 校验。

生产环境只接受真实人工审核（reviewer_kind='human'）且内容摘要一致的 approved 题目；
test_only 内容只有在 ALLOW_TEST_FIXTURES=true 的隔离环境才可能被选中，并且会强制计划标记 test_only。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import BankQuestion, ContentReview, FactCard

REVIEW_STATUS_POP_KEYS = ("status", "answer_status", "content_sha256", "reviewer_ids")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def content_digest(value: dict) -> str:
    """与 tools/m1/build_contracts.py 完全一致的规范摘要。"""
    content = copy.deepcopy(value)
    for key in REVIEW_STATUS_POP_KEYS:
        content.pop(key, None)
    if isinstance(content.get("evaluation"), dict):
        content["evaluation"].pop("answer_status", None)
        content["evaluation"].pop("trust", None)
    return digest(content)


@dataclass
class EligibleBankSource:
    question: BankQuestion
    content: dict
    reviews: list[ContentReview]
    reviewer_ids: list[str]
    test_only: bool
    jd_requirement_id: str | None = None
    resume_item_id: str | None = None


@dataclass
class EligibleFactCard:
    card: FactCard
    content: dict
    reviews: list[ContentReview]
    reviewer_ids: list[str]
    test_only: bool


def _reviews_for(
    db: Session, resource_type: str, resource_id: str, version: int, content_sha256: str, allow_synthetic: bool
) -> list[ContentReview]:
    rows = db.scalars(
        select(ContentReview).where(
            ContentReview.resource_type == resource_type,
            ContentReview.resource_id == resource_id,
            ContentReview.version == version,
            ContentReview.content_sha256 == content_sha256,
        )
    ).all()
    if not allow_synthetic:
        rows = [row for row in rows if row.reviewer_kind == "human"]
    return list(rows)


def _approved_reviewer_ids(reviews: list[ContentReview]) -> list[str]:
    rejects = {row.reviewer_id for row in reviews if row.decision == "reject"}
    approvers = sorted({row.reviewer_id for row in reviews if row.decision == "approve"} - rejects)
    return approvers


def _has_reject(reviews: list[ContentReview]) -> bool:
    return any(row.decision == "reject" for row in reviews)


def _settings_allow_test(settings: Settings) -> bool:
    return settings.allow_test_fixtures and not settings.is_production


def eligible_bank_questions(
    db: Session, settings: Settings, role_id: str, level: str
) -> list[EligibleBankSource]:
    allow_test = _settings_allow_test(settings)
    rows = db.scalars(select(BankQuestion).where(BankQuestion.status == "approved")).all()
    eligible: list[EligibleBankSource] = []
    for row in rows:
        if row.test_only and not allow_test:
            continue
        if role_id not in (row.role_ids or []):
            continue
        if level not in (row.levels or []):
            continue
        content = row.content or {}
        if content.get("status") != "approved":
            continue
        if content.get("answer_status") != "verified":
            continue
        if content.get("license_status") != "clear":
            continue
        if content.get("provenance") != "authored":
            continue
        if not content.get("source_urls"):
            continue
        if not content.get("stem") or not content.get("evaluation"):
            continue
        current_digest = content_digest(content)
        if row.source_digest != current_digest or content.get("content_sha256") != current_digest:
            # 审核后被修改的内容不再满足原审核摘要
            continue
        reviews = _reviews_for(db, "bank_question", row.question_id, row.version, current_digest, allow_test)
        if _has_reject(reviews):
            continue
        reviewer_ids = _approved_reviewer_ids(reviews)
        if len(reviewer_ids) < 2:
            continue
        synthetic = any(review.reviewer_kind == "synthetic_test" for review in reviews)
        eligible.append(
            EligibleBankSource(
                question=row,
                content=content,
                reviews=reviews,
                reviewer_ids=reviewer_ids,
                test_only=row.test_only or synthetic,
            )
        )
    eligible.sort(key=lambda item: (item.question.question_id, item.question.version))
    return eligible


def eligible_fact_card(
    db: Session, settings: Settings, card_id: str, version: int | None = None
) -> EligibleFactCard | None:
    allow_test = _settings_allow_test(settings)
    stmt = select(FactCard).where(FactCard.id == card_id)
    if version is not None:
        stmt = stmt.where(FactCard.version == version)
    rows = db.scalars(stmt.order_by(FactCard.version.desc())).all()
    for row in rows:
        if row.test_only and not allow_test:
            continue
        content = row.content or {}
        if row.status != "approved" or content.get("status") != "approved":
            continue
        if content.get("license_status") != "clear":
            continue
        if content_digest(content) != row.content_sha256:
            continue
        reviews = _reviews_for(db, "fact_card", row.id, row.version, row.content_sha256, allow_test)
        if _has_reject(reviews):
            continue
        reviewer_ids = _approved_reviewer_ids(reviews)
        if len(reviewer_ids) < 2:
            continue
        synthetic = any(review.reviewer_kind == "synthetic_test" for review in reviews)
        return EligibleFactCard(
            card=row,
            content=content,
            reviews=reviews,
            reviewer_ids=reviewer_ids,
            test_only=row.test_only or synthetic,
        )
    return None


def eligible_fact_cards(db: Session, settings: Settings) -> list[EligibleFactCard]:
    """当前全部可用依据卡，供生成槽位显式选择；仍逐个重核审核与摘要。"""
    allow_test = _settings_allow_test(settings)
    rows = db.scalars(select(FactCard).order_by(FactCard.id, FactCard.version)).all()
    result: list[EligibleFactCard] = []
    for row in rows:
        if row.test_only and not allow_test:
            continue
        content = row.content or {}
        if row.status != "approved" or content.get("status") != "approved":
            continue
        if content.get("license_status") != "clear":
            continue
        if content_digest(content) != row.content_sha256:
            continue
        reviews = _reviews_for(
            db, "fact_card", row.id, row.version, row.content_sha256, allow_test
        )
        if _has_reject(reviews):
            continue
        reviewer_ids = _approved_reviewer_ids(reviews)
        if len(reviewer_ids) < 2:
            continue
        synthetic = any(review.reviewer_kind == "synthetic_test" for review in reviews)
        result.append(
            EligibleFactCard(
                card=row,
                content=content,
                reviews=reviews,
                reviewer_ids=reviewer_ids,
                test_only=row.test_only or synthetic,
            )
        )
    return result


def _jd_skill_index(jd_profile: dict) -> dict[str, str]:
    index: dict[str, str] = {}
    requirements = sorted(
        jd_profile.get("requirements") or [],
        key=lambda item: (0 if item.get("importance") == "must" else 1, item.get("item_id", "")),
    )
    for requirement in requirements:
        for code in requirement.get("skill_codes") or []:
            index.setdefault(code, requirement["item_id"])
    return index


def _resume_skill_index(resume_profile: dict) -> dict[str, str]:
    index: dict[str, str] = {}
    for item in sorted(
        resume_profile.get("projects") or [], key=lambda entry: entry.get("item_id", "")
    ):
        for code in item.get("skill_codes") or []:
            index.setdefault(code, item["item_id"])
    for item in sorted(
        resume_profile.get("skills") or [], key=lambda entry: entry.get("item_id", "")
    ):
        code = item.get("code")
        if code:
            index.setdefault(code, item["item_id"])
    return index


def select_bank_sources(
    eligible: list[EligibleBankSource],
    half: int,
    *,
    resume,
    jd,
) -> list[EligibleBankSource]:
    """按 JD/简历技能相关度选题，并绑定真实 requirement/item 锚点。

    排序：JD 显式技能交集降序 → 简历技能交集降序 → question_id 稳定升序。
    完全没有技能关联的题不能仅凭岗位标签入选；绑定不到实际材料需求的候选跳过。
    """
    from app.services.planning import stem_similarity
    from app.services.text import normalized_stem

    jd_index = _jd_skill_index(jd.profile or {})
    resume_index = _resume_skill_index(resume.profile or {})
    scored: list[tuple[int, int, EligibleBankSource, str | None, str | None]] = []
    for item in eligible:
        codes = set(item.content.get("skill_codes") or [])
        jd_hits = [code for code in codes if code in jd_index]
        resume_hits = [code for code in codes if code in resume_index]
        if not jd_hits and not resume_hits:
            continue
        jd_requirement_id = (
            min((jd_index[code] for code in jd_hits)) if jd_hits else None
        )
        resume_item_id = (
            min((resume_index[code] for code in resume_hits)) if resume_hits else None
        )
        scored.append(
            (
                len(jd_hits),
                len(resume_hits),
                item,
                jd_requirement_id,
                resume_item_id,
            )
        )
    scored.sort(
        key=lambda entry: (
            -entry[0],
            -entry[1],
            entry[2].question.question_id,
            entry[2].question.version,
        )
    )

    selected: list[EligibleBankSource] = []
    seen_canonical: set[str] = set()
    seen_hashes: set[str] = set()
    seen_stems: list[str] = []
    for _, _, item, jd_requirement_id, resume_item_id in scored:
        canonical = item.content.get("canonical_id") or ""
        stem = item.content.get("stem") or ""
        stem_hash = normalized_stem(stem)
        if canonical in seen_canonical or stem_hash in seen_hashes:
            continue
        if any(stem_similarity(stem, prior) >= 0.70 for prior in seen_stems):
            continue
        item.jd_requirement_id = jd_requirement_id
        item.resume_item_id = resume_item_id
        selected.append(item)
        seen_canonical.add(canonical)
        seen_hashes.add(stem_hash)
        seen_stems.append(stem)
        if len(selected) == half:
            break
    if len(selected) < half:
        raise AppError(
            "BANK_INSUFFICIENT",
            "符合岗位、级别、审核要求且与当前 JD/简历有技能关联的题库主问题不足",
            details=[f"required={half}", f"available={len(selected)}"],
        )
    return selected
