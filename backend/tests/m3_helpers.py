from __future__ import annotations

import hashlib
import math
import struct

from fastapi.testclient import TestClient

from tests.conftest import auth, new_key, wait_job


def pcm_answer(seconds: float = 1.0, *, frequency: int = 220) -> bytes:
    sample_rate = 16000
    samples = int(sample_rate * seconds)
    return b"".join(
        struct.pack("<h", int(2200 * math.sin(2 * math.pi * frequency * i / sample_rate)))
        for i in range(samples)
    )


def start_voice_interview(client: TestClient, token: str, plan: dict) -> dict:
    response = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={
            "plan_revision": plan["revision"],
            "external_processing_consent": True,
            "mode": "voice",
            "audio_processing_consent": True,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def upload_pcm(
    client: TestClient,
    token: str,
    session_id: str,
    turn_id: str,
    *,
    session_revision: int,
    expected_voice_revision: int,
    pcm: bytes,
    take_id: str | None = None,
    key: str | None = None,
):
    return client.post(
        f"/v1/interviews/{session_id}/turns/{turn_id}/audio",
        data={
            "take_id": take_id or new_key(),
            "session_revision": str(session_revision),
            "expected_voice_revision": str(expected_voice_revision),
            "sha256": hashlib.sha256(pcm).hexdigest(),
            "format": "pcm_s16le/16000/mono",
        },
        files={"file": ("answer.pcm", pcm, "application/octet-stream")},
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )


def voice_draft(client: TestClient, token: str, session_id: str, turn_id: str) -> dict:
    response = client.get(
        f"/v1/interviews/{session_id}/turns/{turn_id}/voice-draft", headers=auth(token)
    )
    assert response.status_code == 200, response.text
    return response.json()


def transcribe(
    client: TestClient, token: str, asset_id: str, *, expected_voice_revision: int, worker,
    attempts: int = 8,
) -> dict:
    response = client.post(
        f"/v1/audio-assets/{asset_id}/transcriptions",
        json={"expected_voice_revision": expected_voice_revision},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202, response.text
    job = response.json()
    job = wait_job(client, token, job["id"], worker, attempts=attempts)
    assert job["state"] == "succeeded", job
    transcript = client.get(
        f"/v1/audio-assets/{asset_id}/transcript", headers=auth(token)
    )
    assert transcript.status_code == 200, transcript.text
    return transcript.json()


def submit_voice_answer(
    client: TestClient,
    token: str,
    session_id: str,
    *,
    revision: int,
    turn_id: str,
    text: str,
    asset_id: str,
    transcript_revision: int,
    source: str = "asr",
    key: str | None = None,
):
    return client.post(
        f"/v1/interviews/{session_id}/answers",
        json={
            "revision": revision,
            "turn_id": turn_id,
            "text": text,
            "input_source": source,
            "audio_asset_id": asset_id,
            "transcript_revision": transcript_revision,
        },
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )


def speech(
    client: TestClient,
    token: str,
    session_id: str,
    *,
    segment: str,
    turn_id: str | None = None,
    key: str | None = None,
):
    payload = {"segment": segment}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return client.post(
        f"/v1/interviews/{session_id}/speech",
        json=payload,
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )


def finish_voice_interview(
    client: TestClient, token: str, worker, session_id: str, *, corrected_alternate: bool = True
) -> dict:
    for index in range(16):
        detail = client.get(f"/v1/interviews/{session_id}", headers=auth(token)).json()
        if detail["status"] == "ended":
            return detail
        turn = detail["current_turn"]
        assert turn is not None, detail
        pcm = pcm_answer(1.0 + (index % 3) * 0.2, frequency=220 + index * 20)
        uploaded = upload_pcm(
            client,
            token,
            session_id,
            turn["turn_id"],
            session_revision=detail["revision"],
            expected_voice_revision=voice_draft(
                client, token, session_id, turn["turn_id"]
            )["voice_revision"],
            pcm=pcm,
        )
        assert uploaded.status_code in (200, 201), uploaded.text
        receipt = uploaded.json()
        transcript = transcribe(
            client,
            token,
            receipt["audio"]["id"],
            expected_voice_revision=receipt["voice_revision"],
            worker=worker,
        )
        assert transcript["status"] in ("ready", "empty"), transcript
        source = "asr_corrected" if corrected_alternate and index % 2 else "asr"
        text = transcript["text"]
        if source == "asr_corrected":
            text = text + "（核对时补充了适用边界。）"
        response = submit_voice_answer(
            client,
            token,
            session_id,
            revision=detail["revision"],
            turn_id=turn["turn_id"],
            text=text,
            asset_id=receipt["audio"]["id"],
            transcript_revision=transcript["revision"],
            source=source,
        )
        assert response.status_code in (200, 202), response.text
        if response.status_code == 202:
            job = wait_job(client, token, response.json()["job_id"], worker, attempts=8)
            assert job["state"] == "succeeded", job
    raise AssertionError("语音面试未在预期回合内结束")
