from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import auth, login, new_key, register


def test_register_login_me_logout(client: TestClient) -> None:
    username, password = register(client)
    assert client.post(
        "/v1/auth/register", json={"username": username, "password": password}
    ).status_code == 409
    token = login(client, username, password)
    me = client.get("/v1/me", headers=auth(token))
    assert me.status_code == 200
    assert me.json()["username"] == username

    logout = client.post("/v1/auth/logout", headers=auth(token))
    assert logout.status_code == 204
    assert client.get("/v1/me", headers=auth(token)).status_code == 401


def test_wrong_password_is_generic(client: TestClient) -> None:
    username, _ = register(client)
    response = client.post(
        "/v1/auth/login", json={"username": username, "password": "wrongpass1234"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert "不存在" not in response.json()["error"]["message"]


def test_username_validation(client: TestClient) -> None:
    for payload in (
        {"username": "ab", "password": "password12345"},
        {"username": "bad name", "password": "password12345"},
        {"username": "okname", "password": "short"},
    ):
        response = client.post("/v1/auth/register", json=payload)
        assert response.status_code == 422, payload
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_missing_idempotency_key_is_rejected(client: TestClient) -> None:
    _, token = _token(client)
    response = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "t", "text": "内容", "template_id": None, "template_version": None},
        headers=auth(token),
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert response.headers.get("X-Request-ID")


def test_unauthenticated_requests(client: TestClient) -> None:
    assert client.get("/v1/materials").status_code == 401
    assert client.get("/v1/skill-codes").status_code == 401
    response = client.get("/v1/roles", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def _token(client: TestClient) -> tuple[str, str]:
    username, password = register(client)
    return username, login(client, username, password)
