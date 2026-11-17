from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import (
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    new_key,
)


def _prep(client, token, *, resume=None, jd=None, count=2, role="java-backend", level="intern"):
    return client.post(
        "/v1/preparations",
        json={
            "role_id": role,
            "level": level,
            "main_question_count": count,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def test_draft_without_materials_cannot_match_or_generate(client: TestClient) -> None:
    _, token = issue_token(client)
    draft = _prep(client, token, count=6)
    assert draft.status_code == 201, draft.text
    prep = draft.json()
    assert prep["resume_snapshot_id"] is None
    assert prep["revision"] == 1

    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert match.status_code == 409
    assert match.json()["error"]["code"] == "MATERIALS_REQUIRED"
    assert set(match.json()["error"]["details"]) == {"resume_snapshot_id", "jd_snapshot_id"}

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert generate.status_code == 409
    assert generate.json()["error"]["code"] == "MATERIALS_REQUIRED"


def test_generate_requires_match_report(client: TestClient) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _prep(client, token, resume=resume, jd=jd).json()
    response = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "MATCH_REQUIRED"


def test_snapshot_kind_and_ownership_rules(client: TestClient) -> None:
    _, token_a = issue_token(client)
    _, token_b = issue_token(client)
    resume = create_confirmed_resume(client, token_a)
    jd = create_confirmed_template_jd(client, token_a)

    wrong_kind = _prep(client, token_a, resume=jd, jd=resume)
    assert wrong_kind.status_code == 409
    assert wrong_kind.json()["error"]["code"] == "MATERIALS_REQUIRED"

    other_user = _prep(client, token_b, resume=resume, jd=jd)
    assert other_user.status_code == 404
    assert other_user.json()["error"]["code"] == "NOT_FOUND"

    bad_level = _prep(client, token_a, resume=resume, jd=jd, level="senior")
    assert bad_level.status_code == 422

    bad_count = _prep(client, token_a, resume=resume, jd=jd, count=3)
    assert bad_count.status_code == 422


def test_jd_role_level_mismatch_blocks_preparation(client: TestClient) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    response = _prep(client, token, resume=resume, jd=jd, role="java-backend", level="junior")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ROLE_LEVEL_MISMATCH"


def test_edit_revision_clears_pointers_and_supersedes(client: TestClient) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = _prep(client, token, resume=resume, jd=jd, count=4).json()

    updated = client.put(
        f"/v1/preparations/{prep['id']}",
        json={
            "revision": 1,
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["revision"] == 2
    assert body["main_question_count"] == 6
    assert body["match_report_id"] is None
    assert body["current_plan_id"] is None

    stale = client.put(
        f"/v1/preparations/{prep['id']}",
        json={
            "revision": 1,
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "REVISION_CONFLICT"


def test_role_listing_and_templates(client: TestClient) -> None:
    _, token = issue_token(client)
    roles = client.get("/v1/roles", headers=auth(token))
    assert roles.status_code == 200
    items = roles.json()["items"]
    assert items and items[0]["id"] == "java-backend"
    assert items[0]["enabled"] is True
    assert items[0]["sample_only"] is True

    templates = client.get("/v1/jd-templates", headers=auth(token))
    assert templates.status_code == 200
    template = templates.json()["items"][0]
    assert template["label"] == "内置练习岗位"
    assert template["id"] == "java-backend-intern-v1"

    single = client.get(f"/v1/jd-templates/{template['id']}", headers=auth(token))
    assert single.status_code == 200
    assert single.json()["version"] == 1


def test_preparation_list_and_delete(client: TestClient) -> None:
    _, token = issue_token(client)
    prep = _prep(client, token, count=2).json()
    listed = client.get("/v1/preparations", headers=auth(token))
    assert listed.status_code == 200
    assert any(item["id"] == prep["id"] for item in listed.json()["items"])

    deleted = client.delete(
        f"/v1/preparations/{prep['id']}", headers={**auth(token), "Idempotency-Key": new_key()}
    )
    assert deleted.status_code == 204
    assert client.get(f"/v1/preparations/{prep['id']}", headers=auth(token)).status_code == 404
