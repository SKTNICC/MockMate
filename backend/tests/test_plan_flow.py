from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import Settings
from app.models import Job, Plan
from app.services.bank import eligible_bank_questions
from app.services.preparations import create_plan_job, get_preparation
from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    REVIEWED_RESUME_TEXT,
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    new_key,
    wait_job,
)

PUBLIC_FIELDS = {
    "id",
    "preparation_id",
    "input_revision",
    "revision",
    "state",
    "role_id",
    "level",
    "main_question_count",
    "source_counts",
    "resume_snapshot_id",
    "jd_snapshot_id",
    "focus_areas",
    "limitations",
    "created_at",
    "confirmed_at",
}


def _make_prep(client, token, *, count, resume=None, jd=None):
    response = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": count,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _run_match(client, token, worker, prep_id):
    response = client.post(
        f"/v1/preparations/{prep_id}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, token, response.json()["id"], worker)
    assert job["state"] == "succeeded", job
    return job


def _run_plan(client, token, worker, prep_id, *, consent=True):
    response = client.post(
        f"/v1/preparations/{prep_id}/generate-plan",
        json={"revision": 1, "external_processing_consent": consent},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    return response


def _assert_public_plan(payload: dict, count: int) -> None:
    assert set(payload.keys()) == PUBLIC_FIELDS
    assert payload["main_question_count"] == count
    assert payload["source_counts"] == {"bank": count // 2, "generated": count // 2}
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in ("stem", "questions", "reference_approach", "key_points", "rubric", "bank_ref"):
        assert forbidden not in serialized
    assert "测试数据流程" in " ".join(payload["limitations"])


def test_full_journey_plan_confirm_and_reopen(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=6, resume=resume, jd=jd)

    match_job = _run_match(client, token, worker, prep["id"])
    report_id = match_job["result_ref"]["id"]
    report = client.get(f"/v1/match-reports/{report_id}", headers=auth(token))
    assert report.status_code == 200
    rows = report.json()["rows"]
    assert rows and all(row["requirement_id"] for row in rows)
    assert {row["status"] for row in rows} <= {"supported", "to_verify", "not_evidenced"}

    current = client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).json()
    assert current["match_report_id"] == report_id

    generate = _run_plan(client, token, worker, prep["id"])
    assert generate.status_code == 202, generate.text
    plan_job = wait_job(client, token, generate.json()["id"], worker)
    assert plan_job["state"] == "succeeded", plan_job

    plan_id = plan_job["result_ref"]["id"]
    plan = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert plan.status_code == 200
    payload = plan.json()
    _assert_public_plan(payload, 6)
    assert payload["state"] == "ready"
    assert payload["confirmed_at"] is None
    assert payload["input_revision"] == 1

    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 200, confirm.text
    confirmed = confirm.json()
    assert confirmed["state"] == "confirmed"
    assert confirmed["revision"] == 2
    assert confirmed["confirmed_at"] is not None

    # 重复确认读回相同状态，不产生新计划
    again = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert again.status_code == 200
    assert again.json()["id"] == plan_id
    assert again.json()["state"] == "confirmed"
    assert len(db.scalars(select(Plan).where(Plan.preparation_id == prep["id"])).all()) == 1

    # 重新打开：准备列表 → current_plan_id → 同一计划，且不触发新任务
    jobs_before = db.scalar(select(func.count()).select_from(Job))
    listed = client.get("/v1/preparations", headers=auth(token)).json()["items"]
    reopened_prep = next(item for item in listed if item["id"] == prep["id"])
    assert reopened_prep["current_plan_id"] == plan_id
    reopened = client.get(f"/v1/plans/{reopened_prep['current_plan_id']}", headers=auth(token))
    assert reopened.status_code == 200
    assert reopened.json()["id"] == plan_id
    assert reopened.json()["state"] == "confirmed"
    assert reopened.json()["resume_snapshot_id"] == payload["resume_snapshot_id"]
    assert db.scalar(select(func.count()).select_from(Job)) == jobs_before


def test_quotas_two_and_four(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    for count in (2, 4):
        prep = _make_prep(client, token, count=count, resume=resume, jd=jd)
        _run_match(client, token, worker, prep["id"])
        generate = _run_plan(client, token, worker, prep["id"])
        assert generate.status_code == 202, generate.text
        plan_job = wait_job(client, token, generate.json()["id"], worker)
        assert plan_job["state"] == "succeeded", plan_job
        plan = client.get(f"/v1/plans/{plan_job['result_ref']['id']}", headers=auth(token)).json()
        _assert_public_plan(plan, count)


def test_generate_consent_required(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)
    _run_match(client, token, worker, prep["id"])
    response = _run_plan(client, token, worker, prep["id"], consent=False)
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "CONSENT_REQUIRED"


def test_plan_stale_after_material_change(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)
    _run_match(client, token, worker, prep["id"])
    generate = _run_plan(client, token, worker, prep["id"])
    plan_job = wait_job(client, token, generate.json()["id"], worker)
    plan_id = plan_job["result_ref"]["id"]
    assert client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).status_code == 200

    materials = client.get("/v1/materials", headers=auth(token)).json()["items"]
    resume_material = next(item for item in materials if item["kind"] == "resume")
    edited = client.put(
        f"/v1/materials/{resume_material['id']}",
        json={
            "revision": resume_material["revision"],
            "title": resume_material["title"],
            "text": REVIEWED_RESUME_TEXT + "\n补充：参加课程设计答辩。",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200
    new_snapshot = client.post(
        f"/v1/materials/{resume_material['id']}/confirm",
        json={"revision": edited.json()["revision"], "profile": REVIEWED_RESUME_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert new_snapshot.status_code == 201

    current = client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).json()
    put = client.put(
        f"/v1/preparations/{prep['id']}",
        json={
            "revision": current["revision"],
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": new_snapshot.json()["id"],
            "jd_snapshot_id": current["jd_snapshot_id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert put.status_code == 200
    assert put.json()["current_plan_id"] is None
    assert put.json()["revision"] == current["revision"] + 1

    stale_plan = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert stale_plan.status_code == 200
    assert stale_plan.json()["state"] == "confirmed"

    stale_confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": stale_plan.json()["revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert stale_confirm.status_code == 409
    assert stale_confirm.json()["error"]["code"] == "PLAN_STALE"


def test_generate_plan_idempotent_key_replays(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)
    _run_match(client, token, worker, prep["id"])

    key = new_key()
    body = {"revision": 1, "external_processing_consent": True}
    first = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json=body,
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert first.status_code == 202
    replay = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json=body,
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert replay.status_code == 202
    assert replay.json()["id"] == first.json()["id"]

    conflict = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 2, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_same_input_returns_same_job(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)

    first = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    second = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert first.json()["id"] == second.json()["id"]


def test_ai_not_configured_is_explicit(client: TestClient) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)

    from app.core.config import get_settings

    settings = get_settings()
    original = settings.ai_provider
    settings.ai_provider = "disabled"
    try:
        response = client.post(
            f"/v1/preparations/{prep['id']}/match",
            json={"revision": 1, "external_processing_consent": True},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "AI_NOT_CONFIGURED"
    finally:
        settings.ai_provider = original


def test_production_mode_rejects_test_only_bank(client: TestClient, worker, db) -> None:
    import pytest

    from app.core.config import get_settings
    from app.models import Preparation
    from app.schemas import AiJobInput

    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _make_prep(client, token, count=2, resume=resume, jd=jd)
    _run_match(client, token, worker, prep["id"])

    production_like = Settings(
        APP_ENV="development",
        DATABASE_URL=get_settings().database_url,
        PRIVATE_FILE_ROOT=get_settings().private_file_root,
        AI_PROVIDER="deepseek",
        AI_API_KEY="placeholder-for-gate-test",
        AI_MODEL="placeholder-model",
        ALLOW_TEST_FIXTURES=False,
        M1_SAMPLE_CATALOG=True,
    )
    assert eligible_bank_questions(db, production_like, "java-backend", "intern") == []

    prep_row = db.get(Preparation, prep["id"])
    with pytest.raises(Exception) as caught:
        create_plan_job(
            db, prep_row, AiJobInput(revision=1, external_processing_consent=True), production_like
        )
    db.rollback()
    assert getattr(caught.value, "code", "") == "BANK_INSUFFICIENT", repr(caught.value)


def test_minimal_resume_without_experience_allows_jd_hypothetical_scenario_and_publishes_six(
    client: TestClient, worker, db
) -> None:
    """无工作经历与项目时，仍可围绕 JD 要求与真实锚点生成不同任务并发布 3+3 六题计划。"""
    _, token = issue_token(client)
    minimal_resume_text = (
        "软件工程专业本科在读。\n"
        "掌握 Java 集合与面向接口编程，学习 JUnit 单元测试基础。"
    )
    minimal_profile = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "软件工程",
                "evidence": {"quote": "软件工程专业本科在读。", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java 集合与 JUnit",
                "code": "java.core",
                "evidence": {
                    "quote": "掌握 Java 集合与面向接口编程，学习 JUnit 单元测试基础。",
                    "origin": "extracted",
                },
            }
        ],
    }
    created = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "无经历极简简历", "text": minimal_resume_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created.status_code == 201
    snap = client.post(
        f"/v1/materials/{created.json()['id']}/confirm",
        json={"revision": 1, "profile": minimal_profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert snap.status_code == 201
    resume_id = snap.json()["id"]
    jd_id = create_confirmed_template_jd(client, token)

    prep = _make_prep(client, token, count=6, resume=resume_id, jd=jd_id)
    _run_match(client, token, worker, prep["id"])
    generate = _run_plan(client, token, worker, prep["id"])
    assert generate.status_code == 202
    plan_job = wait_job(client, token, generate.json()["id"], worker, attempts=4)
    assert plan_job["state"] == "succeeded", plan_job

    plan_id = plan_job["result_ref"]["id"]
    public = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
    _assert_public_plan(public, 6)

    plan_row = db.get(Plan, plan_id)
    generated = [q for q in plan_row.internal_payload["questions"] if q["source"] == "generated"]
    assert len(generated) == 3
    assert len({q["stem"] for q in generated}) == 3

