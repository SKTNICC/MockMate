"""真实 DeepSeek adapter 的端到端接线测试（使用模拟 HTTP，不产生真实调用）。

覆盖：真实 provider 的提示词构建 → HTTP 请求 → JSON 校验 → worker 匹配/生成 → 计划发布。
不是真实 AI 集成通过；只证明 adapter 代码路径与提示词内容正确。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.provider import DeepSeekProvider
from app.core.config import Settings
from app.models import Plan
from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    REVIEWED_RESUME_TEXT,
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    issue_token,
    new_key,
    wait_job,
)

PROJECT_QUOTE = (
    "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；"
    "我参与了借阅模块与边界测试。"
)

BANK_FIXTURE = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/m1/bank-candidates.json").read_text(
        encoding="utf-8"
    )
)
# 生成题必须复用本次提供的已审核量表，而不是自造一个同名量表。
RUBRIC = BANK_FIXTURE["questions"][0]["evaluation"]["rubric"]

SETTINGS = Settings(
    APP_ENV="test",
    DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
    PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
    AI_PROVIDER="deepseek",
    AI_MODEL="deepseek-stub-model",
    AI_API_KEY="stub-key",
    AI_BASE_URL="https://api.deepseek.test",
    ALLOW_TEST_FIXTURES=True,
)


def _chat(content: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "stub-request",
            "choices": [{"message": {"content": json.dumps(content, ensure_ascii=False)}}],
        },
    )


def _stub_handler(captured: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user = body["messages"][-1]["content"]
        captured.append(user)

        if "把简历原文结构化" in user:
            return _chat(
                {
                    "kind": "resume",
                    "education": REVIEWED_RESUME_PROFILE["education"],
                    "work_experience": [],
                    "projects": REVIEWED_RESUME_PROFILE["projects"],
                    "skills": REVIEWED_RESUME_PROFILE["skills"],
                }
            )

        if "逐条对照 JD 要求" in user:
            snapshot_id = re.search(r'"snapshot_id": "([0-9a-f-]{36})"', user).group(1)
            return _chat(
                {
                    "rows": [
                        {
                            "requirement_id": "req-1",
                            "status": "supported",
                            "resume_evidence": [
                                {
                                    "snapshot_id": snapshot_id,
                                    "item_id": "project-1",
                                    "quote": PROJECT_QUOTE,
                                }
                            ],
                            "explanation": "项目原文直接涉及 Java 集合的使用。",
                            "suggested_focus": "说明集合选择与边界验证",
                        },
                        {
                            "requirement_id": "req-2",
                            "status": "to_verify",
                            "resume_evidence": [],
                            "explanation": "简历提到 JUnit 学习，但没有直接对应边界测试要求。",
                            "suggested_focus": "补充可核对的测试事实",
                        },
                    ]
                }
            )

        if "生成" in user and "主问题" in user:
            count = int(re.search(r"生成 (\d+) 道", user).group(1))
            resume_snapshot = re.search(r"简历快照 ID：([0-9a-f-]{36})", user).group(1)
            profile_block = user.split("简历结构（anchors.item_id 必须取自这里的 item_id）：", 1)[1]
            profile_block = profile_block.split("\n\n简历快照 ID：", 1)[0].strip()
            profile = json.loads(profile_block)
            anchor_item = (profile.get("projects") or profile.get("skills") or [])[0]
            item_id = anchor_item["item_id"]
            quote = anchor_item["evidence"]["quote"]
            questions = []
            for index in range(count):
                questions.append(
                    {
                        "question_type": "experience",
                        "canonical_id": f"stub-generated-{index + 1}",
                        "stem": (
                            f"结合你材料里的第{index + 1}段经历，如果要在真实限制下满足该岗位要求，"
                            "你会先确认哪些前提，并如何验证结论？"
                        ),
                        "focus": "验证思路与边界",
                        "topic_key": "java.core",
                        "skill_codes": ["java.core"],
                        "anchors": [
                            {
                                "snapshot_id": resume_snapshot,
                                "item_id": item_id,
                                "quote": quote,
                            }
                        ],
                        "evaluation": {
                            "goal": "考察材料事实与验证思路",
                            "reference_approach": "先澄清前提，再说明做法，最后给出可执行的验证步骤。",
                            "key_points": [
                                {"point": "引用真实材料事实", "weight": 0.6},
                                {"point": "给出可检验的验证方式", "weight": 0.4},
                            ],
                            "acceptable_alternatives": ["等价且前提明确的方案"],
                            "common_mistakes": ["把一次结果当成全部保证"],
                            "rubric": RUBRIC,
                            "basis": [
                                {
                                    "kind": "material",
                                    "ref_id": f"{resume_snapshot}/{item_id}",
                                    "note": "以已确认简历原文为事实边界",
                                },
                                {
                                    "kind": "reasoning",
                                    "ref_id": "hypothetical-analysis",
                                    "note": "只评价思路",
                                },
                            ],
                            "answer_status": "model_generated",
                            "trust": "provisional",
                            "uncertainties": [],
                        },
                    }
                )
            return _chat({"questions": questions})

        # 去重复核
        return _chat({"duplicate_pairs": [], "grounding_issues": [], "uncertain": False})

    return handler


def test_deepseek_adapter_full_match_and_plan_flow(client: TestClient, worker, db, monkeypatch) -> None:
    import app.workers.runner as runner

    captured: list[str] = []
    provider = DeepSeekProvider(SETTINGS, transport=httpx.MockTransport(_stub_handler(captured)))
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    _, token = issue_token(client)
    resume = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "真实 adapter 简历", "text": REVIEWED_RESUME_TEXT},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    resume_snapshot = client.post(
        f"/v1/materials/{resume['id']}/confirm",
        json={"revision": 1, "profile": REVIEWED_RESUME_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    jd = client.post(
        "/v1/materials/text",
        json={
            "kind": "jd",
            "title": "内置 JD",
            "text": TEMPLATE_JD_TEXT,
            "template_id": "java-backend-intern-v1",
            "template_version": 1,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    jd_snapshot = client.post(
        f"/v1/materials/{jd['id']}/confirm",
        json={"revision": 1, "profile": TEMPLATE_JD_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_snapshot["id"],
            "jd_snapshot_id": jd_snapshot["id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()

    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    match_job = wait_job(client, token, match.json()["id"], worker)
    assert match_job["state"] == "succeeded", match_job
    report = client.get(f"/v1/match-reports/{match_job['result_ref']['id']}", headers=auth(token)).json()
    assert {row["requirement_id"] for row in report["rows"]} == {"req-1", "req-2"}
    assert report["rows"][0]["resume_evidence"][0]["item_id"] == "project-1"

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    plan_job = wait_job(client, token, generate.json()["id"], worker)
    assert plan_job["state"] == "succeeded", plan_job

    generate_prompts = [text for text in captured if "生成" in text and "主问题" in text]
    assert generate_prompts, "应调用真实 adapter 的生成提示词"
    prompt = generate_prompts[0]
    assert "简历结构（anchors.item_id 必须取自这里的 item_id）" in prompt
    assert f"简历快照 ID：{resume_snapshot['id']}" in prompt
    assert f"JD 快照 ID：{jd_snapshot['id']}" in prompt

    plan_row = db.scalars(select(Plan).where(Plan.preparation_id == prep["id"])).one()
    internal = plan_row.internal_payload
    assert internal["model_id"] == "deepseek-stub-model"
    generated = [q for q in internal["questions"] if q["source"] == "generated"]
    assert generated and all(
        anchor["item_id"] == "project-1" and anchor["snapshot_id"] == resume_snapshot["id"]
        for q in generated
        for anchor in q["anchors"]
    )
    public = client.get(f"/v1/plans/{plan_row.id}", headers=auth(token)).json()
    assert public["source_counts"] == {"bank": 1, "generated": 1}
