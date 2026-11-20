"""T04 D01/D02：服务端完成写入但 2xx 回执丢失，原 key/body 重放只保留一份资源。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import func, select

import app.main as app_main
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.models import AudioAsset, InterviewAnswer, InterviewSession, Job, VoiceDraft
from tests.conftest import auth, issue_token, new_key
from tests.m2_helpers import create_confirmed_plan
from tests.m3_helpers import (
    pcm_answer,
    start_voice_interview,
    submit_voice_answer,
    transcribe,
    upload_pcm,
    voice_draft,
)


def _drop_settings(path: str) -> Settings:
    current = get_settings()
    return Settings(
        APP_ENV="test",
        DATABASE_URL=current.database_url,
        PRIVATE_FILE_ROOT=str(current.private_file_root),
        AI_PROVIDER="test",
        ALLOW_TEST_FIXTURES=True,
        SPEECH_PROVIDER="test",
        M1_SAMPLE_CATALOG=True,
        MAX_JOB_ATTEMPTS=2,
        M1_TEST_DROP_RECEIPT_PATH=path,
    )


def _install_drop(monkeypatch, path: str) -> None:
    monkeypatch.setattr(app_main, "get_settings", lambda: _drop_settings(path))
    monkeypatch.setattr(app_main, "_DROPPED_ONCE", False)


def test_upload_201_lost_replays_same_asset_once(
    client: TestClient, worker, monkeypatch
) -> None:
    _install_drop(monkeypatch, "/audio")
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    detail = client.get(f"/v1/interviews/{session_id}", headers=auth(token)).json()
    turn_id = detail["current_turn"]["turn_id"]
    draft = voice_draft(client, token, session_id, turn_id)
    pcm = pcm_answer(1.0)
    take_id = str(uuid.uuid4())
    key = new_key()

    first = upload_pcm(
        client,
        token,
        session_id,
        turn_id,
        session_revision=detail["revision"],
        expected_voice_revision=draft["voice_revision"],
        pcm=pcm,
        take_id=take_id,
        key=key,
    )
    assert first.status_code == 201, first.text
    assert first.content == b"", "测试中间件应丢弃 2xx 正文"

    replay = upload_pcm(
        client,
        token,
        session_id,
        turn_id,
        session_revision=detail["revision"],
        expected_voice_revision=draft["voice_revision"],
        pcm=pcm,
        take_id=take_id,
        key=key,
    )
    assert replay.status_code in (200, 201), replay.text
    receipt = replay.json()
    assert receipt["audio"]["take_id"] == take_id

    with SessionLocal() as db:
        assets = db.scalars(
            select(AudioAsset).where(AudioAsset.turn_id == uuid.UUID(turn_id))
        ).all()
        assert len(assets) == 1
        assert str(assets[0].id) == receipt["audio"]["id"]
        voice = db.scalars(
            select(VoiceDraft).where(VoiceDraft.turn_id == uuid.UUID(turn_id))
        ).one()
        assert str(voice.active_take_id) == receipt["audio"]["id"]
        assert voice.voice_revision == 1


def test_answer_2xx_lost_replays_same_answer_without_second_advance(
    client: TestClient, worker, monkeypatch
) -> None:
    _install_drop(monkeypatch, "/answers")
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    started = start_voice_interview(client, token, plan)
    session_id = started["session_id"]
    detail = client.get(f"/v1/interviews/{session_id}", headers=auth(token)).json()
    turn_id = detail["current_turn"]["turn_id"]
    draft = voice_draft(client, token, session_id, turn_id)
    pcm = pcm_answer(1.0)
    uploaded = upload_pcm(
        client,
        token,
        session_id,
        turn_id,
        session_revision=detail["revision"],
        expected_voice_revision=draft["voice_revision"],
        pcm=pcm,
    )
    assert uploaded.status_code == 201, uploaded.text
    receipt = uploaded.json()
    transcript = transcribe(
        client,
        token,
        receipt["audio"]["id"],
        expected_voice_revision=receipt["voice_revision"],
        worker=worker,
    )
    assert transcript["status"] == "ready"

    before_revision = detail["revision"]
    key = new_key()
    first = submit_voice_answer(
        client,
        token,
        session_id,
        revision=before_revision,
        turn_id=turn_id,
        text=transcript["text"],
        asset_id=receipt["audio"]["id"],
        transcript_revision=transcript["revision"],
        key=key,
    )
    assert first.status_code in (200, 201, 202), first.text
    assert first.content == b"", "测试中间件应丢弃 2xx 正文"

    replay = submit_voice_answer(
        client,
        token,
        session_id,
        revision=before_revision,
        turn_id=turn_id,
        text=transcript["text"],
        asset_id=receipt["audio"]["id"],
        transcript_revision=transcript["revision"],
        key=key,
    )
    assert replay.status_code in (200, 201, 202), replay.text

    with SessionLocal() as db:
        answers = db.scalars(
            select(InterviewAnswer).where(InterviewAnswer.turn_id == uuid.UUID(turn_id))
        ).all()
        assert len(answers) == 1
        session = db.get(InterviewSession, uuid.UUID(session_id))
        assert session is not None
        assert session.revision == before_revision + 1
        follow_jobs = db.scalar(
            select(func.count()).select_from(Job).where(
                Job.kind == "follow_up",
                Job.owner_id == session.owner_id,
                Job.target_id == session.id,
            )
        )
        assert follow_jobs == 1
