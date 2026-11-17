from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.security import hash_token
from app.models import AuthSession, Job, Plan
from app.services import jobs as jobs_service
from tests.conftest import (
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    login,
    new_key,
    register,
    wait_job,
)


def _confirmed_pair(client, token):
    return create_confirmed_resume(client, token), create_confirmed_template_jd(client, token)


def _prep(client, token, resume, jd, count=2):
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


def _full_plan(client, token, worker):
    resume, jd = _confirmed_pair(client, token)
    prep = _prep(client, token, resume, jd)
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
    job = wait_job(client, token, generate.json()["id"], worker)
    assert job["state"] == "succeeded", job
    plan_id = job["result_ref"]["id"]
    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 200
    return prep, plan_id, resume, jd


def test_a02_other_account_gets_404_for_every_private_object(client: TestClient, worker) -> None:
    _, token_a = issue_token(client)
    _, token_b = issue_token(client)
    prep, plan_id, resume, jd = _full_plan(client, token_a, worker)
    job_id = client.get(f"/v1/preparations/{prep['id']}", headers=auth(token_a)).json()
    match_report_id = job_id["match_report_id"]

    for path in (
        f"/v1/snapshots/{resume}",
        f"/v1/snapshots/{jd}",
        f"/v1/preparations/{prep['id']}",
        f"/v1/match-reports/{match_report_id}",
        f"/v1/plans/{plan_id}",
    ):
        assert client.get(path, headers=auth(token_b)).status_code == 404, path

    put = client.put(
        f"/v1/preparations/{prep['id']}",
        json={
            "revision": prep["revision"],
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": None,
            "jd_snapshot_id": None,
        },
        headers={**auth(token_b), "Idempotency-Key": new_key()},
    )
    assert put.status_code == 404
    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 2},
        headers={**auth(token_b), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 404
    delete = client.delete(
        f"/v1/preparations/{prep['id']}",
        headers={**auth(token_b), "Idempotency-Key": new_key()},
    )
    assert delete.status_code == 404
    listed = client.get("/v1/preparations", headers=auth(token_b)).json()["items"]
    assert listed == []


def test_a03_expired_session_is_rejected(client: TestClient, db) -> None:
    username, password = register(client)
    response = client.post("/v1/auth/login", json={"username": username, "password": password})
    token = response.json()["access_token"]
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).one()
    session.expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()

    me = client.get("/v1/me", headers=auth(token))
    assert me.status_code == 401
    assert me.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "过期" in me.json()["error"]["message"]


def test_a03_login_rate_limit_returns_429(client: TestClient) -> None:
    from app.services.rate_limit import reset_login_rate_limit

    settings = get_settings()
    original = settings.login_rate_limit_attempts
    username, _ = register(client)
    settings.login_rate_limit_attempts = 2
    try:
        statuses = []
        for _ in range(3):
            statuses.append(
                client.post(
                    "/v1/auth/login",
                    json={"username": username, "password": "wrongpass1234"},
                ).status_code
            )
        assert statuses[:2] == [401, 401]
        assert statuses[2] == 429
    finally:
        settings.login_rate_limit_attempts = original
        reset_login_rate_limit(username, "testclient")


def test_a04_delete_replay_is_204_and_job_single(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "删除回放", "text": "本科在读。\nJava。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    key = new_key()
    first = client.delete(f"/v1/materials/{material['id']}", headers={**auth(token), "Idempotency-Key": key})
    replay = client.delete(f"/v1/materials/{material['id']}", headers={**auth(token), "Idempotency-Key": key})
    assert first.status_code == 204 and replay.status_code == 204

    resume, jd = _confirmed_pair(client, token)
    prep = _prep(client, token, resume, jd)
    first_job = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    second_job = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert first_job.json()["id"] == second_job.json()["id"]
    wait_job(client, token, first_job.json()["id"], worker)


def test_m02_draft_id_cannot_be_used_as_snapshot(client: TestClient) -> None:
    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "草稿", "text": "本科在读。\nJava。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    response = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": material["id"],
            "jd_snapshot_id": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


def test_m05_corrupt_docx_fails_with_docx_message(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    created = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "坏 DOCX"},
        files={
            "file": (
                "resume.docx",
                b"not a zip file",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created.status_code == 201
    material = created.json()
    extract = client.post(
        f"/v1/materials/{material['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, extract.json()["id"], worker)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "FILE_TEXT_UNAVAILABLE"
    assert "DOCX" in job["error"]["message"]


def test_p01_invalid_question_counts_are_rejected(client: TestClient) -> None:
    _, token = issue_token(client)
    for count in (0, 3, 8):
        response = client.post(
            "/v1/preparations",
            json={
                "role_id": "java-backend",
                "level": "intern",
                "main_question_count": count,
                "resume_snapshot_id": None,
                "jd_snapshot_id": None,
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert response.status_code == 422, count
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_p08_repeated_confirm_creates_no_new_plan_or_job(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    prep, plan_id, _, _ = _full_plan(client, token, worker)
    plan_count_before = db.scalar(select(func.count()).select_from(Plan))
    job_count_before = db.scalar(select(func.count()).select_from(Job))

    repeat = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 99},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert repeat.status_code == 200
    assert repeat.json()["id"] == plan_id
    assert repeat.json()["state"] == "confirmed"
    assert db.scalar(select(func.count()).select_from(Plan)) == plan_count_before
    assert db.scalar(select(func.count()).select_from(Job)) == job_count_before


def test_p09_redo_creates_new_preparation_and_keeps_old_plan(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    prep, plan_id, resume, jd = _full_plan(client, token, worker)
    new_prep = _prep(client, token, resume, jd, count=4)
    assert new_prep["id"] != prep["id"]
    assert new_prep["current_plan_id"] is None

    old_plan = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert old_plan.status_code == 200
    assert old_plan.json()["state"] == "confirmed"
    old_prep = client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).json()
    assert old_prep["current_plan_id"] == plan_id


def test_j03_retry_limit_is_two_attempts(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "重试上限", "text": "本科在读。\nJava 集合。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    parse = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(parse.json()["id"])

    def interrupt(attempt_expected: int) -> None:
        job = db.get(Job, job_id)
        job.state = "running"
        job.attempt = attempt_expected
        job.lease_token = uuid.uuid4()
        job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.commit()
        jobs_service.reap_expired_leases(db)

    interrupt(1)
    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    assert retried.json()["attempt"] == 2

    interrupt(2)
    blocked = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "RETRY_NOT_ALLOWED"


def test_j04_delete_preparation_removes_jobs_and_idempotency(client: TestClient) -> None:
    _, token = issue_token(client)
    resume, jd = _confirmed_pair(client, token)
    prep = _prep(client, token, resume, jd)
    key = new_key()
    job = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": key},
    )
    job_id = job.json()["id"]

    deleted = client.delete(
        f"/v1/preparations/{prep['id']}", headers={**auth(token), "Idempotency-Key": new_key()}
    )
    assert deleted.status_code == 204
    assert client.get(f"/v1/jobs/{job_id}", headers=auth(token)).status_code == 404
    assert client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).status_code == 404

    replay = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert replay.status_code == 404


def test_p06_plan_survives_engine_restart(client: TestClient, worker) -> None:
    from app.core.db import engine

    _, token = issue_token(client)
    _, plan_id, _, _ = _full_plan(client, token, worker)
    before = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
    engine.dispose()
    after = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
    assert after == before
    assert after["state"] == "confirmed"


def test_p05_stale_plan_confirm_rejected(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    prep, plan_id, resume, jd = _full_plan(client, token, worker)
    current = client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).json()
    put = client.put(
        f"/v1/preparations/{prep['id']}",
        json={
            "revision": current["revision"],
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert put.status_code == 200
    stale = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "PLAN_STALE"
