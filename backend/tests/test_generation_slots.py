"""C03：归一化、去重、单槽输出与语义复核输入回归。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.ai.provider import TestProvider
from app.core.errors import AppError
from app.services.planning import (
    PlanValidationContext,
    parse_internal_plan,
    stem_similarity,
    validate_internal_plan,
)
from app.services.text import normalized_stem, normalized_text
from tests.conftest import (
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    new_key,
    wait_job,
)

BASE_PLAN = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/m1/internal-plan.example.json").read_text(
        encoding="utf-8"
    )
)
BANK = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/m1/bank-candidates.json").read_text(
        encoding="utf-8"
    )
)


def _snapshots() -> tuple[SimpleNamespace, SimpleNamespace]:
    resume = SimpleNamespace(**BASE_PLAN["material_snapshots"][0])
    jd = SimpleNamespace(**BASE_PLAN["material_snapshots"][1])
    return resume, jd


def test_normalization_is_shared_and_unicode_safe() -> None:
    assert normalized_text("ＡＢＣ－１２３") == "abc123"
    assert normalized_text("Java 集合！") == normalized_text("ｊａｖａ集合")
    assert normalized_stem("Java 集合！") == normalized_stem("ｊａｖａ集合")
    assert normalized_stem("Café") == normalized_stem("ＣＡＦＥ́")


def test_different_mechanisms_are_not_merged() -> None:
    left = BANK["questions"][0]["stem"]
    right = BANK["questions"][1]["stem"]
    assert stem_similarity(left, right) < 0.70


def test_renamed_near_duplicate_is_rejected() -> None:
    original = BASE_PLAN["questions"][0]["stem"]
    changed = original.replace("HashMap", "TreeMap") + "请再说明一次。"
    assert stem_similarity(original, changed) >= 0.70
    plan = copy.deepcopy(BASE_PLAN)
    plan["questions"][3]["stem"] = changed
    with pytest.raises(AppError) as caught:
        validate_internal_plan(parse_internal_plan(plan), PlanValidationContext(True))
    assert caught.value.code == "DUPLICATE_QUESTION"


def test_generated_is_checked_against_unselected_candidate_pool() -> None:
    unselected = BANK["questions"][4]["stem"]
    plan = copy.deepcopy(BASE_PLAN)
    plan["questions"][3]["stem"] = unselected
    # 未入选候选不在本场快照内，只有 candidate_stems 上下文能拦住它。
    with pytest.raises(AppError) as caught:
        validate_internal_plan(
            parse_internal_plan(plan),
            PlanValidationContext(True, candidate_stems=[unselected]),
        )
    assert caught.value.code == "DUPLICATE_QUESTION"


def test_semantic_output_rejects_unknown_and_duplicate_ids(worker) -> None:
    known = {"q-1", "q-2"}
    bad_unknown = {
        "duplicate_pairs": [{"left_id": "q-1", "right_id": "q-x", "reason": "x"}],
        "grounding_issues": [],
        "uncertain": False,
    }
    with pytest.raises(AppError) as caught:
        worker._validate_semantic_output(bad_unknown, known)
    assert caught.value.code == "AI_INVALID_OUTPUT"

    duplicate_relation = {
        "duplicate_pairs": [
            {"left_id": "q-1", "right_id": "q-2", "reason": "a"},
            {"left_id": "q-2", "right_id": "q-1", "reason": "b"},
        ],
        "grounding_issues": [],
        "uncertain": False,
    }
    with pytest.raises(AppError) as caught:
        worker._validate_semantic_output(duplicate_relation, known)
    assert caught.value.code == "AI_INVALID_OUTPUT"

    unknown_grounding = {
        "duplicate_pairs": [],
        "grounding_issues": [
            {"question_id": "q-x", "claim": "无依据", "reason": "材料没有"}
        ],
        "uncertain": False,
    }
    with pytest.raises(AppError) as caught:
        worker._validate_semantic_output(unknown_grounding, known)
    assert caught.value.code == "AI_INVALID_OUTPUT"


@pytest.mark.parametrize("returned", [0, 2])
def test_slot_must_return_exactly_one_question(worker, returned) -> None:
    resume, jd = _snapshots()
    rubric = BANK["questions"][0]["evaluation"]["rubric"]

    class WrongCountProvider(TestProvider):
        def generate_questions(self, **kwargs):
            if returned == 0:
                return []
            generated = TestProvider().generate_questions(
                count=2,
                resume_snapshot=kwargs["resume_snapshot"],
                jd_snapshot=kwargs["jd_snapshot"],
                rubric=kwargs["rubric"],
                avoid_stems=kwargs["avoid_stems"],
            )
            return generated[:2]

    with pytest.raises(AppError) as caught:
        worker._generate_slot(
            None,
            WrongCountProvider(),
            {"slot_key": "generated-1", "avoid_stems": []},
            resume,
            jd,
            rubric,
            set(),
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert any("returned=" in detail for detail in caught.value.details)


def test_known_invalid_output_gets_one_correction(worker) -> None:
    resume, jd = _snapshots()
    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    contexts: list[dict] = []

    class CorrectingProvider(TestProvider):
        def generate_questions(self, *, context=None, **kwargs):
            contexts.append(copy.deepcopy(context or {}))
            if len(contexts) == 1:
                return []
            return TestProvider().generate_questions(context=context, **kwargs)

    item = worker._generate_slot(
        None,
        CorrectingProvider(),
        {"slot_key": "generated-1", "avoid_stems": []},
        resume,
        jd,
        rubric,
        set(),
    )
    assert item["stem"]
    assert len(contexts) == 2
    assert contexts[1]["correction"]["attempt"] == 2


def test_timeout_is_not_automatically_retried(worker) -> None:
    resume, jd = _snapshots()
    rubric = BANK["questions"][0]["evaluation"]["rubric"]

    class TimeoutProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def generate_questions(self, **kwargs):
            self.calls += 1
            raise AppError("AI_TIMEOUT", "结果未知")

    provider = TimeoutProvider()
    with pytest.raises(AppError) as caught:
        worker._generate_slot(
            None,
            provider,
            {"slot_key": "generated-1", "avoid_stems": []},
            resume,
            jd,
            rubric,
            set(),
        )
    assert caught.value.code == "AI_TIMEOUT"
    assert provider.calls == 1


def test_review_receives_full_evaluation_and_fact_cards(
    client: TestClient, worker, monkeypatch
) -> None:
    import app.workers.runner as runner

    captured: dict = {}

    class CapturingProvider(TestProvider):
        def review_questions(self, *, questions, material_texts, fact_cards=None):
            captured["questions"] = copy.deepcopy(questions)
            captured["fact_cards"] = copy.deepcopy(fact_cards or [])
            return super().review_questions(
                questions=questions,
                material_texts=material_texts,
                fact_cards=fact_cards,
            )

    provider = CapturingProvider()
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
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

    from app.core.db import SessionLocal
    from app.models import Job
    import uuid

    with SessionLocal() as db:
        row = db.get(Job, uuid.UUID(job["id"]))
        stages = [record["stage"] for record in row.ai_call_records]
        assert "plan" in stages
        assert "plan_review" in stages
        assert all("text" not in str(record) for record in row.ai_call_records)

    questions = captured["questions"]
    assert questions
    for question in questions:
        assert question["stem"]
        assert question["focus"]
        assert question["reference_approach"]
        assert question["key_points"]
        assert question["anchors"]
        assert question["basis"]
    assert captured["fact_cards"]
    assert all(card["id"] and card["summary"] for card in captured["fact_cards"])
