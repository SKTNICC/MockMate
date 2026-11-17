from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.ai.provider import DeepSeekProvider, get_provider
from app.core.config import Settings
from app.core.errors import AppError


def make_settings(**overrides) -> Settings:
    values = dict(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT=str(Path(__file__).resolve().parents[1] / "runtime" / "test-private"),
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-test-model",
        AI_API_KEY="test-key-not-real",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=False,
    )
    values.update(overrides)
    return Settings(**values)


def provider_with(handler) -> DeepSeekProvider:
    return DeepSeekProvider(make_settings(), transport=httpx.MockTransport(handler))


RESUME_PROFILE = {
    "kind": "resume",
    "education": [
        {
            "item_id": "edu-1",
            "degree": "本科在读",
            "major": "计算机科学与技术",
            "evidence": {"quote": "本科在读", "origin": "extracted"},
        }
    ],
    "work_experience": [],
    "projects": [],
    "skills": [],
}


def chat_response(content: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        json={"id": "req-test", "choices": [{"message": {"content": content}}]},
    )


def test_parse_material_happy_path_sends_expected_request() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return chat_response(json.dumps(RESUME_PROFILE, ensure_ascii=False))

    provider = provider_with(handler)
    profile = provider.parse_material(text="本科在读", kind="resume", role_id="java-backend", level="intern")
    assert profile["kind"] == "resume"
    assert captured["path"].endswith("/chat/completions")
    assert captured["auth"] == "Bearer test-key-not-real"
    assert captured["body"]["model"] == "deepseek-test-model"
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert "本科在读" in captured["body"]["messages"][-1]["content"]


def test_schema_mismatch_is_ai_invalid_output() -> None:
    provider = provider_with(lambda request: chat_response(json.dumps({"kind": "resume", "education": [{"item_id": "x"}]})))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_non_json_content_is_ai_invalid_output() -> None:
    provider = provider_with(lambda request: chat_response("这不是 JSON"))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_http_500_is_ai_unavailable() -> None:
    provider = provider_with(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_UNAVAILABLE"


def test_timeout_is_ai_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    provider = provider_with(handler)
    with pytest.raises(AppError) as caught:
        provider.parse_material(text="x", kind="resume", role_id="r", level="intern")
    assert caught.value.code == "AI_TIMEOUT"


def test_match_rows_are_schema_validated() -> None:
    provider = provider_with(
        lambda request: chat_response(json.dumps({"rows": [{"requirement_id": "req-1"}]}))
    )
    import types

    resume = types.SimpleNamespace(id="00000000-0000-4000-8000-000000000001", text="简历", profile={"kind": "resume"})
    jd = types.SimpleNamespace(id="00000000-0000-4000-8000-000000000002", text="JD", profile={"kind": "jd"})
    with pytest.raises(AppError) as caught:
        provider.match_requirements(resume_snapshot=resume, jd_snapshot=jd)
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_duplicate_review_output_is_validated() -> None:
    good = {"duplicate_pairs": [], "grounding_issues": [], "uncertain": False}
    provider = provider_with(lambda request: chat_response(json.dumps(good)))
    result = provider.review_questions(questions=[], material_texts=[])
    assert result == good

    bad = provider_with(lambda request: chat_response(json.dumps({"duplicate_pairs": []})))
    with pytest.raises(AppError) as caught:
        bad.review_questions(questions=[], material_texts=[])
    assert caught.value.code == "AI_INVALID_OUTPUT"


def test_deepseek_without_credentials_is_not_configured() -> None:
    settings = make_settings(AI_API_KEY="", AI_MODEL="")
    with pytest.raises(AppError) as caught:
        get_provider(settings)
    assert caught.value.code == "AI_NOT_CONFIGURED"


def test_disabled_provider_is_not_configured() -> None:
    with pytest.raises(AppError) as caught:
        get_provider(make_settings(AI_PROVIDER="disabled"))
    assert caught.value.code == "AI_NOT_CONFIGURED"
