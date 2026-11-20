"""R00/R01/R04：语音重试投影、provider 缓存隔离与严格上传幂等回归。

这些用例来自 2026-11-20 独立验收探针，断言的是目标行为；修复前应失败。
故障只通过 test provider／受控 monkeypatch 制造，不访问真实供应商。
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.speech import TestSpeechProvider as SpeechStub
from app.core.config import get_settings
from app.core.errors import AppError
from app.models import Job, SpeechAsset
from app.services import voice
from tests.conftest import auth, issue_token, new_key, wait_job
from tests.m2_helpers import create_confirmed_plan, detail
from tests.m3_helpers import (
    pcm_answer,
    speech,
    start_voice_interview,
    upload_pcm,
    voice_draft,
)


def _setup(client: TestClient, worker, count: int = 2):
    owner, token = issue_token(client)
    _, plan = create_confirmed_plan(client, token, worker, count=count)
    session_id = start_voice_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    return owner, token, session_id, current


def _upload(client, token, session_id, current, *, pcm=None, take=None, key=None):
    turn = current["current_turn"]
    draft = voice_draft(client, token, session_id, turn["turn_id"])
    response = upload_pcm(
        client,
        token,
        session_id,
        turn["turn_id"],
        session_revision=current["revision"],
        expected_voice_revision=draft["voice_revision"],
        pcm=pcm or pcm_answer(),
        take_id=take,
        key=key,
    )
    assert response.status_code == 201, response.text
    return turn, response.json()


def _asr_job(client, token, asset_id, revision, key=None):
    response = client.post(
        f"/v1/audio-assets/{asset_id}/transcriptions",
        json={"expected_voice_revision": revision},
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _retry(client, token, job, key=None):
    return client.post(
        f"/v1/jobs/{job['id']}/retry",
        json={"expected_attempt": job["attempt"]},
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )


def test_asr_retry_projects_processing_then_publishes_new_attempt(
    client: TestClient, worker
) -> None:
    _, token, session_id, current = _setup(client, worker)
    turn, receipt = _upload(client, token, session_id, current)
    asset_id = receipt["audio"]["id"]

    def timeout(self, wav_bytes):
        raise AppError("AI_TIMEOUT", "孤立探针超时", retryable=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SpeechStub, "transcribe", timeout)
        created = _asr_job(client, token, asset_id, receipt["voice_revision"])
        failed = wait_job(client, token, created["id"], worker)
        assert failed["state"] == "failed" and failed["attempt"] == 1

    retried = _retry(client, token, failed)
    assert retried.status_code == 202, retried.text
    assert retried.json()["attempt"] == 2
    assert retried.json()["state"] == "queued"

    # retry 已被接受后，查询必须立刻反映当前 attempt，而不是旧 failed 行。
    before_worker = client.get(
        f"/v1/audio-assets/{asset_id}/transcript", headers=auth(token)
    )
    assert before_worker.status_code == 202, before_worker.text
    assert before_worker.json()["status"] == "processing", before_worker.text
    draft = voice_draft(client, token, session_id, turn["turn_id"])
    assert draft["asr_job"]["state"] == "queued"
    assert draft["asr_job"]["attempt"] == 2
    assert draft["transcript"]["status"] == "processing"

    job = wait_job(client, token, failed["id"], worker)
    assert job["state"] == "succeeded", job
    ready = client.get(
        f"/v1/audio-assets/{asset_id}/transcript", headers=auth(token)
    )
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    assert ready.json()["text"].strip()


def test_asr_retry_exhaustion_keeps_explicit_failure(
    client: TestClient, worker
) -> None:
    _, token, session_id, current = _setup(client, worker)
    _turn, receipt = _upload(client, token, session_id, current)
    asset_id = receipt["audio"]["id"]

    def timeout(self, wav_bytes):
        raise AppError("AI_TIMEOUT", "孤立探针超时", retryable=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SpeechStub, "transcribe", timeout)
        created = _asr_job(client, token, asset_id, receipt["voice_revision"])
        first = wait_job(client, token, created["id"], worker)
        assert first["state"] == "failed" and first["attempt"] == 1
        retried = _retry(client, token, first)
        assert retried.status_code == 202, retried.text
        second = wait_job(client, token, first["id"], worker)
        assert second["state"] == "failed" and second["attempt"] == 2

    exhausted = _retry(client, token, second)
    assert exhausted.status_code == 409, exhausted.text
    assert exhausted.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    failed = client.get(
        f"/v1/audio-assets/{asset_id}/transcript", headers=auth(token)
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "AI_TIMEOUT"


def test_tts_retry_projects_current_attempt_and_reaches_ready(
    client: TestClient, worker
) -> None:
    _, token, session_id, current = _setup(client, worker)
    turn = current["current_turn"]

    def timeout(self, text):
        raise AppError("AI_TIMEOUT", "孤立 TTS 超时", retryable=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SpeechStub, "synthesize", timeout)
        requested = speech(
            client, token, session_id, segment="turn", turn_id=turn["turn_id"]
        )
        assert requested.status_code == 202, requested.text
        asset_id = requested.json()["id"]
        failed = wait_job(client, token, requested.json()["job_id"], worker)
        assert failed["state"] == "failed" and failed["attempt"] == 1
        failed_asset = client.get(f"/v1/speech-assets/{asset_id}", headers=auth(token))
        assert failed_asset.json()["status"] == "failed"

    retried = _retry(client, token, failed)
    assert retried.status_code == 202, retried.text
    assert retried.json()["attempt"] == 2
    queued = client.get(f"/v1/speech-assets/{asset_id}", headers=auth(token))
    assert queued.json()["status"] in ("queued", "generating"), queued.text

    job = wait_job(client, token, failed["id"], worker)
    assert job["state"] == "succeeded", job
    ready = client.get(f"/v1/speech-assets/{asset_id}", headers=auth(token))
    assert ready.json()["status"] == "ready"
    content = client.get(
        f"/v1/speech-assets/{asset_id}/content", headers=auth(token)
    )
    assert content.status_code == 200 and content.content[:4] == b"RIFF"


def test_replay_after_tts_failure_starts_controlled_retry_without_new_job(
    client: TestClient, worker, db
) -> None:
    _, token, session_id, current = _setup(client, worker)
    turn = current["current_turn"]

    def timeout(self, text):
        raise AppError("AI_TIMEOUT", "孤立 TTS 超时", retryable=True)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SpeechStub, "synthesize", timeout)
        requested = speech(
            client, token, session_id, segment="turn", turn_id=turn["turn_id"]
        )
        failed = wait_job(client, token, requested.json()["job_id"], worker)
        assert failed["state"] == "failed"

    # 用户再次点击“播放”等价于显式重试：复用原 job/asset，不放宽 attempt 上限。
    replayed = speech(
        client, token, session_id, segment="turn", turn_id=turn["turn_id"]
    )
    assert replayed.status_code == 202, replayed.text
    assert replayed.json()["id"] == requested.json()["id"]
    body = replayed.json()
    job = client.get(f"/v1/jobs/{body['job_id']}", headers=auth(token)).json()
    assert job["attempt"] == 2 and job["state"] == "queued", job
    assert len(
        db.scalars(
            select(Job).where(
                Job.kind == "audio_tts", Job.target_id == uuid.UUID(body["id"])
            )
        ).all()
    ) == 1

    job = wait_job(client, token, body["job_id"], worker)
    assert job["state"] == "succeeded", job
    ready = client.get(f"/v1/speech-assets/{body['id']}", headers=auth(token))
    assert ready.json()["status"] == "ready"

    # 已 ready 后重复请求只读缓存，不再新建任务或增加 attempt。
    cached = speech(client, token, session_id, segment="turn", turn_id=turn["turn_id"])
    assert cached.status_code == 200
    assert cached.json()["id"] == body["id"]
    assert cached.json()["job_id"] == body["job_id"]
    assert client.get(f"/v1/jobs/{body['job_id']}", headers=auth(token)).json()["attempt"] == 2


def test_tts_cache_is_isolated_by_provider_and_reuses_each_provider(
    client: TestClient, worker, db
) -> None:
    owner, token, session_id, current = _setup(client, worker)
    turn = current["current_turn"]
    requested = speech(
        client, token, session_id, segment="turn", turn_id=turn["turn_id"]
    )
    assert requested.status_code == 202, requested.text
    test_asset_id = requested.json()["id"]
    succeeded = wait_job(client, token, requested.json()["job_id"], worker)
    assert succeeded["state"] == "succeeded", succeeded

    db.expire_all()
    test_asset = db.get(SpeechAsset, uuid.UUID(test_asset_id))
    assert test_asset is not None and test_asset.provider == "test"
    assert test_asset.synthetic is True

    real_settings = get_settings().model_copy(
        update={"speech_provider": "deepseek", "speech_api_key": "probe-no-network"}
    )
    fresh, ready = voice.request_speech(
        db,
        owner_id=test_asset.owner_id,
        session_id=uuid.UUID(session_id),
        segment="turn",
        turn_id=uuid.UUID(turn["turn_id"]),
        settings=real_settings,
    )
    db.flush()
    assert ready is False
    assert fresh.provider == "deepseek" and fresh.synthetic is False
    assert str(fresh.id) != test_asset_id

    # 同一真实 provider 再次请求命中其自己的缓存，不重复建资产。
    same, _ = voice.request_speech(
        db,
        owner_id=test_asset.owner_id,
        session_id=uuid.UUID(session_id),
        segment="turn",
        turn_id=uuid.UUID(turn["turn_id"]),
        settings=real_settings,
    )
    assert str(same.id) == str(fresh.id)

    # 切回 test provider 仍命中原合成资产。
    test_settings = get_settings().model_copy(update={"speech_provider": "test"})
    again, _ = voice.request_speech(
        db,
        owner_id=test_asset.owner_id,
        session_id=uuid.UUID(session_id),
        segment="turn",
        turn_id=uuid.UUID(turn["turn_id"]),
        settings=test_settings,
    )
    assert str(again.id) == test_asset_id
    db.rollback()


def test_upload_same_key_same_body_replays_and_same_key_new_body_is_409(
    client: TestClient, worker
) -> None:
    _, token, session_id, current = _setup(client, worker)
    take = str(uuid.uuid4())
    key = new_key()
    pcm = pcm_answer(1.0)
    _turn, first = _upload(
        client, token, session_id, current, pcm=pcm, take=take, key=key
    )
    replay = upload_pcm(
        client,
        token,
        session_id,
        current["current_turn"]["turn_id"],
        session_revision=current["revision"],
        expected_voice_revision=0,
        pcm=pcm,
        take_id=take,
        key=key,
    )
    assert replay.status_code in (200, 201), replay.text
    assert replay.json()["audio"]["id"] == first["audio"]["id"]

    # 服务端严格 key+body 幂等：同 key 不同文件必须拒绝，客户端不得靠改 body 恢复。
    changed = upload_pcm(
        client,
        token,
        session_id,
        current["current_turn"]["turn_id"],
        session_revision=current["revision"],
        expected_voice_revision=0,
        pcm=pcm_answer(1.2, frequency=330),
        take_id=take,
        key=key,
    )
    assert changed.status_code == 409, changed.text
    assert changed.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
