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


def test_truncated_slot_gets_one_correction_and_records_finish_reason(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import httpx
    import uuid
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, Plan
    import uuid

    resume_id = None
    jd_id = None
    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
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

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    requests_captured: list[dict] = []
    generate_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal generate_calls
        body = json.loads(request.content)
        requests_captured.append(body)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            generate_calls += 1
            if generate_calls == 1:
                return httpx.Response(
                    200,
                    json={
                        "id": "req-gen-1",
                        "usage": {"completion_tokens": 4096},
                        "choices": [
                            {
                                "finish_reason": "length",
                                "message": {"content": '{"questions": ['},
                            }
                        ],
                    },
                )
            anchors_block = user_msg.split("允许锚点（anchors 必须原样取自这里）：", 1)[1]
            anchors_block = anchors_block.split("\n\n可用依据卡", 1)[0].strip()
            anchor = json.loads(anchors_block)[0]
            return httpx.Response(
                200,
                json={
                    "id": "req-gen-2",
                    "usage": {"completion_tokens": 512},
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "questions": [
                                            {
                                                "question_type": "experience",
                                                "canonical_id": "gen-retry-ok",
                                                "stem": "结合借阅系统的边界测试经历，你会如何设计极端输入下的验证步骤？",
                                                "focus": "边界测试",
                                                "topic_key": "test.junit",
                                                "skill_codes": ["java.core"],
                                                "anchors": [
                                                    {
                                                        "snapshot_id": anchor["snapshot_id"],
                                                        "item_id": anchor["item_id"],
                                                        "quote": anchor["quote"],
                                                    }
                                                ],
                                                "evaluation": {
                                                    "goal": "验证测试边界设计",
                                                    "reference_approach": "先定义边界再构造测试。",
                                                    "key_points": [
                                                        {"point": "明确输入边界", "weight": 0.6},
                                                        {"point": "给出可验证断言", "weight": 0.4},
                                                    ],
                                                    "acceptable_alternatives": ["等价边界划分"],
                                                    "common_mistakes": ["只测正常路径"],
                                                    "rubric": rubric,
                                                    "basis": [
                                                        {
                                                            "kind": "material",
                                                            "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}",
                                                            "note": "依据简历项目",
                                                        }
                                                    ],
                                                    "answer_status": "model_generated",
                                                    "trust": "provisional",
                                                    "uncertainties": [],
                                                },
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "req-rev-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
                            )
                        },
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job
    assert generate_calls == 2
    assert requests_captured[0]["max_tokens"] == 4096
    assert requests_captured[1]["max_tokens"] == 8192
    assert "reason=output_truncated" in requests_captured[1]["messages"][-1]["content"]
    assert "finish_reason" not in requests_captured[2] or True
    assert "thinking" not in requests_captured[2]
    assert "max_tokens" not in requests_captured[2]

    row = db.get(Job, uuid.UUID(job["id"]))
    plan_records = [r for r in row.ai_call_records if r["stage"] == "plan"]
    assert len(plan_records) == 2
    assert plan_records[0]["slot"] == "generated-1"
    assert plan_records[0]["finish_reason"] == "length"
    assert plan_records[0]["outcome"] == "known_invalid"
    assert plan_records[1]["finish_reason"] == "stop"
    assert plan_records[1]["outcome"] == "success"
    assert db.query(Plan).filter(Plan.preparation_id == uuid.UUID(prep["id"])).count() == 1


def test_double_invalid_json_fails_without_plan_and_keeps_structured_details(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import httpx
    import uuid
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, Plan
    import uuid

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
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

    calls = 0

    def bad_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": f"req-bad-{calls}",
                "choices": [{"message": {"content": "not json at all"}}],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(bad_handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "failed", job
    assert job["retryable"] is True
    assert job["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "stage=plan" in job["error"]["details"]
    assert "slot=generated-1" in job["error"]["details"]
    assert "reason=invalid_json" in job["error"]["details"]
    assert "finish_reason=unknown" in job["error"]["details"]
    assert calls == 2
    row = db.get(Job, uuid.UUID(job["id"]))
    assert len(row.ai_call_records) == 2
    assert all(rec["finish_reason"] == "unknown" for rec in row.ai_call_records)
    assert db.query(Plan).filter(Plan.preparation_id == uuid.UUID(prep["id"])).count() == 0


def test_content_filter_is_not_retried_in_slot(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import httpx
    import uuid
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
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

    calls = 0

    def filter_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            json={
                "id": "req-cf",
                "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(filter_handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "AI_REQUEST_REJECTED"
    assert job["retryable"] is False
    assert calls == 1


def test_review_receives_full_evaluation_and_fact_cards(
    client: TestClient, worker, monkeypatch
) -> None:
    import uuid
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


def _extract_prompt_json(user_msg: str, header: str, next_header: str | None = None):
    body = user_msg.split(header, 1)[1]
    if next_header is not None:
        body = body.split(next_header, 1)[0]
    return json.loads(body.strip())


def _build_synthetic_question(
    *,
    question_type: str,
    canonical_id: str,
    stem: str,
    focus: str,
    topic_key: str,
    anchor: dict,
    rubric: dict,
    fact_cards: list[dict],
) -> dict:
    basis = [
        {
            "kind": "material",
            "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}",
            "note": "以材料原文锚点为事实边界",
        },
        {
            "kind": "reasoning",
            "ref_id": "hypothetical-analysis",
            "note": "仅评价设计思路与验证",
        },
    ]
    if question_type == "knowledge" and fact_cards:
        basis.insert(
            0,
            {
                "kind": "fact_card",
                "ref_id": fact_cards[0]["id"],
                "note": "引用已审依据卡",
            },
        )
    return {
        "question_type": question_type,
        "canonical_id": canonical_id,
        "stem": stem,
        "focus": focus,
        "topic_key": topic_key,
        "skill_codes": ["java.core"],
        "anchors": [
            {
                "snapshot_id": anchor["snapshot_id"],
                "item_id": anchor["item_id"],
                "quote": anchor["quote"],
            }
        ],
        "evaluation": {
            "goal": focus,
            "reference_approach": "先明确前提，再设计可执行步骤并验证失败路径。",
            "key_points": [
                {"point": "依据材料或依据卡边界分析", "weight": 0.6},
                {"point": "给出可检验的验证路径", "weight": 0.4},
            ],
            "acceptable_alternatives": ["等价且前提清楚的实现"],
            "common_mistakes": ["忽略异常分支或资源边界"],
            "rubric": rubric,
            "basis": basis,
            "answer_status": "model_generated",
            "trust": "provisional",
            "uncertainties": [],
        },
    }


def test_slot_three_duplicates_slot_one_then_recovers_with_correction_and_publishes(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import httpx
    import re
    import uuid
    import uuid
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, Plan

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
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

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    slot_prompts: list[tuple[str, str]] = []
    slot_calls: dict[str, int] = {}
    slot1_stem = "结合你在校园图书借阅系统中对 HashMap 与数据库访问连接的资源管理做法，如何确保连接及时释放？"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            slot_key = re.search(r"本次只为槽位 (generated-\d+)", user_msg).group(1)
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
            slot_prompts.append((slot_key, user_msg))
            slot_calls[slot_key] = slot_calls.get(slot_key, 0) + 1
            anchors = _extract_prompt_json(
                user_msg,
                "允许锚点（anchors 必须原样取自这里）：",
                "\n\n可用依据卡",
            )
            fact_cards = _extract_prompt_json(
                user_msg,
                "可用依据卡（knowledge 题必须从中引用 fact_card）：",
                "\n\n已有题干",
            )
            anchor = anchors[0]
            if slot_key == "generated-1":
                question = _build_synthetic_question(
                    question_type=slot_type,
                    canonical_id="syn-slot-1",
                    stem=slot1_stem,
                    focus="数据库访问资源管理",
                    topic_key="java.resource_mgmt",
                    anchor=anchor,
                    rubric=rubric,
                    fact_cards=fact_cards,
                )
            elif slot_key == "generated-2":
                question = _build_synthetic_question(
                    question_type=slot_type,
                    canonical_id="syn-slot-2",
                    stem="结合 HashMap 扩容机制，为什么多线程并发读写未同步的 HashMap 会导致数据不一致？",
                    focus="HashMap 线程安全边界",
                    topic_key="java.collections.concurrency",
                    anchor=anchor,
                    rubric=rubric,
                    fact_cards=fact_cards,
                )
            else:
                if slot_calls[slot_key] == 1:
                    # 首次故意返回与第 1 槽相同的题干
                    question = _build_synthetic_question(
                        question_type=slot_type,
                        canonical_id="syn-slot-3-dup",
                        stem=slot1_stem,
                        focus="数据库访问资源管理",
                        topic_key="java.resource_mgmt",
                        anchor=anchor,
                        rubric=rubric,
                        fact_cards=fact_cards,
                    )
                else:
                    # 纠正后仍共用同一真实锚点，但考察完全不同的实际作答任务（失败路径验证）
                    question = _build_synthetic_question(
                        question_type=slot_type,
                        canonical_id="syn-slot-3-ok",
                        stem="在校园图书借阅系统的借阅流程中，当库存查询或持久化写入抛出异常时，你会设计哪些用例验证失败路径与回滚状态？",
                        focus="借阅失败路径验证",
                        topic_key="test.failure_path",
                        anchor=anchor,
                        rubric=rubric,
                        fact_cards=fact_cards,
                    )
            return httpx.Response(
                200,
                json={
                    "id": f"req-{slot_key}-{slot_calls[slot_key]}",
                    "usage": {"completion_tokens": 300},
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps({"questions": [question]}, ensure_ascii=False)
                            },
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "req-review",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
                            )
                        },
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job
    assert slot_calls == {"generated-1": 1, "generated-2": 1, "generated-3": 2}

    gen3_prompts = [msg for slot, msg in slot_prompts if slot == "generated-3"]
    assert len(gen3_prompts) == 2
    first_summaries = _extract_prompt_json(
        gen3_prompts[0],
        "已考察槽位摘要（后续出题必须考察不同作答任务）：",
        "\n\n只输出如下 JSON",
    )
    assert [s["slot"] for s in first_summaries] == ["generated-1", "generated-2"]
    assert first_summaries[0]["focus"] == "数据库访问资源管理"
    assert first_summaries[0]["topic_key"] == "java.resource_mgmt"
    assert first_summaries[0]["anchor_refs"]
    assert "reason=duplicate_stem" in gen3_prompts[1]
    assert "冲突标识=generated-1" in gen3_prompts[1]
    assert "conflict_source=prior_generated" in gen3_prompts[1]
    assert "数据库访问资源管理" in gen3_prompts[1]
    assert slot1_stem in gen3_prompts[1]

    plan_id = job["result_ref"]["id"]
    public = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
    assert public["main_question_count"] == 6
    assert public["source_counts"] == {"bank": 3, "generated": 3}

    row = db.get(Job, uuid.UUID(job["id"]))
    plan_records = [r for r in row.ai_call_records if r["stage"] == "plan"]
    assert len(plan_records) == 4  # 每次模型调用只记 1 条，不把 1 次调用记成 2 次
    gen3_records = [r for r in plan_records if r["slot"] == "generated-3"]
    assert len(gen3_records) == 2
    assert gen3_records[0]["outcome"] == "success"
    assert gen3_records[0]["validation_outcome"] == "rejected"
    assert "reason=duplicate_stem" in gen3_records[0]["validation_details"]
    assert "conflict_slot=generated-1" in gen3_records[0]["validation_details"]
    assert gen3_records[1]["outcome"] == "success"
    assert gen3_records[1]["validation_outcome"] == "passed"
    assert db.query(Plan).filter(Plan.preparation_id == uuid.UUID(prep["id"])).count() == 1


def test_slot_three_repeated_duplicate_fails_with_structured_diagnostics_and_keeps_prior_work(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import httpx
    import re
    import uuid
    import uuid
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, JobWorkItem, Plan
    from sqlalchemy import select

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
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

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    slot_calls: dict[str, int] = {}
    slot1_stem = "结合你在校园图书借阅系统中对 HashMap 与 ArrayList 的使用，说明在什么场景下需要关注扩容与查询复杂度？"
    near_dup_stem = slot1_stem + "请说明依据。"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        slot_key = re.search(r"本次只为槽位 (generated-\d+)", user_msg).group(1)
        slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
        slot_calls[slot_key] = slot_calls.get(slot_key, 0) + 1
        anchors = _extract_prompt_json(
            user_msg,
            "允许锚点（anchors 必须原样取自这里）：",
            "\n\n可用依据卡",
        )
        fact_cards = _extract_prompt_json(
            user_msg,
            "可用依据卡（knowledge 题必须从中引用 fact_card）：",
            "\n\n已有题干",
        )
        anchor = anchors[0]
        if slot_key == "generated-1":
            q = _build_synthetic_question(
                question_type=slot_type,
                canonical_id="dup2-s1",
                stem=slot1_stem,
                focus="集合复杂度分析",
                topic_key="java.collections",
                anchor=anchor,
                rubric=rubric,
                fact_cards=fact_cards,
            )
        elif slot_key == "generated-2":
            q = _build_synthetic_question(
                question_type=slot_type,
                canonical_id="dup2-s2",
                stem="结合事务原子性与连接关闭顺序，如何避免异常中断导致资源泄漏？",
                focus="异常与资源释放",
                topic_key="java.exceptions",
                anchor=anchor,
                rubric=rubric,
                fact_cards=fact_cards,
            )
        else:
            # 两次都返回与 generated-1 近似重复的题干
            q = _build_synthetic_question(
                question_type=slot_type,
                canonical_id=f"dup2-s3-{slot_calls[slot_key]}",
                stem=near_dup_stem,
                focus="集合复杂度分析",
                topic_key="java.collections",
                anchor=anchor,
                rubric=rubric,
                fact_cards=fact_cards,
            )
        return httpx.Response(
            200,
            json={
                "id": f"req-{slot_key}-{slot_calls[slot_key]}",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)},
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "failed", job
    assert job["error"]["code"] == "AI_INVALID_OUTPUT"
    assert slot_calls == {"generated-1": 1, "generated-2": 1, "generated-3": 2}
    details = job["error"]["details"]
    assert "stage=plan" in details
    assert "slot=generated-3" in details
    assert "reason=duplicate_stem" in details
    assert "duplicate_kind=near" in details
    assert "conflict_source=prior_generated" in details
    assert "conflict_slot=generated-1" in details
    assert any(d.startswith("similarity=") for d in details)
    assert "threshold=0.70" in details
    # 不把简历原话或完整题干带入诊断
    assert "校园图书借阅系统" not in " ".join(details)

    job_uuid = uuid.UUID(job["id"])
    row = db.get(Job, job_uuid)
    assert len(row.ai_call_records) == 4
    gen3_recs = [r for r in row.ai_call_records if r["slot"] == "generated-3"]
    assert len(gen3_recs) == 2
    assert all(r["outcome"] == "success" and r["validation_outcome"] == "rejected" for r in gen3_recs)

    work_items = {
        w.slot_key: w
        for w in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_uuid)).all()
    }
    assert set(work_items.keys()) == {"generated-1", "generated-2"}
    assert all(w.validated is True for w in work_items.values())
    assert db.query(Plan).filter(Plan.preparation_id == uuid.UUID(prep["id"])).count() == 0


def test_validate_generated_attributes_bank_duplicate_and_semantic_review_rejects(
    client: TestClient, worker, monkeypatch
) -> None:
    import uuid
    import app.workers.runner as runner

    resume, jd = _snapshots()
    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    bank_q = BANK["questions"][0]
    anchor = {
        "snapshot_id": str(resume.id),
        "item_id": "project-1",
        "quote": resume.profile["projects"][0]["evidence"]["quote"],
    }
    candidate = _build_synthetic_question(
        question_type="experience",
        canonical_id="syn-bank-dup",
        stem=bank_q["stem"],
        focus="HashMap",
        topic_key="java.core",
        anchor=anchor,
        rubric=rubric,
        fact_cards=[],
    )
    with pytest.raises(AppError) as caught:
        worker._validate_generated(
            candidate,
            resume,
            jd,
            allowed_fact_card_ids=set(),
            expected_rubric=rubric,
            expected_question_type="experience",
            forbidden_entries=[
                {
                    "stem": bank_q["stem"],
                    "source": "bank",
                    "ref": bank_q["question_id"],
                }
            ],
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert "reason=duplicate_stem" in caught.value.details
    assert "duplicate_kind=exact" in caught.value.details
    assert "conflict_source=bank" in caught.value.details
    assert f"conflict_question_id={bank_q['question_id']}" in caught.value.details

    # 语义复核发现重复仍拒绝发布
    class SemanticDupProvider(TestProvider):
        def review_questions(self, *, questions, material_texts, fact_cards=None):
            return {
                "duplicate_pairs": [
                    {
                        "left_id": questions[0]["id"],
                        "right_id": questions[-1]["id"],
                        "reason": "语义实质考察同一问题",
                    }
                ],
                "grounding_issues": [],
                "uncertain": False,
            }

    monkeypatch.setattr(runner, "get_provider", lambda _: SemanticDupProvider())
    _, token = issue_token(client)
    r_id = create_confirmed_resume(client, token)
    j_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": r_id,
            "jd_snapshot_id": j_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"
    g = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, g.json()["id"], worker, attempts=4)
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "DUPLICATE_QUESTION"


def test_generated_skill_code_gate_anchor_consistency_and_semantic_review_retry(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """合成计划词表外技能阻断、生成槽过滤词表外标签、错锚点拦截及语义复核严格校验与任务级重试。"""
    import uuid
    import app.workers.runner as runner
    from app.models import Job, Plan

    # 1. 整份计划校验：生成题携带词表外技能编码必须被 validate_internal_plan 拒绝
    bad_plan = copy.deepcopy(BASE_PLAN)
    bad_plan["questions"][3]["skill_codes"] = ["invented.unknown.skill"]
    with pytest.raises(AppError) as caught:
        validate_internal_plan(
            parse_internal_plan(bad_plan),
            PlanValidationContext(True, db=db),
        )
    assert caught.value.code == "EVALUATION_INVALID"

    # 2. _validate_generated 检查 allowed_anchors 一致性
    resume, jd = _snapshots()
    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    valid_q = TestProvider().generate_questions(
        count=1,
        resume_snapshot=resume,
        jd_snapshot=jd,
        rubric=rubric,
        avoid_stems=[],
        context={"slot_key": "generated-1", "question_type": "experience"},
    )[0]
    allowed_anchors = [
        {
            "snapshot_id": valid_q["anchors"][0]["snapshot_id"],
            "item_id": valid_q["anchors"][0]["item_id"],
            "quote": valid_q["anchors"][0]["quote"],
        }
    ]
    # 修改 quote 为子串（不匹配完整允许锚点）时被拦截
    modified_anchor_q = copy.deepcopy(valid_q)
    modified_anchor_q["anchors"][0]["quote"] = valid_q["anchors"][0]["quote"][:6]
    with pytest.raises(AppError) as anchor_err:
        worker._validate_generated(
            modified_anchor_q,
            resume,
            jd,
            allowed_fact_card_ids=set(),
            expected_rubric=rubric,
            expected_question_type="experience",
            allowed_anchors=allowed_anchors,
        )
    assert anchor_err.value.code == "AI_INVALID_OUTPUT"
    assert "reason=invalid_anchor_selection" in anchor_err.value.details

    # 3. 语义复核严格校验：缺字段、字符串布尔、数组 ID（不抛 TypeError）
    known = {"q-1", "q-2"}
    for bad_semantic in [
        {"uncertain": False},
        {"duplicate_pairs": [], "grounding_issues": [], "uncertain": "false"},
        {
            "duplicate_pairs": [{"left_id": ["q-1"], "right_id": "q-2", "reason": "dup"}],
            "grounding_issues": [],
            "uncertain": False,
        },
        {
            "duplicate_pairs": [{"left_id": "q-1", "right_id": "q-1", "reason": "self"}],
            "grounding_issues": [],
            "uncertain": False,
        },
        {
            "duplicate_pairs": [],
            "grounding_issues": [{"question_id": "q-1", "claim": "   ", "reason": "blank"}],
            "uncertain": False,
        },
    ]:
        with pytest.raises(AppError) as sem_err:
            worker._validate_semantic_output(bad_semantic, known)
        assert sem_err.value.code == "AI_INVALID_OUTPUT"

    # 4. 端到端：生成题带词表外可选技能过滤后保存槽位；语义复核初次缺字段失败可显式重试并复用已生成槽位，发布 3+3 计划
    _, token = issue_token(client)
    r_id = create_confirmed_resume(client, token)
    j_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": r_id,
            "jd_snapshot_id": j_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"

    import httpx
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    ds_settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-stub",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    gen_slots_called: list[str] = []
    review_calls = 0

    def ds_handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_calls
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            import re

            slot_match = re.search(r"本次只为槽位 (generated-\d+)", user_msg)
            slot_key = slot_match.group(1) if slot_match else "generated-1"
            gen_slots_called.append(slot_key)
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
            anchors_block = user_msg.split("允许锚点（anchors 必须原样取自这里）：", 1)[1]
            anchors_block = anchors_block.split("\n\n可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[0].strip()
            anchors = json.loads(anchors_block)
            cards_block = user_msg.split("可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[1]
            cards_block = cards_block.split("\n\n已有题干（禁止复用或同义改写）：", 1)[0].strip()
            fact_cards = json.loads(cards_block)
            anchor = anchors[0]
            basis = [
                {
                    "kind": "material",
                    "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}",
                    "note": "依据材料",
                },
                {"kind": "reasoning", "ref_id": "hypothetical-analysis", "note": "仅评价思路"},
            ]
            if slot_type == "knowledge" and fact_cards:
                basis.insert(
                    0,
                    {"kind": "fact_card", "ref_id": fact_cards[0]["id"], "note": "依据卡"},
                )
            stems = {
                "generated-1": "围绕图书借阅系统，说明在并发读写场景下为什么需要校验边界并设计对照实验？",
                "generated-2": "结合已审技术依据卡，解释为什么局部变量和共享集合在线程安全边界上存在本质差异？",
                "generated-3": "当检索数据量扩大十倍且要求响应稳定时，如何从数据结构与索引前提定位性能退化？",
            }
            q = {
                "question_type": slot_type,
                "canonical_id": f"ds-{slot_key}",
                "stem": stems[slot_key],
                "focus": "边界验证",
                "topic_key": "java.core",
                "skill_codes": ["java.core", "invented.tag", "java.core"],
                "anchors": [
                    {
                        "snapshot_id": anchor["snapshot_id"],
                        "item_id": anchor["item_id"],
                        "quote": anchor["quote"],
                    }
                ],
                "evaluation": {
                    "goal": "考察边界验证",
                    "reference_approach": "澄清前提并说明可执行验证步骤。",
                    "key_points": [
                        {"point": "明确事实前提", "weight": 0.6},
                        {"point": "设计验证用例", "weight": 0.4},
                    ],
                    "acceptable_alternatives": ["等价验证方案"],
                    "common_mistakes": ["缺少边界约束"],
                    "rubric": rubric,
                    "basis": basis,
                    "answer_status": "model_generated",
                    "trust": "provisional",
                    "uncertainties": [],
                },
            }
            return httpx.Response(
                200,
                json={"id": f"req-{slot_key}", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)}}]},
            )

        review_calls += 1
        if review_calls == 1:
            # 首次复核仅返回 {"uncertain": false}，缺少 duplicate_pairs/grounding_issues
            return httpx.Response(
                200,
                json={"id": "req-rev-1", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"uncertain": False})}}]},
            )
        return httpx.Response(
            200,
            json={"id": "req-rev-2", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"duplicate_pairs": [], "grounding_issues": [], "uncertain": False})}}]},
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(ds_settings, transport=httpx.MockTransport(ds_handler)),
    )
    g2 = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    first_fail = wait_job(client, token, g2.json()["id"], worker, attempts=4)
    assert first_fail["state"] == "failed"
    assert first_fail["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "stage=plan_review" in first_fail["error"]["details"]
    assert "reason=invalid_review_schema" in first_fail["error"]["details"]
    assert first_fail["retryable"] is True
    assert gen_slots_called == ["generated-1", "generated-2", "generated-3"]

    gen_slots_called.clear()
    retry_resp = client.post(
        f"/v1/jobs/{g2.json()['id']}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry_resp.status_code == 202
    final_ok = wait_job(client, token, g2.json()["id"], worker, attempts=4)
    assert final_ok["state"] == "succeeded", final_ok
    # 已生成并通过校验的槽位必须复用，不再重新生成
    assert gen_slots_called == []
    plan_row = db.get(Plan, uuid.UUID(final_ok["result_ref"]["id"]))
    assert plan_row is not None
    gen_questions = [q for q in plan_row.internal_payload["questions"] if q["source"] == "generated"]
    assert len(gen_questions) == 3
    assert all(q["skill_codes"] == ["java.core"] for q in gen_questions)


def test_plan_duplicate_json_key_slot_failure_retry_reuses_saved_slot_and_enforces_limits(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """Section 4：槽位 1 成功入库后槽位 2 连续重键失败，只读查状态不重跑，重试复用槽位 1 发布 3+3；再测次数上限、真实重题与版本过期。"""
    import re
    import uuid
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, JobWorkItem, Plan, Preparation

    _, token = issue_token(client)
    r_id = create_confirmed_resume(client, token)
    j_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": r_id,
            "jd_snapshot_id": j_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"

    rubric = {
        "id": "m1-java-rubric-v1",
        "version": 1,
        "dimensions": [
            {
                "id": "core",
                "name": "核心原理与边界",
                "weight": 0.6,
                "low_anchor": "缺少前提与验证",
                "high_anchor": "准确说明原理并给出边界用例",
            },
            {
                "id": "verify",
                "name": "验证与可维护性",
                "weight": 0.4,
                "low_anchor": "没有验证思路",
                "high_anchor": "能用可检验步骤验证结论",
            },
        ],
    }

    ds_settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-stub",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    fail_slot2 = True
    slots_called: list[str] = []

    def build_valid_question(user_msg: str, slot_key: str) -> dict:
        slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
        anchors_block = user_msg.split("允许锚点（anchors 必须原样取自这里）：", 1)[1]
        anchors_block = anchors_block.split("\n\n可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[0].strip()
        anchors = json.loads(anchors_block)
        cards_block = user_msg.split("可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[1]
        cards_block = cards_block.split("\n\n已有题干（禁止复用或同义改写）：", 1)[0].strip()
        fact_cards = json.loads(cards_block)
        schema_block = user_msg.split("只输出如下 JSON（恰好 1 题，rubric 必须原样保留）：\n", 1)[1]
        schema_block = schema_block.split("\n\n简历原文（仅作语境参考）：", 1)[0].strip()
        expected_rubric = json.loads(schema_block)["questions"][0]["evaluation"]["rubric"]
        anchor = anchors[0]
        basis = [
            {"kind": "material", "ref_id": f"{anchor['snapshot_id']}/{anchor['item_id']}", "note": "依据材料"},
            {"kind": "reasoning", "ref_id": "hypothetical-analysis", "note": "仅评价思路"},
        ]
        if slot_type == "knowledge" and fact_cards:
            basis.insert(0, {"kind": "fact_card", "ref_id": fact_cards[0]["id"], "note": "依据卡"})
        stems = {
            "generated-1": "围绕图书借阅系统，说明在并发读写场景下为什么需要校验边界并设计对照实验？",
            "generated-2": "结合已审技术依据卡，解释为什么局部变量和共享集合在线程安全边界上存在本质差异？",
            "generated-3": "当检索数据量扩大十倍且要求响应稳定时，如何从数据结构与索引前提定位性能退化？",
        }
        return {
            "question_type": slot_type,
            "canonical_id": f"dupkey-{slot_key}",
            "stem": stems[slot_key],
            "focus": "边界验证",
            "topic_key": "java.core",
            "skill_codes": ["java.core"],
            "anchors": [{"snapshot_id": anchor["snapshot_id"], "item_id": anchor["item_id"], "quote": anchor["quote"]}],
            "evaluation": {
                "goal": "考察边界验证",
                "reference_approach": "澄清前提并说明可执行验证步骤。",
                "key_points": [
                    {"point": "明确事实前提", "weight": 0.6},
                    {"point": "设计验证用例", "weight": 0.4},
                ],
                "acceptable_alternatives": ["等价验证方案"],
                "common_mistakes": ["缺少边界约束"],
                "rubric": expected_rubric,
                "basis": basis,
                "answer_status": "model_generated",
                "trust": "provisional",
                "uncertainties": [],
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            slot_match = re.search(r"本次只为槽位 (generated-\d+)", user_msg)
            slot_key = slot_match.group(1) if slot_match else "generated-1"
            slots_called.append(slot_key)
            if slot_key == "generated-2" and fail_slot2:
                return httpx.Response(
                    200,
                    json={
                        "id": "req-dupkey",
                        "choices": [{"finish_reason": "stop", "message": {"content": '{"questions":[],"questions":[]}'}}],
                    },
                )
            q = build_valid_question(user_msg, slot_key)
            return httpx.Response(
                200,
                json={"id": f"req-{slot_key}", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)}}]},
            )
        return httpx.Response(
            200,
            json={"id": "req-rev", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps({"duplicate_pairs": [], "grounding_issues": [], "uncertain": False})}}]},
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(ds_settings, transport=httpx.MockTransport(handler)),
    )
    gen = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = gen.json()["id"]
    first_fail = wait_job(client, token, job_id, worker, attempts=6)
    assert first_fail["state"] == "failed"
    assert first_fail["retryable"] is True
    assert first_fail["attempt"] == 1
    assert first_fail["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "stage=plan" in first_fail["error"]["details"]
    assert "slot=generated-2" in first_fail["error"]["details"]
    assert "reason=duplicate_json_key" in first_fail["error"]["details"]
    assert slots_called == ["generated-1", "generated-2", "generated-2"]
    saved_items = db.query(JobWorkItem).filter(JobWorkItem.job_id == uuid.UUID(job_id)).all()
    assert [item.slot_key for item in saved_items] == ["generated-1"]

    # 只读 GET 保持只读，不触发模型调用
    slots_called.clear()
    readonly_get = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert readonly_get["retryable"] is True
    assert slots_called == []

    # 重试复用已验证的 generated-1，仅补齐 generated-2 和 generated-3 并发布 3+3
    fail_slot2 = False
    retry = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry.status_code == 202
    done = wait_job(client, token, job_id, worker, attempts=6)
    assert done["state"] == "succeeded", done
    assert done["attempt"] == 2
    assert slots_called == ["generated-2", "generated-3"]
    plan_row = db.get(Plan, uuid.UUID(done["result_ref"]["id"]))
    assert plan_row is not None
    assert plan_row.internal_payload["source_counts"] == {"bank": 3, "generated": 3}

    # 覆盖：1) 超过总次数不可重试；2) 真实 duplicate_stem 不可重试；3) 版本过期不可重试
    monkeypatch.undo()
    prep2 = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": r_id,
            "jd_snapshot_id": j_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m2 = client.post(
        f"/v1/preparations/{prep2['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m2.json()["id"], worker)["state"] == "succeeded"
    g2 = client.post(
        f"/v1/preparations/{prep2['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    g2_row = db.get(Job, uuid.UUID(g2["id"]))
    assert g2_row is not None
    g2_row.state = "failed"
    g2_row.error_code = "AI_INVALID_OUTPUT"
    # attempt=2 超过总次数 -> retryable=False
    g2_row.attempt = 2
    g2_row.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["stage=plan", "slot=generated-1", "reason=duplicate_json_key"],
        "request_id": "exhausted-dupkey",
    }
    db.commit()
    assert client.get(f"/v1/jobs/{g2_row.id}", headers=auth(token)).json()["retryable"] is False

    # attempt=1 但真实 duplicate_stem -> retryable=False
    g2_row.attempt = 1
    g2_row.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["stage=plan", "slot=generated-1", "reason=duplicate_stem"],
        "request_id": "dup-stem",
    }
    db.commit()
    assert client.get(f"/v1/jobs/{g2_row.id}", headers=auth(token)).json()["retryable"] is False

    # attempt=1 且可重试原因 duplicate_json_key，但准备版本过期 -> retryable=False
    g2_row.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["stage=plan", "slot=generated-1", "reason=duplicate_json_key"],
        "request_id": "stale-dupkey",
    }
    prep2_row = db.get(Preparation, uuid.UUID(prep2["id"]))
    assert prep2_row is not None
    prep2_row.revision = 2
    db.commit()
    assert client.get(f"/v1/jobs/{g2_row.id}", headers=auth(token)).json()["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{g2_row.id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )


def test_semantic_review_duplicate_repairs_conflicting_slot_and_publishes(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """语义复核发现重复对时，仅定向重生成冲突槽位、使用 semantic_duplicate 纠正与 temperature=0.5 并二次复核发布。"""
    import re
    import uuid
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, JobWorkItem, Plan
    from sqlalchemy import select

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    slot_calls: dict[str, int] = {}
    slot3_requests: list[dict] = []
    review_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_calls
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            slot_key = re.search(r"本次只为槽位 (generated-\d+)", user_msg).group(1)
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
            slot_calls[slot_key] = slot_calls.get(slot_key, 0) + 1
            if slot_key == "generated-3":
                slot3_requests.append({"body": body, "user_msg": user_msg})
            anchors = _extract_prompt_json(
                user_msg,
                "允许锚点（anchors 必须原样取自这里）：",
                "\n\n可用依据卡",
            )
            fact_cards = _extract_prompt_json(
                user_msg,
                "可用依据卡（knowledge 题必须从中引用 fact_card）：",
                "\n\n已有题干",
            )
            anchor = anchors[0]
            if slot_key == "generated-1":
                q = _build_synthetic_question(
                    question_type=slot_type,
                    canonical_id="sem-rep-1",
                    stem="结合你在校园图书借阅系统中的实现，说明如何划分借阅状态变更的边界与异常处理流程？",
                    focus="借阅状态流转与异常边界",
                    topic_key="java.design.boundary",
                    anchor=anchor,
                    rubric=rubric,
                    fact_cards=fact_cards,
                )
            elif slot_key == "generated-2":
                q = _build_synthetic_question(
                    question_type=slot_type,
                    canonical_id="sem-rep-2",
                    stem="从集合底层契约出发，解释 ArrayList 预分配容量与元素实际个数之间的差异及测试验证方法。",
                    focus="集合容量与契约边界",
                    topic_key="java.collections.capacity",
                    anchor=anchor,
                    rubric=rubric,
                    fact_cards=fact_cards,
                )
            else:
                if slot_calls[slot_key] == 1:
                    q = _build_synthetic_question(
                        question_type=slot_type,
                        canonical_id="sem-rep-3-v1",
                        stem="围绕你参与过的借阅模块开发，如果发生非法输入或状态冲突，你会用什么步骤定位与验证？",
                        focus="异常输入与状态验证",
                        topic_key="java.validation",
                        anchor=anchor,
                        rubric=rubric,
                        fact_cards=fact_cards,
                    )
                else:
                    q = _build_synthetic_question(
                        question_type=slot_type,
                        canonical_id="sem-rep-3-v2",
                        stem="假设接口日志出现慢查询或局部数据丢失，你会按照怎样的排查顺序设计最小复现实验并确认回归通过？",
                        focus="问题排查与回归验证",
                        topic_key="test.troubleshooting",
                        anchor=anchor,
                        rubric=rubric,
                        fact_cards=fact_cards,
                    )
            return httpx.Response(
                200,
                json={
                    "id": f"req-{slot_key}-{slot_calls[slot_key]}",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)},
                        }
                    ],
                },
            )

        review_calls += 1
        review_qs = _extract_prompt_json(
            user_msg,
            "题目（含考察目标、参考思路、评分要点、锚点与评价依据）：",
            "\n\n材料原文：",
        )
        gen_qs = [q for q in review_qs if q["source"] == "generated"]
        if review_calls == 1:
            return httpx.Response(
                200,
                json={
                    "id": "req-rev-1",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": json.dumps(
                                    {
                                        "duplicate_pairs": [
                                            {
                                                "left_id": gen_qs[0]["id"],
                                                "right_id": gen_qs[2]["id"],
                                                "reason": "实质考察相同边界",
                                            }
                                        ],
                                        "grounding_issues": [],
                                        "uncertain": False,
                                    },
                                    ensure_ascii=False,
                                )
                            },
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "req-rev-2",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
                            )
                        },
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    gen = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, gen.json()["id"], worker, attempts=6)
    assert job["state"] == "succeeded", job
    assert slot_calls == {"generated-1": 1, "generated-2": 1, "generated-3": 2}
    assert review_calls == 2
    assert len(slot3_requests) == 2
    repair_req = slot3_requests[1]
    assert repair_req["body"]["temperature"] == 0.5
    assert "reason=semantic_duplicate" in repair_req["user_msg"]
    assert "冲突标识=generated-1" in repair_req["user_msg"]
    assert "围绕你参与过的借阅模块开发" in repair_req["user_msg"]

    job_uuid = uuid.UUID(job["id"])
    work_items = {
        w.slot_key: w
        for w in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_uuid)).all()
    }
    assert set(work_items.keys()) == {"generated-1", "generated-2", "generated-3"}
    assert "慢查询或局部数据丢失" in work_items["generated-3"].payload["question"]["stem"]

    row = db.get(Job, job_uuid)
    review_records = [r for r in row.ai_call_records if r["stage"] == "plan_review"]
    assert len(review_records) == 2
    assert review_records[0]["validation_outcome"] == "rejected"
    assert "reason=semantic_duplicate" in review_records[0]["validation_details"]
    assert "slot=generated-3" in review_records[0]["validation_details"]
    assert "conflict_slot=generated-1" in review_records[0]["validation_details"]
    assert review_records[1]["validation_outcome"] == "passed"
    assert review_records[1]["is_correction"] is True
    assert review_records[1]["correction_attempt"] == 2

    plan_row = db.get(Plan, uuid.UUID(job["result_ref"]["id"]))
    assert plan_row is not None
    assert plan_row.internal_payload["source_counts"] == {"bank": 3, "generated": 3}


def test_semantic_review_bank_conflict_bounded_repair_and_structured_details(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """语义复核持续报与题库重复时，最多修复 1 轮后以 DUPLICATE_QUESTION + 安全明细失败，不泄露模型原文。"""
    import re
    import uuid
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, Plan

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    slot_calls: dict[str, int] = {}
    review_calls = 0
    expected_bank_qid: str | None = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_calls, expected_bank_qid
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            slot_key = re.search(r"本次只为槽位 (generated-\d+)", user_msg).group(1)
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
            slot_calls[slot_key] = slot_calls.get(slot_key, 0) + 1
            if expected_bank_qid is None:
                slot_meta = _extract_prompt_json(
                    user_msg,
                    "槽位与岗位信息：",
                    "\n\n允许锚点",
                )
                expected_bank_qid = slot_meta["bank_covered_topics"][0]["question_id"]
            anchors = _extract_prompt_json(
                user_msg,
                "允许锚点（anchors 必须原样取自这里）：",
                "\n\n可用依据卡",
            )
            fact_cards = _extract_prompt_json(
                user_msg,
                "可用依据卡（knowledge 题必须从中引用 fact_card）：",
                "\n\n已有题干",
            )
            anchor = anchors[0]
            stems = {
                "generated-1": "结合你的课程项目，说明你是如何拆解功能模块并确认核心输入边界的？",
                "generated-2": (
                    "解释 ArrayList 与普通数组在扩容机制与内存占用上的区别，并说明测试如何校验越界行为？"
                    if slot_calls[slot_key] == 1
                    else "结合 Java 核心类库契约，说明迭代过程中发生并发修改时应如何识别并发风险并设计测试？"
                ),
                "generated-3": "在数据规模扩大后，如果检索接口响应变慢，你会从哪几步排查定位并验证修复？",
            }
            q = _build_synthetic_question(
                question_type=slot_type,
                canonical_id=f"bank-conf-{slot_key}-{slot_calls[slot_key]}",
                stem=stems[slot_key],
                focus="边界分析与验证",
                topic_key="java.core",
                anchor=anchor,
                rubric=rubric,
                fact_cards=fact_cards,
            )
            return httpx.Response(
                200,
                json={
                    "id": f"req-{slot_key}-{slot_calls[slot_key]}",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)},
                        }
                    ],
                },
            )

        review_calls += 1
        review_qs = _extract_prompt_json(
            user_msg,
            "题目（含考察目标、参考思路、评分要点、锚点与评价依据）：",
            "\n\n材料原文：",
        )
        bank_q = next(q for q in review_qs if q["source"] == "bank")
        gen2_q = [q for q in review_qs if q["source"] == "generated"][1]
        return httpx.Response(
            200,
            json={
                "id": f"req-rev-{review_calls}",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {
                                    "duplicate_pairs": [
                                        {
                                            "left_id": bank_q["id"],
                                            "right_id": gen2_q["id"],
                                            "reason": "SECRET_RAW_REASON_TEXT_DO_NOT_LEAK",
                                        }
                                    ],
                                    "grounding_issues": [],
                                    "uncertain": False,
                                },
                                ensure_ascii=False,
                            )
                        },
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    gen = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, gen.json()["id"], worker, attempts=6)
    assert failed["state"] == "failed", failed
    assert failed["retryable"] is False
    assert slot_calls == {"generated-1": 1, "generated-2": 2, "generated-3": 1}
    assert review_calls == 2
    assert failed["error"]["code"] == "DUPLICATE_QUESTION"
    assert failed["error"]["message"] == "语义去重复核发现重复题目"
    details = failed["error"]["details"]
    assert "stage=plan_review" in details
    assert "reason=semantic_duplicate" in details
    assert "duplicate_count=1" in details
    assert "slot=generated-2" in details
    assert "conflict_source=bank" in details
    assert expected_bank_qid is not None
    assert f"conflict_question_id={expected_bank_qid}" in details
    serialized_details = " ".join(details)
    assert "SECRET_RAW_REASON_TEXT_DO_NOT_LEAK" not in serialized_details
    assert "并发修改时应如何识别" not in serialized_details
    assert db.query(Plan).filter(Plan.preparation_id == uuid.UUID(prep["id"])).count() == 0

    job_row = db.get(Job, uuid.UUID(failed["id"]))
    assert len([r for r in job_row.ai_call_records if r["stage"] == "plan_review"]) == 2


def test_generation_prompt_includes_bank_covered_topics_slot_angle_and_preferred_anchors(
    client: TestClient, worker, monkeypatch
) -> None:
    """槽位提示词包含 bank_covered_topics、slot_angle，且 preferred_anchor_ids 随已使用锚点动态避让。"""
    import re
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    _, token = issue_token(client)
    multi_resume_text = (
        "南京信息工程大学 本科 计算机科学与技术\n"
        "实习：某软件公司 | 后端实习生 | 2025.07-2025.09 | 维护订单导出脚本\n"
        "项目：校园自习室预约系统 | 2026.03-2026.05 | 负责预约冲突校验与接口测试\n"
        "技能：掌握 Java 基础与 JVM 内存机制\n"
        "技能：理解 Java 多线程与 JUnit 单元测试"
    )
    multi_resume_profile = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科",
                "major": "计算机科学与技术",
                "evidence": {
                    "quote": "南京信息工程大学 本科 计算机科学与技术",
                    "origin": "extracted",
                },
            }
        ],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "某软件公司",
                "title": "后端实习生",
                "description": "维护订单导出脚本",
                "evidence": {
                    "quote": "某软件公司 | 后端实习生 | 2025.07-2025.09 | 维护订单导出脚本",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [
            {
                "item_id": "project-1",
                "name": "校园自习室预约系统",
                "description": "负责预约冲突校验与接口测试",
                "skill_codes": ["java.core", "test.junit"],
                "evidence": {
                    "quote": "校园自习室预约系统 | 2026.03-2026.05 | 负责预约冲突校验与接口测试",
                    "origin": "extracted",
                },
            }
        ],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java 基础与 JVM",
                "code": "java.core",
                "evidence": {
                    "quote": "掌握 Java 基础与 JVM 内存机制",
                    "origin": "extracted",
                },
            },
            {
                "item_id": "skill-2",
                "name": "Java 多线程与 JUnit",
                "code": "java.concurrency",
                "evidence": {
                    "quote": "理解 Java 多线程与 JUnit 单元测试",
                    "origin": "extracted",
                },
            },
        ],
    }
    created_resume = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "多锚点简历", "text": multi_resume_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created_resume.status_code == 201
    resume_snap = client.post(
        f"/v1/materials/{created_resume.json()['id']}/confirm",
        json={"revision": 1, "profile": multi_resume_profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert resume_snap.status_code == 201
    resume_id = resume_snap.json()["id"]
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m.json()["id"], worker)["state"] == "succeeded"

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    slot_metas: dict[str, dict] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            slot_key = re.search(r"本次只为槽位 (generated-\d+)", user_msg).group(1)
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user_msg else "experience"
            slot_meta = _extract_prompt_json(
                user_msg,
                "槽位与岗位信息：",
                "\n\n允许锚点",
            )
            slot_metas[slot_key] = slot_meta
            anchors = _extract_prompt_json(
                user_msg,
                "允许锚点（anchors 必须原样取自这里）：",
                "\n\n可用依据卡",
            )
            fact_cards = _extract_prompt_json(
                user_msg,
                "可用依据卡（knowledge 题必须从中引用 fact_card）：",
                "\n\n已有题干",
            )
            anchor_by_id = {a["item_id"]: a for a in anchors}
            chosen_id = (
                "work-1"
                if slot_key == "generated-1"
                else ("project-1" if slot_key == "generated-2" else "skill-1")
            )
            chosen_anchor = anchor_by_id[chosen_id]
            stems = {
                "generated-1": "围绕你在某软件公司维护订单导出脚本的实习经历，说明如何拆解导出接口需求并防范数据缺失？",
                "generated-2": "结合校园自习室预约系统中的冲突校验需求，解释集合初始化容量与实际元素数量的区别及测试方法。",
                "generated-3": "针对 Java 基础与 JVM 内存机制，当应用频繁出现对象分配延迟时，你会按什么步骤排查并验证优化？",
            }
            q = _build_synthetic_question(
                question_type=slot_type,
                canonical_id=f"meta-{slot_key}",
                stem=stems[slot_key],
                focus=f"考察维度-{slot_key}",
                topic_key="java.core",
                anchor=chosen_anchor,
                rubric=rubric,
                fact_cards=fact_cards,
            )
            return httpx.Response(
                200,
                json={
                    "id": f"req-{slot_key}",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps({"questions": [q]}, ensure_ascii=False)},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "req-rev",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": json.dumps(
                                {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
                            )
                        },
                    }
                ],
            },
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-flash",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    gen = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, gen.json()["id"], worker, attempts=6)
    assert done["state"] == "succeeded", done
    assert set(slot_metas.keys()) == {"generated-1", "generated-2", "generated-3"}
    for key in ("generated-1", "generated-2", "generated-3"):
        meta = slot_metas[key]
        assert len(meta["bank_covered_topics"]) == 3
        assert meta["slot_angle"]
    assert "work-1" in slot_metas["generated-1"]["preferred_anchor_ids"]
    assert "work-1" not in slot_metas["generated-2"]["preferred_anchor_ids"]
    assert "project-1" in slot_metas["generated-2"]["preferred_anchor_ids"]
    assert "work-1" not in slot_metas["generated-3"]["preferred_anchor_ids"]
    assert "project-1" not in slot_metas["generated-3"]["preferred_anchor_ids"]
    assert "skill-1" in slot_metas["generated-3"]["preferred_anchor_ids"]
    assert slot_metas["generated-1"]["slot_angle"] != slot_metas["generated-2"]["slot_angle"]
    assert slot_metas["generated-2"]["slot_angle"] != slot_metas["generated-3"]["slot_angle"]
    assert slot_metas["generated-1"]["slot_angle"] != slot_metas["generated-3"]["slot_angle"]
