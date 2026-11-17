from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from tests.conftest import REVIEWED_RESUME_TEXT, auth, issue_token, new_key


def test_same_key_same_body_replays(client: TestClient) -> None:
    _, token = issue_token(client)
    key = new_key()
    body = {"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT}
    first = client.post("/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key})
    second = client.post("/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key})
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    conflict = client.post(
        "/v1/materials/text",
        json={**body, "title": "另一个标题"},
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_different_keys_create_different_objects(client: TestClient) -> None:
    _, token = issue_token(client)
    body = {"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT}
    first = client.post(
        "/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": new_key()}
    )
    second = client.post(
        "/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": new_key()}
    )
    assert first.json()["id"] != second.json()["id"]


def test_file_upload_idempotency_uses_file_digest(client: TestClient) -> None:
    _, token = issue_token(client)
    key = new_key()
    data = "简历正文：Java 集合和 JUnit。".encode("utf-8")
    first = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "文件简历"},
        files={"file": ("resume.txt", data, "text/plain")},
        headers={**auth(token), "Idempotency-Key": key},
    )
    second = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "文件简历"},
        files={"file": ("resume.txt", data, "text/plain")},
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["id"] == second.json()["id"]

    different = client.post(
        "/v1/materials/file",
        data={"kind": "resume", "title": "文件简历"},
        files={"file": ("resume.txt", "另一份不同内容的简历".encode("utf-8"), "text/plain")},
        headers={**auth(token), "Idempotency-Key": key},
    )
    assert different.status_code == 409
    assert different.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_idempotency_key_must_be_uuid(client: TestClient) -> None:
    _, token = issue_token(client)
    response = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT},
        headers={**auth(token), "Idempotency-Key": "not-a-uuid"},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"


def test_idempotency_record_not_revived_after_logout(client: TestClient) -> None:
    username, token = issue_token(client)
    key = new_key()
    body = {"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT}
    first = client.post("/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key})
    assert first.status_code == 201
    client.post("/v1/auth/logout", headers=auth(token))

    replay = client.post("/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key})
    assert replay.status_code == 401
    assert replay.json()["error"]["code"] == "UNAUTHENTICATED"


def test_delete_purges_replayable_private_responses(client: TestClient) -> None:
    _, token = issue_token(client)
    key = new_key()
    body = {"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT}
    created = client.post(
        "/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key}
    ).json()
    deleted = client.delete(
        f"/v1/materials/{created['id']}", headers={**auth(token), "Idempotency-Key": new_key()}
    )
    assert deleted.status_code == 204

    replay = client.post("/v1/materials/text", json=body, headers={**auth(token), "Idempotency-Key": key})
    assert replay.status_code == 201
    assert replay.json()["id"] != created["id"]
    assert replay.json()["id"] != str(uuid.UUID(int=0))
