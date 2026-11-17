"""C04：匹配/材料驱动题库选题、锚点、生成重点与 JD 级别冲突。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import Plan
from app.services.bank import EligibleBankSource, select_bank_sources
from app.services.text import detect_jd_level_conflict
from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    REVIEWED_RESUME_TEXT,
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    new_key,
    wait_job,
)


def _source(question_id: str, codes: list[str]) -> EligibleBankSource:
    question = SimpleNamespace(question_id=question_id, version=1)
    content = {
        "canonical_id": f"canonical-{question_id}",
        "stem": f"{question_id} 的题干内容，考察实际工程判断与边界。",
        "skill_codes": codes,
    }
    return EligibleBankSource(
        question=question,
        content=content,
        reviews=[],
        reviewer_ids=["A", "B"],
        test_only=True,
    )


def _resume() -> SimpleNamespace:
    return SimpleNamespace(
        profile={
            "kind": "resume",
            "projects": [{"item_id": "project-1", "skill_codes": ["java.core"]}],
            "skills": [],
        }
    )


def _jd(*requirements: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        profile={
            "kind": "jd",
            "requirements": [
                {
                    "item_id": requirement_id,
                    "skill_codes": [code],
                    "importance": "must",
                }
                for requirement_id, code in requirements
            ],
        }
    )


def test_bank_selection_uses_jd_and_resume_skill_overlap() -> None:
    resume = _resume()
    jd = _jd(("req-concurrency", "java.concurrency"), ("req-core", "java.core"))
    selected = select_bank_sources(
        [
            _source("q-core", ["java.core"]),
            _source("q-both", ["java.concurrency", "java.core"]),
            _source("q-concurrency", ["java.concurrency"]),
            _source("q-unrelated", ["product.management"]),
        ],
        2,
        resume=resume,
        jd=jd,
    )
    assert [item.question.question_id for item in selected] == ["q-both", "q-core"]
    assert selected[0].jd_requirement_id == "req-concurrency"
    assert selected[1].jd_requirement_id == "req-core"

    jd_core_only = _jd(("req-core", "java.core"))
    selected_core = select_bank_sources(
        [
            _source("q-core", ["java.core"]),
            _source("q-concurrency", ["java.concurrency"]),
        ],
        1,
        resume=resume,
        jd=jd_core_only,
    )
    assert [item.question.question_id for item in selected_core] == ["q-core"]


def test_bank_without_material_relation_is_insufficient() -> None:
    with pytest.raises(AppError) as caught:
        select_bank_sources(
            [_source("q-unrelated", ["product.management"])],
            1,
            resume=_resume(),
            jd=_jd(("req-core", "java.core")),
        )
    assert caught.value.code == "BANK_INSUFFICIENT"


def test_bank_plan_anchors_bind_real_requirement(
    client: TestClient, worker
) -> None:
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
    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job

    with SessionLocal() as db:
        plan = db.get(Plan, uuid.UUID(job["result_ref"]["id"]))
        bank_questions = [
            q for q in plan.internal_payload["questions"] if q["source"] == "bank"
        ]
        assert bank_questions
        for question in bank_questions:
            assert question["anchors"][0]["item_id"] == "req-1"
            assert question["skill_codes"] == ["java.core"]


def test_senior_jd_yields_conflict_not_intern_plan(
    client: TestClient, worker
) -> None:
    _, token = issue_token(client)
    text = (
        "资深 Java 后端工程师\n"
        "职责：负责核心交易系统架构与团队技术方案。\n"
        "要求：5年以上 Java 后端经验，精通并发与性能调优。"
    )
    material = client.post(
        "/v1/materials/text",
        json={"kind": "jd", "title": "资深 JD", "text": text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert material.status_code == 201, material.text
    parse = client.post(
        f"/v1/materials/{material.json()['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert parse.status_code == 202, parse.text
    job = wait_job(client, token, parse.json()["id"], worker, attempts=3)
    assert job["state"] == "failed", job
    assert job["error"]["code"] == "JD_LEVEL_CONFLICT"
    assert detect_jd_level_conflict(text, "intern")


def test_generated_focus_changes_with_two_jds_same_resume(
    client: TestClient, worker
) -> None:
    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)

    def make_jd(title: str, requirements: list[dict]) -> str:
        response = client.post(
            "/v1/materials/text",
            json={"kind": "jd", "title": title, "text": title + "\n职责：完成 Java 功能开发。\n" + "\n".join(
                f"要求：{item['text']}" for item in requirements
            )},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert response.status_code == 201, response.text
        base_quote = "职责：完成 Java 功能开发。"
        profile = {
            "kind": "jd",
            "role_id": "java-backend",
            "level": "intern",
            "title": title,
            "responsibilities": [
                {
                    "item_id": "responsibility-1",
                    "text": base_quote,
                    "evidence": {"quote": base_quote, "origin": "extracted"},
                }
            ],
            "requirements": requirements,
        }
        from app.schemas import JdProfile

        JdProfile.model_validate(profile)
        snapshot = client.post(
            f"/v1/materials/{response.json()['id']}/confirm",
            json={"revision": 1, "profile": profile},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert snapshot.status_code == 201, snapshot.text
        return snapshot.json()["id"]

    def req(item_id: str, text: str, code: str) -> dict:
        return {
            "item_id": item_id,
            "text": text,
            "skill_codes": [code],
            "importance": "must",
            "evidence": {"quote": f"要求：{text}", "origin": "extracted"},
        }

    jd_a = make_jd(
        "集合岗",
        [
            req("req-1", "理解 Java 集合的使用及基本边界", "java.core"),
            req("req-2", "能够为简单功能设计边界测试", "test.junit"),
        ],
    )
    jd_b = make_jd(
        "测试岗",
        [
            req("req-1", "能够为简单功能设计边界测试", "test.junit"),
            req("req-2", "理解 Java 集合的使用及基本边界", "java.core"),
        ],
    )

    focuses: list[list[str]] = []
    for jd_id in (jd_a, jd_b):
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
        with SessionLocal() as db:
            plan = db.get(Plan, uuid.UUID(job["result_ref"]["id"]))
            generated = [
                q for q in plan.internal_payload["questions"] if q["source"] == "generated"
            ]
            focuses.append([q["focus"] for q in generated])

    assert focuses[0] != focuses[1]
