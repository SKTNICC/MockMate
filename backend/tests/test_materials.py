from __future__ import annotations

import io

from fastapi.testclient import TestClient

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
