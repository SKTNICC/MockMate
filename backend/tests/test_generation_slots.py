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
