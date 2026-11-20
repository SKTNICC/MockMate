"""M3 语音资产、转写草稿与受控播报。

服务端独立执行：固定 PCM 格式/时长/字节上限、会话与回合归属、take CAS、转写来源与
答案冻结、TTS 允许文本与缓存键。文件只写私有目录，数据库与日志不保存音频字节。
"""

from __future__ import annotations

import hashlib
import io
import os
import uuid
import wave
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.errors import AppError
from app.models import (
    AudioAsset,
    AudioTranscript,
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
    InterviewTurn,
    Job,
    SpeechAsset,
    VoiceDraft as VoiceDraftRow,
)
from app.schemas import (
    InterviewJobSummary,
    SpeechAsset as SpeechAssetSchema,
    VoiceAudioAsset,
    VoiceConfig,
    VoiceDraft,
    VoiceTranscript,
)
from app.services import jobs as jobs_service

VOICE_FORMAT = "pcm_s16le/16000/mono"
TTS_FORMAT = "pcm_s16le/24000/mono"
AUDIO_CONSENT_VERSION = "m3-voice-processing-v1"
MAX_AUDIO_BYTES = 3_840_000  # 120s × 16000Hz × 16bit × mono
PCM_BYTES_PER_MS = 32
TRANSCRIPT_REVISION = 1


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def voice_config(settings: Settings) -> VoiceConfig:
    return VoiceConfig(
        available=settings.speech_available,
        max_answer_seconds=min(settings.voice_max_answer_seconds, 120),
        format=VOICE_FORMAT,
        tts_voice_id=settings.tts_voice_id,
        speech_provider=settings.speech_provider,
    )


# --------------------------------------------------------------------------- 查询辅助


def _session_for_owner(
    db: Session, owner_id: uuid.UUID, session_id: uuid.UUID, *, lock: bool = False
) -> InterviewSession:
    stmt = select(InterviewSession).where(
        InterviewSession.id == session_id, InterviewSession.owner_id == owner_id
    )
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    row = db.scalars(stmt).one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "面试不存在", status_code=404)
    return row


def _turn_for_session(
    db: Session, session: InterviewSession, turn_id: uuid.UUID, *, lock: bool = False
) -> tuple[InterviewTurn, InterviewQuestion]:
    stmt = (
        select(InterviewTurn, InterviewQuestion)
        .join(InterviewQuestion, InterviewTurn.question_id == InterviewQuestion.id)
        .where(
            InterviewTurn.id == turn_id,
            InterviewQuestion.session_id == session.id,
        )
    )
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    row = db.execute(stmt).one_or_none()
    if row is None:
        raise AppError("NOT_FOUND", "回合不存在", status_code=404)
    turn, question = row
    return turn, question


def _require_voice_session(session: InterviewSession) -> None:
    if session.mode != "voice":
        raise AppError("INTERVIEW_MODE_TEXT", "该场是文字面试，没有语音草稿")


def _active_draft(
    db: Session, turn_id: uuid.UUID, *, lock: bool = False
) -> VoiceDraftRow | None:
    stmt = select(VoiceDraftRow).where(VoiceDraftRow.turn_id == turn_id)
    if lock:
        stmt = stmt.execution_options(populate_existing=True).with_for_update()
    return db.scalars(stmt).one_or_none()


def _audio_for_draft(db: Session, draft: VoiceDraftRow | None) -> AudioAsset | None:
    if draft is None or draft.active_take_id is None:
        return None
    asset = db.get(AudioAsset, draft.active_take_id)
    if asset is None:
        return None
    return asset


def _asset_dto(asset: AudioAsset) -> VoiceAudioAsset:
    return VoiceAudioAsset(
        id=asset.id,
        session_id=asset.session_id,
        turn_id=asset.turn_id,
        take_id=asset.take_id,
        sha256=asset.sha256,
        byte_length=asset.byte_length,
        duration_ms=asset.duration_ms,
        format=VOICE_FORMAT,
        status="stored" if asset.status == "stored" else "expired",
        expires_at=asset.expires_at,
        created_at=asset.created_at,
    )


def _latest_transcript(db: Session, asset_id: uuid.UUID) -> AudioTranscript | None:
    return db.scalars(
        select(AudioTranscript)
        .where(AudioTranscript.audio_asset_id == asset_id)
        .order_by(AudioTranscript.revision.desc())
        .limit(1)
    ).one_or_none()


def _asr_job(db: Session, asset: AudioAsset, voice_revision: int) -> Job | None:
    return db.scalars(
        select(Job).where(
            Job.kind == "audio_asr",
            Job.target_id == asset.id,
            Job.input_revision == voice_revision,
        )
    ).one_or_none()


def _transcript_dto(
    asset: AudioAsset, row: AudioTranscript | None, job: Job | None
) -> VoiceTranscript | None:
    if row is not None:
        return VoiceTranscript(
            audio_asset_id=asset.id,
            revision=row.revision,
            status=row.status,
            text=row.text,
            provider=row.provider,
            request_id=row.request_id,
            error_code=row.error_code,
            updated_at=row.updated_at,
        )
    if job is None:
        return None
    if job.state in ("failed", "interrupted"):
        return VoiceTranscript(
            audio_asset_id=asset.id,
            revision=TRANSCRIPT_REVISION,
            status="failed",
            text=None,
            provider=None,
            request_id=None,
            error_code=job.error_code or "AI_UNAVAILABLE",
            updated_at=job.updated_at,
        )
    return VoiceTranscript(
        audio_asset_id=asset.id,
        revision=TRANSCRIPT_REVISION,
        status="processing",
        text=None,
        provider=None,
        request_id=None,
        error_code=None,
        updated_at=job.updated_at,
    )


def get_voice_draft(
    db: Session, *, owner_id: uuid.UUID, session_id: uuid.UUID, turn_id: uuid.UUID
) -> VoiceDraft:
    session = _session_for_owner(db, owner_id, session_id)
    _require_voice_session(session)
    _turn_for_session(db, session, turn_id)
    draft = _active_draft(db, turn_id)
    asset = _audio_for_draft(db, draft)
    transcript = None
    job = None
    if asset is not None:
        transcript = _transcript_dto(asset, _latest_transcript(db, asset.id), None)
        job_row = _asr_job(db, asset, asset.voice_revision)
        if transcript is None:
            transcript = _transcript_dto(asset, None, job_row)
        if job_row is not None:
            job = InterviewJobSummary(
                id=job_row.id,
                state=job_row.state,
                attempt=job_row.attempt,
                retryable=jobs_service.is_retryable(db, job_row, 2),
                error_code=job_row.error_code,
            )
    return VoiceDraft(
        session_id=session.id,
        turn_id=turn_id,
        voice_revision=draft.voice_revision if draft is not None else 0,
        active_audio=_asset_dto(asset) if asset is not None else None,
        transcript=transcript,
        asr_job=job,
    )


# --------------------------------------------------------------------------- 文件封装


def _wav_bytes(pcm: bytes, sample_rate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    data = buffer.getvalue()
    with wave.open(io.BytesIO(data), "rb") as check:
        if (
            check.getnchannels() != 1
            or check.getsampwidth() != 2
            or check.getframerate() != sample_rate
            or check.getnframes() * 2 != len(pcm)
        ):
            raise AppError("SPEECH_PROTOCOL_ERROR", "WAV 封装回读校验失败")
    return data


def _write_private(root, relative: str, data: bytes) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".part-{uuid.uuid4().hex}"
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _safe_read(root, relative: str | None) -> bytes:
    if not relative:
        raise AppError("AUDIO_FILE_UNAVAILABLE", "音频文件不可用", status_code=404)
    base = root.resolve()
    path = (base / relative).resolve()
    if not str(path).startswith(str(base)) or not path.exists():
        raise AppError("AUDIO_FILE_UNAVAILABLE", "音频文件不可用", status_code=404)
    return path.read_bytes()


def _delete_private(root, relative: str | None) -> None:
    if not relative:
        return
    base = root.resolve()
    path = (base / relative).resolve()
    if str(path).startswith(str(base)):
        path.unlink(missing_ok=True)


def _supersede_audio_jobs(db: Session, turn_id: uuid.UUID) -> None:
    asset_ids = list(
        db.scalars(select(AudioAsset.id).where(AudioAsset.turn_id == turn_id)).all()
    )
    if not asset_ids:
        return
    for job in db.scalars(
        select(Job).where(
            Job.kind == "audio_asr",
            Job.target_id.in_(asset_ids),
            Job.state.in_(("queued", "running")),
        )
    ).all():
        jobs_service.supersede_job(job)


# --------------------------------------------------------------------------- 上传与草稿 CAS


def store_audio(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    turn_id: uuid.UUID,
    session_revision: int,
    expected_voice_revision: int,
    take_id: uuid.UUID,
    declared_sha256: str,
    declared_format: str,
    pcm: bytes,
    settings: Settings,
) -> tuple[AudioAsset, int, bool]:
    """返回 (asset, voice_revision, created)。同一 take 同摘要可重放，不新建资产。"""
    if declared_format != VOICE_FORMAT:
        raise AppError("AUDIO_FORMAT_INVALID", "只接受 pcm_s16le/16000/mono")
    if not pcm:
        raise AppError("AUDIO_FORMAT_INVALID", "录音为空")
    if len(pcm) % 2 != 0:
        raise AppError("AUDIO_FORMAT_INVALID", "PCM 样本不完整（字节数必须为偶数）")
    if len(pcm) > MAX_AUDIO_BYTES:
        raise AppError("AUDIO_TOO_LARGE", "录音超过 120 秒上限", status_code=413)
    if pcm[:4] in (b"RIFF", b"OggS"):
        raise AppError("AUDIO_FORMAT_INVALID", "请上传原始 PCM，不要上传 WAV/Ogg 容器")
    actual_sha = hashlib.sha256(pcm).hexdigest()
    if actual_sha != declared_sha256:
        raise AppError("AUDIO_HASH_MISMATCH", "音频摘要与服务端计算不一致")
    duration_ms = max(1, len(pcm) // PCM_BYTES_PER_MS)
    if duration_ms > 120_000:
        raise AppError("AUDIO_TOO_LARGE", "录音超过 120 秒上限", status_code=413)

    session = _session_for_owner(db, owner_id, session_id, lock=True)
    _require_voice_session(session)
    if session.revision != session_revision:
        raise AppError(
            "REVISION_CONFLICT",
            "面试版本已变化，请刷新后重试",
            status_code=409,
            details=[f"current_revision={session.revision}"],
        )
    if session.status != "active" or session.phase != "awaiting_answer":
        raise AppError("INTERVIEW_STATE_INVALID", "当前状态不能上传录音", status_code=409)
    turn, _question = _turn_for_session(db, session, turn_id, lock=True)
    if session.current_turn_id != turn_id or turn.status != "pending":
        raise AppError("TURN_NOT_CURRENT", "该回合已不是当前待作答回合，请刷新", status_code=409)

    existing = db.scalars(
        select(AudioAsset)
        .where(AudioAsset.owner_id == owner_id, AudioAsset.take_id == take_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    draft = _active_draft(db, turn_id, lock=True)
    if existing is not None:
        if (
            existing.sha256 != actual_sha
            or existing.session_id != session_id
            or existing.turn_id != turn_id
        ):
            raise AppError("AUDIO_TAKE_CONFLICT", "同一 take 已存在不同音频或不属于本回合", status_code=409)
        if draft is not None and draft.active_take_id == existing.id:
            return existing, draft.voice_revision, False
        raise AppError("AUDIO_TAKE_CONFLICT", "该 take 已上传但已不是当前录音，请重新录制", status_code=409)

    current_revision = draft.voice_revision if draft is not None else 0
    if current_revision != expected_voice_revision:
        raise AppError(
            "VOICE_REVISION_CONFLICT",
            "语音草稿版本已变化，请刷新后重试",
            status_code=409,
            details=[f"current_voice_revision={current_revision}"],
        )
    next_revision = current_revision + 1
    wav = _wav_bytes(pcm, settings.voice_sample_rate)
    relative = (
        f"voice/{owner_id.hex}/{session_id.hex}/{turn_id.hex}/"
        f"{take_id.hex}-r{next_revision}.wav"
    )
    _write_private(settings.private_file_root, relative, wav)
    try:
        asset = AudioAsset(
            id=uuid.uuid4(),
            owner_id=owner_id,
            session_id=session_id,
            turn_id=turn_id,
            take_id=take_id,
            voice_revision=next_revision,
            sha256=actual_sha,
            byte_length=len(pcm),
            duration_ms=duration_ms,
            format=VOICE_FORMAT,
            storage_path=relative,
            status="stored",
            expires_at=utcnow() + timedelta(hours=settings.audio_retention_hours),
        )
        db.add(asset)
        if draft is None:
            draft = VoiceDraftRow(
                id=uuid.uuid4(),
                owner_id=owner_id,
                session_id=session_id,
                turn_id=turn_id,
                voice_revision=next_revision,
                active_take_id=asset.id,
            )
            db.add(draft)
        else:
            draft.voice_revision = next_revision
            draft.active_take_id = asset.id
        db.flush()
        _supersede_audio_jobs(db, turn_id)
        return asset, next_revision, True
    except Exception:
        _delete_private(settings.private_file_root, relative)
        raise


# --------------------------------------------------------------------------- 转写任务


def _require_active_asset(
    db: Session,
    *,
    owner_id: uuid.UUID,
    asset_id: uuid.UUID,
    expected_voice_revision: int | None = None,
) -> tuple[InterviewSession, InterviewTurn, VoiceDraftRow, AudioAsset]:
    asset_probe = db.get(AudioAsset, asset_id)
    if asset_probe is None or asset_probe.owner_id != owner_id:
        raise AppError("NOT_FOUND", "录音不存在", status_code=404)
    session = _session_for_owner(db, owner_id, asset_probe.session_id, lock=True)
    _require_voice_session(session)
    turn, _question = _turn_for_session(db, session, asset_probe.turn_id, lock=True)
    draft = _active_draft(db, turn.id, lock=True)
    asset = db.scalars(
        select(AudioAsset)
        .where(AudioAsset.id == asset_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if asset is None or asset.owner_id != owner_id:
        raise AppError("NOT_FOUND", "录音不存在", status_code=404)
    if draft is None or draft.active_take_id != asset.id:
        raise AppError("AUDIO_NOT_ACTIVE", "该录音已不是当前 take，请重新录制", status_code=409)
    if expected_voice_revision is not None and draft.voice_revision != expected_voice_revision:
        raise AppError(
            "VOICE_REVISION_CONFLICT",
            "语音草稿版本已变化，请刷新后重试",
            status_code=409,
            details=[f"current_voice_revision={draft.voice_revision}"],
        )
    if session.status != "active" or session.phase != "awaiting_answer":
        raise AppError("INTERVIEW_STATE_INVALID", "当前状态不能转写录音", status_code=409)
    if session.current_turn_id != turn.id or turn.status != "pending":
        raise AppError("TURN_NOT_CURRENT", "该回合已不是当前待作答回合，请刷新", status_code=409)
    if asset.status != "stored":
        raise AppError("AUDIO_EXPIRED", "录音已过期，请重新录制")
    return session, turn, draft, asset


def create_transcription_job(
    db: Session,
    *,
    owner_id: uuid.UUID,
    asset_id: uuid.UUID,
    expected_voice_revision: int,
) -> Job:
    _session, _turn, draft, asset = _require_active_asset(
        db,
        owner_id=owner_id,
        asset_id=asset_id,
        expected_voice_revision=expected_voice_revision,
    )
    existing = _latest_transcript(db, asset.id)
    existing_job = _asr_job(db, asset, draft.voice_revision)
    if existing is not None and existing.status in ("ready", "empty") and existing_job is not None:
        return existing_job
    input_hash = hashlib.sha256(
        f"{asset.id}|{draft.voice_revision}|{asset.sha256}|audio_asr|v1".encode()
    ).hexdigest()
    job, _created = jobs_service.create_or_get_job(
        db,
        owner_id=owner_id,
        kind="audio_asr",
        target_id=asset.id,
        input_revision=draft.voice_revision,
        input_hash=input_hash,
        input_payload={
            "session_id": str(asset.session_id),
            "turn_id": str(asset.turn_id),
            "audio_asset_id": str(asset.id),
            "voice_revision": draft.voice_revision,
            "audio_sha256": asset.sha256,
        },
    )
    return job


def get_transcript(
    db: Session, *, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> tuple[VoiceTranscript, bool]:
    asset = db.get(AudioAsset, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise AppError("NOT_FOUND", "录音不存在", status_code=404)
    row = _latest_transcript(db, asset.id)
    if row is not None:
        return (
            _transcript_dto(asset, row, None),
            row.status in ("ready", "empty", "failed"),
        )
    job = _asr_job(db, asset, asset.voice_revision)
    dto = _transcript_dto(asset, None, job)
    if dto is None:
        raise AppError("TRANSCRIPT_NOT_FOUND", "尚未创建转写任务", status_code=404)
    return dto, dto.status == "failed"


def validate_answer_source(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session: InterviewSession,
    turn: InterviewTurn,
    input_source: str,
    audio_asset_id: uuid.UUID | None,
    transcript_revision: int | None,
    text: str,
) -> tuple[uuid.UUID | None, int | None]:
    if input_source == "text":
        if audio_asset_id is not None or transcript_revision is not None:
            raise AppError("ANSWER_SOURCE_INVALID", "文字回答不能关联语音资产")
        return None, None
    if audio_asset_id is None or transcript_revision is None:
        raise AppError("ANSWER_SOURCE_INVALID", "语音来源必须提供录音与转写版本")
    draft = _active_draft(db, turn.id, lock=True)
    asset = db.scalars(
        select(AudioAsset)
        .where(AudioAsset.id == audio_asset_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    if (
        asset is None
        or asset.owner_id != owner_id
        or asset.session_id != session.id
        or asset.turn_id != turn.id
    ):
        raise AppError("ANSWER_SOURCE_INVALID", "录音不属于本场当前回合")
    if draft is None or draft.active_take_id != asset.id:
        raise AppError("AUDIO_NOT_ACTIVE", "该录音已不是当前 take，不能提交", status_code=409)
    transcript = _latest_transcript(db, asset.id)
    if transcript is None or transcript.status != "ready":
        raise AppError("TRANSCRIPT_NOT_READY", "转写尚未完成，不能提交语音来源", status_code=409)
    if transcript.revision != transcript_revision:
        raise AppError("TRANSCRIPT_REVISION_CONFLICT", "转写版本已变化，请刷新后重试", status_code=409)
    if input_source == "asr" and text != transcript.text:
        raise AppError("ANSWER_SOURCE_INVALID", "原样转写提交必须与原始转写完全一致")
    return asset.id, transcript.revision


def answer_source_view(answer: InterviewAnswer) -> dict:
    return {
        "input_source": answer.input_source or "text",
        "audio_asset_id": answer.audio_asset_id,
        "transcript_revision": answer.transcript_revision,
    }


# --------------------------------------------------------------------------- 播报与缓存


def _allowed_speech_text(
    db: Session, session: InterviewSession, segment: str, turn_id: uuid.UUID | None
) -> tuple[str, str, InterviewTurn | None]:
    if segment == "opening":
        return session.opening_text, "opening", None
    if segment == "closing":
        if session.status != "ended" or not session.closing_text:
            raise AppError("SPEECH_NOT_AVAILABLE", "本场尚未收尾，不能播报结束语", status_code=409)
        return session.closing_text, "closing", None
    if segment != "turn" or turn_id is None:
        raise AppError("SPEECH_SEGMENT_INVALID", "播报当前题必须提供已曝光回合")
    turn, _question = _turn_for_session(db, session, turn_id)
    return turn.prompt, f"turn:{turn.id}", turn


def _speech_dto(asset: SpeechAsset, job: Job | None) -> SpeechAssetSchema:
    status = asset.status
    error_code = asset.error_code
    if status == "queued" and job is not None and job.state in ("failed", "interrupted"):
        status = "failed"
        error_code = job.error_code or error_code
    return SpeechAssetSchema(
        id=asset.id,
        session_id=asset.session_id,
        segment=asset.segment,
        turn_id=asset.turn_id,
        status=status,
        voice_id=asset.voice_id,
        provider=asset.provider,
        format=asset.format,
        duration_ms=asset.duration_ms,
        sha256=asset.sha256,
        job_id=asset.job_id,
        error_code=error_code,
        synthetic=asset.synthetic,
        created_at=asset.created_at,
        updated_at=asset.updated_at,
    )


def on_retry_attempt(db: Session, job: Job) -> None:
    """重试已被接受后，清理旧 attempt 的终态投影。

    旧失败行只属于上一 attempt；保留它会让客户端在 retry 202 后仍看到 failed 并停止轮询。
    这里在创建 202 回执的同一事务中重置投影，旧 worker 的迟到写入仍由 attempt/lease fence 拒绝。
    """
    if job.kind == "audio_asr":
        db.execute(
            AudioTranscript.__table__.delete().where(
                AudioTranscript.audio_asset_id == job.target_id,
                AudioTranscript.revision == TRANSCRIPT_REVISION,
                AudioTranscript.status == "failed",
            )
        )
    elif job.kind == "audio_tts":
        asset = db.scalars(
            select(SpeechAsset)
            .where(SpeechAsset.id == job.target_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if asset is not None and asset.job_id == job.id and asset.status == "failed":
            asset.status = "queued"
            asset.error_code = None
            asset.updated_at = utcnow()
    db.flush()


def _ensure_speech_job(
    db: Session, asset: SpeechAsset, *, owner_id: uuid.UUID
) -> Job:
    if asset.job_id is not None:
        job = db.get(Job, asset.job_id)
        if job is not None:
            if job.state in ("failed", "interrupted") and asset.status == "queued":
                return job
            return job
    input_hash = hashlib.sha256(
        (
            f"{asset.id}|{asset.content_hash}|{asset.provider}|{asset.voice_id}|"
            f"{asset.format}|{asset.adapter_version}|synthetic={asset.synthetic}"
        ).encode()
    ).hexdigest()
    job, _created = jobs_service.create_or_get_job(
        db,
        owner_id=owner_id,
        kind="audio_tts",
        target_id=asset.id,
        input_revision=1,
        input_hash=input_hash,
        input_payload={
            "session_id": str(asset.session_id),
            "segment": asset.segment,
            "segment_key": asset.segment_key,
            "turn_id": str(asset.turn_id) if asset.turn_id else None,
            "content_hash": asset.content_hash,
            "provider": asset.provider,
            "voice_id": asset.voice_id,
            "format": asset.format,
            "adapter_version": asset.adapter_version,
            "synthetic": asset.synthetic,
        },
    )
    asset.job_id = job.id
    db.flush()
    return job


def request_speech(
    db: Session,
    *,
    owner_id: uuid.UUID,
    session_id: uuid.UUID,
    segment: str,
    turn_id: uuid.UUID | None,
    settings: Settings,
) -> tuple[SpeechAssetSchema, bool]:
    session = _session_for_owner(db, owner_id, session_id, lock=True)
    _require_voice_session(session)
    if not settings.speech_available:
        raise AppError("SPEECH_NOT_CONFIGURED", "语音服务未配置", status_code=503)
    text, segment_key, turn = _allowed_speech_text(db, session, segment, turn_id)
    if not text.strip():
        raise AppError("SPEECH_NOT_AVAILABLE", "没有可播报文本")
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    now = utcnow()
    asset_id = uuid.uuid4()
    stmt = (
        pg_insert(SpeechAsset)
        .values(
            id=asset_id,
            owner_id=owner_id,
            session_id=session.id,
            segment=segment,
            segment_key=segment_key,
            turn_id=turn.id if turn is not None else None,
            content_hash=content_hash,
            text=text,
            voice_id=settings.tts_voice_id,
            provider=settings.speech_provider,
            format=TTS_FORMAT,
            adapter_version=settings.speech_adapter_version,
            status="queued",
            synthetic=settings.speech_provider == "test",
            expires_at=now + timedelta(hours=settings.audio_retention_hours),
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_speech_cache_key")
        .returning(SpeechAsset.id)
    )
    inserted = db.execute(stmt).scalar_one_or_none()
    asset = db.scalars(
        select(SpeechAsset)
        .where(
            SpeechAsset.session_id == session.id,
            SpeechAsset.segment_key == segment_key,
            SpeechAsset.provider == settings.speech_provider,
            SpeechAsset.voice_id == settings.tts_voice_id,
            SpeechAsset.format == TTS_FORMAT,
            SpeechAsset.adapter_version == settings.speech_adapter_version,
            SpeechAsset.content_hash == content_hash,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one()
    if inserted is None and asset.status == "expired":
        asset.status = "queued"
        asset.storage_path = None
        asset.sha256 = None
        asset.duration_ms = None
        asset.error_code = None
        job = db.get(Job, asset.job_id) if asset.job_id else None
        if job is not None:
            job.state = "queued"
            job.attempt = 1
            job.error = None
            job.error_code = None
            job.lease_token = None
            job.lease_expires_at = None
            job.result = None
            job.updated_at = now
        else:
            asset.job_id = None
        asset.updated_at = now
        db.flush()
    elif inserted is None and asset.status == "failed" and asset.job_id is not None:
        # 用户再次点击“播放”是显式重试入口：复用原 asset/job，遵守既有 attempt 上限。
        failed_job = db.scalars(
            select(Job)
            .where(Job.id == asset.job_id, Job.kind == "audio_tts")
            .execution_options(populate_existing=True)
            .with_for_update()
        ).one_or_none()
        if (
            failed_job is not None
            and jobs_service.is_retryable(db, failed_job, settings.max_job_attempts)
        ):
            retried_job = jobs_service.retry_job(
                db, failed_job, failed_job.attempt, settings.max_job_attempts
            )
            on_retry_attempt(db, retried_job)
    job = _ensure_speech_job(db, asset, owner_id=owner_id)
    db.flush()
    return _speech_dto(asset, job), asset.status == "ready"


def get_speech_asset(
    db: Session, *, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> SpeechAssetSchema:
    asset = db.get(SpeechAsset, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise AppError("NOT_FOUND", "语音不存在", status_code=404)
    job = db.get(Job, asset.job_id) if asset.job_id else None
    return _speech_dto(asset, job)


def read_speech_audio(
    settings: Settings, db: Session, *, owner_id: uuid.UUID, asset_id: uuid.UUID
) -> bytes:
    asset = db.get(SpeechAsset, asset_id)
    if asset is None or asset.owner_id != owner_id:
        raise AppError("NOT_FOUND", "语音不存在", status_code=404)
    if asset.status != "ready" or not asset.storage_path:
        raise AppError("SPEECH_NOT_READY", "语音尚未生成完成", status_code=409)
    data = _safe_read(settings.private_file_root, asset.storage_path)
    if asset.sha256 and hashlib.sha256(data).hexdigest() != asset.sha256:
        raise AppError("SPEECH_INTEGRITY_FAILED", "语音文件校验失败", status_code=409)
    with wave.open(io.BytesIO(data), "rb") as check:
        if (
            check.getnchannels() != 1
            or check.getsampwidth() != 2
            or check.getframerate() != 24000
            or check.getnframes() <= 0
        ):
            raise AppError("SPEECH_INTEGRITY_FAILED", "语音文件格式校验失败", status_code=409)
    return data


# --------------------------------------------------------------------------- worker 发布


def publish_transcription(
    db: Session,
    *,
    asset: AudioAsset,
    job: Job,
    text: str,
    request_id: str | None,
    provider: str,
) -> AudioTranscript:
    """在 worker 锁住 session/turn/draft/asset 与 job fence 的事务内发布原始转写。"""
    turn, _question = _turn_for_session(db, _session_for_owner(db, asset.owner_id, asset.session_id), asset.turn_id)
    draft = _active_draft(db, turn.id, lock=True)
    row = db.scalars(
        select(AudioTranscript)
        .where(
            AudioTranscript.audio_asset_id == asset.id,
            AudioTranscript.revision == TRANSCRIPT_REVISION,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one_or_none()
    status = "ready" if text.strip() else "empty"
    if row is None:
        row = AudioTranscript(
            id=uuid.uuid4(),
            audio_asset_id=asset.id,
            revision=TRANSCRIPT_REVISION,
            text=text,
            status=status,
            provider=provider,
            request_id=request_id,
            error_code=None,
        )
        db.add(row)
    else:
        row.text = text
        row.status = status
        row.provider = provider
        row.request_id = request_id
        row.error_code = None
        row.updated_at = utcnow()
    if draft is not None and draft.active_take_id != asset.id:
        # 迟到结果只保留在该 take 自己的元数据中，不覆盖当前草稿。
        db.flush()
        return row
    db.flush()
    return row


def publish_speech(
    db: Session,
    *,
    settings: Settings,
    asset: SpeechAsset,
    pcm: bytes,
    request_id: str | None,
) -> tuple[str, str, int]:
    if len(pcm) < 2 or len(pcm) % 2 != 0:
        raise AppError("SPEECH_PROTOCOL_ERROR", "语音合成为空或样本不完整")
    wav = _wav_bytes(pcm, 24000)
    sha = hashlib.sha256(wav).hexdigest()
    duration_ms = max(1, (len(pcm) // 2) * 1000 // 24000)
    relative = f"speech/{asset.owner_id.hex}/{asset.session_id.hex}/{asset.id.hex}.wav"
    _write_private(settings.private_file_root, relative, wav)
    return relative, sha, duration_ms


def mark_speech_failed(db: Session, asset: SpeechAsset, code: str) -> None:
    asset.status = "failed"
    asset.error_code = code
    asset.updated_at = utcnow()
    db.flush()


# --------------------------------------------------------------------------- 过期清理


def cleanup_expired(db: Session, settings: Settings, *, now: datetime | None = None) -> dict:
    """删除过期临时音频；活跃 take、运行中任务与已提交答案引用的文件不清理。"""
    moment = now or utcnow()
    active_rows = db.scalars(
        select(VoiceDraftRow).where(VoiceDraftRow.active_take_id.is_not(None))
    ).all()
    active_ids = {row.active_take_id for row in active_rows}
    open_asset_ids = set(
        db.scalars(
            select(Job.target_id).where(
                Job.kind == "audio_asr", Job.state.in_(("queued", "running"))
            )
        ).all()
    )
    removed_files = 0
    expired_assets = 0
    for asset in db.scalars(
        select(AudioAsset).where(
            AudioAsset.status == "stored",
            AudioAsset.expires_at < moment,
        )
    ).all():
        if asset.id in active_ids or asset.id in open_asset_ids:
            continue
        _delete_private(settings.private_file_root, asset.storage_path)
        asset.status = "expired"
        asset.storage_path = None
        expired_assets += 1
        removed_files += 1
    open_speech_ids = set(
        db.scalars(
            select(Job.target_id).where(
                Job.kind == "audio_tts", Job.state.in_(("queued", "running"))
            )
        ).all()
    )
    for asset in db.scalars(
        select(SpeechAsset).where(
            SpeechAsset.status.in_(("ready", "failed")),
            SpeechAsset.expires_at < moment,
        )
    ).all():
        if asset.id in open_speech_ids:
            continue
        _delete_private(settings.private_file_root, asset.storage_path)
        asset.status = "expired"
        asset.storage_path = None
        asset.sha256 = None
        asset.duration_ms = None
        removed_files += 1
    db.flush()
    return {"expired_assets": expired_assets, "speech_expired": removed_files - expired_assets}
