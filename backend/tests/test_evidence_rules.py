"""C02：证据归属、审核快照绑定、评价依据与解析来源回归。"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.ai.provider import TestProvider
from app.core.errors import AppError
from app.schemas import GeneratedQuestionOutput
from app.services.planning import (
    PlanValidationContext,
    parse_internal_plan,
    validate_internal_plan,
)
from app.services.text import validate_profile
from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    auth,
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


def _validate_mutated(mutate) -> None:
    plan = copy.deepcopy(BASE_PLAN)
    mutate(plan)
    validate_internal_plan(parse_internal_plan(plan), PlanValidationContext(True))


def test_bank_focus_and_evaluation_must_match_reviewed_snapshot() -> None:
    with pytest.raises(AppError) as focus_error:
        _validate_mutated(lambda plan: plan["questions"][0].update(focus="完全不同的考察目标"))
    assert focus_error.value.code == "BANK_INELIGIBLE"

    with pytest.raises(AppError) as evaluation_error:
        _validate_mutated(
            lambda plan: plan["questions"][0]["evaluation"].update(
                reference_approach="替换后的参考思路"
            )
        )
    assert evaluation_error.value.code == "BANK_INELIGIBLE"


def test_generated_rubric_must_match_reviewed_rubric() -> None:
    def mutate(plan: dict) -> None:
        plan["questions"][3]["evaluation"]["rubric"]["version"] = 99

    with pytest.raises(AppError) as caught:
        _validate_mutated(mutate)
    assert caught.value.code == "EVALUATION_INVALID"


def test_generated_knowledge_requires_provided_fact_card(worker) -> None:
    resume = type("Snap", (), {})()
    resume.id = uuid.UUID(BASE_PLAN["material_snapshots"][0]["id"])
    resume.text = BASE_PLAN["material_snapshots"][0]["text"]
    resume.profile = BASE_PLAN["material_snapshots"][0]["profile"]
    jd = type("Snap", (), {})()
    jd.id = uuid.UUID(BASE_PLAN["material_snapshots"][1]["id"])
    jd.text = BASE_PLAN["material_snapshots"][1]["text"]
    jd.profile = BASE_PLAN["material_snapshots"][1]["profile"]

    item = {
        key: copy.deepcopy(value)
        for key, value in BASE_PLAN["questions"][3].items()
        if key not in {"id", "position", "source", "bank_ref"}
    }
    item["question_type"] = "knowledge"
    item["evaluation"]["basis"] = [
        {
            "kind": "material",
            "ref_id": f"{resume.id}/project-1",
            "note": "只有课程材料，没有技术依据卡",
        },
        {"kind": "reasoning", "ref_id": "hypothetical-analysis", "note": "推理"},
    ]
    with pytest.raises(AppError) as caught:
        worker._validate_generated(
            item,
            resume,
            jd,
            allowed_fact_card_ids={"FC-HASHMAP"},
            expected_rubric=item["evaluation"]["rubric"],
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_parse_output_cannot_claim_user_confirmed() -> None:
    profile = copy.deepcopy(REVIEWED_RESUME_PROFILE)
    profile["projects"][0]["evidence"]["origin"] = "user_confirmed"
    with pytest.raises(AppError) as caught:
        validate_profile(
            profile,
            "resume",
            "本科在读，计算机科学与技术专业。\n课程项目：校园图书借阅系统。",
            allow_user_confirmed=False,
            allowed_skill_codes={"java.core", "test.junit"},
        )
    assert caught.value.code == "EVIDENCE_INVALID"


def test_unknown_skill_code_is_rejected() -> None:
    profile = copy.deepcopy(REVIEWED_RESUME_PROFILE)
    profile["projects"][0]["skill_codes"] = ["made.up.code"]
    with pytest.raises(AppError) as caught:
        validate_profile(
            profile,
            "resume",
            "本科在读，计算机科学与技术专业。\n"
            "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；"
            "我参与了借阅模块与边界测试。\n学习过 Java 集合基础和 JUnit 单元测试。",
            allow_user_confirmed=True,
            allowed_skill_codes={"java.core", "test.junit"},
        )
    assert caught.value.code == "PROFILE_INVALID"


def test_jd_only_hypothetical_plan_can_publish_without_project(
    client: TestClient, worker, monkeypatch
) -> None:
    import app.workers.runner as runner

    resume_text = (
        "本科在读，计算机科学与技术专业。\n"
        "学习过 Java 集合基础与 JUnit 单元测试。\n"
        "没有工作经历，也没有项目经历。"
    )
    resume_profile = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术专业。", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java 集合基础与 JUnit",
                "code": "java.core",
                "evidence": {
                    "quote": "学习过 Java 集合基础与 JUnit 单元测试。",
                    "origin": "extracted",
                },
            }
        ],
    }
    jd_text = (
        "内置练习岗位：Java 后端开发实习生。\n"
        "职责：在指导下完成 Java 功能开发并说明实现思路。\n"
        "要求：理解 Java 集合的使用及基本边界。\n"
        "要求：能够为简单功能设计边界测试。"
    )
    jd_profile = {
        "kind": "jd",
        "role_id": "java-backend",
        "level": "intern",
        "title": "Java 后端开发实习生",
        "responsibilities": [
            {
                "item_id": "responsibility-1",
                "text": "职责：在指导下完成 Java 功能开发并说明实现思路。",
                "evidence": {
                    "quote": "职责：在指导下完成 Java 功能开发并说明实现思路。",
                    "origin": "extracted",
                },
            }
        ],
        "requirements": [
            {
                "item_id": "req-1",
                "text": "理解 Java 集合的使用及基本边界",
                "skill_codes": ["java.core"],
                "importance": "must",
                "evidence": {"quote": "要求：理解 Java 集合的使用及基本边界。", "origin": "extracted"},
            },
            {
                "item_id": "req-2",
                "text": "能够为简单功能设计边界测试",
                "skill_codes": ["test.junit"],
                "importance": "must",
                "evidence": {"quote": "要求：能够为简单功能设计边界测试。", "origin": "extracted"},
            },
        ],
    }

    _, token = issue_token(client)
    resume = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "无项目简历", "text": resume_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    resume_snapshot = client.post(
        f"/v1/materials/{resume['id']}/confirm",
        json={"revision": 1, "profile": resume_profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert resume_snapshot.status_code == 201, resume_snapshot.text
    jd = client.post(
        "/v1/materials/text",
        json={"kind": "jd", "title": "自备 JD", "text": jd_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    jd_snapshot = client.post(
        f"/v1/materials/{jd['id']}/confirm",
        json={"revision": 1, "profile": jd_profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert jd_snapshot.status_code == 201, jd_snapshot.text
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_snapshot.json()["id"],
            "jd_snapshot_id": jd_snapshot.json()["id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert prep.status_code == 201, prep.text

    rubric = BANK["questions"][0]["evaluation"]["rubric"]
    requirement = jd_profile["requirements"][0]

    class JdOnlyProvider(TestProvider):
        def generate_questions(
            self,
            *,
            count,
            resume_snapshot,
            jd_snapshot,
            rubric,
            avoid_stems,
            context=None,
        ):
            assert count == 1
            item = {
                "question_type": "experience",
                "canonical_id": f"jd-only-{uuid.uuid4().hex[:8]}",
                "stem": "假设你要仅依据岗位要求实现一个 Java 集合相关功能，你会如何分析边界并验证结论？",
                "focus": "假设场景下的边界与验证",
                "topic_key": "java.core",
                "skill_codes": ["java.core"],
                "anchors": [
                    {
                        "snapshot_id": str(jd_snapshot.id),
                        "item_id": requirement["item_id"],
                        "quote": requirement["evidence"]["quote"],
                    }
                ],
                "evaluation": {
                    "goal": "考察在无项目经历时依据岗位要求分析问题的能力",
                    "reference_approach": "先澄清假设与边界，再给出可验证的步骤，不声称做过项目。",
                    "key_points": [
                        {"point": "明确假设场景", "weight": 0.5},
                        {"point": "给出可检验的验证步骤", "weight": 0.5},
                    ],
                    "acceptable_alternatives": ["前提明确且可验证的等价分析"],
                    "common_mistakes": ["把假设说成真实项目经历"],
                    "rubric": rubric,
                    "basis": [
                        {
                            "kind": "material",
                            "ref_id": f"{jd_snapshot.id}/{requirement['item_id']}",
                            "note": "仅依据岗位要求构造假设场景",
                        },
                        {
                            "kind": "reasoning",
                            "ref_id": "hypothetical-analysis",
                            "note": "评价分析过程",
                        },
                    ],
                    "answer_status": "model_generated",
                    "trust": "provisional",
                    "uncertainties": [],
                },
            }
            return [GeneratedQuestionOutput.model_validate(item).model_dump(mode="json")]

    monkeypatch.setattr(runner, "get_provider", lambda _: JdOnlyProvider())
    match = client.post(
        f"/v1/preparations/{prep.json()['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert match.status_code == 202, match.text
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    generate = client.post(
        f"/v1/preparations/{prep.json()['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert generate.status_code == 202, generate.text
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job
    plan = client.get(f"/v1/plans/{job['result_ref']['id']}", headers=auth(token)).json()
    assert plan["source_counts"] == {"bank": 1, "generated": 1}

    from app.core.db import SessionLocal
    from app.models import Plan

    with SessionLocal() as db:
        row = db.get(Plan, uuid.UUID(job["result_ref"]["id"]))
        generated = [
            q for q in row.internal_payload["questions"] if q["source"] == "generated"
        ]
        assert generated
        assert generated[0]["question_type"] == "experience"
        assert str(generated[0]["anchors"][0]["snapshot_id"]) == jd_snapshot.json()["id"]
        assert generated[0]["anchors"][0]["item_id"] == "req-1"


def test_match_misattributed_quote_is_rejected_while_id_selection_succeeds() -> None:
    import httpx
    from pathlib import Path
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.evidence import profile_items, validate_anchor, validate_match_rows

    resume_text = (
        "技能：熟悉 Java 集合与并发基础。\n"
        "项目：校园图书借阅系统，使用 ArrayList 保存借阅记录。\n"
        "其他课程笔记：曾用 HashMap 做过词频统计。"
    )
    resume = type("Snap", (), {})()
    resume.id = uuid.UUID("00000000-0000-4000-8000-000000000011")
    resume.text = resume_text
    resume.profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [],
        "projects": [
            {
                "item_id": "project-1",
                "name": "校园图书借阅系统",
                "description": "使用 ArrayList 保存借阅记录",
                "skill_codes": ["java.core"],
                "evidence": {
                    "quote": "项目：校园图书借阅系统，使用 ArrayList 保存借阅记录。",
                    "origin": "extracted",
                },
            }
        ],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java 集合与并发",
                "code": "java.core",
                "evidence": {"quote": "技能：熟悉 Java 集合与并发基础。", "origin": "extracted"},
            }
        ],
    }
    jd = type("Snap", (), {})()
    jd.id = uuid.UUID("00000000-0000-4000-8000-000000000012")
    jd.text = "要求：掌握 Java 集合。"
    jd.profile = {
        "kind": "jd",
        "role_id": "java-backend",
        "level": "intern",
        "title": "Java 实习",
        "responsibilities": [
            {
                "item_id": "resp-1",
                "text": "完成模块开发",
                "evidence": {"quote": "要求：掌握 Java 集合。", "origin": "extracted"},
            }
        ],
        "requirements": [
            {
                "item_id": "req-1",
                "text": "掌握 Java 集合",
                "skill_codes": ["java.core"],
                "importance": "must",
                "evidence": {"quote": "要求：掌握 Java 集合。", "origin": "extracted"},
            }
        ],
    }

    # 某句文字只出现在整份原文的其它位置：原 validate_anchor 仍拒绝把它挂到 skill-1。
    with pytest.raises(AppError) as caught:
        validate_anchor(
            {
                "snapshot_id": str(resume.id),
                "item_id": "skill-1",
                "quote": "其他课程笔记：曾用 HashMap 做过词频统计。",
            },
            snapshots={str(resume.id): resume},
            items_by_snapshot={str(resume.id): profile_items(resume.profile)},
            error_code="AI_INVALID_OUTPUT",
        )
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert "引用原文不属于所引用的材料条目" in caught.value.message

    # 模拟 DeepSeek 仅返回合适的条目 ID，后端按条目已保存原文生成 Anchor 并通过校验。
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-stub",
        AI_API_KEY="stub-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": "req-synth",
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    {
                                        "rows": [
                                            {
                                                "requirement_id": "req-1",
                                                "status": "supported",
                                                "resume_item_ids": ["skill-1"],
                                                "explanation": "技能条目直接体现 Java 集合基础。",
                                                "suggested_focus": "说明集合底层结构与边界。",
                                            }
                                        ]
                                    },
                                    ensure_ascii=False,
                                )
                            }
                        }
                    ],
                },
            )
        ),
    )
    rows = provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
    validated = validate_match_rows(rows, resume, jd)
    assert validated[0]["resume_evidence"] == [
        {
            "snapshot_id": str(resume.id),
            "item_id": "skill-1",
            "quote": "技能：熟悉 Java 集合与并发基础。",
        }
    ]
