"""M3 独立语音 provider：DeepSeek ASR／TTS 与隔离 test 实现。

语音适配器与文字 Chat Completions 分离：
- ASR 使用官方整段文件接口 `POST /v1/tools/asr`；
- TTS 使用官方 WebSocket，必须收到 `finish` 且 code=success 后才认可完整音频；
- test 只在 ALLOW_TEST_FIXTURES 的隔离环境启用，并标记 synthetic，不能冒充真实语音。
"""

from __future__ import annotations

import asyncio
import json
import math
import struct
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import Settings
from app.core.diagnostics import sanitize_external_request_id, sanitize_usage
from app.core.errors import AppError

TTS_SAMPLE_RATE = 24000
TTS_FORMAT = "pcm_s16le/24000/mono"
MAX_TTS_CHARS = 10000
MAX_TRANSCRIPT_CHARS = 20000


@dataclass
class SpeechTranscription:
    text: str
    duration_ms: int
    request_id: str | None
    usage: dict


@dataclass
class SpeechAudio:
    pcm: bytes
    sample_rate: int
    format: str
    request_id: str | None


def speech_error_from_status(status_code: int, *, stage: str) -> AppError:
    mapping = {
        400: ("SPEECH_INPUT_INVALID", "语音内容不是受支持的音频格式", False),
        401: ("AI_AUTH_FAILED", "语音服务认证失败，请检查服务端配置", False),
        403: ("AI_AUTH_FAILED", "语音服务认证失败，请检查服务端配置", False),
        408: ("AI_TIMEOUT", "语音服务超时，结果未知", True),
        413: ("SPEECH_INPUT_TOO_LARGE", "语音文件超过供应商限制", False),
        429: ("AI_RATE_LIMITED", "语音服务限流，结果未知", True),
        503: ("AI_UNAVAILABLE", "语音服务暂时不可用", True),
        504: ("AI_TIMEOUT", "语音服务超时，结果未知", True),
    }
    code, message, retryable = mapping.get(
        status_code, ("AI_REQUEST_REJECTED", "语音服务拒绝了请求", False)
    )
    return AppError(
        code,
        message,
        retryable=retryable,
        status_code=503 if retryable else 502,
        details=[f"stage={stage}", f"upstream_status={status_code}"],
    )


class SpeechProvider:
    name: str = "disabled"
    voice_id: str = "stella"
    adapter_version: str = "disabled"
    test_only: bool = False
    synthetic: bool = False
    last_call: dict[str, Any] | None = None

    def transcribe(self, wav_bytes: bytes) -> SpeechTranscription:
        raise AppError("SPEECH_NOT_CONFIGURED", "语音服务未配置")

    def synthesize(self, text: str) -> SpeechAudio:
        raise AppError("SPEECH_NOT_CONFIGURED", "语音服务未配置")


class DisabledSpeechProvider(SpeechProvider):
    pass


class TestSpeechProvider(SpeechProvider):
    name = "test"
    test_only = True
    synthetic = True
    adapter_version = "test-speech-v1"

    def __init__(self, *, voice_id: str = "stella", transcript: str = "这是测试语音回答。") -> None:
        self.voice_id = voice_id
        self.transcript = transcript

    def transcribe(self, wav_bytes: bytes) -> SpeechTranscription:
        # 固定 16k mono PCM16 计算时长；过短音频按无有效语音处理，不伪造长回答。
        duration_ms = max(1, len(wav_bytes) * 1000 // (16000 * 2))
        text = self.transcript if len(wav_bytes) >= 6400 else ""
        self.last_call = {
            "request_id": "test-asr",
            "usage": {"audio_seconds": duration_ms // 1000},
            "finish_reason": "stop",
        }
        return SpeechTranscription(
            text=text, duration_ms=duration_ms, request_id="test-asr",
            usage={"audio_seconds": duration_ms // 1000},
        )

    def synthesize(self, text: str) -> SpeechAudio:
        if not text.strip():
            raise AppError("SPEECH_INPUT_INVALID", "播报文本不能为空")
        # 0.2 秒 24k 单声道正弦波；仅用于协议与播放链路测试，明确 synthetic。
        samples = int(TTS_SAMPLE_RATE * 0.2)
        pcm = b"".join(
            struct.pack("<h", int(1200 * math.sin(2 * math.pi * 440 * i / TTS_SAMPLE_RATE)))
            for i in range(samples)
        )
        self.last_call = {
            "request_id": "test-tts",
            "usage": {"billed_characters": len(text)},
            "finish_reason": "stop",
        }
        return SpeechAudio(
            pcm=pcm, sample_rate=TTS_SAMPLE_RATE, format=TTS_FORMAT, request_id="test-tts"
        )


class DeepSeekSpeechProvider(SpeechProvider):
    name = "deepseek"
    adapter_version = "deepseek-speech-v1"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.speech_api_key_value
        self.base_url = _normalize_base(settings.speech_base_url)
        self.voice_id = settings.tts_voice_id
        self.timeout = settings.speech_timeout_seconds

    def transcribe(self, wav_bytes: bytes) -> SpeechTranscription:
        url = f"{self.base_url}/v1/tools/asr"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        files = {"file": ("answer.wav", wav_bytes, "audio/wav")}
        try:
            with httpx.Client(timeout=httpx.Timeout(self.timeout), follow_redirects=False) as client:
                response = client.post(url, headers=headers, files=files)
        except httpx.TimeoutException as exc:
            raise AppError("AI_TIMEOUT", "语音识别超时，结果未知", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise AppError("AI_UNAVAILABLE", "语音识别服务暂时不可用", retryable=True) from exc
        if response.status_code >= 400:
            raise speech_error_from_status(response.status_code, stage="asr")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AppError("SPEECH_PROTOCOL_ERROR", "语音识别返回无法解析") from exc
        text = payload.get("text")
        duration = payload.get("duration_ms")
        request_id = sanitize_external_request_id(payload.get("request_id"))
        if not isinstance(text, str) or not isinstance(duration, int) or duration < 0:
            raise AppError("SPEECH_PROTOCOL_ERROR", "语音识别返回结构不符合协议")
        if len(text) > MAX_TRANSCRIPT_CHARS:
            raise AppError(
                "SPEECH_TRANSCRIPT_TOO_LONG",
                "转写文本过长，未保存；请缩短回答后重录",
                details=[f"codepoints={len(text)}"],
            )
        self.last_call = {
            "request_id": request_id,
            "usage": {"audio_seconds": max(0, duration // 1000)},
            "finish_reason": "stop",
        }
        return SpeechTranscription(
            text=text, duration_ms=duration, request_id=request_id,
            usage={"audio_seconds": max(0, duration // 1000)},
        )

    def synthesize(self, text: str) -> SpeechAudio:
        if not text.strip():
            raise AppError("SPEECH_INPUT_INVALID", "播报文本不能为空")
        if len(text) > MAX_TTS_CHARS:
            raise AppError("SPEECH_INPUT_INVALID", "播报文本超过供应商上限")
        try:
            return asyncio.run(self._synthesize_async(text))
        except AppError:
            raise
        except TimeoutError as exc:
            raise AppError("AI_TIMEOUT", "语音合成超时，结果未知", retryable=True) from exc
        except Exception as exc:  # 不把底层消息或 Key 暴露到 API
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(status, int):
                raise speech_error_from_status(status, stage="tts") from exc
            raise AppError(
                "AI_UNAVAILABLE",
                "语音合成服务暂时不可用",
                retryable=True,
                details=[f"stage=tts", f"error_type={type(exc).__name__}"],
            ) from exc

    async def _synthesize_async(self, text: str) -> SpeechAudio:
        from websockets.sync.client import connect

        ws_base = self.base_url.replace("https://", "wss://").replace("http://", "ws://")
        url = f"{ws_base}/v1/tools/tts?voice_id={self.voice_id}&format=pcm"
        headers = {"Authorization": f"Bearer {self.api_key}"}
        chunks: list[bytes] = []
        finish_payload: dict | None = None
        try:
            with connect(
                url,
                additional_headers=headers,
                open_timeout=self.timeout,
                close_timeout=5,
                max_size=None,
            ) as ws:
                ws.send(json.dumps({"event": "text", "text": text}, ensure_ascii=False))
                ws.send(json.dumps({"event": "finish_text"}))
                while True:
                    message = ws.recv(timeout=self.timeout)
                    if isinstance(message, bytes):
                        chunks.append(message)
                        continue
                    try:
                        event = json.loads(message)
                    except ValueError as exc:
                        raise AppError("SPEECH_PROTOCOL_ERROR", "语音合成事件无法解析") from exc
                    if event.get("event") == "finish":
                        finish_payload = event
                        break
        except AppError:
            raise
        if finish_payload is None:
            raise AppError("SPEECH_PROTOCOL_ERROR", "语音合成缺少成功结束事件")
        if finish_payload.get("code") != "success":
            code = str(finish_payload.get("code") or "unknown")
            if code in ("content_filter", "invalid_input"):
                raise AppError("SPEECH_CONTENT_REJECTED", "播报文本未通过语音服务审核")
            if code == "input_timeout":
                raise AppError("AI_TIMEOUT", "语音合成等待文本超时", retryable=True)
            raise AppError(
                "AI_UNAVAILABLE", "语音合成未成功完成", retryable=True,
                details=[f"stage=tts", f"provider_code={code[:32]}"],
            )
        pcm = b"".join(chunks)
        if len(pcm) < 2 or len(pcm) % 2 != 0:
            raise AppError("SPEECH_PROTOCOL_ERROR", "语音合成为空或样本不完整")
        request_id = sanitize_external_request_id(finish_payload.get("request_id"))
        billed = sum(2 if ord(ch) > 127 else 1 for ch in text)
        self.last_call = {
            "request_id": request_id,
            "usage": sanitize_usage({"billed_characters": billed}),
            "finish_reason": "stop",
        }
        return SpeechAudio(
            pcm=pcm, sample_rate=TTS_SAMPLE_RATE, format=TTS_FORMAT, request_id=request_id
        )


def _normalize_base(raw: str) -> str:
    base = raw.strip().rstrip("/")
    for suffix in ("/v1/tools", "/v1"):
        if base.endswith(suffix):
            base = base[: -len(suffix)]
            break
    return base


def get_speech_provider(
    settings: Settings, provider_name: str | None = None
) -> SpeechProvider:
    """按资产冻结的 provider 取适配器；未指定时使用当前配置。

    TTS 资产一旦入队，worker 必须按资产记录的 provider 生成，不能因为服务端配置变化
    把合成音频标成另一个 provider。
    """
    name = provider_name or settings.speech_provider
    if name == "deepseek":
        if not settings.speech_api_key_value:
            raise AppError("SPEECH_NOT_CONFIGURED", "语音服务未配置")
        return DeepSeekSpeechProvider(settings)
    if name == "test":
        if not (settings.allow_test_fixtures and not settings.is_production):
            raise AppError("SPEECH_NOT_CONFIGURED", "测试语音 provider 只能在隔离测试环境启用")
        return TestSpeechProvider(voice_id=settings.tts_voice_id)
    return DisabledSpeechProvider()
