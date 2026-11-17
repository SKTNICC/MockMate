"""C05：provider 错误分类、私有调用记录与独立 worker 中断恢复。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.provider import DeepSeekProvider
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import Job
from app.services import jobs as jobs_service
from tests.conftest import (
    REVIEWED_RESUME_TEXT,
    auth,
    issue_token,
    new_key,
    wait_job,
)


def _settings() -> Settings:
    return Settings(
        APP_ENV="test",
        DATABASE_URL=get_settings().database_url,
        PRIVATE_FILE_ROOT=str(get_settings().private_file_root),
        AI_PROVIDER="deepseek",
        AI_MODEL="audit-model",
        AI_API_KEY="AUDIT-FAKE-NOT-A-KEY",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=False,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, "AI_AUTH_FAILED"),
        (403, "AI_AUTH_FAILED"),
        (429, "AI_RATE_LIMITED"),
        (400, "AI_REQUEST_REJECTED"),
        (500, "AI_UNAVAILABLE"),
        (503, "AI_UNAVAILABLE"),
    ],
)
def test_http_statuses_are_classified(status: int, expected: str) -> None:
    provider = DeepSeekProvider(
        _settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json={"error": "x"}, request=request)
        ),
    )
    with pytest.raises(AppError) as caught:
        provider._chat_json("system", "user")
    assert caught.value.code == expected
    assert provider.last_call["outcome"]


@pytest.mark.parametrize(
    "body",
    [
        {"id": "x", "choices": []},
        {"id": "x", "choices": [{}]},
        {"id": "x", "choices": [{"message": {"content": None}}]},
        {"id": "x", "choices": [{"message": {"content": {"not": "string"}}}]},
        {"id": "x", "choices": [{"message": {"content": "[]"}}]},
    ],
)
def test_malformed_outer_envelope_is_invalid_output(body: dict) -> None:
    provider = DeepSeekProvider(
        _settings(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body, request=request)
        ),
    )
    with pytest.raises(AppError) as caught:
        provider._chat_json("system", "user")
    assert caught.value.code == "AI_INVALID_OUTPUT"
    assert provider.last_call["outcome"] == "known_invalid"


def test_parse_job_writes_private_ai_call_record(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "记录测试", "text": REVIEWED_RESUME_TEXT},
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
    job = wait_job(client, token, parse.json()["id"], worker)
    assert job["state"] == "succeeded", job

    with SessionLocal() as db:
        row = db.get(Job, uuid.UUID(job["id"]))
        records = row.ai_call_records
        assert len(records) == 1
        record = records[0]
        assert record["stage"] == "parse"
        assert record["attempt"] == 1
        assert record["provider"] == "test"
        assert record["model"] == "TEST-PROVIDER-NO-AI-CALL"
        assert record["request_id"].startswith("test-")
        assert record["outcome"] == "success"
        serialized = str(record)
        assert REVIEWED_RESUME_TEXT not in serialized
        assert "AUDIT-FAKE-NOT-A-KEY" not in serialized


def test_unexpected_provider_error_is_recorded_as_unknown(
    client: TestClient, worker, monkeypatch
) -> None:
    import app.workers.runner as runner
    from app.ai.provider import TestProvider

    class BrokenProvider(TestProvider):
        def parse_material(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(runner, "get_provider", lambda _: BrokenProvider())
    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "异常记录", "text": REVIEWED_RESUME_TEXT},
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
    job = wait_job(client, token, parse.json()["id"], worker, attempts=3)
    assert job["state"] == "failed", job
    with SessionLocal() as db:
        row = db.get(Job, uuid.UUID(job["id"]))
        assert row.ai_call_records
        assert row.ai_call_records[-1]["outcome"] == "unknown"
        assert row.ai_call_records[-1]["error_code"] == "INTERNAL_ERROR"


def test_independent_worker_interrupt_is_queryable_and_not_resubmitted(
    client: TestClient, worker
) -> None:
    # 确保队列为空，独立子进程领取到的就是本测试创建的任务。
    for _ in range(20):
        if not worker.run_once():
            break

    _, token = issue_token(client)
    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "中断恢复", "text": REVIEWED_RESUME_TEXT},
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

    backend = str(Path(__file__).resolve().parents[1])
    code = (
        "import sys,time\n"
        f"sys.path.insert(0, {backend!r})\n"
        "from app.core.db import SessionLocal\n"
        "from app.services import jobs\n"
        "db=SessionLocal()\n"
        "job=jobs.claim_next_job(db,'interrupt-test-worker')\n"
        "print('NONE' if job is None else f'CLAIMED={job.id}', flush=True)\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        assert line == f"CLAIMED={job_id}", line
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    with SessionLocal() as db:
        row = db.get(Job, job_id)
        assert row.state == "running"
        row.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
        db.commit()
        assert jobs_service.reap_expired_leases(db) >= 1

    interrupted = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert interrupted["state"] == "interrupted"
    assert interrupted["error"]["code"] == "JOB_INTERRUPTED"
    assert interrupted["retryable"] is True
    assert interrupted["attempt"] == 1

    # 回收后不会自动重发外部调用。
    worker.run_once()
    still = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert still["state"] == "interrupted"
    assert still["attempt"] == 1
    with SessionLocal() as db:
        assert db.get(Job, job_id).ai_call_records == []
