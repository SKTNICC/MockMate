from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Job, MaterialDraft
from app.services import jobs as jobs_service
from tests.conftest import (
    REVIEWED_RESUME_TEXT,
    auth,
    issue_token,
    new_key,
    wait_job,
)


def _create_resume(client, token, text=REVIEWED_RESUME_TEXT):
    response = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "简历", "text": text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201
    return response.json()


def _parse(client, token, material_id, revision=1, role="java-backend", level="intern"):
    return client.post(
        f"/v1/materials/{material_id}/parse",
        json={
            "revision": revision,
            "external_processing_consent": True,
            "role_id": role,
            "level": level,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def test_parse_job_is_shared_for_same_revision(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    first = _parse(client, token, material["id"])
    second = _parse(client, token, material["id"])
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


def test_parse_job_input_conflict_on_role_change(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    assert _parse(client, token, material["id"], level="intern").status_code == 202
    conflict = _parse(client, token, material["id"], level="junior")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "JOB_INPUT_CONFLICT"


def test_expired_lease_interrupted_and_explicit_retry(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_response = _parse(client, token, material["id"])
    job_id = uuid.UUID(job_response.json()["id"])

    # 模拟 worker 领取后失联：直接改租约过期并回收
    job = db.get(Job, job_id)
    job.state = "running"
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()

    reaped = jobs_service.reap_expired_leases(db)
    assert reaped >= 1
    interrupted = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert interrupted["state"] == "interrupted"
    assert interrupted["error"]["code"] == "JOB_INTERRUPTED"
    assert interrupted["retryable"] is True
    assert interrupted["result_ref"] is None

    # 不会自动重发外部调用
    worker.run_once()
    still = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert still["state"] == "interrupted"
    assert still["attempt"] == 1

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["attempt"] == 2
    assert retried.json()["state"] == "queued"

    final = wait_job(client, token, str(job_id), worker, attempts=3)
    assert final["state"] == "succeeded"

    too_late = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert too_late.status_code == 409
    assert too_late.json()["error"]["code"] == "RETRY_NOT_ALLOWED"


def test_retry_rejected_when_not_allowed(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = _parse(client, token, material["id"]).json()["id"]
    final = wait_job(client, token, job_id, worker)
    assert final["state"] == "succeeded"

    response = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_NOT_ALLOWED"

    wrong_attempt = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wrong_attempt.status_code == 409


def test_old_worker_cannot_submit_after_lease_lost(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = uuid.UUID(_parse(client, token, material["id"]).json()["id"])

    claimed = jobs_service.claim_next_job(db, "test-worker")
    while claimed is not None and claimed.id != job_id:
        claimed = jobs_service.claim_next_job(db, "test-worker")
    assert claimed is not None
    assert claimed.id == job_id
    stale_token = claimed.lease_token
    stale_attempt = claimed.attempt

    # 另一进程回收并重新入队（模拟新尝试）
    claimed.state = "interrupted"
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    fenced = jobs_service.fence_job(db, job_id, stale_attempt, stale_token)
    assert fenced is None


def test_job_visibility_is_owner_only(client: TestClient) -> None:
    _, token_a = issue_token(client)
    _, token_b = issue_token(client)
    material = _create_resume(client, token_a)
    job_id = _parse(client, token_a, material["id"]).json()["id"]
    assert client.get(f"/v1/jobs/{job_id}", headers=auth(token_b)).status_code == 404
    assert (
        client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth(token_a)).status_code == 404
    )


def test_edit_supersedes_open_parse_job(client: TestClient, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = uuid.UUID(_parse(client, token, material["id"]).json()["id"])

    draft = db.get(MaterialDraft, uuid.UUID(material["id"]))
    assert draft is not None
    edited = client.put(
        f"/v1/materials/{material['id']}",
        json={"revision": 1, "title": "简历2", "text": REVIEWED_RESUME_TEXT + "\n补充内容。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200

    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "superseded"
    assert job["retryable"] is False


def test_plan_retry_reuses_validated_work_items(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """真实失败后显式 retry：已合格槽位复用，只重做失败槽位。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

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

    class FlakyProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_two = True
            self.calls: list[str] = []

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            if slot == "generated-2" and self.fail_slot_two:
                raise AppError("AI_TIMEOUT", "模拟第二槽外部超时，结果未知")
            return super().generate_questions(context=context, **kwargs)

    provider = FlakyProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=3)
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "AI_TIMEOUT"
    assert provider.calls == ["generated-1", "generated-2"]

    saved = db.scalars(
        select(JobWorkItem).where(
            JobWorkItem.job_id == job_id, JobWorkItem.slot_key == "generated-1"
        )
    ).one_or_none()
    assert saved is not None and saved.validated is True
    assert saved.payload["validation_version"] == "m1-work-item-v2"
    assert saved.payload["context_hash"]
    first_question = saved.payload["question"]
    assert first_question["stem"]

    provider.fail_slot_two = False
    provider.calls.clear()
    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202, retried.text
    final = wait_job(client, token, str(job_id), worker, attempts=3)
    assert final["state"] == "succeeded", final
    assert provider.calls == ["generated-2"], "已保存槽位不得再次调用模型"

    plan_row = db.scalars(select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))).one()
    plan = client.get(f"/v1/plans/{plan_row.id}", headers=auth(token)).json()
    assert plan["source_counts"] == {"bank": 2, "generated": 2}
    generated = [
        q for q in plan_row.internal_payload["questions"] if q["source"] == "generated"
    ]
    assert generated[0]["stem"] == first_question["stem"]
