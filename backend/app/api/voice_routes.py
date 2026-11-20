"""M3 语音面试接口：配置、录音上传、转写、确认来源与受控播报。"""

from __future__ import annotations

import hashlib
import uuid

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.models import User
from app.schemas import (
    ErrorResponse,
    Job,
    SpeechAsset,
    SpeechRequestInput,
    TranscribeInput,
    VoiceConfig,
    VoiceDraft,
    VoiceTranscript,
    VoiceUploadReceipt,
)
from app.services import idempotency as idem
from app.services import voice as voice_service

ERROR_RESPONSES = {
    400: {"model": ErrorResponse, "description": "请求内容不合法"},
    401: {"model": ErrorResponse, "description": "登录会话无效"},
    404: {"model": ErrorResponse, "description": "对象不存在或不属于本人"},
    409: {"model": ErrorResponse, "description": "状态／版本／take 冲突"},
    413: {"model": ErrorResponse, "description": "录音超过 120 秒或请求体上限"},
    415: {"model": ErrorResponse, "description": "音频格式不受支持"},
    422: {"model": ErrorResponse, "description": "请求结构不符合契约"},
    429: {"model": ErrorResponse, "description": "语音服务限流"},
    503: {"model": ErrorResponse, "description": "语音服务未配置或不可用"},
}

router = APIRouter(tags=["voice"], responses=ERROR_RESPONSES)


def _uuid(raw: str, message: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise AppError("NOT_FOUND", message, status_code=404)


def _begin(db: Session, user: User, request: Request, request_hash: str):
    return idem.begin(
        db,
        user_id=user.id,
        method=request.method,
        path=request.url.path,
        raw_key=request.headers.get("Idempotency-Key"),
        request_hash=request_hash,
    )


@router.get("/voice/config", response_model=VoiceConfig, operation_id="getVoiceConfig")
def get_voice_config() -> VoiceConfig:
    return voice_service.voice_config(get_settings())


@router.get(
    "/interviews/{session_id}/turns/{turn_id}/voice-draft",
    response_model=VoiceDraft,
    operation_id="getVoiceDraft",
)
def get_voice_draft(
    session_id: str,
    turn_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> VoiceDraft:
    return voice_service.get_voice_draft(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        turn_id=_uuid(turn_id, "回合不存在"),
    )


@router.post(
    "/interviews/{session_id}/turns/{turn_id}/audio",
    response_model=VoiceUploadReceipt,
    status_code=201,
    operation_id="uploadVoiceAudio",
    responses={200: {"model": VoiceUploadReceipt, "description": "同 take 回放"}},
)
async def upload_voice_audio(
    session_id: str,
    turn_id: str,
    request: Request,
    response: Response,
    take_id: str = Form(...),
    session_revision: int = Form(...),
    expected_voice_revision: int = Form(...),
    sha256: str = Form(...),
    format: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    settings = get_settings()
    parsed_take = _uuid(take_id, "take 不存在")
    parsed_session = _uuid(session_id, "面试不存在")
    parsed_turn = _uuid(turn_id, "回合不存在")
    if len(sha256) != 64 or any(ch not in "0123456789abcdef" for ch in sha256):
        raise AppError("AUDIO_HASH_MISMATCH", "sha256 必须是 64 位小写十六进制")
    if format != voice_service.VOICE_FORMAT:
        raise AppError("AUDIO_FORMAT_INVALID", "只接受 pcm_s16le/16000/mono", status_code=415)
    limit = voice_service.MAX_AUDIO_BYTES
    chunks: list[bytes] = []
    size = 0
    while True:
        chunk = await file.read(65536)
        if not chunk:
            break
        size += len(chunk)
        if size > limit:
            raise AppError("AUDIO_TOO_LARGE", "录音超过 120 秒上限", status_code=413)
        chunks.append(chunk)
    pcm = b"".join(chunks)
    actual = hashlib.sha256(pcm).hexdigest()
    request_hash = idem.digest_json_body(
        (
            f"{parsed_session}|{parsed_turn}|{parsed_take}|{session_revision}|"
            f"{expected_voice_revision}|{sha256}|{format}|{actual}"
        ).encode("utf-8")
    )
    handle = _begin(db, user, request, request_hash)
    if handle.replay is not None:
        return handle.replay
    asset, revision, created = await run_in_threadpool(
        voice_service.store_audio,
        db,
        owner_id=user.id,
        session_id=parsed_session,
        turn_id=parsed_turn,
        session_revision=session_revision,
        expected_voice_revision=expected_voice_revision,
        take_id=parsed_take,
        declared_sha256=sha256,
        declared_format=format,
        pcm=pcm,
        settings=settings,
    )
    body = VoiceUploadReceipt(
        session_id=parsed_session,
        turn_id=parsed_turn,
        voice_revision=revision,
        audio=voice_service._asset_dto(asset),
    ).model_dump(mode="json")
    try:
        status_code = 201 if created else 200
        return idem.complete(db, handle, status_code, body)
    except Exception:
        if created and asset.storage_path:
            voice_service._delete_private(settings.private_file_root, asset.storage_path)
        raise


@router.post(
    "/audio-assets/{audio_asset_id}/transcriptions",
    response_model=Job,
    status_code=202,
    operation_id="createAudioTranscription",
)
def create_audio_transcription(
    audio_asset_id: str,
    payload: TranscribeInput,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    request_hash = idem.digest_json_body(payload.model_dump_json().encode("utf-8"))
    handle = _begin(db, user, request, request_hash)
    if handle.replay is not None:
        return handle.replay
    job = voice_service.create_transcription_job(
        db,
        owner_id=user.id,
        asset_id=_uuid(audio_asset_id, "录音不存在"),
        expected_voice_revision=payload.expected_voice_revision,
    )
    from app.services import jobs as jobs_service

    body = jobs_service.job_to_schema(db, job, get_settings().max_job_attempts).model_dump(
        mode="json"
    )
    return idem.complete(db, handle, 202, body)


@router.get(
    "/audio-assets/{audio_asset_id}/transcript",
    response_model=VoiceTranscript,
    operation_id="getAudioTranscript",
    responses={202: {"model": VoiceTranscript, "description": "转写仍在处理中"}},
)
def get_audio_transcript(
    audio_asset_id: str,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    dto, terminal = voice_service.get_transcript(
        db, owner_id=user.id, asset_id=_uuid(audio_asset_id, "录音不存在")
    )
    response.status_code = 200 if terminal else 202
    return dto


@router.post(
    "/interviews/{session_id}/speech",
    response_model=SpeechAsset,
    status_code=202,
    operation_id="requestInterviewSpeech",
    responses={200: {"model": SpeechAsset, "description": "已有完整缓存，直接读取"}},
)
def request_interview_speech(
    session_id: str,
    payload: SpeechRequestInput,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    request_hash = idem.digest_json_body(payload.model_dump_json().encode("utf-8"))
    handle = _begin(db, user, request, request_hash)
    if handle.replay is not None:
        return handle.replay
    dto, ready = voice_service.request_speech(
        db,
        owner_id=user.id,
        session_id=_uuid(session_id, "面试不存在"),
        segment=payload.segment,
        turn_id=payload.turn_id,
        settings=get_settings(),
    )
    status_code = 200 if ready else 202
    try:
        return idem.complete(db, handle, status_code, dto.model_dump(mode="json"))
    finally:
        response.status_code = status_code


@router.get(
    "/speech-assets/{speech_asset_id}",
    response_model=SpeechAsset,
    operation_id="getSpeechAsset",
)
def get_speech_asset(
    speech_asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SpeechAsset:
    return voice_service.get_speech_asset(
        db, owner_id=user.id, asset_id=_uuid(speech_asset_id, "语音不存在")
    )


@router.get(
    "/speech-assets/{speech_asset_id}/content",
    operation_id="getSpeechAssetContent",
    responses={
        200: {"content": {"audio/wav": {"schema": {"type": "string", "format": "binary"}}}},
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        404: {"model": ErrorResponse},
        409: {"model": ErrorResponse},
        503: {"model": ErrorResponse},
    },
)
def get_speech_asset_content(
    speech_asset_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    data = voice_service.read_speech_audio(
        get_settings(),
        db,
        owner_id=user.id,
        asset_id=_uuid(speech_asset_id, "语音不存在"),
    )
    return Response(
        content=data,
        media_type="audio/wav",
        headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"},
    )
