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
            assert '"resume_item_ids"' in user
            assert "anchors.item_id" not in user
            assert "可引用简历候选表" in user
            assert "project-1" in user
            return _chat(
                {
                    "rows": [
                        {
                            "requirement_id": "req-1",
                            "status": "supported",
                            "resume_item_ids": ["project-1"],
                            "explanation": "项目原文直接涉及 Java 集合的使用。",
                            "suggested_focus": "说明集合选择与边界验证",
                        },
                        {
                            "requirement_id": "req-2",
                            "status": "to_verify",
                            "resume_item_ids": [],
                            "explanation": "简历提到 JUnit 学习，但没有直接对应边界测试要求。",
                            "suggested_focus": "补充可核对的测试事实",
                        },
                    ]
                }
            )

        if "生成" in user and "主问题" in user:
            count = int(re.search(r"生成 (\d+) 道", user).group(1))
            slot_match = re.search(r"本次只为槽位 (generated-\d+)", user)
            slot_key = slot_match.group(1) if slot_match else "generated-1"
            slot_type = "knowledge" if "槽位类型固定为 knowledge" in user else "experience"
            anchors_block = user.split("允许锚点（anchors 必须原样取自这里）：", 1)[1]
            anchors_block = anchors_block.split("\n\n可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[0].strip()
            anchors = json.loads(anchors_block)
            cards_block = user.split("可用依据卡（knowledge 题必须从中引用 fact_card）：", 1)[1]
            cards_block = cards_block.split("\n\n已有题干（禁止复用或同义改写）：", 1)[0].strip()
            fact_cards = json.loads(cards_block)
            anchor = anchors[0]
            resume_snapshot = anchor["snapshot_id"]
            item_id = anchor["item_id"]
            quote = anchor["quote"]
            questions = []
            for index in range(count):
                basis = [
                    {
                        "kind": "material",
                        "ref_id": f"{resume_snapshot}/{item_id}",
                        "note": "以已确认材料原文为事实边界",
                    },
                    {
                        "kind": "reasoning",
                        "ref_id": "hypothetical-analysis",
                        "note": "只评价思路",
                    },
                ]
                if slot_type == "knowledge" and fact_cards:
                    basis.insert(
                        0,
                        {
                            "kind": "fact_card",
                            "ref_id": fact_cards[0]["id"],
                            "note": "引用本次提供的已审依据卡",
                        },
                    )
                stem = (
                    f"在槽位 {slot_key} 中针对知识卡原理，说明为什么不能把单次局部结果直接外推为全局结论，并给出可验证边界？"
                    if slot_type == "knowledge"
                    else f"结合你材料在槽位 {slot_key} 第{index + 1}段经历，如果在高并发或边界输入下满足岗位要求，你会先核实什么前提并如何测试？"
                )
                if slot_key == "generated-3":
                    stem = "请结合图书借阅系统的检索流程，当数据规模增长十倍时，你会如何定位瓶颈并设计对照实验验证优化前后差异？"
                questions.append(
                    {
                        "question_type": slot_type,
                        "canonical_id": f"stub-{slot_key}-{index + 1}",
                        "stem": stem,
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
                            "basis": basis,
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
    assert report["rows"][0]["resume_evidence"] == [
        {
            "snapshot_id": resume_snapshot["id"],
            "item_id": "project-1",
            "quote": PROJECT_QUOTE,
        }
    ]
    match_prompts = [text for text in captured if "逐条对照 JD 要求" in text]
    assert match_prompts and '"resume_item_ids"' in match_prompts[0]

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
    assert "允许锚点（anchors 必须原样取自这里）" in prompt
    assert f'"resume_snapshot_id": "{resume_snapshot["id"]}"' in prompt
    assert f'"jd_snapshot_id": "{jd_snapshot["id"]}"' in prompt
    assert '"uncertainties": []' in prompt

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


def test_deepseek_match_invalid_selection_publishes_no_report(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import app.workers.runner as runner
    from app.models import MatchReport, Preparation

    def bad_match_handler(request: httpx.Request) -> httpx.Response:
        return _chat(
            {
                "rows": [
                    {
                        "requirement_id": "req-1",
                        "status": "supported",
                        "resume_item_ids": ["non-existent-item"],
                        "explanation": "引用了不存在的条目 ID",
                        "suggested_focus": "核实",
                    },
                    {
                        "requirement_id": "req-2",
                        "status": "not_evidenced",
                        "resume_item_ids": [],
                        "explanation": "未体现",
                        "suggested_focus": "补充",
                    },
                ]
            }
        )

    provider = DeepSeekProvider(SETTINGS, transport=httpx.MockTransport(bad_match_handler))
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    _, token = issue_token(client)
    resume = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "异常匹配简历", "text": REVIEWED_RESUME_TEXT},
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
    failed_job = wait_job(client, token, match.json()["id"], worker)
    assert failed_job["state"] == "failed"
    assert failed_job["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "stage=match" in failed_job["error"]["details"]
    assert "reason=invalid_evidence_selection" in failed_job["error"]["details"]
    assert failed_job["result_ref"] is None
    assert failed_job["retryable"] is True

    prep_row = db.get(Preparation, prep["id"])
    assert prep_row is not None and prep_row.match_report_id is None
    reports = db.scalars(
        select(MatchReport).where(MatchReport.preparation_id == prep_row.id)
    ).all()
    assert reports == []


def test_deepseek_six_question_plan_quota_request_options_and_confirm(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import app.workers.runner as runner

    captured: list[str] = []
    request_bodies: list[dict] = []
    base_handler = _stub_handler(captured)

    def recording_handler(request: httpx.Request) -> httpx.Response:
        request_bodies.append(json.loads(request.content))
        return base_handler(request)

    provider = DeepSeekProvider(SETTINGS, transport=httpx.MockTransport(recording_handler))
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    _, token = issue_token(client)
    resume = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "六题合成简历", "text": REVIEWED_RESUME_TEXT},
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
            "main_question_count": 6,
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
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    plan_job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert plan_job["state"] == "succeeded", plan_job

    # 请求选项范围：非思考配置与输出预算只进入出题路径，不进入匹配与语义复核
    gen_bodies = [b for b in request_bodies if "本次只为槽位" in b["messages"][-1]["content"]]
    non_gen_bodies = [b for b in request_bodies if "本次只为槽位" not in b["messages"][-1]["content"]]
    assert len(gen_bodies) == 3
    assert all(b.get("thinking") == {"type": "disabled"} and b.get("max_tokens") == 4096 for b in gen_bodies)
    assert non_gen_bodies and all("thinking" not in b and "max_tokens" not in b for b in non_gen_bodies)

    plan_id = plan_job["result_ref"]["id"]
    public = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
    assert public["main_question_count"] == 6
    assert public["source_counts"] == {"bank": 3, "generated": 3}

    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 200
    assert confirm.json()["state"] == "confirmed"


def test_deepseek_match_rejects_free_text_quote_and_keeps_verified_evidence() -> None:
    import types

    resume_text = "技能：熟悉 Java 集合基础。"
    resume = types.SimpleNamespace(
        id="00000000-0000-4000-8000-000000000001",
        text=resume_text,
        profile={
            "kind": "resume",
            "education": [],
            "work_experience": [],
            "projects": [],
            "skills": [
                {
                    "item_id": "skill-1",
                    "name": "Java 集合",
                    "code": "java.core",
                    "evidence": {"quote": "技能：熟悉 Java 集合基础。", "origin": "extracted"},
                }
            ],
        },
    )
    jd = types.SimpleNamespace(
        id="00000000-0000-4000-8000-000000000002",
        text="JD",
        profile={"kind": "jd", "requirements": [{"item_id": "req-1", "text": "理解 Java 集合"}]},
    )

    # 旧版错误自由引文形式必须被拒绝
    legacy_bad_provider = DeepSeekProvider(
        SETTINGS,
        transport=httpx.MockTransport(
            lambda _: _chat(
                {
                    "rows": [
                        {
                            "requirement_id": "req-1",
                            "status": "supported",
                            "resume_evidence": [
                                {
                                    "snapshot_id": "00000000-0000-4000-8000-000000000001",
                                    "item_id": "skill-1",
                                    "quote": "模型自由拼写的错误引文",
                                }
                            ],
                            "explanation": "说明",
                            "suggested_focus": "建议",
                        }
                    ]
                }
            )
        ),
    )
    try:
        legacy_bad_provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
        raise AssertionError("应拒绝模型自由文本 resume_evidence")
    except Exception as exc:
        assert getattr(exc, "code", None) == "AI_INVALID_OUTPUT"

    # 正常选择 item_id 时由服务端已确认快照还原准确引文
    good_provider = DeepSeekProvider(
        SETTINGS,
        transport=httpx.MockTransport(
            lambda _: _chat(
                {
                    "rows": [
                        {
                            "requirement_id": "req-1",
                            "status": "supported",
                            "resume_item_ids": ["skill-1"],
                            "explanation": "技能条目说明熟悉 Java 集合基础。",
                            "suggested_focus": "说明扩容和哈希冲突边界。",
                        }
                    ]
                }
            )
        ),
    )
    rows = good_provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
    assert rows[0]["resume_evidence"] == [
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000001",
            "item_id": "skill-1",
            "quote": "技能：熟悉 Java 集合基础。",
        }
    ]
