"""C01：真实 PostgreSQL 并发、锁序与租约竞争回归。"""

from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.provider import TestProvider
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import (
    Job,
    JobWorkItem,
    MaterialDraft,
    MaterialSnapshot,
    Plan,
    Preparation,
)
from app.schemas import ConfirmMaterialInput
from app.services import jobs as jobs_service
from app.services import materials as materials_service
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


def _create_material(client: TestClient, token: str) -> dict:
    response = client.post(
        "/v1/materials/text",
        json={
            "kind": "resume",
            "title": "并发确认材料",
            "text": REVIEWED_RESUME_TEXT,
            "template_id": None,
            "template_version": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_two_keys_confirm_same_revision_create_one_snapshot(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _create_material(client, token)
    owner_id = uuid.UUID(client.get("/v1/me", headers=auth(token)).json()["id"])
    material_id = uuid.UUID(material["id"])
    payload = ConfirmMaterialInput(revision=1, profile=REVIEWED_RESUME_PROFILE)

    barrier = threading.Barrier(2)

    def confirm_once() -> str:
        db = SessionLocal()
        try:
            draft = materials_service.get_material(db, owner_id, material_id)
            assert draft.revision == 1
            barrier.wait(timeout=10)
            snapshot = materials_service.confirm_material(db, draft, payload)
            db.commit()
            return str(snapshot.id)
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: confirm_once(), range(2)))

    assert len(set(results)) == 1
    with SessionLocal() as check:
        count = check.scalar(
            select(func.count())
            .select_from(MaterialSnapshot)
            .where(MaterialSnapshot.material_id == material_id)
        )
        assert count == 1


def test_edit_wins_over_inflight_plan_publish(
    client: TestClient, worker, monkeypatch
) -> None:
    import app.workers.runner as runner

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

    entered = threading.Event()
    release = threading.Event()

    class PausingProvider(TestProvider):
        def review_questions(self, *, questions, material_texts, fact_cards=None):
            entered.set()
            assert release.wait(timeout=15), "测试未释放暂停的生成任务"
            return super().review_questions(
                questions=questions,
                material_texts=material_texts,
                fact_cards=fact_cards,
            )

    monkeypatch.setattr(runner, "get_provider", lambda _: PausingProvider())
    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert generate.status_code == 202, generate.text
    job_id = generate.json()["id"]

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(worker.run_once)
        assert entered.wait(timeout=15), "生成任务没有进入模型复核阶段"

        edited = client.put(
            f"/v1/preparations/{prep['id']}",
            json={
                "revision": 1,
                "role_id": "java-backend",
                "level": "intern",
                "main_question_count": 4,
                "resume_snapshot_id": resume_id,
                "jd_snapshot_id": jd_id,
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert edited.status_code == 200, edited.text
        assert edited.json()["revision"] == 2
        release.set()
        future.result(timeout=20)

    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "superseded", job
    with SessionLocal() as check:
        plan = check.scalars(
            select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))
        ).one_or_none()
        assert plan is None
        prep_row = check.get(Preparation, uuid.UUID(prep["id"]))
        assert prep_row is not None
        assert prep_row.current_plan_id is None
        assert prep_row.revision == 2


def test_expired_lease_cannot_be_renewed_and_is_reaped(
    client: TestClient, db
) -> None:
    _, token = issue_token(client)
    material = _create_material(client, token)
    job = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    job_id = uuid.UUID(job["id"])

    row = db.get(Job, job_id)
    row.state = "running"
    row.lease_token = uuid.uuid4()
    row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()
    token_before = row.lease_token

    assert jobs_service.renew_lease(db, job_id, row.attempt, token_before) is False
    db.expire_all()
    refreshed = db.get(Job, job_id)
    assert refreshed.state == "running"
    assert jobs_service.reap_expired_leases(db) >= 1
    db.expire_all()
    refreshed = db.get(Job, job_id)
    assert refreshed.state == "interrupted"
    assert refreshed.error_code == "JOB_INTERRUPTED"
    assert refreshed.lease_token is None


def test_stale_worker_cannot_write_work_item(client: TestClient, worker, db) -> None:
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
    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert generate.status_code == 202, generate.text

    claimed = jobs_service.claim_next_job(db, "stale-worker")
    while claimed is not None and str(claimed.id) != generate.json()["id"]:
        claimed = jobs_service.claim_next_job(db, "stale-worker")
    assert claimed is not None
    stale = claimed

    # 模拟租约被回收后新 attempt 接管：旧 worker 不能再写槽位。
    with SessionLocal() as other:
        row = other.get(Job, stale.id)
        row.state = "interrupted"
        row.lease_token = None
        row.lease_expires_at = None
        other.commit()

    with SessionLocal() as stale_db:
        old = stale_db.get(Job, stale.id)
        with pytest.raises(AppError) as caught:
            jobs_service.save_work_item(
                stale_db,
                old,
                slot_key="generated-1",
                payload={"stem": "旧 worker 不得写入"},
                content_hash="0" * 64,
            )
        assert caught.value.code == "JOB_LEASE_LOST"
        stale_db.rollback()

    with SessionLocal() as check:
        count = check.scalar(
            select(func.count())
            .select_from(JobWorkItem)
            .where(JobWorkItem.job_id == stale.id)
        )
        assert count == 0
