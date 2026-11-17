from __future__ import annotations

import io

from fastapi.testclient import TestClient
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from app.core.config import get_settings
from app.models import BankQuestion, JdTemplate, MaterialDraft, MaterialSnapshot
from app.services.bank import eligible_bank_questions
from tests.conftest import (
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    issue_token,
    make_pdf,
    new_key,
    wait_job,
)


def _encrypted_pdf() -> bytes:
    reader = PdfReader(io.BytesIO(make_pdf(["Secret resume text"])))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt("owner-password")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_encrypted_pdf_fails_with_explicit_error(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    created = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "加密 PDF"},
        files={"file": ("resume.pdf", _encrypted_pdf(), "application/pdf")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created.status_code == 201, created.text
    material = created.json()
    extract = client.post(
        f"/v1/materials/{material['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job = wait_job(client, token, extract.json()["id"], worker)
    assert job["state"] == "failed"
    assert job["error"]["code"] == "FILE_TEXT_UNAVAILABLE"
    assert "加密" in job["error"]["message"]
    assert job["retryable"] is False


def test_template_update_does_not_change_confirmed_snapshot(client: TestClient, db) -> None:
    _, token = issue_token(client)
    created = client.post(
        "/v1/materials/text",
        json={
            "kind": "jd",
            "title": "内置JD",
            "text": TEMPLATE_JD_TEXT,
            "template_id": "java-backend-intern-v1",
            "template_version": 1,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    snapshot = client.post(
        f"/v1/materials/{created['id']}/confirm",
        json={"revision": 1, "profile": TEMPLATE_JD_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert snapshot.status_code == 201
    confirmed = snapshot.json()

    template = db.get(JdTemplate, ("java-backend-intern-v1", 1))
    original = template.text
    template.text = original + "\n（模板被更新）"
    db.commit()
    try:
        fetched = client.get(f"/v1/snapshots/{confirmed['id']}", headers=auth(token)).json()
        assert fetched["text"] == original
        assert fetched["template_version"] == 1

        listing = client.get("/v1/jd-templates", headers=auth(token)).json()["items"]
        assert any("模板被更新" in item["text"] for item in listing)
    finally:
        template = db.get(JdTemplate, ("java-backend-intern-v1", 1))
        template.text = original
        db.commit()


def test_delete_material_removes_snapshot_and_file(client: TestClient, worker, db, settings) -> None:
    _, token = issue_token(client)
    data = "Java backend resume for deletion test".encode("utf-8")
    created = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "待删除材料"},
        files={"file": ("resume.txt", data, "text/plain")},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    extract = client.post(
        f"/v1/materials/{created['id']}/extract",
        json={"revision": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, extract.json()["id"], worker)["state"] == "succeeded"

    profile = {
        "kind": "resume",
        "education": [],
        "work_experience": [],
        "projects": [],
        "skills": [
            {
                "item_id": "skill-1",
                "name": "Java",
                "code": "java.core",
                "evidence": {"quote": "Java backend resume for deletion test", "origin": "extracted"},
            }
        ],
    }
    confirm = client.post(
        f"/v1/materials/{created['id']}/confirm",
        json={"revision": 1, "profile": profile},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 201, confirm.text
    snapshot_id = confirm.json()["id"]

    draft = db.get(MaterialDraft, created["id"])
    file_path = settings.private_file_root / draft.file_ref
    assert file_path.exists()

    response = client.delete(
        f"/v1/materials/{created['id']}", headers={**auth(token), "Idempotency-Key": new_key()}
    )
    assert response.status_code == 204
    assert not file_path.exists()
    assert client.get(f"/v1/snapshots/{snapshot_id}", headers=auth(token)).status_code == 404
    assert db.get(MaterialSnapshot, snapshot_id) is None


def test_only_current_approved_bank_rows_are_eligible(client: TestClient, db) -> None:
    settings = get_settings()
    approved = db.scalars(
        select(BankQuestion).where(BankQuestion.status == "approved").limit(1)
    ).one()
    base_content = dict(approved.content)
    variants = []
    for index, status in enumerate(("review", "draft", "deprecated")):
        content = dict(base_content)
        content["question_id"] = f"EDGE-{status.upper()}-{index}"
        content["status"] = status
        row = BankQuestion(
            question_id=content["question_id"],
            version=1,
            canonical_id=f"edge-{status}-{index}",
            status=status,
            role_ids=list(approved.role_ids),
            levels=list(approved.levels),
            content=content,
            source_digest=approved.source_digest,
            test_only=True,
        )
        db.add(row)
        variants.append(row)
    db.commit()
    try:
        eligible = eligible_bank_questions(db, settings, "java-backend", "intern")
        eligible_ids = {item.question.question_id for item in eligible}
        for row in variants:
            assert row.question_id not in eligible_ids
        assert all(item.question.status == "approved" for item in eligible)
    finally:
        for row in variants:
            db.delete(row)
        db.commit()


def test_test_only_bank_excluded_without_test_mode(client: TestClient, db) -> None:
    from app.core.config import Settings

    production_like = Settings(
        APP_ENV="development",
        DATABASE_URL=get_settings().database_url,
        PRIVATE_FILE_ROOT=get_settings().private_file_root,
        AI_PROVIDER="test",
        ALLOW_TEST_FIXTURES=False,
        M1_SAMPLE_CATALOG=True,
    )
    assert eligible_bank_questions(db, production_like, "java-backend", "intern") == []
