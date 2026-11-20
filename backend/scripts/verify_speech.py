"""受控真实语音验证：1 次 TTS、1 次 ASR、1 次文字追问。

只使用虚构短句，不读取简历/JD，不重试、不批量。Key 只在服务端环境，输出不含 Key。
用法（从项目根）：
  .\\.work\\venv\\Scripts\\python.exe backend/scripts/verify_speech.py --real
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import wave
from datetime import datetime, timezone
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.ai.provider import get_provider  # noqa: E402
from app.ai.speech import get_speech_provider  # noqa: E402
from app.core.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="确认真实收费调用（最多3次）")
    args = parser.parse_args()
    if not args.real:
        print("未设置 --real：本脚本不会发起真实付费调用。")
        return 0

    settings = get_settings()
    if settings.speech_provider != "deepseek" or not settings.speech_api_key_value:
        print(
            json.dumps(
                {
                    "ok": False,
                    "stage": "config",
                    "reason": "SPEECH_PROVIDER 不是 deepseek 或缺少服务端语音 Key",
                    "speech_provider": settings.speech_provider,
                },
                ensure_ascii=False,
            )
        )
        return 1

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = ROOT / ".work" / "m3-real-speech" / stamp
    evidence.mkdir(parents=True, exist_ok=True)
    text = "这是一次受控的语音接口连通性测试，内容是虚构的，不包含任何真实个人材料。"
    summary: dict = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "provider": settings.speech_provider,
        "voice_id": settings.tts_voice_id,
        "adapter_version": settings.speech_adapter_version,
        "calls": [],
        "test_text": text,
    }
    calls = 0
    try:
        speech = get_speech_provider(settings)
        calls += 1
        audio = speech.synthesize(text)
        if len(audio.pcm) < 2 or len(audio.pcm) % 2 != 0:
            raise RuntimeError("TTS 返回空样本或样本不完整")
        duration_ms = (len(audio.pcm) // 2) * 1000 // audio.sample_rate
        wav_path = evidence / "tts.wav"
        with wave.open(str(wav_path), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(audio.sample_rate)
            handle.writeframes(audio.pcm)
        tts_call = {
            "stage": "tts",
            "request_id": audio.request_id,
            "sample_rate": audio.sample_rate,
            "pcm_bytes": len(audio.pcm),
            "duration_ms": duration_ms,
            "usage": (speech.last_call or {}).get("usage"),
        }
        summary["calls"].append(tts_call)
        print(json.dumps({"stage": "tts", **tts_call}, ensure_ascii=False))

        calls += 1
        wav_bytes = wav_path.read_bytes()
        transcription = speech.transcribe(wav_bytes)
        asr_call = {
            "stage": "asr",
            "request_id": transcription.request_id,
            "text": transcription.text,
            "duration_ms": transcription.duration_ms,
            "usage": transcription.usage,
        }
        summary["calls"].append(asr_call)
        print(json.dumps({"stage": "asr", **asr_call}, ensure_ascii=False))
        if not transcription.text.strip():
            raise RuntimeError("ASR 返回空文本")

        calls += 1
        text_provider = get_provider(settings)
        decision = text_provider.decide_follow_up(
            question={
                "id": "00000000-0000-4000-8000-000000000901",
                "position": 1,
                "source": "generated",
                "stem": "请说明一次虚构的缓存更新流程，并指出一个适用边界。",
                "focus": "流程与边界",
                "evaluation": {"goal": "说明流程与边界"},
                "anchors": [],
                "bank_ref": None,
            },
            answer_text="我会先读取旧值，再在锁内比较版本；如果版本不一致就放弃更新。",
            context={"is_first_of_source": True},
        )
        text_call = {
            "stage": "follow_up",
            "request_id": (text_provider.last_call or {}).get("request_id"),
            "decision": decision.get("decision"),
            "usage": (text_provider.last_call or {}).get("usage"),
        }
        summary["calls"].append(text_call)
        print(json.dumps({"stage": "follow_up", **text_call}, ensure_ascii=False))

        summary.update(
            {
                "ok": True,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "call_count": calls,
                "evidence_dir": str(evidence),
            }
        )
        (evidence / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"ok": True, "call_count": calls, "evidence": str(evidence)}, ensure_ascii=False))
        return 0
    except Exception as exc:  # 结构化失败，不打印 Key/请求头
        summary.update(
            {
                "ok": False,
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "call_count": calls,
                "error_type": type(exc).__name__,
                "error_code": getattr(exc, "code", None),
                "error_message": str(exc)[:300],
            }
        )
        (evidence / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps(summary, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
