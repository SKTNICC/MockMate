"""第二轮独立验收 S04／S05 的正确行为回归。

这些用例来自 `outputs/m1/review-20261116-r2/` 的诊断，但断言的是修复后的正确行为；
诊断脚本退出 0 只表示缺陷曾存在，不能作为验收通过。
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ai.provider import TestProvider
from app.core.errors import AppError
from app.models import FactCard
from app.services.bank import content_digest
from app.services.content_import import import_content
from app.services.planning import build_fact_cards

ROOT = Path(__file__).resolve().parents[2]
BASE_PLAN = json.loads((ROOT / "fixtures/m1/internal-plan.example.json").read_text(encoding="utf-8"))
BANK = json.loads((ROOT / "fixtures/m1/bank-candidates.json").read_text(encoding="utf-8"))

RESUME = SimpleNamespace(**BASE_PLAN["material_snapshots"][0])
JD = SimpleNamespace(**BASE_PLAN["material_snapshots"][1])
RUBRIC = BANK["questions"][0]["evaluation"]["rubric"]


def _generated_experience() -> dict:
    fixture = BASE_PLAN["questions"][3]
    return {
        key: copy.deepcopy(value)
        for key, value in fixture.items()
        if key not in {"id", "position", "source", "bank_ref"}
    }


def _knowledge_context() -> dict:
    return {
        "slot_key": "generated-2",
        "slot_index": 2,
        "question_type": "knowledge",
        "target_skill_codes": ["java.core"],
        "match_focus": ["集合顺序契约"],
        "prefer_jd_scenario": False,
        "allowed_anchors": [
            {
                "snapshot_id": str(JD.id),
                "item_id": "req-1",
                "quote": JD.profile["requirements"][0]["evidence"]["quote"],
                "source_kind": "jd",
                "label": JD.profile["requirements"][0]["text"],
            }
        ],
        "fact_cards": [
            {
                "id": "FC-HASHMAP",
                "version": 1,
                "summary": "HashMap 不承诺遍历顺序；常数时间说明依赖散列分布。",
                "url": "https://docs.oracle.com/en/java/javase/25/docs/api/java.base/java/util/HashMap.html",
                "content_sha256": "0" * 64,
            }
        ],
        "rubric": copy.deepcopy(RUBRIC),
        "avoid_stems": [],
        "resume_snapshot_id": str(RESUME.id),
        "jd_snapshot_id": str(JD.id),
        "preparation_revision": 1,
        "main_question_count": 6,
        "match_report_id": "00000000-0000-4000-8000-000000000099",
        "bank_source_digests": [],
    }


def test_slot_rejects_wrong_declared_type_even_if_material_basis_is_valid(worker) -> None:
    """S04：knowledge 槽位返回 experience 不能靠自报类型绕过技术依据卡要求。"""

    class WrongTypeProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def generate_questions(self, **kwargs):
            self.calls += 1
            return [copy.deepcopy(_generated_experience())]

    provider = WrongTypeProvider()
    context = _knowledge_context()
    with pytest.raises(AppError) as caught:
        worker._generate_slot(
            None,
            provider,
            context,
            RESUME,
            JD,
            RUBRIC,
            {"FC-HASHMAP"},
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert any("类型" in detail for detail in caught.value.details)
    # 一次错误输出必须进入既有的一次纠正，而不是直接接受。
    assert provider.calls == 2


def test_slot_corrects_wrong_type_once_then_accepts_matching_type(worker) -> None:
    class CorrectingProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0
            self.contexts: list[dict] = []

        def generate_questions(self, **kwargs):
            self.calls += 1
            self.contexts.append(copy.deepcopy(kwargs.get("context") or {}))
            if self.calls == 1:
                return [copy.deepcopy(_generated_experience())]
            return super().generate_questions(**kwargs)

    provider = CorrectingProvider()
    item = worker._generate_slot(
        None,
        provider,
        _knowledge_context(),
        RESUME,
        JD,
        RUBRIC,
        {"FC-HASHMAP"},
    )
    assert item["question_type"] == "knowledge"
    assert provider.calls == 2
    assert provider.contexts[1]["correction"]["attempt"] == 2


def test_slot_rejects_stem_already_in_forbidden_pool(worker) -> None:
    """S04：禁止题干是服务端断言，不能只放在发给模型的 context 里。"""
    forbidden_stem = BASE_PLAN["questions"][0]["stem"]

    class DuplicateProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def generate_questions(self, **kwargs):
            self.calls += 1
            item = copy.deepcopy(_generated_experience())
            item["stem"] = forbidden_stem
            return [item]

    provider = DuplicateProvider()
    context = _knowledge_context()
    # 槽位按 experience 校验重复；类型本身合法，重复必须被拦下。
    context["question_type"] = "experience"
    context["fact_cards"] = []
    context["avoid_stems"] = [forbidden_stem]
    with pytest.raises(AppError) as caught:
        worker._generate_slot(
            None,
            provider,
            context,
            RESUME,
            JD,
            RUBRIC,
            set(),
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert any("重复" in detail for detail in caught.value.details)
    assert provider.calls == 2


def _fact_cards(license_status: str = "clear") -> list[dict]:
    suffix = uuid.uuid4().hex[:8]
    return [
        {
            "id": f"FC-REG-{suffix}-{index}",
            "version": 1,
            "status": "draft",
            "reviewer_ids": [],
            "url": f"https://example.com/official-{suffix}-{index}",
            "summary": f"隔离测试依据卡 {index}：官方契约摘要，仅用于合成审核流程。",
            "license_status": license_status,
        }
        for index in (1, 2)
    ]


def _reviews_for(cards: list[dict]) -> dict:
    decisions = []
    for card in cards:
        content = copy.deepcopy(card)
        content["status"] = "draft"
        content["reviewer_ids"] = []
        digest = content_digest(content)
        for reviewer in ("TEST-ONLY-A", "TEST-ONLY-B"):
            decisions.append(
                {
                    "resource_type": "fact_card",
                    "resource_id": card["id"],
                    "version": 1,
                    "content_sha256": digest,
                    "reviewer_id": reviewer,
                    "decision": "approve",
                    "reviewed_at": "2026-11-16T10:00:00Z",
                    "note": "SYNTHETIC TEST ONLY：隔离测试库合成审核，不是真实人工批准",
                }
            )
    return {"synthetic_test": True, "decisions": decisions}


def test_fact_card_projection_keeps_review_digest_and_license(db, settings) -> None:
    """S05：导入→数据库→内部 FactCard 投影必须使用同一份规范化内容摘要。"""
    cards = _fact_cards("clear")
    imported = import_content(
        db,
        bank_data={"questions": []},
        fact_data={"cards": cards},
        reviews_data=_reviews_for(cards),
        settings=settings,
        allow_test_fixtures=True,
    )
    db.commit()
    assert imported.approved_fact == 2

    projected, reviews, _ = build_fact_cards(db, settings, {card["id"] for card in cards})
    assert len(reviews) == 4
    for card in projected:
        dumped = card.model_dump(mode="json")
        assert "license_status" in dumped
        assert dumped["license_status"] == "clear"
        # 投影后的内容必须仍与审核绑定的摘要一致，否则发布校验会失败。
        assert content_digest(dumped) == card.content_sha256


def test_fact_card_with_pending_license_cannot_be_approved(db, settings) -> None:
    """S05b：来源许可未确认时，两位审核者的 approve 也不能派生 approved。"""
    cards = _fact_cards("pending_owner_review")
    with pytest.raises(AppError) as caught:
        import_content(
            db,
            bank_data={"questions": []},
            fact_data={"cards": cards},
            reviews_data=_reviews_for(cards),
            settings=settings,
            allow_test_fixtures=True,
        )
    assert caught.value.code == "CONTENT_IMPORT_INVALID"
    db.rollback()
    for card in cards:
        assert db.get(FactCard, (card["id"], 1)) is None


def _bank_payload(card_id: str) -> dict:
    suffix = uuid.uuid4().hex[:8]
    question = copy.deepcopy(BANK["questions"][0])
    question.update(
        {
            "question_id": f"BANK-REG-{suffix}",
            "canonical_id": f"bank-reg-{suffix}",
            "status": "draft",
            "answer_status": "model_generated",
            "license_status": "clear",
            "stem": f"请说明集合行为的前提，并给出一个可验证的边界测试（{suffix}）。",
            "evaluation": copy.deepcopy(BANK["questions"][0]["evaluation"]),
        }
    )
    question["evaluation"]["answer_status"] = "model_generated"
    question["evaluation"]["trust"] = "provisional"
    question["evaluation"]["basis"] = [
        {"kind": "fact_card", "ref_id": card_id, "note": "以隔离导入的已审依据卡为准"}
    ]
    question.pop("content_sha256", None)
    question["content_sha256"] = content_digest(question)
    return question


def _approve_bank(question: dict) -> list[dict]:
    decisions = []
    for reviewer in ("TEST-ONLY-A", "TEST-ONLY-B"):
        decisions.append(
            {
                "resource_type": "bank_question",
                "resource_id": question["question_id"],
                "version": 1,
                "content_sha256": question["content_sha256"],
                "reviewer_id": reviewer,
                "decision": "approve",
                "reviewed_at": "2026-11-16T10:00:00Z",
                "note": "SYNTHETIC TEST ONLY：隔离测试库合成审核，不是真实人工批准",
            }
        )
    return decisions


def test_imported_fact_card_flows_through_plan_confirm_and_reopen(
    client, worker, db, settings
) -> None:
    """D04：正式输入形状跑通导入→选题→内部计划→校验→发布→确认→重开。"""
    from tests.conftest import (
        auth,
        create_confirmed_resume,
        create_confirmed_template_jd,
        issue_token,
        new_key,
        wait_job,
    )

    cards = _fact_cards("clear")
    question = _bank_payload(cards[0]["id"])
    reviews = _reviews_for(cards)
    reviews["decisions"] += _approve_bank(question)
    imported = import_content(
        db,
        bank_data={"questions": [question]},
        fact_data={"cards": cards},
        reviews_data=reviews,
        settings=settings,
        allow_test_fixtures=True,
    )
    db.commit()
    assert imported.approved_bank == 1
    assert imported.approved_fact == 2

    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep_response = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert prep_response.status_code == 201, prep_response.text
    prep = prep_response.json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"
    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job
    plan_id = job["result_ref"]["id"]

    from app.core.db import SessionLocal
    from app.models import Plan
    import uuid as uuid_module

    with SessionLocal() as session:
        plan = session.get(Plan, uuid_module.UUID(plan_id))
        payload = plan.internal_payload
        used = {card["id"]: card for card in payload["fact_cards"]}
        assert cards[0]["id"] in used or cards[1]["id"] in used
        for card in payload["fact_cards"]:
            if card["id"].startswith("FC-REG-"):
                assert card["license_status"] == "clear"
                assert content_digest(card) == card["content_sha256"]

    public = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert public.status_code == 200
    assert public.json()["state"] == "ready"
    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": public.json()["revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["state"] == "confirmed"
    reopened = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert reopened.json()["state"] == "confirmed"
    assert reopened.json()["revision"] == confirm.json()["revision"]
