from __future__ import annotations

import io
import json

import pytest
from fastapi.testclient import TestClient

from app.core.errors import AppError

from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    REVIEWED_RESUME_TEXT,
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    create_confirmed_resume,
    issue_token,
    login,
    make_docx,
    make_docx_with_tables,
    make_pdf,
    new_key,
    register,
    wait_job,
)


def _post_text(client, token, *, kind, title, text, template_id=None, template_version=None):
    return client.post(
        "/v1/materials/text",
        json={
            "kind": kind,
            "title": title,
            "text": text,
            "template_id": template_id,
            "template_version": template_version,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def _confirm(client, token, material, profile):
    return client.post(
        f"/v1/materials/{material['id']}/confirm",
        json={"revision": material["revision"], "profile": profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def test_text_material_and_confirm_snapshot(client: TestClient) -> None:
    _, token = issue_token(client)
    created = _post_text(
        client, token, kind="resume", title="我的简历", text=REVIEWED_RESUME_TEXT
    )
    assert created.status_code == 201
    material = created.json()
    assert material["state"] == "text_ready"
    assert material["profile"] is None

    first = _confirm(client, token, material, REVIEWED_RESUME_PROFILE)
    assert first.status_code == 201, first.text
    snapshot = first.json()
    assert snapshot["source_revision"] == 1
    assert snapshot["kind"] == "resume"
    assert snapshot["profile"]["work_experience"] == []

    # 同 revision、同内容重复确认返回同 snapshot
    second = _confirm(client, token, material, REVIEWED_RESUME_PROFILE)
    assert second.status_code == 201
    assert second.json()["id"] == snapshot["id"]

    # 不同 profile 再次确认必须冲突，而不是静默覆盖
    changed = dict(REVIEWED_RESUME_PROFILE)
    changed["skills"] = []
    conflict = _confirm(client, token, material, changed)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "REVISION_CONFLICT"

    snapshots = client.get(f"/v1/materials/{material['id']}/snapshots", headers=auth(token))
    assert snapshots.status_code == 200
    assert len(snapshots.json()["items"]) == 1
    fetched = client.get(f"/v1/snapshots/{snapshot['id']}", headers=auth(token))
    assert fetched.status_code == 200
    assert fetched.json()["text"] == REVIEWED_RESUME_TEXT


def test_text_validation_and_limits(client: TestClient) -> None:
    _, token = issue_token(client)
    blank = _post_text(client, token, kind="resume", title="空", text="   \n  ")
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "TEXT_INVALID"

    too_long = _post_text(client, token, kind="resume", title="长", text="字" * 20001)
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"


def test_template_copy_must_match_exactly(client: TestClient) -> None:
    _, token = issue_token(client)
    mismatched = _post_text(
        client,
        token,
        kind="jd",
        title="内置JD",
        text=TEMPLATE_JD_TEXT + "\n附：额外要求",
        template_id="java-backend-intern-v1",
        template_version=1,
    )
    assert mismatched.status_code == 409
    assert mismatched.json()["error"]["code"] == "TEMPLATE_MISMATCH"

    ok = _post_text(
        client,
        token,
        kind="jd",
        title="内置JD",
        text=TEMPLATE_JD_TEXT,
        template_id="java-backend-intern-v1",
        template_version=1,
    )
    assert ok.status_code == 201
    material = ok.json()
    assert material["template_id"] == "java-backend-intern-v1"
    confirmed = _confirm(client, token, material, TEMPLATE_JD_PROFILE)
    assert confirmed.status_code == 201
    assert confirmed.json()["template_version"] == 1


def test_evidence_must_exist_in_text(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _post_text(
        client, token, kind="resume", title="简历", text=REVIEWED_RESUME_TEXT
    ).json()
    bad = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "这是一段不存在的工作经历", "origin": "user_confirmed"},
            }
        ],
        "work_experience": [],
        "projects": [],
        "skills": [],
    }
    response = _confirm(client, token, material, bad)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "EVIDENCE_INVALID"


def test_student_without_work_or_projects_is_legal(client: TestClient) -> None:
    _, token = issue_token(client)
    text = "本科在读，计算机科学与技术专业。\n学习过 Java 集合基础和 JUnit 单元测试。"
    profile = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术专业。", "origin": "user_confirmed"},
            }
        ],
        "work_experience": [],
        "projects": [],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java 集合基础和 JUnit",
                "code": "java.core",
                "evidence": {"quote": "学习过 Java 集合基础和 JUnit 单元测试。", "origin": "extracted"},
            }
        ],
    }
    material = _post_text(client, token, kind="resume", title="学生简历", text=text).json()
    response = _confirm(client, token, material, profile)
    assert response.status_code == 201, response.text
    assert response.json()["profile"]["projects"] == []
    assert response.json()["profile"]["work_experience"] == []


def test_edit_bumps_revision_and_late_parse_cannot_overwrite(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = _post_text(
        client, token, kind="resume", title="简历", text=REVIEWED_RESUME_TEXT
    ).json()

    job = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": material["revision"],
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert job.status_code == 202

    edited = client.put(
        f"/v1/materials/{material['id']}",
        json={"revision": 1, "title": "简历（修改）", "text": REVIEWED_RESUME_TEXT + "\n补充：参与课程设计。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 2
    assert edited.json()["profile"] is None

    final = wait_job(client, token, job.json()["id"], worker, attempts=3)
    assert final["state"] == "superseded"

    current = client.get(f"/v1/materials/{material['id']}", headers=auth(token)).json()
    assert current["revision"] == 2
    assert current["profile"] is None


def test_parse_job_then_manual_confirm(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = _post_text(
        client, token, kind="resume", title="简历", text=REVIEWED_RESUME_TEXT
    ).json()
    response = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202
    job = wait_job(client, token, response.json()["id"], worker)
    assert job["state"] == "succeeded"
    assert job["result_ref"]["entity_type"] == "material"

    parsed = client.get(f"/v1/materials/{material['id']}", headers=auth(token)).json()
    assert parsed["state"] == "parsed"
    assert parsed["profile"]["kind"] == "resume"
    assert parsed["profile"]["work_experience"] == []

    confirmed = _confirm(client, token, parsed, parsed["profile"])
    assert confirmed.status_code == 201, confirmed.text


def test_parse_requires_consent_and_jd_role_context(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _post_text(
        client, token, kind="resume", title="简历", text=REVIEWED_RESUME_TEXT
    ).json()
    without_consent = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": False,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert without_consent.status_code == 400
    assert without_consent.json()["error"]["code"] == "CONSENT_REQUIRED"

    bad_level = client.post(
        f"/v1/materials/{material['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "senior",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert bad_level.status_code == 422


def test_file_upload_docx_extract_and_confirm(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    data = make_docx(REVIEWED_RESUME_TEXT.split("\n"))
    response = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "DOCX 简历"},
        files={
            "file": (
                "resume.docx",
                data,
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    material = response.json()
    assert material["state"] == "uploaded"
    assert material["text"] is None

    extract = client.post(
        f"/v1/materials/{material['id']}/extract",
        json={"revision": material["revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert extract.status_code == 202
    job = wait_job(client, token, extract.json()["id"], worker)
    assert job["state"] == "succeeded", job

    extracted = client.get(f"/v1/materials/{material['id']}", headers=auth(token)).json()
    assert extracted["state"] == "text_ready"
    assert "校园图书借阅系统" in extracted["text"]

    confirmed = _confirm(client, token, extracted, REVIEWED_RESUME_PROFILE)
    assert confirmed.status_code == 201, confirmed.text


def test_pdf_extraction(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    data = make_pdf(["Java backend resume", "ArrayList HashMap JUnit"])
    response = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "PDF 简历"},
        files={"file": ("resume.pdf", data, "application/pdf")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    material = response.json()
    extract = client.post(
        f"/v1/materials/{material['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, extract.json()["id"], worker)
    assert job["state"] == "succeeded", job
    extracted = client.get(f"/v1/materials/{material['id']}", headers=auth(token)).json()
    assert "ArrayList" in extracted["text"]


def test_scanned_pdf_and_fake_extension_fail_clearly(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    blank_pdf = make_pdf([])
    created = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "扫描件"},
        files={"file": ("scan.pdf", blank_pdf, "application/pdf")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    extract = client.post(
        f"/v1/materials/{created['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, extract.json()["id"], worker)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "FILE_TEXT_UNAVAILABLE"
    assert job["retryable"] is False

    fake = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "伪扩展名"},
        files={"file": ("fake.pdf", b"this is plain text", "application/pdf")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert fake.status_code == 201
    extract_fake = client.post(
        f"/v1/materials/{fake.json()['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, extract_fake.json()["id"], worker)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "FILE_TEXT_UNAVAILABLE"


def test_unsupported_files_rejected(client: TestClient) -> None:
    _, token = issue_token(client)
    doc = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "doc"},
        files={"file": ("resume.doc", b"legacy", "application/msword")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert doc.status_code == 415
    assert doc.json()["error"]["code"] == "UNSUPPORTED_FILE_TYPE"

    mismatch = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "mismatch"},
        files={"file": ("resume.pdf", b"%PDF-1.4", "text/plain")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert mismatch.status_code == 415

    big = b"a" * (10 * 1024 * 1024 + 1)
    too_large = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "big"},
        files={"file": ("resume.txt", big, "text/plain")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "FILE_TOO_LARGE"


def test_ownership_isolation_and_delete_rules(client: TestClient) -> None:
    _, token_a = issue_token(client)
    _, token_b = issue_token(client)
    material_a = _post_text(
        client, token_a, kind="resume", title="A 的简历", text=REVIEWED_RESUME_TEXT
    ).json()

    assert client.get(f"/v1/materials/{material_a['id']}", headers=auth(token_b)).status_code == 404
    assert (
        client.get(f"/v1/materials/{material_a['id']}/snapshots", headers=auth(token_b)).status_code
        == 404
    )
    assert client.delete(
        f"/v1/materials/{material_a['id']}", headers={**auth(token_b), "Idempotency-Key": new_key()}
    ).status_code == 404
    assert client.get("/v1/materials", headers=auth(token_b)).json()["items"] == []

    snapshot = _confirm(client, token_a, material_a, REVIEWED_RESUME_PROFILE)
    assert snapshot.status_code == 201
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": snapshot.json()["id"],
            "jd_snapshot_id": None,
        },
        headers={**auth(token_a), "Idempotency-Key": new_key()},
    )
    assert prep.status_code == 201
    in_use = client.delete(
        f"/v1/materials/{material_a['id']}", headers={**auth(token_a), "Idempotency-Key": new_key()}
    )
    assert in_use.status_code == 409
    assert in_use.json()["error"]["code"] == "MATERIAL_IN_USE"

    deleted_prep = client.delete(
        f"/v1/preparations/{prep.json()['id']}",
        headers={**auth(token_a), "Idempotency-Key": new_key()},
    )
    assert deleted_prep.status_code == 204
    deleted = client.delete(
        f"/v1/materials/{material_a['id']}", headers={**auth(token_a), "Idempotency-Key": new_key()}
    )
    assert deleted.status_code == 204
    assert client.get(f"/v1/materials/{material_a['id']}", headers=auth(token_a)).status_code == 404


def test_unsupported_kind_and_bad_ids(client: TestClient) -> None:
    _, token = issue_token(client)
    bad = _post_text(client, token, kind="jd", title="x", text="内容", template_id="nope", template_version=9)
    assert bad.status_code == 404
    assert bad.json()["error"]["code"] == "NOT_FOUND"


def test_parse_sanitizes_unknown_and_disabled_skill_codes_for_resume_and_jd(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import json
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import SkillCode

    row_conc = db.get(SkillCode, "java.concurrency")
    row_junit = db.get(SkillCode, "test.junit")
    row_core = db.get(SkillCode, "java.core")
    assert row_conc and row_junit and row_core

    captured_prompts: list[str] = []
    resume_text = (
        "本科在读，计算机科学与技术专业。\n"
        "项目：商品缓存系统，使用 Java 集合与 Redis 缓存热点数据。\n"
        "技能：熟悉 Redis 缓存与 Java 集合。"
    )
    jd_text = (
        "Java 后端实习生\n"
        "职责：参与后端接口开发。\n"
        "要求：熟悉 Redis 缓存设计与失效策略。\n"
        "要求：掌握 Java 集合与基础并发。\n"
        "要求：具备良好的沟通能力。"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        user_prompt = body["messages"][-1]["content"]
        captured_prompts.append(user_prompt)
        if "把简历原文结构化" in user_prompt:
            payload = {
                "kind": "resume",
                "education": [
                    {
                        "item_id": "edu-1",
                        "degree": "本科在读",
                        "major": "计算机科学与技术",
                        "evidence": {"quote": "本科在读，计算机科学与技术专业。", "origin": "extracted"},
                    }
                ],
                "work_experience": [],
                "projects": [
                    {
                        "item_id": "project-1",
                        "name": "商品缓存系统",
                        "description": "使用 Java 集合与 Redis 缓存热点数据。",
                        "skill_codes": ["redis.cache", "java.core", "java.concurrency", "java.core"],
                        "evidence": {
                            "quote": "项目：商品缓存系统，使用 Java 集合与 Redis 缓存热点数据。",
                            "origin": "extracted",
                        },
                    }
                ],
                "skills": [
                    {
                        "item_id": "skill-1",
                        "name": "Redis 缓存",
                        "code": "redis.cache",
                        "evidence": {"quote": "技能：熟悉 Redis 缓存与 Java 集合。", "origin": "extracted"},
                    },
                    {
                        "item_id": "skill-2",
                        "name": "Java 集合",
                        "code": "java.core",
                        "evidence": {"quote": "技能：熟悉 Redis 缓存与 Java 集合。", "origin": "extracted"},
                    },
                ],
            }
        else:
            payload = {
                "kind": "jd",
                "role_id": "java-backend",
                "level": "intern",
                "title": "Java 后端实习生",
                "responsibilities": [
                    {
                        "item_id": "responsibility-1",
                        "text": "参与后端接口开发",
                        "evidence": {"quote": "职责：参与后端接口开发。", "origin": "extracted"},
                    }
                ],
                "requirements": [
                    {
                        "item_id": "req-1",
                        "text": "熟悉 Redis 缓存设计与失效策略",
                        "skill_codes": ["redis.cache"],
                        "importance": "must",
                        "evidence": {"quote": "要求：熟悉 Redis 缓存设计与失效策略。", "origin": "extracted"},
                    },
                    {
                        "item_id": "req-2",
                        "text": "掌握 Java 集合与基础并发",
                        "skill_codes": ["redis.cache", "java.core", "java.concurrency", "java.core"],
                        "importance": "nice",
                        "evidence": {"quote": "要求：掌握 Java 集合与基础并发。", "origin": "extracted"},
                    },
                    {
                        "item_id": "req-3",
                        "text": "具备良好的沟通能力",
                        "skill_codes": [],
                        "importance": "nice",
                        "evidence": {"quote": "要求：具备良好的沟通能力。", "origin": "extracted"},
                    },
                ],
            }
        return httpx.Response(
            200,
            json={"id": "mock-req", "choices": [{"message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(settings, transport=httpx.MockTransport(handler))
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    _, token = issue_token(client)
    try:
        # 测试词表只启用 java.core
        row_conc.enabled = False
        row_junit.enabled = False
        row_core.enabled = True
        db.commit()

        resume = _post_text(client, token, kind="resume", title="Redis 简历", text=resume_text).json()
        job_r = client.post(
            f"/v1/materials/{resume['id']}/parse",
            json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert job_r.status_code == 202
        done_r = wait_job(client, token, job_r.json()["id"], worker)
        assert done_r["state"] == "succeeded", done_r

        parsed_r = client.get(f"/v1/materials/{resume['id']}", headers=auth(token)).json()
        assert parsed_r["state"] == "parsed"
        profile_r = parsed_r["profile"]
        assert profile_r["skills"][0]["item_id"] == "skill-1"
        assert profile_r["skills"][0]["name"] == "Redis 缓存"
        assert profile_r["skills"][0]["code"] is None
        assert profile_r["skills"][0]["evidence"]["quote"] == "技能：熟悉 Redis 缓存与 Java 集合。"
        assert profile_r["skills"][1]["code"] == "java.core"
        assert profile_r["projects"][0]["skill_codes"] == ["java.core"]

        jd = _post_text(client, token, kind="jd", title="Redis JD", text=jd_text).json()
        job_j = client.post(
            f"/v1/materials/{jd['id']}/parse",
            json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert job_j.status_code == 202
        done_j = wait_job(client, token, job_j.json()["id"], worker)
        assert done_j["state"] == "succeeded", done_j

        parsed_j = client.get(f"/v1/materials/{jd['id']}", headers=auth(token)).json()
        reqs = parsed_j["profile"]["requirements"]
        assert reqs[0]["item_id"] == "req-1"
        assert reqs[0]["skill_codes"] == []
        assert reqs[0]["importance"] == "must"
        assert reqs[1]["skill_codes"] == ["java.core"]
        assert reqs[1]["importance"] == "nice"
        assert reqs[2]["skill_codes"] == []

        assert len(captured_prompts) == 2
        for prompt in captured_prompts:
            assert "java.core" in prompt
            assert "Java 基础" in prompt
            assert "java.concurrency" not in prompt
            assert "test.junit" not in prompt

        # 空词表时结构和证据有效仍解析成功
        row_core.enabled = False
        db.commit()
        resume_empty = _post_text(client, token, kind="resume", title="空词表简历", text=resume_text).json()
        job_empty = client.post(
            f"/v1/materials/{resume_empty['id']}/parse",
            json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        done_empty = wait_job(client, token, job_empty.json()["id"], worker)
        assert done_empty["state"] == "succeeded", done_empty
        parsed_empty = client.get(f"/v1/materials/{resume_empty['id']}", headers=auth(token)).json()["profile"]
        assert parsed_empty["skills"][0]["code"] is None
        assert parsed_empty["skills"][1]["code"] is None
        assert parsed_empty["projects"][0]["skill_codes"] == []
    finally:
        row_core.enabled = True
        row_conc.enabled = True
        row_junit.enabled = True
        db.commit()


def test_parse_invalid_evidence_still_fails_and_manual_confirm_rejects_unknown_with_details(
    client: TestClient, worker, monkeypatch
) -> None:
    import json
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    bad_payload = {
        "kind": "resume",
        "education": [],
        "work_experience": [],
        "projects": [],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Redis",
                "code": "redis.cache",
                "evidence": {"quote": "原文里根本没有这一句", "origin": "extracted"},
            }
        ],
    }
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    provider = DeepSeekProvider(
        settings,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={"id": "req", "choices": [{"message": {"content": json.dumps(bad_payload, ensure_ascii=False)}}]},
            )
        ),
    )
    monkeypatch.setattr(runner, "get_provider", lambda _: provider)

    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="坏引用", text=REVIEWED_RESUME_TEXT).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "failed"
    assert done["error"]["code"] == "EVIDENCE_INVALID"

    # 人工确认带未知技能编码返回 PROFILE_INVALID 及机器可读字段详情
    bad_confirm = dict(REVIEWED_RESUME_PROFILE)
    bad_confirm["projects"] = [
        {**REVIEWED_RESUME_PROFILE["projects"][0], "skill_codes": ["made.up.skill"]}
    ]
    res = _confirm(client, token, mat, bad_confirm)
    assert res.status_code == 400
    err = res.json()["error"]
    assert err["code"] == "PROFILE_INVALID"
    assert "section=projects" in err["details"]
    assert "item_id=project-1" in err["details"]
    assert "field=skill_codes" in err["details"]
    assert "made.up.skill" in err["details"]


def test_parse_work_experience_four_field_mismatch_correction_and_grounding(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """复现截图四条字段错误：首次纠正成功并只写一次；连续失败保留原文并可查明原因。"""
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job, MaterialDraft

    resume_text = (
        "示例实验室（A区）｜开发实习生｜负责库存接口开发\n"
        "项目：库存工具，使用 Java 和 Redis 缓存查询。\n"
        "SENTINEL_MATERIAL_MARKER"
    )
    bad_work_payload = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "name": "示例实验室",
                "description": "负责库存接口开发",
                "skill_codes": ["java.core"],
                "evidence": {
                    "quote": "示例实验室（A区）｜开发实习生｜负责库存接口开发",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    good_work_payload = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例实验室 (A区)",
                "title": "开发实习生",
                "description": "负责库存接口开发",
                "evidence": {
                    "quote": "示例实验室（A区）｜开发实习生｜负责库存接口开发",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [
            {
                "item_id": "project-1",
                "name": "库存工具",
                "description": "使用 Java 和 Redis 缓存查询",
                "skill_codes": ["java.core", "unknown.redis", "java.core"],
                "evidence": {
                    "quote": "项目：库存工具，使用 Java 和 Redis 缓存查询。",
                    "origin": "extracted",
                },
            }
        ],
        "skills": [],
    }
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )

    captured_prompts: list[str] = []
    attempt_count = 0

    def recovering_handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt_count
        attempt_count += 1
        body = json.loads(request.content)
        captured_prompts.append(body["messages"][-1]["content"])
        payload = bad_work_payload if attempt_count == 1 else good_work_payload
        return httpx.Response(
            200,
            json={"id": f"req-{attempt_count}", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload, ensure_ascii=False)}}]},
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(recovering_handler)),
    )
    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="实习简历", text=resume_text).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "succeeded", done
    assert attempt_count == 2
    assert "上一次解析纠正要求" in captured_prompts[1]
    assert "work_experience.0.organization" in captured_prompts[1]

    saved_mat = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert saved_mat["state"] == "parsed"
    assert saved_mat["profile"]["work_experience"][0]["organization"] == "示例实验室 (A区)"
    assert saved_mat["profile"]["projects"][0]["skill_codes"] == ["java.core"]

    # 连续两次返回四字段错位：保留原文且 profile 为空，任务标记 stage=parse 且可重试
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"id": "req-bad", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad_work_payload, ensure_ascii=False)}}]},
                )
            ),
        ),
    )
    mat2 = _post_text(client, token, kind="resume", title="连续失败简历", text=resume_text).json()
    job2_r = client.post(
        f"/v1/materials/{mat2['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, job2_r.json()["id"], worker)
    assert failed["state"] == "failed"
    assert failed["retryable"] is True
    assert failed["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "stage=parse" in failed["error"]["details"]
    assert "SENTINEL_MATERIAL_MARKER" not in json.dumps(failed, ensure_ascii=False)
    draft2 = client.get(f"/v1/materials/{mat2['id']}", headers=auth(token)).json()
    assert draft2["state"] == "text_ready"
    assert draft2["text"] == resume_text
    assert draft2["profile"] is None

    # 编造工作经历单位或职务、纯空白引用、JD 编造岗位码均被拒绝；跨岗位有效 JD 保留事实
    from app.services.text import validate_profile

    fabricated_org = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "编造的宇宙科技公司",
                "title": "开发实习生",
                "description": "负责库存接口开发",
                "evidence": {
                    "quote": "示例实验室（A区）｜开发实习生｜负责库存接口开发",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as fab_err:
        validate_profile(fabricated_org, "resume", resume_text, allow_user_confirmed=False)
    assert fab_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in fab_err.value.details

    blank_quote_profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [],
        "projects": [
            {
                "item_id": "project-1",
                "name": "库存工具",
                "description": "使用 Java",
                "skill_codes": [],
                "evidence": {"quote": " ", "origin": "extracted"},
            }
        ],
        "skills": [],
    }
    with pytest.raises(AppError) as blank_err:
        validate_profile(blank_quote_profile, "resume", "a b", allow_user_confirmed=False)
    assert blank_err.value.code == "EVIDENCE_INVALID"
    assert "reason=blank_quote" in blank_err.value.details

    # JD 模型编造目录外岗位码 -> AI_INVALID_OUTPUT (reason=invalid_jd_catalog_role)
    bad_jd_role_payload = {
        "kind": "jd",
        "role_id": "invented-role-999",
        "level": "intern",
        "title": "后端实习",
        "responsibilities": [
            {"item_id": "resp-1", "text": "负责接口开发", "evidence": {"quote": "负责接口开发", "origin": "extracted"}}
        ],
        "requirements": [
            {"item_id": "req-1", "text": "熟悉 Java", "skill_codes": [], "importance": "must", "evidence": {"quote": "熟悉 Java", "origin": "extracted"}}
        ],
    }
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"id": "req-jd-bad", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad_jd_role_payload, ensure_ascii=False)}}]},
                )
            ),
        ),
    )
    jd_mat = _post_text(client, token, kind="jd", title="JD", text="负责接口开发\n熟悉 Java").json()
    jd_job_r = client.post(
        f"/v1/materials/{jd_mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    jd_failed = wait_job(client, token, jd_job_r.json()["id"], worker)
    assert jd_failed["state"] == "failed"
    assert jd_failed["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "reason=invalid_jd_catalog_role" in jd_failed["error"]["details"]
    assert jd_failed["retryable"] is True

    # 解析后模型记录目录内另一个有效组合（如 java-backend / junior，与当前准备 intern 不同）时保留事实
    other_valid_jd_payload = {
        **bad_jd_role_payload,
        "role_id": "java-backend",
        "level": "junior",
    }
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={"id": "req-jd-other", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(other_valid_jd_payload, ensure_ascii=False)}}]},
                )
            ),
        ),
    )
    jd_mat2 = _post_text(client, token, kind="jd", title="初级JD", text="负责接口开发\n熟悉 Java").json()
    jd_job2_r = client.post(
        f"/v1/materials/{jd_mat2['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    jd_ok = wait_job(client, token, jd_job2_r.json()["id"], worker)
    assert jd_ok["state"] == "succeeded", jd_ok
    jd_saved = client.get(f"/v1/materials/{jd_mat2['id']}", headers=auth(token)).json()
    assert jd_saved["profile"]["level"] == "junior"
    # 确认后在匹配创建入口仍校验 JD 与本次准备设置一致
    jd_snap = _confirm(client, token, jd_saved, jd_saved["profile"]).json()
    res_mat = _post_text(client, token, kind="resume", title="简历", text=REVIEWED_RESUME_TEXT).json()
    res_snap = _confirm(client, token, res_mat, REVIEWED_RESUME_PROFILE).json()
    prep_mismatch = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": res_snap["id"],
            "jd_snapshot_id": jd_snap["id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert prep_mismatch.status_code == 409
    assert prep_mismatch.json()["error"]["code"] == "ROLE_LEVEL_MISMATCH"

    # 若准备绑定后处于不一致状态，匹配入口仍会以 ROLE_LEVEL_MISMATCH 阻断
    from app.models import Preparation
    import uuid as _uuid

    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": res_snap["id"],
            "jd_snapshot_id": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    prep_row = db.get(Preparation, _uuid.UUID(prep["id"]))
    assert prep_row is not None
    prep_row.jd_snapshot_id = _uuid.UUID(jd_snap["id"])
    db.commit()
    match_mismatch = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert match_mismatch.status_code == 409
    assert match_mismatch.json()["error"]["code"] == "ROLE_LEVEL_MISMATCH"


def test_parse_redacts_synthetic_extra_key_unknown_id_usage_and_preserves_legacy_missing(
    client: TestClient, worker, db, monkeypatch, caplog
) -> None:
    import logging
    import uuid as _uuid
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.models import Job

    resume_text = "示例甲实验室｜后端实习生｜维护库存接口"
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    captured_requests: list[dict] = []

    def bad_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured_requests.append(body)
        if len(captured_requests) == 1:
            # 第 1 次返回额外未知键 + 额外 usage + 结构化请求 ID
            payload = {
                "kind": "resume",
                "education": [],
                "work_experience": [
                    {
                        "item_id": "SYNTHETIC_ITEM_7469",
                        "organization": "示例甲实验室",
                        "title": "后端实习生",
                        "description": "维护库存接口",
                        "SYNTHETIC_MARKER_7469": "leak-value",
                        "evidence": {"quote": resume_text, "origin": "extracted"},
                    }
                ],
                "projects": [],
                "skills": [],
            }
        else:
            # 第 2 次返回未知 item_id 且引文不存在（触发 validate_profile 拒绝）
            payload = {
                "kind": "resume",
                "education": [],
                "work_experience": [
                    {
                        "item_id": "SYNTHETIC_ITEM_7469",
                        "organization": "示例甲实验室",
                        "title": "后端实习生",
                        "description": "维护库存接口",
                        "evidence": {"quote": "不在原文中的引文", "origin": "extracted"},
                    }
                ],
                "projects": [],
                "skills": [],
            }
        return httpx.Response(
            200,
            json={
                "id": {"unexpected": "SYNTHETIC_REQ_7469"},
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": -1,
                    "SYNTHETIC_USAGE_7469": 88,
                },
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload, ensure_ascii=False)},
                    }
                ],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(bad_handler)),
    )

    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="安全摘要回归", text=resume_text).json()
    with caplog.at_level(logging.INFO):
        job_r = client.post(
            f"/v1/materials/{mat['id']}/parse",
            json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        failed = wait_job(client, token, job_r.json()["id"], worker)

    assert failed["state"] == "failed"
    assert len(captured_requests) == 2
    correction_prompt = captured_requests[1]["messages"][-1]["content"]
    assert "work_experience.0.extra_field" in correction_prompt
    assert "SYNTHETIC_MARKER_7469" not in correction_prompt
    assert "SYNTHETIC_ITEM_7469" not in correction_prompt

    failed_json = json.dumps(failed, ensure_ascii=False)
    assert "SYNTHETIC_MARKER_7469" not in failed_json
    assert "SYNTHETIC_ITEM_7469" not in failed_json
    assert "SYNTHETIC_USAGE_7469" not in failed_json
    assert "SYNTHETIC_REQ_7469" not in failed_json

    row = db.get(Job, _uuid.UUID(failed["id"]))
    assert row is not None
    records_json = json.dumps(row.ai_call_records, ensure_ascii=False)
    assert "SYNTHETIC_MARKER_7469" not in records_json
    assert "SYNTHETIC_ITEM_7469" not in records_json
    assert "SYNTHETIC_USAGE_7469" not in records_json
    assert "SYNTHETIC_REQ_7469" not in records_json
    assert row.ai_call_records[0]["usage"] == {"prompt_tokens": 10}
    assert row.ai_call_records[0]["request_id"] == ""
    assert "work_experience.0.extra_field" in row.ai_call_records[0]["field_paths"]
    assert "SYNTHETIC_MARKER_7469" not in caplog.text
    assert "SYNTHETIC_ITEM_7469" not in caplog.text

    # 历史缺值正例（含旧合成标记）：读出仍可看到 organization/title 两处缺失且任务仍可重试
    legacy_mat = _post_text(client, token, kind="resume", title="历史缺值正例", text=resume_text).json()
    legacy_job_r = client.post(
        f"/v1/materials/{legacy_mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    legacy_job_id = _uuid.UUID(legacy_job_r.json()["id"])
    legacy_job = db.get(Job, legacy_job_id)
    assert legacy_job is not None
    legacy_job.state = "failed"
    legacy_job.attempt = 1
    legacy_job.error_code = "AI_INVALID_OUTPUT"
    legacy_job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": [
            "work_experience.0.organization: Field required",
            "work_experience.0.title: Field required",
            "work_experience.0.SYNTHETIC_OLD_EXTRA: Extra inputs are not permitted",
        ],
        "request_id": "legacy-missing",
    }
    db.commit()

    queried_legacy = client.get(f"/v1/jobs/{legacy_job_id}", headers=auth(token)).json()
    assert queried_legacy["retryable"] is True
    assert "work_experience.0.organization: Field required" in queried_legacy["error"]["details"]
    assert "work_experience.0.title: Field required" in queried_legacy["error"]["details"]
    assert "SYNTHETIC_OLD_EXTRA" not in json.dumps(queried_legacy, ensure_ascii=False)


def test_parse_null_char_rejected_and_explicit_retry_succeeds(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    resume_text = "示例甲实验室｜后端实习生｜维护库存接口"
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count <= 2:
            # 两次都返回含 \u0000 的工作经历描述
            raw_json = (
                '{"kind":"resume","education":[],"work_experience":[{'
                '"item_id":"work-1","organization":"示例甲实验室","title":"后端实习生",'
                '"description":"维护\\u0000库存接口",'
                '"evidence":{"quote":"示例甲实验室｜后端实习生｜维护库存接口","origin":"extracted"}'
                '}],"projects":[],"skills":[]}'
            )
        else:
            raw_json = json.dumps(
                {
                    "kind": "resume",
                    "education": [],
                    "work_experience": [
                        {
                            "item_id": "work-1",
                            "organization": "示例甲实验室",
                            "title": "后端实习生",
                            "description": "维护库存接口",
                            "evidence": {"quote": resume_text, "origin": "extracted"},
                        }
                    ],
                    "projects": [],
                    "skills": [],
                },
                ensure_ascii=False,
            )
        return httpx.Response(
            200,
            json={
                "id": f"req-null-{call_count}",
                "choices": [{"finish_reason": "stop", "message": {"content": raw_json}}],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="空字符拦截", text=resume_text).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, job_r.json()["id"], worker)
    assert call_count == 2
    assert failed["state"] == "failed"
    assert failed["retryable"] is True
    assert failed["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "reason=invalid_json_text" in failed["error"]["details"]

    draft_after_fail = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert draft_after_fail["profile"] is None
    assert draft_after_fail["text"] == resume_text
    assert draft_after_fail["state"] == "text_ready"

    # 显式重试同一个 Job 可成功
    retry_r = client.post(
        f"/v1/jobs/{failed['id']}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry_r.status_code == 202
    done = wait_job(client, token, failed["id"], worker)
    assert done["state"] == "succeeded"
    assert done["attempt"] == 2
    draft_after_ok = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert draft_after_ok["state"] == "parsed"
    assert draft_after_ok["profile"]["work_experience"][0]["organization"] == "示例甲实验室"


def test_work_experience_same_entry_grounding_rejects_splicing_and_allows_multiline_pdf_and_correction(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.text import validate_profile

    two_entries_text = (
        "示例甲实验室｜后端实习生｜维护库存接口\n"
        "示例乙银行｜数据实习生｜开发台账工具"
    )
    # 1) 反例：短引文跨段拼接（组织取第一段，职务和引文取第二段）
    short_spliced = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {
                    "quote": "示例乙银行｜数据实习生｜开发台账工具",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as short_err:
        validate_profile(short_spliced, "resume", two_entries_text, allow_user_confirmed=False)
    assert short_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in short_err.value.details

    # 2) 反例：把两段都包进一条长引文
    long_spliced = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {
                    "quote": two_entries_text,
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as long_err:
        validate_profile(long_spliced, "resume", two_entries_text, allow_user_confirmed=False)
    assert long_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in long_err.value.details

    # 2b) 反例：空行分隔两段且长引文跨空行包住两段
    blank_sep_text = (
        "示例甲实验室\n后端实习生\n维护库存接口\n\n"
        "示例乙银行\n数据实习生\n开发台账工具"
    )
    blank_long = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {"quote": blank_sep_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as blank_long_err:
        validate_profile(blank_long, "resume", blank_sep_text, allow_user_confirmed=False)
    assert blank_long_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in blank_long_err.value.details

    # 3) 正例：同条多行（连续行覆盖组织与职务）与 PDF 空格/换行/标点差异
    multiline_text = (
        "示例甲实验室\n"
        "后端实习生｜2025.07 - 2025.09\n"
        "维护库存接口与单元测试"
    )
    multiline_profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口与单元测试",
                "evidence": {
                    "quote": "示例甲实验室\n后端实习生｜2025.07 - 2025.09",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(multiline_profile, "resume", multiline_text, allow_user_confirmed=False)

    pdf_text = "示例实验室 ( A 区 )\n后端 开发\n实习生 | 2025.06-2025.09\n负责库存接口"
    pdf_profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例实验室（A区）",
                "title": "后端开发实习生",
                "description": "负责库存接口",
                "evidence": {
                    "quote": "示例实验室 ( A 区 )\n后端 开发\n实习生 | 2025.06-2025.09",
                    "origin": "extracted",
                },
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(pdf_profile, "resume", pdf_text, allow_user_confirmed=False)

    # 4) 正例：确实无工作经历的简历（仅教育与项目）
    no_work_text = "本科在读，计算机科学与技术\n项目：库存工具，使用 Java。"
    no_work_profile = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [
            {
                "item_id": "project-1",
                "name": "库存工具",
                "description": "使用 Java",
                "skill_codes": [],
                "evidence": {"quote": "项目：库存工具，使用 Java。", "origin": "extracted"},
            }
        ],
        "skills": [],
    }
    validate_profile(no_work_profile, "resume", no_work_text, allow_user_confirmed=False)

    # 5) 人工确认路径仍允许保存人工核对后的工作经历
    validate_profile(
        {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": "示例甲实验室",
                    "title": "后端实习生",
                    "description": "维护库存接口",
                    "evidence": {"quote": "示例甲实验室｜后端实习生｜维护库存接口", "origin": "user_confirmed"},
                }
            ],
            "projects": [],
            "skills": [],
        },
        "resume",
        two_entries_text,
        allow_user_confirmed=True,
    )

    # 6) Worker 端到端：第一次返回拼接错误（不落库），针对性纠正后第二次成功
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    prompts_seen: list[str] = []
    good_two_entries = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口",
                "evidence": {"quote": "示例甲实验室｜后端实习生｜维护库存接口", "origin": "extracted"},
            },
            {
                "item_id": "work-2",
                "organization": "示例乙银行",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {"quote": "示例乙银行｜数据实习生｜开发台账工具", "origin": "extracted"},
            },
        ],
        "projects": [],
        "skills": [],
    }

    def splice_then_fix(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts_seen.append(body["messages"][-1]["content"])
        payload = short_spliced if len(prompts_seen) == 1 else good_two_entries
        return httpx.Response(
            200,
            json={
                "id": f"req-splice-{len(prompts_seen)}",
                "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(payload, ensure_ascii=False)}}],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(splice_then_fix)),
    )
    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="双段工作经历", text=two_entries_text).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "succeeded", done
    assert len(prompts_seen) == 2
    assert "reason=ungrounded_work_experience" in prompts_seen[1]
    saved = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert saved["state"] == "parsed"
    assert len(saved["profile"]["work_experience"]) == 2
    assert saved["profile"]["work_experience"][0]["organization"] == "示例甲实验室"
    assert saved["profile"]["work_experience"][0]["title"] == "后端实习生"
    assert "示例甲实验室" in saved["profile"]["work_experience"][0]["evidence"]["quote"]
    assert "后端实习生" in saved["profile"]["work_experience"][0]["evidence"]["quote"]
    assert saved["profile"]["work_experience"][1]["organization"] == "示例乙银行"
    assert saved["profile"]["work_experience"][1]["title"] == "数据实习生"
    assert "示例乙银行" in saved["profile"]["work_experience"][1]["evidence"]["quote"]
    assert "数据实习生" in saved["profile"]["work_experience"][1]["evidence"]["quote"]

    # 两次都拼接失败时，错误结果不落库且保留原文
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "id": "req-splice-bad",
                        "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(long_spliced, ensure_ascii=False)}}],
                    },
                )
            ),
        ),
    )
    mat_bad = _post_text(client, token, kind="resume", title="连续拼接失败", text=two_entries_text).json()
    job_bad = client.post(
        f"/v1/materials/{mat_bad['id']}/parse",
        json={"revision": 1, "external_processing_consent": True, "role_id": "java-backend", "level": "intern"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, job_bad.json()["id"], worker)
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in failed["error"]["details"]
    assert failed["retryable"] is True
    mat_after_fail = client.get(f"/v1/materials/{mat_bad['id']}", headers=auth(token)).json()
    assert mat_after_fail["profile"] is None
    assert mat_after_fail["text"] == two_entries_text


def test_consecutive_labeled_work_experience_segmentation_and_worker_correction(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.text import validate_profile

    # 1) 无空行连续字段标签反例：原文有两段，单条引文跨两段拼接必须拒绝
    labeled_no_blank_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "职责：维护库存接口\n"
        "公司：示例乙银行\n"
        "职位：数据实习生\n"
        "职责：开发台账工具"
    )
    spliced_labeled = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {"quote": labeled_no_blank_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as err_cn:
        validate_profile(
            spliced_labeled, "resume", labeled_no_blank_text, allow_user_confirmed=False
        )
    assert err_cn.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in err_cn.value.details

    # 2) 英文 Company/Title 对照（跨段拒绝，分段通过）
    en_text = (
        "Company: Alpha Lab\n"
        "Title: Backend Intern\n"
        "Responsibilities: Maintain inventory APIs\n"
        "Company: Beta Bank\n"
        "Title: Data Intern\n"
        "Responsibilities: Build ledger tools"
    )
    en_spliced = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "Alpha Lab",
                "title": "Data Intern",
                "description": "Build ledger tools",
                "evidence": {"quote": en_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as err_en:
        validate_profile(en_spliced, "resume", en_text, allow_user_confirmed=False)
    assert err_en.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in err_en.value.details

    en_valid = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "Alpha Lab",
                "title": "Backend Intern",
                "description": "Maintain inventory APIs",
                "evidence": {
                    "quote": "Company: Alpha Lab\nTitle: Backend Intern\nResponsibilities: Maintain inventory APIs",
                    "origin": "extracted",
                },
            },
            {
                "item_id": "work-2",
                "organization": "Beta Bank",
                "title": "Data Intern",
                "description": "Build ledger tools",
                "evidence": {
                    "quote": "Company: Beta Bank\nTitle: Data Intern\nResponsibilities: Build ledger tools",
                    "origin": "extracted",
                },
            },
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(en_valid, "resume", en_text, allow_user_confirmed=False)

    # 3) 同公司两段任职：跨两段引文拒绝；各自落在对应段内的引文通过
    same_org_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "职责：维护库存接口\n"
        "公司：示例甲实验室\n"
        "职位：正式后端工程师\n"
        "职责：负责订单服务"
    )
    same_org_spliced = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "正式后端工程师",
                "description": "负责订单服务",
                "evidence": {"quote": same_org_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as err_same:
        validate_profile(
            same_org_spliced, "resume", same_org_text, allow_user_confirmed=False
        )
    assert err_same.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in err_same.value.details

    same_org_valid = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口",
                "evidence": {
                    "quote": "公司：示例甲实验室\n职位：后端实习生\n职责：维护库存接口",
                    "origin": "extracted",
                },
            },
            {
                "item_id": "work-2",
                "organization": "示例甲实验室",
                "title": "正式后端工程师",
                "description": "负责订单服务",
                "evidence": {
                    "quote": "公司：示例甲实验室\n职位：正式后端工程师\n职责：负责订单服务",
                    "origin": "extracted",
                },
            },
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(same_org_valid, "resume", same_org_text, allow_user_confirmed=False)

    # 4) 公司与职务分属相邻行的合法输入
    adjacent_text = "公司：示例甲实验室\n职位：后端实习生\n职责：维护库存接口"
    adjacent_valid = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口",
                "evidence": {"quote": "公司：示例甲实验室\n职位：后端实习生", "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(adjacent_valid, "resume", adjacent_text, allow_user_confirmed=False)

    # 5) 单段包含多条编号职责（不应误判成多段经历）
    numbered_bullets_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "职责：\n"
        "1. 维护库存接口｜支持并发查询\n"
        "2. 开发台账工具 - 优化对账流程\n"
        "3. 编写 JUnit 单元测试"
    )
    numbered_bullets_valid = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口、开发台账工具并编写单元测试",
                "evidence": {"quote": numbered_bullets_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(
        numbered_bullets_valid, "resume", numbered_bullets_text, allow_user_confirmed=False
    )

    # 6) 确实没有工作经历的输入
    no_work_text = "本科在读，计算机科学与技术\n项目：校园图书借阅系统，使用 Java。"
    no_work_valid = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [
            {
                "item_id": "project-1",
                "name": "校园图书借阅系统",
                "description": "使用 Java",
                "skill_codes": [],
                "evidence": {"quote": "项目：校园图书借阅系统，使用 Java。", "origin": "extracted"},
            }
        ],
        "skills": [],
    }
    validate_profile(no_work_valid, "resume", no_work_text, allow_user_confirmed=False)

    # 7) Worker 端到端：先返回跨段引文（不落库），再返回分段同条引文后成功入库
    good_two_labeled = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "维护库存接口",
                "evidence": {
                    "quote": "公司：示例甲实验室\n职位：后端实习生\n职责：维护库存接口",
                    "origin": "extracted",
                },
            },
            {
                "item_id": "work-2",
                "organization": "示例乙银行",
                "title": "数据实习生",
                "description": "开发台账工具",
                "evidence": {
                    "quote": "公司：示例乙银行\n职位：数据实习生\n职责：开发台账工具",
                    "origin": "extracted",
                },
            },
        ],
        "projects": [],
        "skills": [],
    }
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    calls = 0

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        payload = spliced_labeled if calls == 1 else good_two_labeled
        return httpx.Response(
            200,
            json={
                "id": f"req-lbl-{calls}",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload, ensure_ascii=False)},
                    }
                ],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    _, token = issue_token(client)
    mat = _post_text(
        client, token, kind="resume", title="连续字段标签简历", text=labeled_no_blank_text
    ).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "succeeded", done
    assert calls == 2
    saved = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert saved["state"] == "parsed"
    assert len(saved["profile"]["work_experience"]) == 2
    assert saved["profile"]["work_experience"][0]["organization"] == "示例甲实验室"
    assert saved["profile"]["work_experience"][0]["title"] == "后端实习生"
    assert saved["profile"]["work_experience"][1]["organization"] == "示例乙银行"
    assert saved["profile"]["work_experience"][1]["title"] == "数据实习生"


def test_parse_empty_profile_and_duplicate_item_id_correction_and_user_confirm_boundary(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    resume_text = "本科在读，计算机科学与技术\n项目：库存工具，使用 Java。"
    valid_payload = {
        "kind": "resume",
        "education": [
            {
                "item_id": "edu-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [
            {
                "item_id": "project-1",
                "name": "库存工具",
                "description": "使用 Java",
                "skill_codes": [],
                "evidence": {"quote": "项目：库存工具，使用 Java。", "origin": "extracted"},
            }
        ],
        "skills": [],
    }
    empty_obj = {"kind": "resume"}
    empty_arrays = {
        "kind": "resume",
        "education": [],
        "work_experience": [],
        "projects": [],
        "skills": [],
    }
    dup_payload = {
        "kind": "resume",
        "education": [
            {
                "item_id": "dup-1",
                "degree": "本科在读",
                "major": "计算机科学与技术",
                "evidence": {"quote": "本科在读，计算机科学与技术", "origin": "extracted"},
            }
        ],
        "work_experience": [],
        "projects": [
            {
                "item_id": "dup-1",
                "name": "库存工具",
                "description": "使用 Java",
                "skill_codes": [],
                "evidence": {"quote": "项目：库存工具，使用 Java。", "origin": "extracted"},
            }
        ],
        "skills": [],
    }

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    _, token = issue_token(client)

    for first_bad, expected_reason in (
        (empty_obj, "empty_profile"),
        (empty_arrays, "empty_profile"),
        (dup_payload, "duplicate_item_id"),
    ):
        prompts: list[str] = []

        def corr_handler(request: httpx.Request, bad=first_bad) -> httpx.Response:
            body = json.loads(request.content)
            prompts.append(body["messages"][-1]["content"])
            payload = bad if len(prompts) == 1 else valid_payload
            return httpx.Response(
                200,
                json={
                    "id": f"req-{len(prompts)}",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(payload, ensure_ascii=False)},
                        }
                    ],
                },
            )

        monkeypatch.setattr(
            runner,
            "get_provider",
            lambda _, h=corr_handler: DeepSeekProvider(settings, transport=httpx.MockTransport(h)),
        )
        mat = _post_text(client, token, kind="resume", title="纠正测试", text=resume_text).json()
        job_r = client.post(
            f"/v1/materials/{mat['id']}/parse",
            json={
                "revision": 1,
                "external_processing_consent": True,
                "role_id": "java-backend",
                "level": "intern",
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        done = wait_job(client, token, job_r.json()["id"], worker)
        assert done["state"] == "succeeded", (expected_reason, done)
        assert len(prompts) == 2
        assert f"reason={expected_reason}" in prompts[1]

    # 用户确认提交的空结果或重复 ID 仍直接返回 400 PROFILE_INVALID，不当作 AI 重试
    mat_confirm = _post_text(
        client, token, kind="resume", title="人工确认拦截", text=resume_text
    ).json()
    res_empty = _confirm(client, token, mat_confirm, empty_arrays)
    assert res_empty.status_code == 400
    assert res_empty.json()["error"]["code"] == "PROFILE_INVALID"
    dup_confirm = {
        **dup_payload,
        "education": [
            {**dup_payload["education"][0], "evidence": {"quote": "本科在读，计算机科学与技术", "origin": "user_confirmed"}}
        ],
        "projects": [
            {**dup_payload["projects"][0], "evidence": {"quote": "项目：库存工具，使用 Java。", "origin": "user_confirmed"}}
        ],
    }
    res_dup = _confirm(client, token, mat_confirm, dup_confirm)
    assert res_dup.status_code == 400
    assert res_dup.json()["error"]["code"] == "PROFILE_INVALID"


def test_explicit_work_experience_labels_block_responsibility_entity_substitution(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.text import validate_profile

    labeled_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "职责：为数据实习生编写上手手册，并对接示例乙银行。"
    )
    # 1) 两个独立反例：职责中的“数据实习生”和“示例乙银行”不得替代显式公司或职位
    wrong_title = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "编写上手手册",
                "evidence": {"quote": labeled_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as title_err:
        validate_profile(wrong_title, "resume", labeled_text, allow_user_confirmed=False)
    assert title_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in title_err.value.details
    assert "field=title" in title_err.value.details

    wrong_org = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例乙银行",
                "title": "后端实习生",
                "description": "对接示例乙银行",
                "evidence": {"quote": labeled_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as org_err:
        validate_profile(wrong_org, "resume", labeled_text, allow_user_confirmed=False)
    assert org_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in org_err.value.details
    assert "field=organization" in org_err.value.details

    # 即使引文故意省略“公司：”行，所在段落已显式标注公司，仍不得用职责实体充当公司
    omitted_org_quote = "职位：后端实习生\n职责：为数据实习生编写上手手册，并对接示例乙银行。"
    wrong_org_short_quote = {
        **wrong_org,
        "work_experience": [
            {
                **wrong_org["work_experience"][0],
                "evidence": {"quote": omitted_org_quote, "origin": "extracted"},
            }
        ],
    }
    with pytest.raises(AppError) as short_quote_err:
        validate_profile(
            wrong_org_short_quote, "resume", labeled_text, allow_user_confirmed=False
        )
    assert short_quote_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in short_quote_err.value.details
    assert "field=organization" in short_quote_err.value.details

    # 正例：正确组织与职务通过
    right_labeled = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "后端实习生",
                "description": "为数据实习生编写上手手册并对接示例乙银行",
                "evidence": {"quote": labeled_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(right_labeled, "resume", labeled_text, allow_user_confirmed=False)

    # 1b) 无职责标签的叙述句不得作为显式职位字段的续行
    unlabeled_resp_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "为数据实习生编写手册。"
    )
    wrong_unlabeled_title = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "数据实习生",
                "description": "编写手册",
                "evidence": {"quote": unlabeled_resp_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as unlabeled_title_err:
        validate_profile(
            wrong_unlabeled_title,
            "resume",
            unlabeled_resp_text,
            allow_user_confirmed=False,
        )
    assert unlabeled_title_err.value.code == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in unlabeled_title_err.value.details
    assert "field=title" in unlabeled_title_err.value.details

    right_unlabeled_title = {
        **wrong_unlabeled_title,
        "work_experience": [
            {
                **wrong_unlabeled_title["work_experience"][0],
                "title": "后端实习生",
            }
        ],
    }
    validate_profile(
        right_unlabeled_title, "resume", unlabeled_resp_text, allow_user_confirmed=False
    )

    # 1c) 显式字段值必须完整匹配：公司后缀与职务限定词不得被截短
    full_value_text = "公司：示例大学出版社\n职位：前端工程师助理"
    wrong_short_org = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例大学",
                "title": "前端工程师助理",
                "description": "整理资料",
                "evidence": {"quote": full_value_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as short_org_err:
        validate_profile(
            wrong_short_org, "resume", full_value_text, allow_user_confirmed=False
        )
    assert short_org_err.value.code == "EVIDENCE_INVALID"
    assert "field=organization" in short_org_err.value.details

    wrong_short_title = {
        **wrong_short_org,
        "work_experience": [
            {
                **wrong_short_org["work_experience"][0],
                "organization": "示例大学出版社",
                "title": "前端工程师",
            }
        ],
    }
    with pytest.raises(AppError) as short_title_err:
        validate_profile(
            wrong_short_title, "resume", full_value_text, allow_user_confirmed=False
        )
    assert short_title_err.value.code == "EVIDENCE_INVALID"
    assert "field=title" in short_title_err.value.details

    full_value_profile = {
        **wrong_short_title,
        "work_experience": [
            {
                **wrong_short_title["work_experience"][0],
                "title": "前端工程师助理",
            }
        ],
    }
    validate_profile(
        full_value_profile, "resume", full_value_text, allow_user_confirmed=False
    )

    # 1d) 显式短职务「助理」后跟未标职责的普通短职务：不按长度截断，也不擅自续接
    short_title_text = "公司：示例甲实验室\n职位：助理\n后端开发"
    short_title_profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲实验室",
                "title": "助理",
                "description": "整理资料",
                "evidence": {"quote": short_title_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(
        short_title_profile, "resume", short_title_text, allow_user_confirmed=False
    )
    with pytest.raises(AppError) as appended_title_err:
        validate_profile(
            {
                **short_title_profile,
                "work_experience": [
                    {
                        **short_title_profile["work_experience"][0],
                        "title": "助理后端开发",
                    }
                ],
            },
            "resume",
            short_title_text,
            allow_user_confirmed=False,
        )
    assert appended_title_err.value.code == "EVIDENCE_INVALID"
    assert "field=title" in appended_title_err.value.details

    # 2) 英文 Company/Title/Responsibilities 正反例
    en_text = (
        "Company: Alpha Lab\n"
        "Title: Backend Intern\n"
        "Responsibilities: Write onboarding guide for Data Intern and integrate Beta Bank."
    )
    en_bad_title = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "Alpha Lab",
                "title": "Data Intern",
                "description": "Write onboarding guide",
                "evidence": {"quote": en_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as en_title_err:
        validate_profile(en_bad_title, "resume", en_text, allow_user_confirmed=False)
    assert en_title_err.value.code == "EVIDENCE_INVALID"
    assert "field=title" in en_title_err.value.details

    en_bad_org = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "Beta Bank",
                "title": "Backend Intern",
                "description": "Integrate Beta Bank",
                "evidence": {"quote": en_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as en_org_err:
        validate_profile(en_bad_org, "resume", en_text, allow_user_confirmed=False)
    assert en_org_err.value.code == "EVIDENCE_INVALID"
    assert "field=organization" in en_org_err.value.details

    en_good = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "Alpha Lab",
                "title": "Backend Intern",
                "description": "Write onboarding guide and integrate Beta Bank",
                "evidence": {"quote": en_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(en_good, "resume", en_text, allow_user_confirmed=False)

    # 3) 字段值折行 + 合作方/时间子字段隔离 + 全半角空格差异兼容
    wrapped_text = (
        "公司：示例甲科技 ( 北京 )\n"
        "有限公司\n"
        "职位：后端 开发\n"
        "实习生\n"
        "合作方：示例乙银行\n"
        "时间：2025.07 - 2025.09\n"
        "职责：为数据实习生编写上手手册。"
    )
    wrapped_good = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例甲科技（北京）有限公司",
                "title": "后端开发实习生",
                "description": "为数据实习生编写上手手册",
                "evidence": {"quote": wrapped_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    validate_profile(wrapped_good, "resume", wrapped_text, allow_user_confirmed=False)

    wrapped_bad_org = {
        "kind": "resume",
        "education": [],
        "work_experience": [
            {
                "item_id": "work-1",
                "organization": "示例乙银行",
                "title": "后端开发实习生",
                "description": "合作方对接",
                "evidence": {"quote": wrapped_text, "origin": "extracted"},
            }
        ],
        "projects": [],
        "skills": [],
    }
    with pytest.raises(AppError) as wrapped_err:
        validate_profile(wrapped_bad_org, "resume", wrapped_text, allow_user_confirmed=False)
    assert wrapped_err.value.code == "EVIDENCE_INVALID"
    assert "field=organization" in wrapped_err.value.details

    # 4) 仅单一字段显式标注：有标签字段受约束，无标签字段保留段落核对
    single_labeled_text = (
        "公司：示例甲实验室\n"
        "后端实习生｜2025.07 - 2025.09\n"
        "职责：对接示例乙银行。"
    )
    validate_profile(
        {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": "示例甲实验室",
                    "title": "后端实习生",
                    "description": "对接示例乙银行",
                    "evidence": {"quote": single_labeled_text, "origin": "extracted"},
                }
            ],
            "projects": [],
            "skills": [],
        },
        "resume",
        single_labeled_text,
        allow_user_confirmed=False,
    )
    with pytest.raises(AppError) as single_err:
        validate_profile(
            {
                "kind": "resume",
                "education": [],
                "work_experience": [
                    {
                        "item_id": "work-1",
                        "organization": "示例乙银行",
                        "title": "后端实习生",
                        "description": "对接示例乙银行",
                        "evidence": {"quote": single_labeled_text, "origin": "extracted"},
                    }
                ],
                "projects": [],
                "skills": [],
            },
            "resume",
            single_labeled_text,
            allow_user_confirmed=False,
        )
    assert single_err.value.code == "EVIDENCE_INVALID"
    assert "field=organization" in single_err.value.details

    # 5) 合法无标签版式兼容
    unlabeled_text = "示例甲实验室｜后端实习生｜为数据实习生编写上手手册，并对接示例乙银行。"
    validate_profile(
        {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": "示例甲实验室",
                    "title": "后端实习生",
                    "description": "编写上手手册并对接示例乙银行",
                    "evidence": {"quote": unlabeled_text, "origin": "extracted"},
                }
            ],
            "projects": [],
            "skills": [],
        },
        "resume",
        unlabeled_text,
        allow_user_confirmed=False,
    )

    # 6) 连续多段标签：每段各自按自身标签值范围核对
    multi_labeled_text = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "职责：为数据实习生编写上手手册，并对接示例乙银行。\n"
        "公司：示例丙科技\n"
        "职位：测试实习生\n"
        "职责：协助架构师完善自动化回归用例。"
    )
    seg1_quote = "公司：示例甲实验室\n职位：后端实习生\n职责：为数据实习生编写上手手册，并对接示例乙银行。"
    seg2_quote = "公司：示例丙科技\n职位：测试实习生\n职责：协助架构师完善自动化回归用例。"
    validate_profile(
        {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": "示例甲实验室",
                    "title": "后端实习生",
                    "description": "编写上手手册",
                    "evidence": {"quote": seg1_quote, "origin": "extracted"},
                },
                {
                    "item_id": "work-2",
                    "organization": "示例丙科技",
                    "title": "测试实习生",
                    "description": "完善回归用例",
                    "evidence": {"quote": seg2_quote, "origin": "extracted"},
                },
            ],
            "projects": [],
            "skills": [],
        },
        "resume",
        multi_labeled_text,
        allow_user_confirmed=False,
    )
    with pytest.raises(AppError) as multi_err:
        validate_profile(
            {
                "kind": "resume",
                "education": [],
                "work_experience": [
                    {
                        "item_id": "work-1",
                        "organization": "示例甲实验室",
                        "title": "数据实习生",
                        "description": "编写上手手册",
                        "evidence": {"quote": seg1_quote, "origin": "extracted"},
                    }
                ],
                "projects": [],
                "skills": [],
            },
            "resume",
            multi_labeled_text,
            allow_user_confirmed=False,
        )
    assert multi_err.value.code == "EVIDENCE_INVALID"
    assert "field=title" in multi_err.value.details

    # 7) Worker 端到端纠正：首次返回职责里的错误实体不持久化，二次纠正后保存正确组织和职务
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    prompts_seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts_seen.append(body["messages"][-1]["content"])
        payload = wrong_title if len(prompts_seen) == 1 else right_labeled
        return httpx.Response(
            200,
            json={
                "id": f"req-lbl-entity-{len(prompts_seen)}",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload, ensure_ascii=False)},
                    }
                ],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
    )
    _, token = issue_token(client)
    mat = _post_text(client, token, kind="resume", title="显式标签约束", text=labeled_text).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "succeeded", done
    assert len(prompts_seen) == 2
    assert "reason=ungrounded_work_experience" in prompts_seen[1]
    saved = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert saved["state"] == "parsed"
    assert saved["profile"]["work_experience"][0]["organization"] == "示例甲实验室"
    assert saved["profile"]["work_experience"][0]["title"] == "后端实习生"

    # 若两次均返回职责误代实体，错误结果不得落库
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "id": "req-lbl-entity-bad",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {"content": json.dumps(wrong_org, ensure_ascii=False)},
                            }
                        ],
                    },
                )
            ),
        ),
    )
    mat_bad = _post_text(client, token, kind="resume", title="显式标签拦截", text=labeled_text).json()
    job_bad = client.post(
        f"/v1/materials/{mat_bad['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, job_bad.json()["id"], worker)
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in failed["error"]["details"]
    assert failed["retryable"] is True
    mat_after_fail = client.get(f"/v1/materials/{mat_bad['id']}", headers=auth(token)).json()
    assert mat_after_fail["profile"] is None
    assert mat_after_fail["text"] == labeled_text

    # 无职责标签的叙述句被当成职位、以及显式值被截短：两次纠正后仍失败且不落库
    for wrong_profile, source_text, expected_field in (
        (wrong_unlabeled_title, unlabeled_resp_text, "field=title"),
        (wrong_short_org, full_value_text, "field=organization"),
        (wrong_short_title, full_value_text, "field=title"),
    ):
        monkeypatch.setattr(
            runner,
            "get_provider",
            lambda _, payload=wrong_profile: DeepSeekProvider(
                settings,
                transport=httpx.MockTransport(
                    lambda _: httpx.Response(
                        200,
                        json={
                            "id": "req-unlabeled-field-bad",
                            "choices": [
                                {
                                    "finish_reason": "stop",
                                    "message": {
                                        "content": json.dumps(payload, ensure_ascii=False)
                                    },
                                }
                            ],
                        },
                    )
                ),
            ),
        )
        mat_wrong = _post_text(
            client, token, kind="resume", title="字段完整性与续行", text=source_text
        ).json()
        job_wrong = client.post(
            f"/v1/materials/{mat_wrong['id']}/parse",
            json={
                "revision": 1,
                "external_processing_consent": True,
                "role_id": "java-backend",
                "level": "intern",
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        failed_wrong = wait_job(client, token, job_wrong.json()["id"], worker)
        assert failed_wrong["state"] == "failed", failed_wrong
        assert failed_wrong["error"]["code"] == "EVIDENCE_INVALID"
        assert "reason=ungrounded_work_experience" in failed_wrong["error"]["details"]
        assert expected_field in failed_wrong["error"]["details"]
        mat_wrong_after = client.get(
            f"/v1/materials/{mat_wrong['id']}", headers=auth(token)
        ).json()
        assert mat_wrong_after["profile"] is None
        assert mat_wrong_after["text"] == source_text


def test_markdown_work_tables_with_outer_pipes_and_separator() -> None:
    from app.services.text import validate_profile

    def resume_profile(org: str, title: str, quote: str) -> dict:
        return {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": org,
                    "title": title,
                    "description": "维护库存接口",
                    "evidence": {"quote": quote, "origin": "extracted"},
                }
            ],
            "projects": [],
            "skills": [],
        }

    def assert_rejected(profile: dict, text: str, field: str) -> None:
        with pytest.raises(AppError) as err:
            validate_profile(profile, "resume", text, allow_user_confirmed=False)
        assert err.value.code == "EVIDENCE_INVALID"
        assert "reason=ungrounded_work_experience" in err.value.details
        assert f"field={field}" in err.value.details

    fenced_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    )
    fenced_quote = "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    validate_profile(
        resume_profile("示例甲", "后端实习生", fenced_quote),
        "resume",
        fenced_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", fenced_quote),
        fenced_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", fenced_quote),
        fenced_text,
        "title",
    )

    reordered_text = (
        "| 职位 | 公司 | 职责 |\n"
        "| :--- | :---: | ---: |\n"
        "| 后端实习生 | 示例甲 | 维护库存接口。 |"
    )
    reordered_quote = "| 后端实习生 | 示例甲 | 维护库存接口。 |"
    validate_profile(
        resume_profile("示例甲", "后端实习生", reordered_quote),
        "resume",
        reordered_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", reordered_quote),
        reordered_text,
        "title",
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", reordered_quote),
        reordered_text,
        "organization",
    )

    bare_separator_text = (
        "公司 | 职位 | 职责\n"
        "--- | --- | ---\n"
        "示例甲 | 后端实习生 | 维护库存接口。"
    )
    validate_profile(
        resume_profile("示例甲", "后端实习生", "示例甲 | 后端实习生 | 维护库存接口。"),
        "resume",
        bare_separator_text,
        allow_user_confirmed=False,
    )

    # 包围式语法保留真正的内部空列；表头与数据行按同一列位置绑定。
    internal_empty_text = (
        "| 公司 | | 职位 | 职责 |\n"
        "| --- | --- | --- | --- |\n"
        "| 示例甲 | | 后端实习生 | 维护库存接口。 |"
    )
    internal_empty_quote = "| 示例甲 | | 后端实习生 | 维护库存接口。 |"
    validate_profile(
        resume_profile("示例甲", "后端实习生", internal_empty_quote),
        "resume",
        internal_empty_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", internal_empty_quote),
        internal_empty_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", internal_empty_quote),
        internal_empty_text,
        "title",
    )

    # 包围式语法保留真正的内部空列和末尾空列，首列缺值不被外侧竖线吞掉。
    empty_last_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲 | 后端实习生 | |"
    )
    validate_profile(
        resume_profile("示例甲", "后端实习生", "| 示例甲 | 后端实习生 | |"),
        "resume",
        empty_last_text,
        allow_user_confirmed=False,
    )
    empty_first_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| | 后端实习生 | 维护库存接口。 |"
    )
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "| | 后端实习生 | 维护库存接口。 |"),
        empty_first_text,
        "organization",
    )
    empty_middle_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲 | | 维护库存接口。 |"
    )
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "| 示例甲 | | 维护库存接口。 |"),
        empty_middle_text,
        "title",
    )

    escaped_pipe_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲 | 后端实习生 | 维护接口 \\| 对接示例乙银行。 |"
    )
    escaped_pipe_quote = "| 示例甲 | 后端实习生 | 维护接口 \\| 对接示例乙银行。 |"
    validate_profile(
        resume_profile("示例甲", "后端实习生", escaped_pipe_quote),
        "resume",
        escaped_pipe_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", escaped_pipe_quote),
        escaped_pipe_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", escaped_pipe_quote),
        escaped_pipe_text,
        "title",
    )

    # 裸竖线表的数据行首尾同时为空：边缘空单元格按真实列位置保留。
    bare_edge_text = "编号｜公司｜职位｜备注\n｜示例甲实验室｜后端实习生｜"
    bare_edge_quote = "｜示例甲实验室｜后端实习生｜"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", bare_edge_quote),
        "resume",
        bare_edge_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("后端实习生", "后端实习生", bare_edge_quote),
        bare_edge_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲实验室", "示例甲实验室", bare_edge_quote),
        bare_edge_text,
        "title",
    )

    # 裸竖线表的公司首列为空、备注末列为空：备注中的公司不能补进单位列。
    bare_empty_org_text = "编号｜公司｜职位｜备注\n｜｜后端实习生｜示例甲实验室"
    bare_empty_org_quote = "｜｜后端实习生｜示例甲实验室"
    assert_rejected(
        resume_profile("示例甲实验室", "后端实习生", bare_empty_org_quote),
        bare_empty_org_text,
        "organization",
    )
    bare_note_empty_text = "编号｜公司｜职位｜备注\n｜示例甲实验室｜后端实习生｜"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", "｜示例甲实验室｜后端实习生｜"),
        "resume",
        bare_note_empty_text,
        allow_user_confirmed=False,
    )

    # 裸竖线表单元格内的转义竖线仍留在原列（4 列对应 3 根未转义竖线）。
    bare_escaped_text = (
        "编号｜公司｜职位｜备注\n"
        "｜示例甲实验室｜后端实习生｜维护接口 \\| 对接示例乙银行"
    )
    bare_escaped_quote = "｜示例甲实验室｜后端实习生｜维护接口 \\| 对接示例乙银行"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", bare_escaped_quote),
        "resume",
        bare_escaped_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", bare_escaped_quote),
        bare_escaped_text,
        "organization",
    )

    bare_escaped_nonsuffix_text = (
        "编号｜公司｜职位｜备注\n"
        "｜示例甲｜后端实习生｜维护接口 \\| 对接示例乙银行"
    )
    bare_escaped_nonsuffix_quote = "｜示例甲｜后端实习生｜维护接口 \\| 对接示例乙银行"
    validate_profile(
        resume_profile("示例甲", "后端实习生", bare_escaped_nonsuffix_quote),
        "resume",
        bare_escaped_nonsuffix_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", bare_escaped_nonsuffix_quote),
        bare_escaped_nonsuffix_text,
        "organization",
    )

    bare_five_col_escaped_text = (
        "编号｜公司｜职位｜职责｜备注\n"
        "｜示例甲｜后端实习生｜维护接口 \\| 对接示例乙银行｜"
    )
    bare_five_col_escaped_quote = "｜示例甲｜后端实习生｜维护接口 \\| 对接示例乙银行｜"
    validate_profile(
        resume_profile("示例甲", "后端实习生", bare_five_col_escaped_quote),
        "resume",
        bare_five_col_escaped_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", bare_five_col_escaped_quote),
        bare_five_col_escaped_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", bare_five_col_escaped_quote),
        bare_five_col_escaped_text,
        "title",
    )

    # Markdown 外框下表头已明确，数据行允许省略任意一侧边界。
    left_open_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲实验室 | 后端实习生 | 维护接口，并对接示例乙银行。"
    )
    left_open_quote = "| 示例甲实验室 | 后端实习生 | 维护接口，并对接示例乙银行。"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", left_open_quote),
        "resume",
        left_open_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", left_open_quote),
        left_open_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲实验室", "示例甲实验室", left_open_quote),
        left_open_text,
        "title",
    )
    right_open_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "示例甲实验室 | 后端实习生 | 维护接口，并对接示例乙银行。 |"
    )
    right_open_quote = "示例甲实验室 | 后端实习生 | 维护接口，并对接示例乙银行。 |"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", right_open_quote),
        "resume",
        right_open_text,
        allow_user_confirmed=False,
    )

    # 两列键值版式按右列完整值绑定单位与职务，职责里的其他实体不能替换。
    key_value_text = (
        "| 公司 | 示例甲实验室 |\n"
        "| 职位 | 后端实习生 |\n"
        "| 职责 | 维护接口，并对接示例乙银行，同时指导数据实习生。 |"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", key_value_text),
        "resume",
        key_value_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", key_value_text),
        key_value_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲实验室", "数据实习生", key_value_text),
        key_value_text,
        "title",
    )

    # 键值右列保留单元格内换行，按完整值归一化比较。
    key_value_wrapped_text = (
        "| 公司 | 示例甲科技\\n有限公司 |\n"
        "| 职位 | 后端开发\\n实习生 |\n"
        "| 职责 | 维护接口 |"
    )
    validate_profile(
        resume_profile("示例甲科技有限公司", "后端开发实习生", key_value_wrapped_text),
        "resume",
        key_value_wrapped_text,
        allow_user_confirmed=False,
    )

    # 表头省略外侧竖线但紧邻有效 Markdown 分隔行时，数据行可用单侧、双侧或无外框。
    unfenced_md_header = "公司 | 职位 | 职责\n--- | --- | ---\n"
    for row_quote in (
        "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。",
        "示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |",
        "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |",
        "示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。",
    ):
        md_text = unfenced_md_header + row_quote
        validate_profile(
            resume_profile("示例甲", "后端实习生", row_quote),
            "resume",
            md_text,
            allow_user_confirmed=False,
        )
        assert_rejected(
            resume_profile("示例乙银行", "后端实习生", row_quote),
            md_text,
            "organization",
        )
        assert_rejected(
            resume_profile("示例甲", "数据实习生", row_quote),
            md_text,
            "title",
        )

    # 单侧外框表头 + 有效分隔行、对齐语法分隔行同样应识别为 Markdown 表。
    left_fenced_md_text = (
        "| 公司 | 职位 | 职责\n"
        "| --- | --- | ---\n"
        "示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    )
    left_fenced_md_quote = "示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    validate_profile(
        resume_profile("示例甲", "后端实习生", left_fenced_md_quote),
        "resume",
        left_fenced_md_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", left_fenced_md_quote),
        left_fenced_md_text,
        "organization",
    )

    aligned_sep_unfenced_text = (
        "公司 | 职位 | 职责\n"
        "--- | :---: | ---:\n"
        "| 示例甲 | 后端实习生 | 维护库存接口。"
    )
    aligned_sep_unfenced_quote = "| 示例甲 | 后端实习生 | 维护库存接口。"
    validate_profile(
        resume_profile("示例甲", "后端实习生", aligned_sep_unfenced_quote),
        "resume",
        aligned_sep_unfenced_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", aligned_sep_unfenced_quote),
        aligned_sep_unfenced_text,
        "title",
    )

    # 无外框 Markdown 表头下实际缺公司或缺职位时仍准确拦截。
    unfenced_missing_org_text = (
        "公司 | 职位 | 职责\n"
        "--- | --- | ---\n"
        "| | 后端实习生 | 维护库存接口。"
    )
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "| | 后端实习生 | 维护库存接口。"),
        unfenced_missing_org_text,
        "organization",
    )
    unfenced_missing_title_text = (
        "公司 | 职位 | 职责\n"
        "--- | --- | ---\n"
        "| 示例甲 | | 维护库存接口。"
    )
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "| 示例甲 | | 维护库存接口。"),
        unfenced_missing_title_text,
        "title",
    )

    # 分隔行列数与表头不匹配时，不误升格为包围式 Markdown，仍按裸表处理并拦截错列。
    mismatched_sep_text = (
        "公司 | 职位 | 职责\n"
        "--- | ---\n"
        "| 示例甲 | 后端实习生 | 维护库存接口。"
    )
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "| 示例甲 | 后端实习生 | 维护库存接口。"),
        mismatched_sep_text,
        "organization",
    )

    # 仅引用表头行本身不得通过校验。
    header_only_source = (
        "公司 | 职位 | 职责\n"
        "--- | --- | ---\n"
        "| 示例甲 | 后端实习生 | 维护库存接口。"
    )
    assert_rejected(
        resume_profile("公司", "职位", "公司 | 职位 | 职责"),
        header_only_source,
        "organization",
    )

    # 行列数异常（4 列表头末列为备注，数据行因未转义竖线变成 5 列）：
    # 不得退回宽松子串匹配，正确及错误替换均必须按不可对齐拒绝。
    ambiguous_row_text = (
        "公司｜职位｜职责｜备注\n"
        "示例甲｜后端实习生｜为数据实习生编写手册｜对接示例乙银行。｜社团项目接口"
    )
    ambiguous_row_quote = "示例甲｜后端实习生｜为数据实习生编写手册｜对接示例乙银行。｜社团项目接口"
    for org, title in (
        ("示例甲", "后端实习生"),
        ("示例乙银行", "后端实习生"),
        ("示例甲", "数据实习生"),
    ):
        assert_rejected(
            resume_profile(org, title, ambiguous_row_quote),
            ambiguous_row_text,
            "quote",
        )

    # 删除未转义竖线恢复 4 列、或转义职责内竖线后，正确字段通过，替换字段被准确拦截。
    for fixed_text, fixed_quote in (
        (
            "公司｜职位｜职责｜备注\n示例甲｜后端实习生｜为数据实习生编写手册、对接示例乙银行。｜社团项目接口",
            "示例甲｜后端实习生｜为数据实习生编写手册、对接示例乙银行。｜社团项目接口",
        ),
        (
            "公司｜职位｜职责｜备注\n示例甲｜后端实习生｜为数据实习生编写手册 \\| 对接示例乙银行。｜社团项目接口",
            "示例甲｜后端实习生｜为数据实习生编写手册 \\| 对接示例乙银行。｜社团项目接口",
        ),
        (
            "公司｜职位｜职责｜备注\n示例甲｜后端实习生｜为数据实习生编写手册 \\｜ 对接示例乙银行。｜社团项目接口",
            "示例甲｜后端实习生｜为数据实习生编写手册 \\｜ 对接示例乙银行。｜社团项目接口",
        ),
        (
            "公司｜职位｜职责｜备注\n示例甲｜后端实习生｜为数据实习生编写手册、对接示例乙银行。",
            "示例甲｜后端实习生｜为数据实习生编写手册、对接示例乙银行。",
        ),
    ):
        validate_profile(
            resume_profile("示例甲", "后端实习生", fixed_quote),
            "resume",
            fixed_text,
            allow_user_confirmed=False,
        )
        assert_rejected(
            resume_profile("示例乙银行", "后端实习生", fixed_quote),
            fixed_text,
            "organization",
        )
        assert_rejected(
            resume_profile("示例甲", "数据实习生", fixed_quote),
            fixed_text,
            "title",
        )

    # 无表头自由文本继续保持正常放行。
    free_text = "在示例甲担任后端实习生，为数据实习生编写手册并对接示例乙银行。"
    validate_profile(
        resume_profile("示例甲", "后端实习生", free_text),
        "resume",
        free_text,
        allow_user_confirmed=False,
    )

    # 多行表中中间存在一行歧义行时，前后合法行仍正常继承表头，仅歧义行报 field=quote。
    mixed_rows_table = (
        "公司｜职位｜职责｜备注\n"
        "示例甲｜后端实习生｜编写手册｜校内项目\n"
        "示例丁｜测试实习生｜维护用例｜对接示例乙银行。｜社团项目接口\n"
        "示例丙｜数据工程师｜开发台账｜对接示例戊银行"
    )
    row1_q = "示例甲｜后端实习生｜编写手册｜校内项目"
    row2_q = "示例丁｜测试实习生｜维护用例｜对接示例乙银行。｜社团项目接口"
    row3_q = "示例丙｜数据工程师｜开发台账｜对接示例戊银行"
    validate_profile(
        resume_profile("示例甲", "后端实习生", row1_q),
        "resume",
        mixed_rows_table,
        allow_user_confirmed=False,
    )
    assert_rejected(resume_profile("示例丁", "测试实习生", row2_q), mixed_rows_table, "quote")
    validate_profile(
        resume_profile("示例丙", "数据工程师", row3_q),
        "resume",
        mixed_rows_table,
        allow_user_confirmed=False,
    )
    assert_rejected(resume_profile("示例戊银行", "数据工程师", row3_q), mixed_rows_table, "organization")


def test_docx_work_tables_keep_order_columns_and_multiline_cells(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.text import validate_profile

    def resume_profile(
        org: str, title: str, quote: str, extra_work: list[dict] | None = None
    ) -> dict:
        work = [
            {
                "item_id": "work-1",
                "organization": org,
                "title": title,
                "description": "维护库存接口",
                "evidence": {"quote": quote, "origin": "extracted"},
            }
        ]
        if extra_work:
            work.extend(extra_work)
        return {
            "kind": "resume",
            "education": [],
            "work_experience": work,
            "projects": [],
            "skills": [],
        }

    _, token = issue_token(client)

    def upload_docx(title: str, blocks: list) -> dict:
        data = make_docx_with_tables(blocks)
        response = client.post(
            "/v1/materials/file",
            data={"kind": "resume", "title": title},
            files={
                "file": (
                    "resume.docx",
                    data,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert response.status_code == 201, response.text
        material = response.json()
        extract = client.post(
            f"/v1/materials/{material['id']}/extract",
            json={"revision": material["revision"]},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert extract.status_code == 202
        job = wait_job(client, token, extract.json()["id"], worker)
        assert job["state"] == "succeeded", job
        return client.get(f"/v1/materials/{material['id']}", headers=auth(token)).json()

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )

    def use_payload(payload: dict) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "req-docx-table",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(payload, ensure_ascii=False)},
                        }
                    ],
                },
            )

        monkeypatch.setattr(
            runner,
            "get_provider",
            lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
        )

    def parse(material: dict) -> dict:
        job_r = client.post(
            f"/v1/materials/{material['id']}/parse",
            json={
                "revision": material["revision"],
                "external_processing_consent": True,
                "role_id": "java-backend",
                "level": "intern",
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert job_r.status_code == 202, job_r.text
        return wait_job(client, token, job_r.json()["id"], worker)

    first_row = "| 示例甲 | 后端开发\\n实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    second_row = "| 示例丙 | 数据工程师 | 开发台账工具 |"
    project_row = "| 模拟项目 | 维护库存接口 |"

    document_text = upload_docx(
        "DOCX 表格",
        [
            "工作经历",
            [
                ["公司", "职位", "职责"],
                ["示例甲", "后端开发\n实习生", "为数据实习生编写手册，并对接示例乙银行。"],
            ],
            [
                ["公司", "职位", "职责"],
                ["示例丙", "数据工程师", "开发台账工具"],
            ],
            "项目经历",
            [
                ["项目名称", "项目描述"],
                ["模拟项目", "维护库存接口"],
            ],
        ],
    )["text"]
    assert document_text.index("工作经历") < document_text.index("公司")
    assert document_text.index("示例甲") < document_text.index("示例丙")
    assert document_text.index("示例丙") < document_text.index("项目经历")
    assert document_text.index("项目经历") < document_text.index("模拟项目")
    assert first_row in document_text
    assert second_row in document_text
    assert project_row in document_text
    assert document_text.count("| 公司 | 职位 | 职责 |") == 2

    correct_profile = resume_profile(
        "示例甲",
        "后端开发实习生",
        first_row,
        extra_work=[
            {
                "item_id": "work-2",
                "organization": "示例丙",
                "title": "数据工程师",
                "description": "开发台账工具",
                "evidence": {"quote": second_row, "origin": "extracted"},
            }
        ],
    )
    correct_profile["projects"] = [
        {
            "item_id": "project-1",
            "name": "模拟项目",
            "description": "维护库存接口",
            "skill_codes": [],
            "evidence": {"quote": project_row, "origin": "extracted"},
        }
    ]
    use_payload(correct_profile)
    saved_material = upload_docx("DOCX 表格正确", [
        "工作经历",
        [
            ["公司", "职位", "职责"],
            ["示例甲", "后端开发\n实习生", "为数据实习生编写手册，并对接示例乙银行。"],
        ],
        [
            ["公司", "职位", "职责"],
            ["示例丙", "数据工程师", "开发台账工具"],
        ],
        "项目经历",
        [
            ["项目名称", "项目描述"],
            ["模拟项目", "维护库存接口"],
        ],
    ])
    succeeded = parse(saved_material)
    assert succeeded["state"] == "succeeded", succeeded
    saved = client.get(f"/v1/materials/{saved_material['id']}", headers=auth(token)).json()
    assert [item["title"] for item in saved["profile"]["work_experience"]] == [
        "后端开发实习生",
        "数据工程师",
    ]

    wrong_org_profile = resume_profile("示例乙银行", "后端开发实习生", first_row)
    use_payload(wrong_org_profile)
    wrong_org_material = upload_docx("DOCX 表格错单位", [
        "工作经历",
        [
            ["公司", "职位", "职责"],
            ["示例甲", "后端开发\n实习生", "为数据实习生编写手册，并对接示例乙银行。"],
        ],
    ])
    failed_org = parse(wrong_org_material)
    assert failed_org["state"] == "failed"
    assert failed_org["error"]["code"] == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in failed_org["error"]["details"]
    assert "field=organization" in failed_org["error"]["details"]
    assert (
        client.get(f"/v1/materials/{wrong_org_material['id']}", headers=auth(token)).json()[
            "profile"
        ]
        is None
    )

    wrong_title_profile = resume_profile("示例甲", "数据实习生", first_row)
    use_payload(wrong_title_profile)
    wrong_title_material = upload_docx("DOCX 表格错职位", [
        "工作经历",
        [
            ["公司", "职位", "职责"],
            ["示例甲", "后端开发\n实习生", "为数据实习生编写手册，并对接示例乙银行。"],
        ],
    ])
    failed_title = parse(wrong_title_material)
    assert failed_title["state"] == "failed"
    assert failed_title["error"]["code"] == "EVIDENCE_INVALID"
    assert "field=title" in failed_title["error"]["details"]
    assert (
        client.get(
            f"/v1/materials/{wrong_title_material['id']}", headers=auth(token)
        ).json()["profile"]
        is None
    )

    # 空任职列不从职责或相邻列补齐，校验必须先拒绝。
    empty_title_material = upload_docx(
        "DOCX 空任职列",
        [
            "工作经历",
            [
                ["公司", "职位", "职责"],
                ["示例丁", "", "维护库存接口"],
            ],
        ],
    )
    empty_title_row = "| 示例丁 |  | 维护库存接口 |"
    assert empty_title_row in empty_title_material["text"]
    with pytest.raises(AppError) as err:
        validate_profile(
            resume_profile("示例丁", "后端实习生", empty_title_row),
            "resume",
            empty_title_material["text"],
            allow_user_confirmed=False,
        )
    assert "field=title" in err.value.details

    # 同一内容的 Markdown 表格走文本材料，也应能保存正确结果并拒绝错列。
    markdown_text = (
        "| 公司 | 职位 | 职责 |\n"
        "| --- | --- | --- |\n"
        "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    )
    markdown_quote = "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。 |"
    use_payload(resume_profile("示例甲", "后端实习生", markdown_quote))
    markdown_material = _post_text(
        client, token, kind="resume", title="Markdown 表格", text=markdown_text
    ).json()
    markdown_job = parse(markdown_material)
    assert markdown_job["state"] == "succeeded", markdown_job
    markdown_saved = client.get(
        f"/v1/materials/{markdown_material['id']}", headers=auth(token)
    ).json()
    assert markdown_saved["profile"]["work_experience"][0]["organization"] == "示例甲"

    use_payload(resume_profile("示例甲", "数据实习生", markdown_quote))
    markdown_bad = _post_text(
        client, token, kind="resume", title="Markdown 表格错列", text=markdown_text
    ).json()
    markdown_failed = parse(markdown_bad)
    assert markdown_failed["state"] == "failed"
    assert "field=title" in markdown_failed["error"]["details"]
    assert (
        client.get(f"/v1/materials/{markdown_bad['id']}", headers=auth(token)).json()[
            "profile"
        ]
        is None
    )

    # 带“备注”列的 4 列正常 DOCX（职责含竖线且被自动转义）端到端正反例。
    docx_remark_row = "| 示例甲 | 后端实习生 | 为数据实习生编写手册\\｜对接示例乙银行。 | 社团项目接口 |"
    docx_remark_material = upload_docx(
        "DOCX 四列带备注",
        [
            "工作经历",
            [
                ["公司", "职位", "职责", "备注"],
                ["示例甲", "后端实习生", "为数据实习生编写手册｜对接示例乙银行。", "社团项目接口"],
            ],
        ],
    )
    assert docx_remark_row in docx_remark_material["text"]
    use_payload(resume_profile("示例甲", "后端实习生", docx_remark_row))
    docx_remark_ok = parse(docx_remark_material)
    assert docx_remark_ok["state"] == "succeeded", docx_remark_ok

    use_payload(resume_profile("示例乙银行", "后端实习生", docx_remark_row))
    docx_remark_bad_material = upload_docx(
        "DOCX 四列带备注错单位",
        [
            "工作经历",
            [
                ["公司", "职位", "职责", "备注"],
                ["示例甲", "后端实习生", "为数据实习生编写手册｜对接示例乙银行。", "社团项目接口"],
            ],
        ],
    )
    docx_remark_bad = parse(docx_remark_bad_material)
    assert docx_remark_bad["state"] == "failed"
    assert docx_remark_bad["error"]["code"] == "EVIDENCE_INVALID"
    assert "field=organization" in docx_remark_bad["error"]["details"]

    # 无外框表头 Markdown + 单侧外框数据行经模拟 DeepSeek Worker 端到端解析与拦截。
    unfenced_md_text = (
        "公司 | 职位 | 职责\n"
        "--- | --- | ---\n"
        "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。"
    )
    unfenced_md_quote = "| 示例甲 | 后端实习生 | 为数据实习生编写手册，并对接示例乙银行。"
    use_payload(resume_profile("示例甲", "后端实习生", unfenced_md_quote))
    unfenced_md_mat = _post_text(
        client, token, kind="resume", title="无外框表头 Markdown", text=unfenced_md_text
    ).json()
    unfenced_md_job = parse(unfenced_md_mat)
    assert unfenced_md_job["state"] == "succeeded", unfenced_md_job
    unfenced_saved = client.get(
        f"/v1/materials/{unfenced_md_mat['id']}", headers=auth(token)
    ).json()
    assert unfenced_saved["profile"]["work_experience"][0]["organization"] == "示例甲"
    assert unfenced_saved["profile"]["work_experience"][0]["title"] == "后端实习生"

    use_payload(resume_profile("示例乙银行", "后端实习生", unfenced_md_quote))
    unfenced_bad_mat = _post_text(
        client, token, kind="resume", title="无外框表头 Markdown 错列", text=unfenced_md_text
    ).json()
    unfenced_bad_job = parse(unfenced_bad_mat)
    assert unfenced_bad_job["state"] == "failed"
    assert unfenced_bad_job["error"]["code"] == "EVIDENCE_INVALID"
    assert "field=organization" in unfenced_bad_job["error"]["details"]

    # 4 列表头 + 5 列歧义行经 Worker 有限纠正后仍失败：返回受控错误且保留原文，可继续重试或手动核对。
    ambiguous_text = (
        "公司｜职位｜职责｜备注\n"
        "示例甲｜后端实习生｜为数据实习生编写手册｜对接示例乙银行。｜社团项目接口"
    )
    ambiguous_quote = "示例甲｜后端实习生｜为数据实习生编写手册｜对接示例乙银行。｜社团项目接口"
    use_payload(resume_profile("示例乙银行", "后端实习生", ambiguous_quote))
    ambiguous_mat = _post_text(
        client, token, kind="resume", title="五列歧义行", text=ambiguous_text
    ).json()
    ambiguous_job = parse(ambiguous_mat)
    assert ambiguous_job["state"] == "failed", ambiguous_job
    assert ambiguous_job["retryable"] is True
    assert ambiguous_job["error"]["code"] == "EVIDENCE_INVALID"
    assert ambiguous_job["error"]["message"] == "工作经历表格行无法按表头可靠对齐列，请核对原文"
    assert "stage=parse" in ambiguous_job["error"]["details"]
    assert "reason=ungrounded_work_experience" in ambiguous_job["error"]["details"]
    assert "field=quote" in ambiguous_job["error"]["details"]
    ambiguous_after = client.get(
        f"/v1/materials/{ambiguous_mat['id']}", headers=auth(token)
    ).json()
    assert ambiguous_after["profile"] is None
    assert ambiguous_after["text"] == ambiguous_text


def test_docx_key_value_work_tables_bind_columns_and_split_entries(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    def resume_profile(entries: list[tuple[str, str, str]]) -> dict:
        return {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": f"work-{index}",
                    "organization": org,
                    "title": title,
                    "description": "维护接口",
                    "evidence": {"quote": quote, "origin": "extracted"},
                }
                for index, (org, title, quote) in enumerate(entries, start=1)
            ],
            "projects": [],
            "skills": [],
        }

    _, token = issue_token(client)

    def upload_docx(title: str, blocks: list) -> dict:
        data = make_docx_with_tables(blocks)
        response = client.post(
            "/v1/materials/file",
            data={"kind": "resume", "title": title},
            files={
                "file": (
                    "resume.docx",
                    data,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert response.status_code == 201, response.text
        material = response.json()
        extract = client.post(
            f"/v1/materials/{material['id']}/extract",
            json={"revision": material["revision"]},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert extract.status_code == 202
        job = wait_job(client, token, extract.json()["id"], worker)
        assert job["state"] == "succeeded", job
        return client.get(
            f"/v1/materials/{material['id']}", headers=auth(token)
        ).json()

    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )

    def use_payload(payload: dict) -> None:
        def handler(_: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "id": "req-docx-key-value",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": json.dumps(payload, ensure_ascii=False)},
                        }
                    ],
                },
            )

        monkeypatch.setattr(
            runner,
            "get_provider",
            lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(handler)),
        )

    def parse(material: dict) -> dict:
        job_r = client.post(
            f"/v1/materials/{material['id']}/parse",
            json={
                "revision": material["revision"],
                "external_processing_consent": True,
                "role_id": "java-backend",
                "level": "intern",
            },
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert job_r.status_code == 202, job_r.text
        return wait_job(client, token, job_r.json()["id"], worker)

    single_blocks = [
        "工作经历",
        [
            ["公司", "示例甲实验室"],
            ["职位", "后端实习生"],
            ["职责", "维护接口，并对接示例乙银行，同时指导数据实习生。"],
        ],
    ]
    single_rows = (
        "| 公司 | 示例甲实验室 |\n"
        "| 职位 | 后端实习生 |\n"
        "| 职责 | 维护接口，并对接示例乙银行，同时指导数据实习生。 |"
    )
    single_material = upload_docx("DOCX 键值经历", single_blocks)
    assert single_rows in single_material["text"]

    use_payload(resume_profile([("示例甲实验室", "后端实习生", single_rows)]))
    single_job = parse(single_material)
    assert single_job["state"] == "succeeded", single_job
    single_saved = client.get(
        f"/v1/materials/{single_material['id']}", headers=auth(token)
    ).json()
    assert single_saved["profile"]["work_experience"][0]["organization"] == "示例甲实验室"
    assert single_saved["profile"]["work_experience"][0]["title"] == "后端实习生"

    # 职责中提到的公司或职务不能替换键值右列。
    for wrong_entries, expected_field in (
        ([("示例乙银行", "后端实习生", single_rows)], "field=organization"),
        ([("示例甲实验室", "数据实习生", single_rows)], "field=title"),
    ):
        use_payload(resume_profile(wrong_entries))
        wrong_material = upload_docx("DOCX 键值错列", single_blocks)
        wrong_job = parse(wrong_material)
        assert wrong_job["state"] == "failed", wrong_job
        assert wrong_job["error"]["code"] == "EVIDENCE_INVALID"
        assert "reason=ungrounded_work_experience" in wrong_job["error"]["details"]
        assert expected_field in wrong_job["error"]["details"]
        assert (
            client.get(
                f"/v1/materials/{wrong_material['id']}", headers=auth(token)
            ).json()["profile"]
            is None
        )

    # 公司或职位右列为空时按缺失字段报错，不用职责内容补齐。
    empty_org_material = upload_docx(
        "DOCX 键值空公司",
        [
            [
                ["公司", ""],
                ["职位", "后端实习生"],
                ["职责", "维护接口，并对接示例乙银行。"],
            ]
        ],
    )
    empty_org_rows = (
        "| 公司 |  |\n"
        "| 职位 | 后端实习生 |\n"
        "| 职责 | 维护接口，并对接示例乙银行。 |"
    )
    assert empty_org_rows in empty_org_material["text"]
    use_payload(resume_profile([("示例乙银行", "后端实习生", empty_org_rows)]))
    empty_org_job = parse(empty_org_material)
    assert empty_org_job["state"] == "failed", empty_org_job
    assert "field=organization" in empty_org_job["error"]["details"]

    empty_title_material = upload_docx(
        "DOCX 键值空职位",
        [
            [
                ["公司", "示例甲实验室"],
                ["职位", ""],
                ["职责", "维护接口，并指导数据实习生。"],
            ]
        ],
    )
    empty_title_rows = (
        "| 公司 | 示例甲实验室 |\n"
        "| 职位 |  |\n"
        "| 职责 | 维护接口，并指导数据实习生。 |"
    )
    assert empty_title_rows in empty_title_material["text"]
    use_payload(resume_profile([("示例甲实验室", "数据实习生", empty_title_rows)]))
    empty_title_job = parse(empty_title_material)
    assert empty_title_job["state"] == "failed", empty_title_job
    assert "field=title" in empty_title_job["error"]["details"]

    # 同一张表里重复公司与职位标签时划分成两段经历。
    adjacent_rows = [
        "| 公司 | 示例甲实验室 |",
        "| 职位 | 后端实习生 |",
        "| 职责 | 维护库存接口 |",
        "| 公司 | 示例乙银行 |",
        "| 职位 | 数据实习生 |",
        "| 职责 | 开发台账工具 |",
    ]
    adjacent_material = upload_docx(
        "DOCX 相邻键值经历",
        [
            [
                ["公司", "示例甲实验室"],
                ["职位", "后端实习生"],
                ["职责", "维护库存接口"],
                ["公司", "示例乙银行"],
                ["职位", "数据实习生"],
                ["职责", "开发台账工具"],
            ]
        ],
    )
    assert "\n".join(adjacent_rows) in adjacent_material["text"]
    use_payload(
        resume_profile(
            [
                ("示例甲实验室", "后端实习生", "\n".join(adjacent_rows[:3])),
                ("示例乙银行", "数据实习生", "\n".join(adjacent_rows[3:])),
            ]
        )
    )
    adjacent_job = parse(adjacent_material)
    assert adjacent_job["state"] == "succeeded", adjacent_job
    adjacent_saved = client.get(
        f"/v1/materials/{adjacent_material['id']}", headers=auth(token)
    ).json()
    assert [
        (item["organization"], item["title"])
        for item in adjacent_saved["profile"]["work_experience"]
    ] == [("示例甲实验室", "后端实习生"), ("示例乙银行", "数据实习生")]

    # 键值表与横向表头表混排时，各自仍按所属版式绑定。
    mixed_key_rows = (
        "| 公司 | 示例甲实验室 |\n"
        "| 职位 | 后端实习生 |\n"
        "| 职责 | 维护库存接口 |"
    )
    mixed_table_row = "| 示例丙 | 数据工程师 | 开发台账工具 |"
    mixed_material = upload_docx(
        "DOCX 键值与横向表混排",
        [
            "工作经历",
            [
                ["公司", "示例甲实验室"],
                ["职位", "后端实习生"],
                ["职责", "维护库存接口"],
            ],
            [
                ["公司", "职位", "职责"],
                ["示例丙", "数据工程师", "开发台账工具"],
            ],
        ],
    )
    assert mixed_key_rows in mixed_material["text"]
    assert mixed_table_row in mixed_material["text"]
    use_payload(
        resume_profile(
            [
                ("示例甲实验室", "后端实习生", mixed_key_rows),
                ("示例丙", "数据工程师", mixed_table_row),
            ]
        )
    )
    mixed_job = parse(mixed_material)
    assert mixed_job["state"] == "succeeded", mixed_job
    mixed_saved = client.get(
        f"/v1/materials/{mixed_material['id']}", headers=auth(token)
    ).json()
    assert [
        (item["organization"], item["title"])
        for item in mixed_saved["profile"]["work_experience"]
    ] == [("示例甲实验室", "后端实习生"), ("示例丙", "数据工程师")]


def test_standalone_work_labels_and_unlabeled_columns_bind_full_field_values(
    client: TestClient, worker, monkeypatch
) -> None:
    import httpx
    import app.workers.runner as runner
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings
    from app.services.text import validate_profile

    def resume_profile(org: str, title: str, quote: str) -> dict:
        return {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": org,
                    "title": title,
                    "description": "维护库存接口",
                    "evidence": {"quote": quote, "origin": "extracted"},
                }
            ],
            "projects": [],
            "skills": [],
        }

    def assert_rejected(profile: dict, text: str, field: str) -> None:
        with pytest.raises(AppError) as err:
            validate_profile(profile, "resume", text, allow_user_confirmed=False)
        assert err.value.code == "EVIDENCE_INVALID"
        assert "reason=ungrounded_work_experience" in err.value.details
        assert f"field={field}" in err.value.details

    # 1) 中英文独占标签：紧邻同段的第一个值可以接收，别的实体不能替代
    standalone_text = (
        "公司：\n"
        "示例甲实验室\n"
        "职位：\n"
        "后端实习生\n"
        "职责：为数据实习生编写手册。"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", standalone_text),
        "resume",
        standalone_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲实验室", "数据实习生", standalone_text),
        standalone_text,
        "title",
    )

    only_title_wrapped = (
        "公司：示例甲实验室\n"
        "职位：\n"
        "后端实习生\n"
        "职责：维护库存接口。"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", only_title_wrapped),
        "resume",
        only_title_wrapped,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲实验室", "维护库存接口", only_title_wrapped),
        only_title_wrapped,
        "title",
    )

    standalone_wrapped = (
        "公司：\n"
        "示例甲实验室\n"
        "职位：\n"
        "后端开发\n"
        "实习生\n"
        "职责：维护库存接口。"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端开发实习生", standalone_wrapped),
        "resume",
        standalone_wrapped,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲实验室", "后端开发", standalone_wrapped),
        standalone_wrapped,
        "title",
    )

    en_standalone = (
        "Company:\n"
        "Alpha Lab\n"
        "Title:\n"
        "Backend Intern\n"
        "Responsibilities: Write guide for Data Intern."
    )
    validate_profile(
        resume_profile("Alpha Lab", "Backend Intern", en_standalone),
        "resume",
        en_standalone,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("Alpha Lab", "Data Intern", en_standalone),
        en_standalone,
        "title",
    )

    # 2) 空字段后直接进入新标签、职责或段落：不得用后续内容补齐
    empty_then_label = (
        "公司：\n"
        "职位：后端实习生\n"
        "职责：对接示例乙银行。"
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", empty_then_label),
        empty_then_label,
        "organization",
    )
    empty_then_resp = (
        "公司：示例甲实验室\n"
        "职位：\n"
        "负责维护库存接口"
    )
    assert_rejected(
        resume_profile("示例甲实验室", "负责维护库存接口", empty_then_resp),
        empty_then_resp,
        "title",
    )
    empty_then_engineer_sentence = (
        "公司：示例甲实验室\n"
        "职位：\n"
        "工程师协助设计接口"
    )
    assert_rejected(
        resume_profile("示例甲实验室", "工程师协助设计接口", empty_then_engineer_sentence),
        empty_then_engineer_sentence,
        "title",
    )
    empty_then_paragraph = (
        "公司：\n"
        "\n"
        "示例甲实验室\n"
        "职位：后端实习生"
    )
    assert_rejected(
        resume_profile("示例甲实验室", "后端实习生", empty_then_paragraph),
        empty_then_paragraph,
        "organization",
    )

    # 3) 已有值后的职责句即使以职务名词开头也不能续接；合法折行仍完整匹配
    title_then_sentence = (
        "公司：示例甲实验室\n"
        "职位：后端实习生\n"
        "工程师协助设计接口"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", title_then_sentence),
        "resume",
        title_then_sentence,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile(
            "示例甲实验室", "后端实习生工程师协助设计接口", title_then_sentence
        ),
        title_then_sentence,
        "title",
    )

    wrapped_text = (
        "公司：示例甲科技 ( 北京 )\n"
        "有限公司\n"
        "职位：后端 开发\n"
        "实习生\n"
        "职责：维护库存接口。"
    )
    validate_profile(
        resume_profile("示例甲科技（北京）有限公司", "后端开发实习生", wrapped_text),
        "resume",
        wrapped_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲科技（北京）有限公司", "后端开发", wrapped_text),
        wrapped_text,
        "title",
    )

    # 4) 无标签分栏：单位和职务绑定各自列，正确值通过，单独替换任一列被拒绝
    column_text = "示例甲实验室｜后端实习生｜为数据实习生编写手册，并对接示例乙银行。"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", column_text),
        "resume",
        column_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", column_text),
        column_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲实验室", "数据实习生", column_text),
        column_text,
        "title",
    )

    dated_column_text = "2025.07 - 2025.09｜示例甲实验室｜后端实习生｜维护库存接口"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", dated_column_text),
        "resume",
        dated_column_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲实验室", "数据实习生", dated_column_text),
        dated_column_text,
        "title",
    )

    # 5) 两段相邻经历仍按各自引文核对；跨两段引文被拒绝
    two_entries_text = (
        "示例甲实验室｜后端实习生｜维护库存接口\n"
        "示例乙银行｜数据实习生｜开发台账工具"
    )
    first_quote = "示例甲实验室｜后端实习生｜维护库存接口"
    second_quote = "示例乙银行｜数据实习生｜开发台账工具"
    validate_profile(
        {
            "kind": "resume",
            "education": [],
            "work_experience": [
                {
                    "item_id": "work-1",
                    "organization": "示例甲实验室",
                    "title": "后端实习生",
                    "description": "维护库存接口",
                    "evidence": {"quote": first_quote, "origin": "extracted"},
                },
                {
                    "item_id": "work-2",
                    "organization": "示例乙银行",
                    "title": "数据实习生",
                    "description": "开发台账工具",
                    "evidence": {"quote": second_quote, "origin": "extracted"},
                },
            ],
            "projects": [],
            "skills": [],
        },
        "resume",
        two_entries_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲实验室", "后端实习生", two_entries_text),
        two_entries_text,
        "quote",
    )

    # 6) 职责文本自带竖线：不得被当成新的单位／职务表头
    pipe_resp_text = "示例甲实验室｜后端实习生\n维护库存接口｜支持并发查询"
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", pipe_resp_text),
        "resume",
        pipe_resp_text,
        allow_user_confirmed=False,
    )
    labeled_pipe_resp_text = (
        "示例甲实验室｜后端实习生\n职责：维护库存接口｜支持并发查询"
    )
    validate_profile(
        resume_profile("示例甲实验室", "后端实习生", labeled_pipe_resp_text),
        "resume",
        labeled_pipe_resp_text,
        allow_user_confirmed=False,
    )

    # 7) 明确表头：按列名绑定单位与职务，支持无后缀公司、英文、列顺序调整与空列
    header_text = (
        "公司｜职位｜职责\n"
        "示例甲｜后端实习生｜为数据实习生编写手册，并对接示例乙银行。"
    )
    header_quote = "示例甲｜后端实习生｜为数据实习生编写手册，并对接示例乙银行。"
    validate_profile(
        resume_profile("示例甲", "后端实习生", header_quote),
        "resume",
        header_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", header_quote),
        header_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", header_quote),
        header_text,
        "title",
    )

    en_header_text = (
        "Company | Job Title | Responsibilities\n"
        "Sample Alpha | Backend Engineer | Mentored a Data Intern."
    )
    en_header_quote = "Sample Alpha | Backend Engineer | Mentored a Data Intern."
    validate_profile(
        resume_profile("Sample Alpha", "Backend Engineer", en_header_quote),
        "resume",
        en_header_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("Data Intern", "Backend Engineer", en_header_quote),
        en_header_text,
        "organization",
    )
    assert_rejected(
        resume_profile("Sample Alpha", "Data Intern", en_header_quote),
        en_header_text,
        "title",
    )

    reordered_header_text = (
        "职位｜公司｜职责\n"
        "后端实习生｜示例甲｜为数据实习生编写手册。"
    )
    reordered_header_quote = "后端实习生｜示例甲｜为数据实习生编写手册。"
    validate_profile(
        resume_profile("示例甲", "后端实习生", reordered_header_quote),
        "resume",
        reordered_header_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", reordered_header_quote),
        reordered_header_text,
        "title",
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", reordered_header_quote),
        reordered_header_text,
        "organization",
    )

    empty_resp_header_text = "公司｜职位｜职责\n示例甲｜后端实习生｜"
    empty_resp_quote = "示例甲｜后端实习生｜"
    validate_profile(
        resume_profile("示例甲", "后端实习生", empty_resp_quote),
        "resume",
        empty_resp_header_text,
        allow_user_confirmed=False,
    )
    empty_title_header_text = "公司｜职位｜职责\n示例甲｜｜维护库存接口。"
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "示例甲｜｜维护库存接口。"),
        empty_title_header_text,
        "title",
    )
    empty_org_header_text = "公司｜职位｜职责\n｜后端实习生｜维护库存接口。"
    assert_rejected(
        resume_profile("示例甲", "后端实习生", "｜后端实习生｜维护库存接口。"),
        empty_org_header_text,
        "organization",
    )

    # 职责列内的竖线不拆成新列；表头不跨空行／章节继承
    resp_pipe_header_text = "公司｜职位｜职责\n示例甲｜后端实习生｜维护接口｜对接示例乙银行"
    resp_pipe_header_quote = "示例甲｜后端实习生｜维护接口｜对接示例乙银行"
    validate_profile(
        resume_profile("示例甲", "后端实习生", resp_pipe_header_quote),
        "resume",
        resp_pipe_header_text,
        allow_user_confirmed=False,
    )
    assert_rejected(
        resume_profile("示例乙银行", "后端实习生", resp_pipe_header_quote),
        resp_pipe_header_text,
        "organization",
    )
    assert_rejected(
        resume_profile("示例甲", "数据实习生", resp_pipe_header_quote),
        resp_pipe_header_text,
        "title",
    )
    cross_section_header_text = (
        "公司｜职位｜职责\n"
        "示例甲｜后端实习生｜编写手册\n"
        "\n"
        "其他经历\n"
        "示例丙｜数据工程师｜开发台账"
    )
    cross_section_quote = "示例丙｜数据工程师｜开发台账"
    validate_profile(
        resume_profile("示例丙", "数据工程师", cross_section_quote),
        "resume",
        cross_section_header_text,
        allow_user_confirmed=False,
    )

    # 8) Worker 端到端：合法独占标签可保存；单列替换进入纠正，二次仍错误则不落库
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL="postgresql+psycopg://mockmate@127.0.0.1:55432/mockmate_test",
        PRIVATE_FILE_ROOT="runtime/test-private",
        AI_PROVIDER="deepseek",
        AI_MODEL="deepseek-mock",
        AI_API_KEY="mock-key",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=True,
    )
    prompts_seen: list[str] = []
    wrong_standalone_title = resume_profile("示例甲实验室", "数据实习生", standalone_text)
    right_standalone = resume_profile("示例甲实验室", "后端实习生", standalone_text)

    def standalone_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        prompts_seen.append(body["messages"][-1]["content"])
        payload = wrong_standalone_title if len(prompts_seen) == 1 else right_standalone
        return httpx.Response(
            200,
            json={
                "id": f"req-standalone-{len(prompts_seen)}",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(payload, ensure_ascii=False)},
                    }
                ],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(standalone_handler)),
    )
    _, token = issue_token(client)
    mat = _post_text(
        client, token, kind="resume", title="独占标签", text=standalone_text
    ).json()
    job_r = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    done = wait_job(client, token, job_r.json()["id"], worker)
    assert done["state"] == "succeeded", done
    assert len(prompts_seen) == 2
    assert "reason=ungrounded_work_experience" in prompts_seen[1]
    assert "field=title" in prompts_seen[1]
    saved = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert saved["profile"]["work_experience"][0]["title"] == "后端实习生"

    wrong_column_org = resume_profile("示例乙银行", "后端实习生", column_text)
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "id": "req-column-bad",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(wrong_column_org, ensure_ascii=False)
                                },
                            }
                        ],
                    },
                )
            ),
        ),
    )
    mat_bad = _post_text(
        client, token, kind="resume", title="分栏错列", text=column_text
    ).json()
    job_bad = client.post(
        f"/v1/materials/{mat_bad['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed = wait_job(client, token, job_bad.json()["id"], worker)
    assert failed["state"] == "failed"
    assert failed["error"]["code"] == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in failed["error"]["details"]
    assert "field=organization" in failed["error"]["details"]
    mat_after_fail = client.get(f"/v1/materials/{mat_bad['id']}", headers=auth(token)).json()
    assert mat_after_fail["profile"] is None
    assert mat_after_fail["text"] == column_text

    # 明确表头下把职责里的其他岗位当职位：纠正后仍错误则任务失败且不入草稿
    wrong_header_title = resume_profile("示例甲", "数据实习生", header_quote)
    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(
            settings,
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200,
                    json={
                        "id": "req-header-bad",
                        "choices": [
                            {
                                "finish_reason": "stop",
                                "message": {
                                    "content": json.dumps(wrong_header_title, ensure_ascii=False)
                                },
                            }
                        ],
                    },
                )
            ),
        ),
    )
    mat_header_bad = _post_text(
        client, token, kind="resume", title="表头错列", text=header_text
    ).json()
    job_header_bad = client.post(
        f"/v1/materials/{mat_header_bad['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    failed_header = wait_job(client, token, job_header_bad.json()["id"], worker)
    assert failed_header["state"] == "failed"
    assert failed_header["error"]["code"] == "EVIDENCE_INVALID"
    assert "reason=ungrounded_work_experience" in failed_header["error"]["details"]
    assert "field=title" in failed_header["error"]["details"]
    mat_header_after = client.get(
        f"/v1/materials/{mat_header_bad['id']}", headers=auth(token)
    ).json()
    assert mat_header_after["profile"] is None
    assert mat_header_after["text"] == header_text
