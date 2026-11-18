from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ai.provider import DeepSeekProvider, get_provider
from app.core.config import Settings
from app.core.errors import AppError


def make_settings(**overrides) -> Settings:
    values = dict(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-test-model",
        AI_API_KEY="test-key-not-real",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=False,
    )
    values.update(overrides)
    return Settings(**values)


def provider_with(handler) -> DeepSeekProvider:
    return DeepSeekProvider(make_settings(), transport=httpx.MockTransport(handler))


RESUME_PROFILE = {
    "kind": "resume",
    "education": [
        {
            "item_id": "edu-1",
            "degree": "本科在读",
            "major": "计算机科学与技术",
            "evidence": {"quote": "本科在读", "origin": "extracted"},
        }
    ],
    "work_experience": [],
    "projects": [],
    "skills": [],
}


def chat_response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"id": "req-test", "choices": [{"message": {"content": content}}]},
    )


def test_parse_material_happy_path_sends_expected_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return chat_response(json.dumps(RESUME_PROFILE, ensure_ascii=False))

    provider = provider_with(handler)
    profile = provider.parse_material(
        text="本科在读",
        kind="resume",
        role_id="java-backend",
        level="intern",
        skill_catalog=[{"code": "java.core", "label": "Java 基础"}],
    )
    assert profile["kind"] == "resume"
    assert captured["path"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key-not-real"
    assert captured["body"]["model"] == "deepseek-test-model"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    user_prompt = captured["body"]["messages"][-1]["content"]
    assert "本科在读" in user_prompt
    assert "java.core" in user_prompt
    assert "Java 基础" in user_prompt
    assert "用于从词表中关联技能" in user_prompt
    assert "java.concurrency" not in user_prompt


def test_parse_material_empty_catalog_forbids_invented_codes() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return chat_response(json.dumps(RESUME_PROFILE, ensure_ascii=False))

    provider = provider_with(handler)
    provider.parse_material(
        text="本科在读",
        kind="resume",
        role_id="java-backend",
        level="intern",
        skill_catalog=[],
    )
    user_prompt = captured["body"]["messages"][-1]["content"]
    assert "当前可用技能词表为空" in user_prompt
    assert "java.core" not in user_prompt


def test_schema_mismatch_is_ai_invalid_output() -> None:
    provider = provider_with(lambda request: chat_response(json.dumps({"kind": "resume", "education": [{"item_id": "x"}]})))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_non_json_content_is_ai_invalid_output() -> None:
    provider = provider_with(lambda request: chat_response("这不是 JSON"))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_http_500_is_ai_unavailable() -> None:
    provider = provider_with(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_UNAVAILABLE"


def test_timeout_is_ai_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = provider_with(handler)
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_TIMEOUT"


def test_match_rows_are_schema_validated() -> None:
    provider = provider_with(
        lambda request: chat_response(json.dumps({"rows": [{"requirement_id": "req-1"}]}))
    )
    import types

    resume = types.SimpleNamespace(id="00000000-0000-4000-8000-000000000001", text="简历", profile={"kind": "resume"})
    jd = types.SimpleNamespace(id="00000000-0000-4000-8000-000000000002", text="JD", profile={"kind": "jd"})
    with pytest.raises(AppError) as caught:
        provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert provider.last_call["outcome"] == "known_invalid"


def test_match_selection_builds_anchors_and_rejects_invalid_selections() -> None:
    import types

    resume_text = (
        "本科在读，计算机科学与技术。\n"
        "技能：熟悉 Java 集合基础。\n"
        "补充说明：了解 JUnit 单元测试框架。\n"
        "额外经历：在课程实验中使用过 HashMap 统计词频。"
    )
    resume = types.SimpleNamespace(
        id="00000000-0000-4000-8000-000000000001",
        text=resume_text,
        profile={
            "kind": "resume",
            "education": [],
            "work_experience": [],
            "projects": [
                {
                    "item_id": "project-1",
                    "name": "词频实验",
                    "description": "HashMap 统计",
                    "skill_codes": ["java.core"],
                    "evidence": {
                        "quote": "额外经历：在课程实验中使用过 HashMap 统计词频。",
                        "origin": "extracted",
                    },
                }
            ],
            "skills": [
                {
                    "item_id": "skill-1",
                    "name": "Java 集合",
                    "code": "java.core",
                    "evidence": {"quote": "技能：熟悉 Java 集合基础。", "origin": "extracted"},
                },
                {
                    "item_id": "skill-unquotable",
                    "name": "不存在原文",
                    "code": None,
                    "evidence": {"quote": "这段话不在简历原文里", "origin": "extracted"},
                },
            ],
        },
    )
    jd = types.SimpleNamespace(
        id="00000000-0000-4000-8000-000000000002",
        text="JD",
        profile={
            "kind": "jd",
            "requirements": [
                {"item_id": "req-1", "text": "理解 Java 集合"},
                {"item_id": "req-2", "text": "了解 JUnit 测试"},
            ],
        },
    )

    captured_prompt: dict = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        captured_prompt["user"] = json.loads(request.content)["messages"][-1]["content"]
        return chat_response(
            json.dumps(
                {
                    "rows": [
                        {
                            "requirement_id": "req-1",
                            "status": "supported",
                            "resume_item_ids": ["skill-1"],
                            "explanation": "技能条目直接体现 Java 集合基础。",
                            "suggested_focus": "说明集合选型与边界。",
                        },
                        {
                            "requirement_id": "req-2",
                            "status": "not_evidenced",
                            "resume_item_ids": [],
                            "explanation": "未找到对应的测试经历。",
                            "suggested_focus": "补充边界测试案例。",
                        },
                    ]
                },
                ensure_ascii=False,
            )
        )

    provider = provider_with(ok_handler)
    rows = provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
    assert '"resume_item_ids"' in captured_prompt["user"]
    assert "anchors.item_id" not in captured_prompt["user"]
    assert "skill-unquotable" not in captured_prompt["user"]
    assert rows[0]["resume_evidence"] == [
        {
            "snapshot_id": "00000000-0000-4000-8000-000000000001",
            "item_id": "skill-1",
            "quote": "技能：熟悉 Java 集合基础。",
        }
    ]
    assert rows[1]["resume_evidence"] == []

    bad_payloads = [
        # 不存在的条目 ID
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": ["skill-999"],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # 不可引用条目（quote 不在原文中）
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": ["skill-unquotable"],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # 过去的自由引文字段
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_evidence": [
                        {
                            "snapshot_id": "00000000-0000-4000-8000-000000000001",
                            "item_id": "skill-1",
                            "quote": "技能：熟悉 Java 集合基础。",
                        }
                    ],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # 重复证据 ID
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": ["skill-1", "skill-1"],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # 缺失 JD 要求
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": ["skill-1"],
                    "explanation": "x",
                    "suggested_focus": "y",
                }
            ]
        },
        # 重复 JD 要求
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": ["skill-1"],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-1",
                    "status": "to_verify",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # supported 无证据
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "supported",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
        # not_evidenced 附带证据
        {
            "rows": [
                {
                    "requirement_id": "req-1",
                    "status": "not_evidenced",
                    "resume_item_ids": ["skill-1"],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
                {
                    "requirement_id": "req-2",
                    "status": "not_evidenced",
                    "resume_item_ids": [],
                    "explanation": "x",
                    "suggested_focus": "y",
                },
            ]
        },
    ]

    for payload in bad_payloads:
        bad_provider = provider_with(
            lambda request, p=payload: chat_response(json.dumps(p, ensure_ascii=False))
        )
        with pytest.raises(AppError) as caught:
            bad_provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
        assert caught.value.code == "AI_INVALID_OUTPUT", payload
        assert "stage=match" in caught.value.details
        assert "reason=invalid_evidence_selection" in caught.value.details
        assert bad_provider.last_call["outcome"] == "known_invalid"


def test_duplicate_review_output_is_validated() -> None:
    good = {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
    provider = provider_with(lambda request: chat_response(json.dumps(good)))
    result = provider.review_questions(questions=[], material_texts=[])
    assert result == good

    bad = provider_with(lambda request: chat_response(json.dumps({"duplicate_pairs": []})))
    with pytest.raises(AppError) as caught:
        bad.review_questions(questions=[], material_texts=[])
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_deepseek_without_credentials_is_not_configured() -> None:
    settings = make_settings(AI_API_KEY="", AI_MODEL="")
    with pytest.raises(AppError) as caught:
        get_provider(settings)
    assert caught.value.code == "AI_NOT_CONFIGURED"


def test_disabled_provider_is_not_configured() -> None:
    with pytest.raises(AppError) as caught:
        get_provider(make_settings(AI_PROVIDER="disabled"))
    assert caught.value.code == "AI_NOT_CONFIGURED"


def _sample_snapshots_and_rubric():
    import types

    resume_id = "00000000-0000-4000-8000-000000000001"
    jd_id = "00000000-0000-4000-8000-000000000002"
    resume = types.SimpleNamespace(
        id=resume_id,
        text="课程项目：校园图书借阅系统，使用 ArrayList 和 HashMap。",
        profile={
            "kind": "resume",
            "education": [],
            "work_experience": [],
            "projects": [
                {
                    "item_id": "project-1",
                    "name": "校园图书借阅系统",
                    "description": "使用 ArrayList 和 HashMap",
                    "skill_codes": ["java.core"],
                    "evidence": {
                        "quote": "课程项目：校园图书借阅系统，使用 ArrayList 和 HashMap。",
                        "origin": "extracted",
                    },
                }
            ],
            "skills": [],
        },
    )
    jd = types.SimpleNamespace(
        id=jd_id,
        text="岗位要求：熟悉 Java 集合与单元测试。",
        profile={
            "kind": "jd",
            "role_id": "java-backend",
            "level": "intern",
            "title": "Java 实习生",
            "responsibilities": [],
            "requirements": [
                {
                    "item_id": "req-1",
                    "text": "熟悉 Java 集合",
                    "skill_codes": ["java.core"],
                    "importance": "must",
                    "evidence": {"quote": "熟悉 Java 集合", "origin": "extracted"},
                }
            ],
        },
    )
    rubric = {
        "id": "rubric-v1",
        "version": 1,
        "dimensions": [
            {
                "id": "core",
                "name": "核心理解",
                "weight": 1.0,
                "low_anchor": "缺少依据",
                "high_anchor": "准确且有验证",
            }
        ],
    }
    return resume, jd, rubric


def test_chat_json_finish_reason_length_prioritized_and_formats_classified() -> None:
    resume, jd, rubric = _sample_snapshots_and_rubric()

    # 即使返回片段碰巧是合法 JSON，finish_reason=length 仍优先判为 output_truncated
    truncated_provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "id": "req-trunc",
                "usage": {"completion_tokens": 4096},
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"questions": []}'},
                    }
                ],
            },
        )
    )
    with pytest.raises(AppError) as caught:
        truncated_provider.generate_questions(
            count=1,
            resume_snapshot=resume,
            jd_snapshot=jd,
            rubric=rubric,
            avoid_stems=[],
            context={"slot_key": "generated-1", "question_type": "experience"},
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert "reason=output_truncated" in caught.value.details
    assert "finish_reason=length" in caught.value.details
    assert truncated_provider.last_call["finish_reason"] == "length"
    assert truncated_provider.last_call["outcome"] == "known_invalid"

    # content_filter 不当作格式错误自动纠正，映射为 AI_REQUEST_REJECTED
    filtered_provider = provider_with(
        lambda request: httpx.Response(
            200,
            json={
                "id": "req-filter",
                "choices": [{"finish_reason": "content_filter", "message": {"content": ""}}],
            },
        )
    )
    with pytest.raises(AppError) as caught_filter:
        filtered_provider.generate_questions(
            count=1,
            resume_snapshot=resume,
            jd_snapshot=jd,
            rubric=rubric,
            avoid_stems=[],
            context={"slot_key": "generated-1", "question_type": "experience"},
        )
    assert caught_filter.value.code == "AI_REQUEST_REJECTED"
    assert filtered_provider.last_call["finish_reason"] == "content_filter"
    assert filtered_provider.last_call["outcome"] == "request_rejected"

    # stop 下的空白、非法 JSON、非对象 JSON 与题目字段错误分类清楚
    cases = [
        ("", "stop", "reason=empty_content", "finish_reason=stop"),
        ("   ", None, "reason=empty_content", "finish_reason=unknown"),
        ("{bad json", "stop", "reason=invalid_json", "finish_reason=stop"),
        ("[1, 2, 3]", "stop", "reason=non_object_json", "finish_reason=stop"),
        (
            json.dumps({"questions": [{"question_type": "experience"}]}),
            "stop",
            "reason=invalid_question_schema",
            "finish_reason=stop",
        ),
    ]
    for content, finish_reason, expected_reason, expected_finish in cases:
        choice: dict = {"message": {"content": content}}
        if finish_reason is not None:
            choice["finish_reason"] = finish_reason
        p = provider_with(
            lambda request, ch=choice: httpx.Response(
                200, json={"id": "req-fmt", "choices": [ch]}
            )
        )
        with pytest.raises(AppError) as err:
            p.generate_questions(
                count=1,
                resume_snapshot=resume,
                jd_snapshot=jd,
                rubric=rubric,
                avoid_stems=[],
                context={"slot_key": "generated-1", "question_type": "experience"},
            )
        assert err.value.code == "AI_INVALID_OUTPUT"
        assert expected_reason in err.value.details
        assert expected_finish in err.value.details


def test_plan_request_options_scope_and_compatible_fallback() -> None:
    resume, jd, rubric = _sample_snapshots_and_rubric()
    bodies: list[dict] = []

    valid_question = {
        "question_type": "experience",
        "canonical_id": "c-1",
        "stem": "请结合图书借阅系统说明为什么选择 HashMap，以及如何验证边界？",
        "focus": "集合边界",
        "topic_key": "java.core",
        "skill_codes": ["java.core"],
        "anchors": [
            {
                "snapshot_id": str(resume.id),
                "item_id": "project-1",
                "quote": "课程项目：校园图书借阅系统，使用 ArrayList 和 HashMap。",
            }
        ],
        "evaluation": {
            "goal": "考察集合选型与验证",
            "reference_approach": "澄清需求后比较结构并给出验证用例。",
            "key_points": [
                {"point": "说明查找复杂度与冲突", "weight": 0.6},
                {"point": "给出边界测试用例", "weight": 0.4},
            ],
            "acceptable_alternatives": ["说明等价实现"],
            "common_mistakes": ["忽略 null 或并发边界"],
            "rubric": rubric,
            "basis": [
                {
                    "kind": "material",
                    "ref_id": f"{resume.id}/project-1",
                    "note": "依据简历项目",
                }
            ],
            "answer_status": "model_generated",
            "trust": "provisional",
            "uncertainties": [],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        bodies.append(body)
        user_msg = body["messages"][-1]["content"]
        if "本次只为槽位" in user_msg:
            return chat_response(json.dumps({"questions": [valid_question]}, ensure_ascii=False))
        return chat_response(
            json.dumps({"duplicate_pairs": [], "grounding_issues": [], "uncertain": False})
        )

    provider = provider_with(handler)
    provider.generate_questions(
        count=1,
        resume_snapshot=resume,
        jd_snapshot=jd,
        rubric=rubric,
        avoid_stems=[],
        context={"slot_key": "generated-1", "question_type": "experience"},
    )
    provider.generate_questions(
        count=1,
        resume_snapshot=resume,
        jd_snapshot=jd,
        rubric=rubric,
        avoid_stems=[],
        context={
            "slot_key": "generated-1",
            "question_type": "experience",
            "correction": {
                "attempt": 2,
                "message": "AI 输出因达到长度上限而未完整结束",
                "details": ["reason=output_truncated", "finish_reason=length"],
            },
        },
    )
    provider.review_questions(questions=[], material_texts=[])

    assert bodies[0]["thinking"] == {"type": "disabled"}
    assert bodies[0]["max_tokens"] == 4096
    assert bodies[1]["thinking"] == {"type": "disabled"}
    assert bodies[1]["max_tokens"] == 8192
    assert "thinking" not in bodies[2]
    assert "max_tokens" not in bodies[2]

    # 配置回退：兼容端点可显式关闭 thinking 和 max_tokens
    compat_bodies: list[dict] = []
    compat_settings = make_settings(
        AI_PLAN_GENERATE_THINKING="omit",
        AI_PLAN_GENERATE_MAX_TOKENS="",
    )
    compat_provider = DeepSeekProvider(
        compat_settings,
        transport=httpx.MockTransport(
            lambda request: (
                compat_bodies.append(json.loads(request.content)),
                chat_response(json.dumps({"questions": [valid_question]}, ensure_ascii=False)),
            )[1]
        ),
    )
    compat_provider.generate_questions(
        count=1,
        resume_snapshot=resume,
        jd_snapshot=jd,
        rubric=rubric,
        avoid_stems=[],
        context={"slot_key": "generated-1", "question_type": "experience"},
    )
    assert "thinking" not in compat_bodies[0]
    assert "max_tokens" not in compat_bodies[0]

    # 兼容端点若 400 拒绝 thinking 参数，自动回退重发不带 thinking 的请求
    fallback_bodies: list[dict] = []

    def fallback_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        fallback_bodies.append(body)
        if "thinking" in body:
            return httpx.Response(400, text='{"error":{"message":"unknown parameter: thinking"}}')
        return chat_response(json.dumps({"questions": [valid_question]}, ensure_ascii=False))

    fb_provider = provider_with(fallback_handler)
    fb_provider.generate_questions(
        count=1,
        resume_snapshot=resume,
        jd_snapshot=jd,
        rubric=rubric,
        avoid_stems=[],
        context={"slot_key": "generated-1", "question_type": "experience"},
    )
    assert len(fallback_bodies) == 2
    assert "thinking" in fallback_bodies[0]
    assert "thinking" not in fallback_bodies[1]


def test_generate_prompt_example_consistency_and_deduped_sections() -> None:
    from app.ai.prompts import generate_prompt

    resume, jd, rubric = _sample_snapshots_and_rubric()
    anchors = [
        {
            "snapshot_id": str(resume.id),
            "item_id": "project-1",
            "quote": "课程项目：校园图书借阅系统，使用 ArrayList 和 HashMap。",
            "source_kind": "resume",
            "label": "校园图书借阅系统",
        }
    ]
    fact_cards = [{"id": "fc-java-hashmap", "version": 1, "summary": "HashMap 扩容与哈希冲突"}]
    avoid = ["已存在题干示例 A"]

    _, exp_user = generate_prompt(
        count=1,
        resume_text=resume.text,
        resume_profile=resume.profile,
        resume_snapshot_id=str(resume.id),
        jd_text=jd.text,
        jd_snapshot_id=str(jd.id),
        jd_profile=jd.profile,
        rubric=rubric,
        avoid_stems=avoid,
        context={
            "slot_key": "generated-1",
            "slot_index": 1,
            "question_type": "experience",
            "allowed_anchors": anchors,
            "fact_cards": fact_cards,
            "rubric": rubric,
            "avoid_stems": avoid,
        },
    )
    assert exp_user.count("已存在题干示例 A") == 1
    assert exp_user.count('"rubric-v1"') == 1
    assert "槽位上下文（" not in exp_user
    assert '"uncertainties": []' in exp_user
    assert f'"ref_id": "{resume.id}/project-1"' in exp_user
    assert "java-backend" in exp_user and "intern" in exp_user

    _, know_user = generate_prompt(
        count=1,
        resume_text=resume.text,
        resume_profile=resume.profile,
        resume_snapshot_id=str(resume.id),
        jd_text=jd.text,
        jd_snapshot_id=str(jd.id),
        jd_profile=jd.profile,
        rubric=rubric,
        avoid_stems=avoid,
        context={
            "slot_key": "generated-2",
            "slot_index": 2,
            "question_type": "knowledge",
            "allowed_anchors": anchors,
            "fact_cards": fact_cards,
            "rubric": rubric,
            "avoid_stems": avoid,
        },
    )
    assert '"uncertainties": []' in know_user
    assert '"kind": "fact_card"' in know_user
    assert '"ref_id": "fc-java-hashmap"' in know_user
    assert f'"ref_id": "{resume.id}/project-1"' in know_user
