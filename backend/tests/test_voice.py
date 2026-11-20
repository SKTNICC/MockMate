from __future__ import annotations

import hashlib
import io
import wave
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.speech import TestSpeechProvider as SpeechStub
from app.core.errors import AppError
from app.models import AudioAsset, InterviewAnswer, InterviewSession, Job, VoiceDraft as VoiceDraftRow
from app.services import voice as voice_service
from tests.conftest import auth, issue_token, new_key, wait_job
from tests.m2_helpers import create_confirmed_plan, detail, finish
from tests.m3_helpers import (
    finish_voice_interview,
    pcm_answer,
    speech,
    start_voice_interview,
    submit_voice_answer,
    transcribe,
    upload_pcm,
    voice_draft,
)


def _upload_current(client, token, session_id):
    current = detail(client, token, session_id)
    turn = current["current_turn"]
    draft = voice_draft(client, token, session_id, turn["turn_id"])
    pcm = pcm_answer(1.0)
    response = upload_pcm(
        client,
        token,
        session_id,
        turn["turn_id"],
        session_revision=current["revision"],
        expected_voice_revision=draft["voice_revision"],
        pcm=pcm,
    )
    assert response.status_code == 201, response.text
    return current, turn, draft, response.json()


def test_voice_start_requires_audio_consent_and_reopens_original_mode(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)

    denied = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={
            "plan_revision": plan["revision"],
            "external_processing_consent": True,
            "mode": "voice",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert denied.status_code == 400, denied.text
    assert denied.json()["error"]["code"] == "AUDIO_CONSENT_REQUIRED"

    started = start_voice_interview(client, token, plan)
    assert started["mode"] == "voice"
    session_id = started["session_id"]
    body = detail(client, token, session_id)
    assert body["mode"] == "voice"
    assert "语音" in body["opening_text"]
    assert body["current_turn"]["kind"] == "main"
    history = client.get("/v1/interviews", headers=auth(token)).json()
    assert history["items"][0]["mode"] == "voice"

    # 同计划再次请求 text 只读打开原场，不转换模式、不复制场次。
    reopened = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()["session_id"] == session_id
    assert reopened.json()["mode"] == "voice"


def test_audio_upload_transcription_source_submit_and_report(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current, turn, draft, receipt = _upload_current(client, token, session_id)
    assert draft["voice_revision"] == 0 and draft["active_audio"] is None
    assert receipt["voice_revision"] == 1
    asset = receipt["audio"]
    assert asset["byte_length"] == 32000
    assert asset["duration_ms"] == 1000
    assert asset["format"] == "pcm_s16le/16000/mono"

    pending = client.get(
        f"/v1/audio-assets/{asset['id']}/transcript", headers=auth(token)
    )
    assert pending.status_code == 404

    transcript = transcribe(
        client,
        token,
        asset["id"],
        expected_voice_revision=1,
        worker=worker,
    )
    assert transcript["status"] == "ready"
    assert transcript["text"].strip()
    assert transcript["revision"] == 1

    answer = submit_voice_answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=turn["turn_id"],
        text=transcript["text"],
        asset_id=asset["id"],
        transcript_revision=1,
    )
    assert answer.status_code == 202, answer.text
    job = wait_job(client, token, answer.json()["job_id"], worker)
    assert job["state"] == "succeeded", job

    after = detail(client, token, session_id)
    answered = next(t for t in after["transcript"] if t["turn_id"] == turn["turn_id"])
    assert answered["answer"]["input_source"] == "asr"
    assert answered["answer"]["audio_asset_id"] == asset["id"]
    assert answered["answer"]["transcript_revision"] == 1

    ended = finish_voice_interview(client, token, worker, session_id)
    report_job = ended["report_job"]
    assert report_job is not None
    report_job = wait_job(client, token, report_job["id"], worker, attempts=10)
    assert report_job["state"] == "succeeded", report_job
    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    sources = {
        turn["answer"]["input_source"]
        for q in report.json()["questions"]
        for turn in q["turns"]
        if turn["answer"]
    }
    assert "asr" in sources
    assert "asr_corrected" in sources


def test_upload_rejects_wrong_format_limits_and_revision_conflicts(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current = detail(client, token, session_id)
    turn = current["current_turn"]

    odd = b"\x00" * 1001
    bad = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0, pcm=odd,
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["error"]["code"] == "AUDIO_FORMAT_INVALID"

    wav_header = b"RIFF" + b"\x00" * 64000
    fake = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0, pcm=wav_header,
    )
    assert fake.status_code == 400, fake.text
    assert fake.json()["error"]["code"] == "AUDIO_FORMAT_INVALID"

    too_large = pcm_answer(120.0) + b"\x00\x00"
    large = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0, pcm=too_large,
    )
    assert large.status_code == 413, large.text

    first = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0, pcm=pcm_answer(1),
    )
    assert first.status_code == 201, first.text
    conflict = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0, pcm=pcm_answer(1.2),
    )
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["error"]["code"] == "VOICE_REVISION_CONFLICT"

    other_user, other_token = issue_token(client)
    denied = client.get(
        f"/v1/interviews/{session_id}/turns/{turn['turn_id']}/voice-draft",
        headers=auth(other_token),
    )
    assert denied.status_code == 404
    denied_asset = client.get(
        f"/v1/audio-assets/{first.json()['audio']['id']}/transcript",
        headers=auth(other_token),
    )
    assert denied_asset.status_code == 404


def test_new_take_supersedes_old_asr_job_and_old_result_cannot_replace_draft(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current, turn, _draft, first = _upload_current(client, token, session_id)
    job_response = client.post(
        f"/v1/audio-assets/{first['audio']['id']}/transcriptions",
        json={"expected_voice_revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert job_response.status_code == 202, job_response.text
    old_job_id = job_response.json()["id"]

    second_pcm = pcm_answer(1.5, frequency=330)
    second = upload_pcm(
        client,
        token,
        session_id,
        turn["turn_id"],
        session_revision=current["revision"],
        expected_voice_revision=1,
        pcm=second_pcm,
    )
    assert second.status_code == 201, second.text
    assert second.json()["voice_revision"] == 2

    old_job = client.get(f"/v1/jobs/{old_job_id}", headers=auth(token)).json()
    assert old_job["state"] == "superseded"

    draft = voice_draft(client, token, session_id, turn["turn_id"])
    assert draft["voice_revision"] == 2
    assert draft["active_audio"]["id"] == second.json()["audio"]["id"]
    assert draft["transcript"] is None

    # 旧 job 已失效；即使再次 run worker 也不会写入当前草稿。
    worker.run_once()
    draft_after = voice_draft(client, token, session_id, turn["turn_id"])
    assert draft_after["active_audio"]["id"] == second.json()["audio"]["id"]
    assert draft_after["transcript"] is None


def test_answer_source_validation_and_text_fallback(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current, turn, _draft, receipt = _upload_current(client, token, session_id)
    asset_id = receipt["audio"]["id"]
    transcript = transcribe(
        client, token, asset_id, expected_voice_revision=1, worker=worker
    )

    wrong_text = submit_voice_answer(
        client, token, session_id, revision=current["revision"],
        turn_id=turn["turn_id"], text="与原始转写不一致", asset_id=asset_id,
        transcript_revision=1, source="asr",
    )
    assert wrong_text.status_code == 400
    assert wrong_text.json()["error"]["code"] == "ANSWER_SOURCE_INVALID"

    wrong_revision = submit_voice_answer(
        client, token, session_id, revision=current["revision"],
        turn_id=turn["turn_id"], text=transcript["text"], asset_id=asset_id,
        transcript_revision=2, source="asr",
    )
    assert wrong_revision.status_code == 409
    assert wrong_revision.json()["error"]["code"] == "TRANSCRIPT_REVISION_CONFLICT"

    text_with_asset = client.post(
        f"/v1/interviews/{session_id}/answers",
        json={
            "revision": current["revision"], "turn_id": turn["turn_id"],
            "text": "文字回答", "input_source": "text",
            "audio_asset_id": asset_id, "transcript_revision": 1,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert text_with_asset.status_code == 400
    assert text_with_asset.json()["error"]["code"] == "ANSWER_SOURCE_INVALID"

    fallback = client.post(
        f"/v1/interviews/{session_id}/answers",
        json={
            "revision": current["revision"], "turn_id": turn["turn_id"],
            "text": "改用文字回答：先检查键，再决定更新还是插入。",
            "input_source": "text",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert fallback.status_code == 202, fallback.text
    after = detail(client, token, session_id)
    answered = next(t for t in after["transcript"] if t["turn_id"] == turn["turn_id"])
    assert answered["answer"]["input_source"] == "text"
    assert answered["answer"]["audio_asset_id"] is None


def test_speech_cache_segments_ownership_and_content(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current = detail(client, token, session_id)
    turn = current["current_turn"]

    opening = speech(client, token, session_id, segment="opening")
    assert opening.status_code == 202, opening.text
    asset_id = opening.json()["id"]
    assert opening.json()["synthetic"] is True
    again = speech(client, token, session_id, segment="opening")
    assert again.json()["id"] == asset_id
    same_job = opening.json()["job_id"]
    assert again.json()["job_id"] == same_job

    job = wait_job(client, token, same_job, worker)
    assert job["state"] == "succeeded", job
    ready = client.get(f"/v1/speech-assets/{asset_id}", headers=auth(token))
    assert ready.status_code == 200
    assert ready.json()["status"] == "ready"
    content = client.get(f"/v1/speech-assets/{asset_id}/content", headers=auth(token))
    assert content.status_code == 200
    assert content.headers["content-type"].startswith("audio/wav")
    assert content.content[:4] == b"RIFF"
    with wave.open(io.BytesIO(content.content), "rb") as wav:
        assert wav.getframerate() == 24000 and wav.getnchannels() == 1
        assert wav.getnframes() > 0

    cached = speech(client, token, session_id, segment="opening")
    assert cached.status_code == 200
    assert cached.json()["id"] == asset_id
    assert len(
        db.scalars(
            select(Job).where(
                Job.kind == "audio_tts",
                Job.target_id == __import__("uuid").UUID(asset_id),
            )
        ).all()
    ) == 1

    turn_speech = speech(
        client, token, session_id, segment="turn", turn_id=turn["turn_id"]
    )
    assert turn_speech.status_code == 202, turn_speech.text
    wait_job(client, token, turn_speech.json()["job_id"], worker)

    closing = speech(client, token, session_id, segment="closing")
    assert closing.status_code == 409

    future = speech(
        client, token, session_id, segment="turn", turn_id="00000000-0000-4000-8000-000000000001"
    )
    assert future.status_code == 404

    other_user, other_token = issue_token(client)
    assert client.get(
        f"/v1/speech-assets/{asset_id}", headers=auth(other_token)
    ).status_code == 404
    assert client.get(
        f"/v1/speech-assets/{asset_id}/content", headers=auth(other_token)
    ).status_code == 404


def test_text_session_has_no_voice_draft_or_speech(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert started.status_code == 201, started.text
    assert started.json()["mode"] == "text"
    session_id = started.json()["session_id"]
    turn = detail(client, token, session_id)["current_turn"]
    draft = client.get(
        f"/v1/interviews/{session_id}/turns/{turn['turn_id']}/voice-draft",
        headers=auth(token),
    )
    assert draft.status_code == 400
    assert draft.json()["error"]["code"] == "INTERVIEW_MODE_TEXT"
    spoken = speech(client, token, session_id, segment="opening")
    assert spoken.status_code == 400


def test_asr_empty_does_not_submit_and_failure_is_explicit(
    client: TestClient, worker, db, monkeypatch
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current = detail(client, token, session_id)
    turn = current["current_turn"]

    short = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=0,
        pcm=b"\x00\x00" * 500,
    )
    assert short.status_code == 201, short.text
    empty = transcribe(
        client, token, short.json()["audio"]["id"], expected_voice_revision=1, worker=worker
    )
    assert empty["status"] == "empty"
    rejected = submit_voice_answer(
        client, token, session_id, revision=current["revision"],
        turn_id=turn["turn_id"], text="", asset_id=short.json()["audio"]["id"],
        transcript_revision=1,
    )
    assert rejected.status_code >= 400
    still = detail(client, token, session_id)
    assert still["phase"] == "awaiting_answer" and still["current_turn"] is not None

    second = upload_pcm(
        client, token, session_id, turn["turn_id"],
        session_revision=current["revision"], expected_voice_revision=1,
        pcm=pcm_answer(1.0, frequency=440),
    )
    assert second.status_code == 201, second.text

    def boom(self, wav_bytes):
        raise AppError("AI_TIMEOUT", "语音识别超时，结果未知", retryable=True)

    monkeypatch.setattr(SpeechStub, "transcribe", boom)
    job_response = client.post(
        f"/v1/audio-assets/{second.json()['audio']['id']}/transcriptions",
        json={"expected_voice_revision": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert job_response.status_code == 202, job_response.text
    job = wait_job(client, token, job_response.json()["id"], worker)
    assert job["state"] == "failed", job
    failed = client.get(
        f"/v1/audio-assets/{second.json()['audio']['id']}/transcript",
        headers=auth(token),
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "AI_TIMEOUT"
    assert not db.scalars(
        select(InterviewAnswer).where(
            InterviewAnswer.turn_id == __import__("uuid").UUID(turn["turn_id"])
        )
    ).all()


def test_cleanup_keeps_active_take_and_expires_unreferenced_audio(
    client: TestClient, worker, db, settings
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    current, turn, _draft, active = _upload_current(client, token, session_id)
    active_asset = db.get(AudioAsset, active["audio"]["id"])
    assert active_asset is not None and active_asset.storage_path
    active_path = settings.private_file_root / active_asset.storage_path

    # 另造一条没有活跃草稿引用的过期资产，验证只清理它。
    old = AudioAsset(
        id=__import__("uuid").uuid4(),
        owner_id=active_asset.owner_id,
        session_id=active_asset.session_id,
        turn_id=active_asset.turn_id,
        take_id=__import__("uuid").uuid4(),
        voice_revision=99,
        sha256="d" * 64,
        byte_length=32000,
        duration_ms=1000,
        format=voice_service.VOICE_FORMAT,
        storage_path="voice/orphan.wav",
        status="stored",
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    db.add(old)
    db.flush()
    answer = InterviewAnswer(
        id=__import__("uuid").uuid4(),
        turn_id=active_asset.turn_id,
        text="过期音频仍保留的已确认文字答案。",
        input_source="asr",
        audio_asset_id=old.id,
        transcript_revision=1,
    )
    db.add(answer)
    db.flush()
    orphan_path = settings.private_file_root / old.storage_path
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_bytes(b"orphan")
    db.commit()

    db.expire_all()
    result = voice_service.cleanup_expired(db, settings)
    db.commit()
    assert result["expired_assets"] >= 1
    assert not orphan_path.exists()
    assert active_path.exists()
    assert db.get(AudioAsset, active_asset.id).status == "stored"
    assert db.get(InterviewAnswer, answer.id).text == "过期音频仍保留的已确认文字答案。"
