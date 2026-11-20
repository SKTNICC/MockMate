"""T04 D05/D06：实际 DeepSeekSpeechProvider 对本地可控 HTTP/WS 桩的故障矩阵。

不访问真实供应商，不伪造成功：ASR 覆盖 401/429/timeout，TTS 覆盖无 finish、
错误 finish、非法 PCM、握手 401 与显式重播恢复。所有 job 走真实 API、PostgreSQL、
worker 与语音服务校验。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from websockets.sync.server import serve as ws_serve

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import Job, SpeechAsset
from app.workers.runner import JobWorker
from tests.conftest import auth, issue_token, new_key
from tests.m2_helpers import create_confirmed_plan
from tests.m3_helpers import (
    pcm_answer,
    speech,
    start_voice_interview,
    upload_pcm,
    voice_draft,
)
from tests.test_voice import _upload_current  # type: ignore[attr-defined]

STATE: dict[str, str] = {"asr": "ok", "tts": "ok"}
GOOD_PCM = (b"\x00\x01" * 2400)


class _AsrHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        if self.path != "/v1/tools/asr":
            self.send_response(404)
            self.end_headers()
            return
        mode = STATE["asr"]
        if mode == "timeout":
            time.sleep(2.0)
        if mode == "401":
            self._json(401, {"error": "unauthorized"})
            return
        if mode == "429":
            self._json(429, {"error": "rate limited"})
            return
        if mode == "malformed":
            self._json(200, {"text": 123, "duration_ms": 1000})
            return
        self._json(
            200,
            {
                "text": "固定虚构的桩转写结果。",
                "duration_ms": 1000,
                "request_id": "stub-asr-request",
            },
        )

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return


def _tts_handler(connection) -> None:
    mode = STATE["tts"]
    try:
        connection.recv()
        connection.recv()
    except Exception:
        return
    if mode == "no_finish":
        connection.send(GOOD_PCM[:4])
        return
    if mode == "error_finish":
        connection.send(json.dumps({"event": "finish", "code": "content_filter"}))
        return
    if mode == "odd_pcm":
        connection.send(b"\x00\x01\x02")
        connection.send(
            json.dumps({"event": "finish", "code": "success", "request_id": "stub-tts"})
        )
        return
    if mode == "timeout":
        time.sleep(2.0)
    connection.send(GOOD_PCM)
    connection.send(
        json.dumps({"event": "finish", "code": "success", "request_id": "stub-tts"})
    )


def _tts_process_request(connection, request):
    if STATE["tts"] == "http_401":
        return connection.respond(401, "unauthorized\n")
    if STATE["tts"] == "http_429":
        return connection.respond(429, "rate limited\n")
    return None


@pytest.fixture(scope="module")
def stub_server():
    STATE.update({"asr": "ok", "tts": "ok"})
    http = ThreadingHTTPServer(("127.0.0.1", 0), _AsrHandler)
    http_thread = threading.Thread(target=http.serve_forever, daemon=True)
    http_thread.start()

    ws_server = ws_serve(
        _tts_handler,
        "127.0.0.1",
        0,
        process_request=_tts_process_request,
        open_timeout=2,
    )
    ws_thread = threading.Thread(target=ws_server.serve_forever, daemon=True)
    ws_thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_address[1]}", ws_server.socket.getsockname()[1]
    finally:
        http.shutdown()
        http.server_close()
        ws_server.shutdown()


def _speech_settings(stub: tuple[str, int], *, tts: bool = False) -> Settings:
    base_url, ws_port = stub
    # ASR 走 HTTP 桩端口，TTS 由同一 base URL 推导 WS 端口；两个桩端口不同。
    base_url = f"http://127.0.0.1:{ws_port}" if tts else base_url
    return Settings(
        APP_ENV="test",
        DATABASE_URL=get_settings().database_url,
        PRIVATE_FILE_ROOT=str(get_settings().private_file_root),
        AI_PROVIDER="test",
        ALLOW_TEST_FIXTURES=True,
        SPEECH_PROVIDER="deepseek",
        SPEECH_BASE_URL=base_url,
        SPEECH_API_KEY="stub-key-not-a-secret",
        SPEECH_TIMEOUT_SECONDS=1,
        TTS_VOICE_ID="stella",
        MAX_JOB_ATTEMPTS=2,
        M1_SAMPLE_CATALOG=True,
    )


def _install_settings(monkeypatch, settings: Settings) -> None:
    import app.api.voice_routes as voice_routes

    monkeypatch.setattr(voice_routes, "get_settings", lambda: settings)


def _voice_session(client: TestClient, worker) -> tuple[str, str, str, dict]:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current, turn, _draft, receipt = _upload_current(client, token, session_id)
    return token, session_id, turn["turn_id"], receipt


def _transcription_job(client, token, receipt) -> dict:
    response = client.post(
        f"/v1/audio-assets/{receipt['audio']['id']}/transcriptions",
        json={"expected_voice_revision": receipt["voice_revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202, response.text
    return response.json()


def _job(client, token, job_id: str) -> dict:
    response = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def _transcript(client, token, asset_id: str) -> dict:
    response = client.get(f"/v1/audio-assets/{asset_id}/transcript", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_asr_401_marks_job_and_transcript_auth_failed(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server)
    _install_settings(monkeypatch, settings)
    token, _sid, _turn, receipt = _voice_session(client, worker)
    job = _transcription_job(client, token, receipt)
    STATE["asr"] = "401"
    speech_worker = JobWorker(settings)
    assert speech_worker.run_once() is True

    failed = _job(client, token, job["id"])
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "AI_AUTH_FAILED", failed
    assert failed["retryable"] is False, failed
    transcript = _transcript(client, token, receipt["audio"]["id"])
    assert transcript["status"] == "failed"
    assert transcript["error_code"] == "AI_AUTH_FAILED"

    retry = client.post(
        f"/v1/jobs/{job['id']}/retry",
        json={"expected_attempt": failed["attempt"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry.status_code == 409, retry.text

    with SessionLocal() as db:
        row = db.get(Job, __import__("uuid").UUID(job["id"]))
        assert row is not None
        assert row.ai_call_records[-1]["outcome"] == "auth_failed"


def test_asr_429_is_retryable_and_retry_succeeds_once(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server)
    _install_settings(monkeypatch, settings)
    STATE.update({"asr": "429", "tts": "ok"})
    token, _sid, _turn, receipt = _voice_session(client, worker)
    job = _transcription_job(client, token, receipt)
    speech_worker = JobWorker(settings)
    assert speech_worker.run_once() is True

    failed = _job(client, token, job["id"])
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "AI_RATE_LIMITED", failed
    assert failed["retryable"] is True, failed

    retry = client.post(
        f"/v1/jobs/{job['id']}/retry",
        json={"expected_attempt": failed["attempt"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["state"] == "queued"
    assert retry.json()["attempt"] == 2

    STATE["asr"] = "ok"
    assert speech_worker.run_once() is True
    succeeded = _job(client, token, job["id"])
    assert succeeded["state"] == "succeeded", succeeded
    transcript = _transcript(client, token, receipt["audio"]["id"])
    assert transcript["status"] == "ready"
    assert "虚构" in transcript["text"]

    # 不自动追加第三次外部调用。
    assert speech_worker.run_once() is False


def test_asr_timeout_is_unknown_and_retryable(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server)
    _install_settings(monkeypatch, settings)
    STATE.update({"asr": "timeout", "tts": "ok"})
    token, _sid, _turn, receipt = _voice_session(client, worker)
    job = _transcription_job(client, token, receipt)
    assert JobWorker(settings).run_once() is True
    failed = _job(client, token, job["id"])
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "AI_TIMEOUT", failed
    assert failed["retryable"] is True, failed
    STATE["asr"] = "ok"


def _opening_speech(client, token, session_id, key=None):
    return speech(client, token, session_id, segment="opening", key=key)


def _speech_asset(client, token, asset_id):
    response = client.get(f"/v1/speech-assets/{asset_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def test_tts_missing_finish_does_not_publish_and_explicit_replay_recovers(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server, tts=True)
    _install_settings(monkeypatch, settings)
    STATE.update({"asr": "ok", "tts": "no_finish"})
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]

    requested = _opening_speech(client, token, session_id)
    assert requested.status_code == 202, requested.text
    asset_id = requested.json()["id"]
    speech_worker = JobWorker(settings)
    assert speech_worker.run_once() is True

    failed = _speech_asset(client, token, asset_id)
    assert failed["status"] == "failed", failed
    assert failed["error_code"] in {"AI_UNAVAILABLE", "SPEECH_PROTOCOL_ERROR"}, failed
    content = client.get(f"/v1/speech-assets/{asset_id}/content", headers=auth(token))
    assert content.status_code == 409, content.text
    assert content.json()["error"]["code"] == "SPEECH_NOT_READY"
    with SessionLocal() as db:
        row = db.get(SpeechAsset, __import__("uuid").UUID(asset_id))
        assert row is not None and row.storage_path is None

    # 用户显式再次播放：复用原 asset/job 做受控重试，而不是把残片发布为 ready。
    STATE["tts"] = "ok"
    replay = _opening_speech(client, token, session_id)
    assert replay.status_code == 202, replay.text
    assert replay.json()["id"] == asset_id
    assert speech_worker.run_once() is True
    ready = _speech_asset(client, token, asset_id)
    assert ready["status"] == "ready", ready
    assert _job(client, token, ready["job_id"])["attempt"] == 2
    content = client.get(f"/v1/speech-assets/{asset_id}/content", headers=auth(token))
    assert content.status_code == 200
    assert hashlib.sha256(content.content).hexdigest() == ready["sha256"]
    assert content.content[44:] == GOOD_PCM


def test_tts_error_finish_and_odd_pcm_never_publish(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server, tts=True)
    _install_settings(monkeypatch, settings)
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    speech_worker = JobWorker(settings)

    STATE.update({"asr": "ok", "tts": "error_finish"})
    rejected = _opening_speech(client, token, session_id)
    assert rejected.status_code == 202, rejected.text
    assert speech_worker.run_once() is True
    rejected_asset = _speech_asset(client, token, rejected.json()["id"])
    assert rejected_asset["status"] == "failed"
    assert rejected_asset["error_code"] == "SPEECH_CONTENT_REJECTED"
    with SessionLocal() as db:
        row = db.get(SpeechAsset, __import__("uuid").UUID(rejected.json()["id"]))
        assert row is not None and row.storage_path is None

    # 另一个场次的 opening，避免复用同一缓存键。
    _prep2, plan2 = create_confirmed_plan(client, token, worker, count=2)
    started2 = start_voice_interview(client, token, plan2)
    STATE["tts"] = "odd_pcm"
    malformed = _opening_speech(client, token, started2["session_id"])
    assert malformed.status_code == 202, malformed.text
    assert speech_worker.run_once() is True
    malformed_asset = _speech_asset(client, token, malformed.json()["id"])
    assert malformed_asset["status"] == "failed"
    assert malformed_asset["error_code"] == "SPEECH_PROTOCOL_ERROR"
    with SessionLocal() as db:
        row = db.get(SpeechAsset, __import__("uuid").UUID(malformed.json()["id"]))
        assert row is not None and row.storage_path is None
    content = client.get(
        f"/v1/speech-assets/{malformed.json()['id']}/content", headers=auth(token)
    )
    assert content.status_code == 409


def test_tts_handshake_401_is_not_retryable(
    client: TestClient, worker, monkeypatch, stub_server
) -> None:
    settings = _speech_settings(stub_server, tts=True)
    _install_settings(monkeypatch, settings)
    STATE.update({"asr": "ok", "tts": "http_401"})
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    requested = _opening_speech(client, token, started["session_id"])
    assert requested.status_code == 202, requested.text
    assert JobWorker(settings).run_once() is True
    asset = _speech_asset(client, token, requested.json()["id"])
    assert asset["status"] == "failed"
    assert asset["error_code"] == "AI_AUTH_FAILED"
    assert _job(client, token, asset["job_id"])["retryable"] is False, asset
    STATE["tts"] = "ok"
