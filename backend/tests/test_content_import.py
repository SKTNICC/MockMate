"""C07：正式内容导入算法、状态派生、幂等与原子性（隔离测试库）。"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import BankQuestion, ContentReview, FactCard
from app.services.bank import content_digest, eligible_bank_questions
from app.services.content_import import import_content

BANK_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/m1/bank-candidates.json").read_text(
        encoding="utf-8"
    )
)


def _digest_bank(question: dict) -> str:
    content = copy.deepcopy(question)
    content["status"] = "draft"
    content["answer_status"] = "model_generated"
    content["evaluation"] = copy.deepcopy(content["evaluation"])
    content["evaluation"]["answer_status"] = "model_generated"
    content["evaluation"]["trust"] = "provisional"
    content.pop("content_sha256", None)
    return content_digest(content)


def _digest_fact(card: dict) -> str:
    content = copy.deepcopy(card)
    content["status"] = "draft"
    content["reviewer_ids"] = []
    content.pop("content_sha256", None)
    return content_digest(content)


def _payloads() -> tuple[dict, dict, dict]:
    suffix = uuid.uuid4().hex[:8]
    fact_id = f"FC-TEST-{suffix}"
    question_id = f"BANK-TEST-{suffix}"
    fact = {
        "id": fact_id,
        "version": 1,
        "status": "draft",
        "reviewer_ids": [],
        "url": "https://example.com/official-spec",
        "summary": "官方规范说明该集合不保证遍历顺序；这是测试用合成依据卡。",
        "license_status": "clear",
    }
    base = BANK_FIXTURE["questions"][0]
    question = {
        "question_id": question_id,
        "version": 1,
        "canonical_id": f"canonical-{suffix}",
        "status": "draft",
        "answer_status": "model_generated",
        "title": "测试题库题",
        "role_ids": ["java-backend"],
        "levels": ["intern"],
        "stem": "请说明该集合行为的前提，并给出一个可验证的边界测试。",
        "focus": "边界与验证",
        "topic_key": "java.core",
        "skill_codes": ["java.core"],
        "evaluation": copy.deepcopy(base["evaluation"]),
        "provenance": "authored",
        "source_urls": ["https://example.com/official-spec"],
        "license_status": "clear",
    }
    question["evaluation"]["basis"] = [
        {"kind": "fact_card", "ref_id": fact_id, "note": "以官方依据卡为准"}
    ]
    reviews = {
        "test_only": False,
        "synthetic_test": True,
        "decisions": [],
    }
    for resource_type, resource_id, digest in (
        ("bank_question", question_id, _digest_bank(question)),
        ("fact_card", fact_id, _digest_fact(fact)),
    ):
        for reviewer in ("fixture-reviewer-a", "fixture-reviewer-b"):
            reviews["decisions"].append(
                {
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "version": 1,
                    "content_sha256": digest,
                    "reviewer_id": reviewer,
                    "decision": "approve",
                    "reviewed_at": "2026-11-16T10:00:00Z",
                    "note": "隔离测试库合成记录，不是真实人工审核",
                }
            )
    return (
        {"test_only": False, "questions": [question]},
        {"test_only": False, "cards": [fact]},
        reviews,
    )


def _run(payloads, *, allow_test=True, settings_obj=None):
    db = SessionLocal()
    try:
        result = import_content(
            db,
            bank_data=payloads[0],
            fact_data=payloads[1],
            reviews_data=payloads[2],
            settings=settings_obj or get_settings(),
            allow_test_fixtures=allow_test,
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_two_distinct_reviews_derive_approved_but_remain_test_only() -> None:
    bank, facts, reviews = _payloads()
    result = _run((bank, facts, reviews))
    assert result.approved_bank == 1
    assert result.approved_fact == 1

    with SessionLocal() as db:
        question = db.get(BankQuestion, (bank["questions"][0]["question_id"], 1))
        card = db.get(FactCard, (facts["cards"][0]["id"], 1))
        assert question.status == "approved"
        assert question.test_only is True
        assert question.content["answer_status"] == "verified"
        assert card.status == "approved"
        assert card.test_only is True
        assert db.scalar(
            select(func.count()).select_from(ContentReview).where(
                ContentReview.resource_id == question.question_id
            )
        ) == 2

    # 合成内容只能在 ALLOW_TEST_FIXTURES 环境被选题；生产设置下不可用。
    production_like = get_settings().model_copy(
        update={"allow_test_fixtures": False, "ai_provider": "deepseek"}
    )
    with SessionLocal() as db:
        assert eligible_bank_questions(db, production_like, "java-backend", "intern") == []


def test_reimport_is_idempotent() -> None:
    payloads = _payloads()
    first = _run(payloads)
    assert first.bank_created == 1
    second = _run(payloads)
    assert second.bank_created == 0
    assert second.bank_unchanged == 1
    assert second.fact_unchanged == 1
    assert second.reviews_created == 0
    assert second.reviews_unchanged == 4


def test_same_version_different_content_is_rejected() -> None:
    payloads = _payloads()
    _run(payloads)
    changed = copy.deepcopy(payloads)
    changed[0]["questions"][0]["stem"] = "换成一个完全不同的题干，但仍然使用同一个版本号。"
    with pytest.raises(AppError) as caught:
        _run(changed)
    assert caught.value.code == "CONTENT_IMPORT_INVALID"


def test_one_review_cannot_approve() -> None:
    payloads = _payloads()
    payloads[2]["decisions"] = [
        row for row in payloads[2]["decisions"]
        if not (row["resource_type"] == "bank_question" and row["reviewer_id"].endswith("b"))
    ]
    result = _run(payloads)
    assert result.approved_bank == 0
    with SessionLocal() as db:
        row = db.get(BankQuestion, (payloads[0]["questions"][0]["question_id"], 1))
        assert row.status == "review"


def test_reject_aborts_batch_without_half_write() -> None:
    payloads = _payloads()
    payloads[2]["decisions"].append(
        {
            "resource_type": "bank_question",
            "resource_id": payloads[0]["questions"][0]["question_id"],
            "version": 1,
            "content_sha256": _digest_bank(payloads[0]["questions"][0]),
            "reviewer_id": "fixture-reviewer-c",
            "decision": "reject",
            "reviewed_at": "2026-11-16T10:00:00Z",
            "note": "拒绝",
        }
    )
    with pytest.raises(AppError):
        _run(payloads)
    with SessionLocal() as db:
        assert db.get(BankQuestion, (payloads[0]["questions"][0]["question_id"], 1)) is None
        assert db.get(FactCard, (payloads[1]["cards"][0]["id"], 1)) is None


def test_test_only_reviewer_and_missing_license_are_rejected() -> None:
    payloads = _payloads()
    for row in payloads[2]["decisions"]:
        row["reviewer_id"] = "TEST-ONLY-A"
    with pytest.raises(AppError):
        _run(payloads, allow_test=False)

    payloads = _payloads()
    payloads[0]["questions"][0]["license_status"] = "pending_owner_review"
    with pytest.raises(AppError):
        _run(payloads)


def test_missing_fact_reference_and_cross_version_review_are_rejected() -> None:
    payloads = _payloads()
    payloads[0]["questions"][0]["evaluation"]["basis"][0]["ref_id"] = "FC-DOES-NOT-EXIST"
    with pytest.raises(AppError):
        _run(payloads)

    payloads = _payloads()
    for row in payloads[2]["decisions"]:
        if row["resource_type"] == "fact_card":
            row["version"] = 2
    with pytest.raises(AppError):
        _run(payloads)
