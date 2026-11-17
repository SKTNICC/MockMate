"""正式内容离线导入：格式／来源／技能／量表／审核校验与原子写入。

生产导入只接受可信 human ledger：reviewer_kind 由导入模式决定，不信任输入里的
“approved”字段；没有两位不同真实审核者时只能得到 draft/review，不能进入选题。
测试通过 `allow_test_fixtures=True` 显式使用 synthetic_test 记录，仍标记 test_only。
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import BankQuestion, ContentReview, FactCard, SkillCode
from app.schemas import BankCandidate
from app.services import catalog
from app.services.bank import content_digest
from app.services.text import normalized_stem


@dataclass
class ImportResult:
    bank_created: int = 0
    bank_updated: int = 0
    bank_unchanged: int = 0
    fact_created: int = 0
    fact_updated: int = 0
    fact_unchanged: int = 0
    reviews_created: int = 0
    reviews_unchanged: int = 0
    approved_bank: int = 0
    approved_fact: int = 0


def _error(message: str, details: list[str] | None = None) -> AppError:
    return AppError("CONTENT_IMPORT_INVALID", message, details=details or [])


def _load_list(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    if not isinstance(value, list):
        raise _error(f"缺少数组字段 {key}")
    return value


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise _error("审核时间必须是 ISO-8601")


def _reject_test_reviewer(reviewer_id: str) -> None:
    upper = reviewer_id.upper()
    if "TEST" in upper or "SYNTHETIC" in upper or "FIXTURE" in upper:
        raise _error("正式导入拒绝 TEST-ONLY／合成审核者", [reviewer_id])


def _is_synthetic_ledger(data: dict, allow_test_fixtures: bool) -> bool:
    synthetic = bool(data.get("synthetic_test"))
    if synthetic and not allow_test_fixtures:
        raise _error("synthetic_test ledger 只能在隔离测试环境使用")
    return synthetic


def _prepare_bank(
    db: Session,
    raw: dict,
    *,
    reviews: list[dict],
    allowed_codes: set[str],
) -> tuple[dict, str, str]:
    if raw.get("status") in {"approved", "deprecated"}:
        raise _error("输入不能自称 approved/deprecated；状态必须由有效审核派生", [raw.get("question_id", "?")])
    content = copy.deepcopy(raw)
    content["status"] = "draft"
    content["answer_status"] = "model_generated"
    content["evaluation"] = copy.deepcopy(content.get("evaluation") or {})
    content["evaluation"]["answer_status"] = "model_generated"
    content["evaluation"]["trust"] = "provisional"
    content.pop("content_sha256", None)
    digest = content_digest(content)
    content["content_sha256"] = digest

    try:
        candidate = BankCandidate.model_validate(content)
    except ValidationError as exc:
        details = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()[:5]]
        raise _error("题库内容不符合契约", details)

    if candidate.provenance != "authored":
        raise _error("题库来源必须是 authored", [candidate.question_id])
    if candidate.license_status != "clear":
        raise _error("题库来源许可未确认（license_status 必须为 clear）", [candidate.question_id])
    if not candidate.source_urls:
        raise _error("题库缺少官方来源", [candidate.question_id])
    if normalized_stem(candidate.stem) == "":
        raise _error("题库题干不能为空", [candidate.question_id])
    unknown_codes = [code for code in candidate.skill_codes if code not in allowed_codes]
    if unknown_codes:
        raise _error("题库技能码不在启用词表", unknown_codes[:5])
    for role_id in candidate.role_ids:
        for level in candidate.levels:
            catalog.require_role_level(db, role_id, level, require_enabled=False)
    if sum(point.weight for point in candidate.evaluation.key_points) != 1.0:
        if abs(sum(point.weight for point in candidate.evaluation.key_points) - 1.0) > 0.001:
            raise _error("评分要点权重之和必须为 1", [candidate.question_id])
    if abs(sum(dim.weight for dim in candidate.evaluation.rubric.dimensions) - 1.0) > 0.001:
        raise _error("评分量表权重之和必须为 1", [candidate.question_id])

    # 状态由有效审核决定派生，不信任输入 status/answer_status。
    approvals = {row["reviewer_id"] for row in reviews if row["decision"] == "approve"}
    rejects = [row for row in reviews if row["decision"] == "reject"]
    if rejects:
        raise _error("题库版本存在拒绝审核", [candidate.question_id])
    if len(approvals) >= 2:
        content["status"] = "approved"
        content["answer_status"] = "verified"
        content["evaluation"]["answer_status"] = "verified"
        content["evaluation"]["trust"] = "reviewed"
        status = "approved"
    elif len(approvals) == 1:
        content["status"] = "review"
        status = "review"
    else:
        status = "draft"
    content["content_sha256"] = content_digest(content)
    return content, content["content_sha256"], status


FACT_INPUT_FIELDS = {
    "id",
    "version",
    "status",
    "reviewer_ids",
    "url",
    "summary",
    "license_status",
    "content_sha256",
}


def _prepare_fact(
    raw: dict,
    *,
    reviews: list[dict],
) -> tuple[dict, str, str]:
    content = copy.deepcopy(raw)
    unknown = sorted(set(content) - FACT_INPUT_FIELDS)
    if unknown:
        # 未明示字段不能悄悄进入一个摘要、又从另一个投影消失。
        raise _error("依据卡包含未知字段", unknown)
    content["status"] = "draft"
    content["reviewer_ids"] = []
    content.pop("content_sha256", None)
    if not content.get("id") or not content.get("version"):
        raise _error("依据卡缺少 id/version")
    if not str(content.get("summary") or "").strip():
        raise _error("依据卡缺少摘要", [str(content.get("id"))])
    if not str(content.get("url") or "").startswith(("http://", "https://")):
        raise _error("依据卡缺少官方来源 URL", [str(content.get("id"))])
    license_status = content.get("license_status")
    if license_status not in {"pending_owner_review", "clear"}:
        raise _error("依据卡来源许可状态缺失或非法（license_status）", [str(content.get("id"))])
    content["license_status"] = license_status
    digest = content_digest(content)
    content["content_sha256"] = digest
    approvals = {row["reviewer_id"] for row in reviews if row["decision"] == "approve"}
    rejects = [row for row in reviews if row["decision"] == "reject"]
    if rejects:
        raise _error("依据卡版本存在拒绝审核", [str(content.get("id"))])
    if len(approvals) >= 2 and license_status != "clear":
        # 审核可以记录，但不能把未确认许可的内容派生为可用；整批失败，避免半批。
        raise _error(
            "依据卡来源许可未确认，不能进入已审核可用状态",
            [str(content.get("id"))],
        )
    if len(approvals) >= 2:
        content["status"] = "approved"
        content["reviewer_ids"] = sorted(approvals)
        digest = content_digest(content)
        content["content_sha256"] = digest
        status = "approved"
    else:
        status = "draft"
    return content, digest, status


def _validate_reviews(
    db: Session,
    decisions: list[dict],
    *,
    prepared_bank: dict[tuple[str, int], dict],
    prepared_fact: dict[tuple[str, int], dict],
    allow_synthetic: bool,
) -> list[dict]:
    normalized: list[dict] = []
    seen: set[tuple[str, str, int, str, str]] = set()
    for decision in decisions:
        try:
            resource_type = decision["resource_type"]
            resource_id = str(decision["resource_id"])
            version = int(decision["version"])
            reviewer_id = str(decision["reviewer_id"]).strip()
            verdict = decision["decision"]
            digest = str(decision["content_sha256"])
            reviewed_at = _parse_time(decision["reviewed_at"])
            note = str(decision["note"]).strip()
        except (KeyError, TypeError, ValueError):
            raise _error("审核记录字段不完整或类型错误")
        if resource_type not in {"bank_question", "fact_card"}:
            raise _error("审核记录 resource_type 非法", [resource_type])
        if verdict not in {"approve", "reject"}:
            raise _error("审核决定必须是 approve/reject", [verdict])
        if not reviewer_id or not note:
            raise _error("审核记录必须有 reviewer_id 与 note")
        if not allow_synthetic:
            _reject_test_reviewer(reviewer_id)
        if decision.get("reviewer_kind") not in (None, "human") and not allow_synthetic:
            raise _error("正式导入不能声明 synthetic_test reviewer_kind")
        key = (resource_type, resource_id, version, reviewer_id, digest)
        if key in seen:
            raise _error("同一审核记录重复", [resource_id, reviewer_id])
        seen.add(key)
        target = prepared_bank if resource_type == "bank_question" else prepared_fact
        if (resource_id, version) not in target:
            existing = None
            if resource_type == "bank_question":
                existing = db.get(BankQuestion, (resource_id, version))
            else:
                existing = db.get(FactCard, (resource_id, version))
            if existing is None:
                raise _error("审核记录引用了不存在的资源版本", [resource_id, str(version)])
            expected_digest = existing.source_digest if resource_type == "bank_question" else existing.content_sha256
            if digest != expected_digest:
                raise _error("审核记录 content_sha256 与资源内容不一致", [resource_id])
        else:
            expected_digest = target[(resource_id, version)]["content_sha256"]
            if digest != expected_digest:
                raise _error("审核记录 content_sha256 与资源内容不一致", [resource_id])
        normalized.append(
            {
                "resource_type": resource_type,
                "resource_id": resource_id,
                "version": version,
                "reviewer_id": reviewer_id,
                "decision": verdict,
                "reviewed_at": reviewed_at,
                "note": note,
                "content_sha256": digest,
            }
        )
    return normalized


def import_content(
    db: Session,
    *,
    bank_data: dict,
    fact_data: dict,
    reviews_data: dict,
    settings: Settings,
    allow_test_fixtures: bool = False,
) -> ImportResult:
    """校验全部输入后再写入；调用方负责 commit/rollback。"""
    allow_synthetic = bool(allow_test_fixtures and not settings.is_production)
    synthetic = _is_synthetic_ledger(reviews_data, allow_test_fixtures)
    if synthetic and not allow_synthetic:
        raise _error("synthetic_test ledger 只能用于隔离测试")

    questions = _load_list(bank_data, "questions")
    cards = _load_list(fact_data, "cards")
    decisions = _load_list(reviews_data, "decisions")
    allowed_codes = catalog.allowed_skill_codes(db)
    if not allowed_codes:
        raise _error("技能词表为空，请先执行 catalog seed")

    bank_ids: set[tuple[str, int]] = set()
    fact_ids: set[tuple[str, int]] = set()
    prepared_bank: dict[tuple[str, int], dict] = {}
    prepared_fact: dict[tuple[str, int], dict] = {}

    for raw in cards:
        key = (str(raw.get("id")), int(raw.get("version") or 0))
        if not key[0] or key[1] < 1:
            raise _error("依据卡 id/version 非法")
        if key in fact_ids:
            raise _error("依据卡 id/version 重复", [key[0]])
        fact_ids.add(key)
        content, digest, status = _prepare_fact(raw, reviews=[
            row for row in decisions
            if row.get("resource_type") == "fact_card"
            and str(row.get("resource_id")) == key[0]
            and int(row.get("version") or 0) == key[1]
        ])
        prepared_fact[key] = {**content, "content_sha256": digest, "_status": status}

    for raw in questions:
        key = (str(raw.get("question_id")), int(raw.get("version") or 0))
        if not key[0] or key[1] < 1:
            raise _error("题库 question_id/version 非法")
        if key in bank_ids:
            raise _error("题库 question_id/version 重复", [key[0]])
        bank_ids.add(key)
        content, digest, status = _prepare_bank(
            db,
            raw,
            reviews=[
                row for row in decisions
                if row.get("resource_type") == "bank_question"
                and str(row.get("resource_id")) == key[0]
                and int(row.get("version") or 0) == key[1]
            ],
            allowed_codes=allowed_codes,
        )
        for basis in content["evaluation"].get("basis") or []:
            if basis.get("kind") != "fact_card":
                continue
            ref = basis.get("ref_id")
            if not any(str(card.get("id")) == ref for card in cards):
                existing = db.get(FactCard, (ref, 1))
                if existing is None:
                    raise _error("题库引用了不存在的依据卡", [key[0], str(ref)])
                if status == "approved" and existing.status != "approved":
                    raise _error("已批准题库不能引用未批准依据卡", [key[0], str(ref)])
        prepared_bank[key] = {**content, "content_sha256": digest, "_status": status}

    # 已批准题库引用的批内 fact card 也必须已批准。
    for key, content in prepared_bank.items():
        if content["_status"] != "approved":
            continue
        for basis in content["evaluation"].get("basis") or []:
            if basis.get("kind") != "fact_card":
                continue
            ref = basis.get("ref_id")
            prepared = prepared_fact.get((ref, 1))
            if prepared is not None and prepared["_status"] != "approved":
                raise _error("已批准题库不能引用未批准依据卡", [key[0], ref])

    normalized_reviews = _validate_reviews(
        db,
        decisions,
        prepared_bank=prepared_bank,
        prepared_fact=prepared_fact,
        allow_synthetic=allow_synthetic,
    )
    if synthetic:
        # 测试算法路径也明确标记，不进入生产选题。
        pass

    result = ImportResult()

    for key, prepared in prepared_fact.items():
        row = db.get(FactCard, key)
        digest = prepared["content_sha256"]
        content = {k: v for k, v in prepared.items() if not k.startswith("_")}
        if row is None:
            row = FactCard(
                id=key[0],
                version=key[1],
                content=content,
                content_sha256=digest,
                status=prepared["_status"],
                test_only=synthetic,
            )
            db.add(row)
            result.fact_created += 1
        else:
            if row.content_sha256 != digest and content_digest(row.content) != digest:
                raise _error("同一依据卡版本的内容已存在且不同；请升级版本", [key[0], str(key[1])])
            if row.test_only != synthetic:
                raise _error("依据卡 test_only 标记与导入模式不一致", [key[0]])
            before = (row.status, row.content_sha256, copy.deepcopy(row.content))
            row.content = content
            row.content_sha256 = digest
            row.status = prepared["_status"]
            if before == (row.status, row.content_sha256, row.content):
                result.fact_unchanged += 1
            else:
                result.fact_updated += 1
        if prepared["_status"] == "approved":
            result.approved_fact += 1

    for key, prepared in prepared_bank.items():
        row = db.get(BankQuestion, key)
        digest = prepared["content_sha256"]
        content = {k: v for k, v in prepared.items() if not k.startswith("_")}
        if row is None:
            row = BankQuestion(
                question_id=key[0],
                version=key[1],
                canonical_id=content["canonical_id"],
                status=prepared["_status"],
                role_ids=list(content["role_ids"]),
                levels=list(content["levels"]),
                content=content,
                source_digest=digest,
                test_only=synthetic,
            )
            db.add(row)
            result.bank_created += 1
        else:
            if row.source_digest != digest and content_digest(row.content) != digest:
                raise _error("同一题库版本的内容已存在且不同；请升级版本", [key[0], str(key[1])])
            if row.test_only != synthetic:
                raise _error("题库 test_only 标记与导入模式不一致", [key[0]])
            before = (row.status, row.source_digest, copy.deepcopy(row.content))
            row.canonical_id = content["canonical_id"]
            row.role_ids = list(content["role_ids"])
            row.levels = list(content["levels"])
            row.content = content
            row.source_digest = digest
            row.status = prepared["_status"]
            if before == (row.status, row.source_digest, row.content):
                result.bank_unchanged += 1
            else:
                result.bank_updated += 1
        if prepared["_status"] == "approved":
            result.approved_bank += 1

    for review in normalized_reviews:
        existing = db.scalars(
            select(ContentReview).where(
                ContentReview.resource_type == review["resource_type"],
                ContentReview.resource_id == review["resource_id"],
                ContentReview.version == review["version"],
                ContentReview.reviewer_id == review["reviewer_id"],
                ContentReview.content_sha256 == review["content_sha256"],
            )
        ).one_or_none()
        if existing is not None:
            result.reviews_unchanged += 1
            continue
        db.add(
            ContentReview(
                resource_type=review["resource_type"],
                resource_id=review["resource_id"],
                version=review["version"],
                reviewer_id=review["reviewer_id"],
                reviewer_kind="synthetic_test" if synthetic else "human",
                decision=review["decision"],
                reviewed_at=review["reviewed_at"],
                note=review["note"],
                content_sha256=review["content_sha256"],
            )
        )
        result.reviews_created += 1
    db.flush()
    return result


def build_review_packet(
    *,
    bank_data: dict,
    fact_data: dict,
    reviews_data: dict,
    output_path,
) -> str:
    lines = [
        "# M1 正式题库人工核对包（待填写，不是审核通过）",
        "",
        "> 本文件由内容导入工具生成。审核者身份、时间、许可确认和结论必须由真实负责人填写；",
        "> 未填写时状态一律视为 pending，不得导入生产或用于生成计划。",
        "",
        "## 待审核题库",
        "",
    ]
    for question in bank_data.get("questions", []):
        content = copy.deepcopy(question)
        content["status"] = "draft"
        content["answer_status"] = "model_generated"
        content["evaluation"] = copy.deepcopy(content.get("evaluation") or {})
        content["evaluation"]["answer_status"] = "model_generated"
        content["evaluation"]["trust"] = "provisional"
        content.pop("content_sha256", None)
        digest = content_digest(content)
        lines.extend(
            [
                f"### {question.get('question_id')} v{question.get('version')}",
                "",
                f"- canonical_id：`{question.get('canonical_id')}`",
                f"- content_digest：`{digest}`",
                f"- 岗位／级别：{question.get('role_ids')} / {question.get('levels')}",
                f"- 官方来源：{', '.join(question.get('source_urls') or [])}",
                f"- 来源许可：{question.get('license_status')}（需负责人确认后改为 clear）",
                f"- 题干：{question.get('stem')}",
                f"- 考察目标：{question.get('evaluation', {}).get('goal')}",
                f"- 参考思路：{question.get('evaluation', {}).get('reference_approach')}",
                "- 评分要点：",
            ]
        )
        for point in question.get("evaluation", {}).get("key_points") or []:
            lines.append(f"  - {point.get('point')}（weight={point.get('weight')}）")
        lines.extend(
            [
                "- 审核者一（未填）：姓名／身份：________；结论：pending；时间：________",
                "- 审核者二（未填）：姓名／身份：________；结论：pending；时间：________",
                "",
            ]
        )
    lines.extend(["## 待审核依据卡", ""])
    for card in fact_data.get("cards", []):
        content = copy.deepcopy(card)
        content["status"] = "draft"
        content["reviewer_ids"] = []
        content.pop("content_sha256", None)
        digest = content_digest(content)
        lines.extend(
            [
                f"### {card.get('id')} v{card.get('version')}",
                "",
                f"- content_digest：`{digest}`",
                f"- 官方来源：{card.get('url')}",
                f"- 摘要：{card.get('summary')}",
                f"- 来源许可：{card.get('license_status', 'pending_owner_review')}（需负责人确认）",
                "- 审核者一（未填）：________；审核者二（未填）：________",
                "",
            ]
        )
    lines.extend(
        [
            "## 导入前必须完成",
            "",
            "1. 确认每条来源许可与官方链接，把 license_status 改为 clear。",
            "2. 两位不同真实审核者逐条核对题干、评分要点、fact card 与来源；",
            "   在 reviews 文件中填写 approve/reject、reviewed_at 与 note。",
            "3. 任一 reject 不得导入；只有两位不同 approve 才可能派生 approved。",
            "4. 使用 `python -m app.cli import-content ...` 导入；命令不会把 draft 自动批准。",
            "",
        ]
    )
    text = "\n".join(lines)
    output_path.write_text(text, encoding="utf-8")
    return text
