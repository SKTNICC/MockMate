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


def test_skill_codes_catalog_and_seed_rules(client: TestClient, db) -> None:
    from app.cli import seed_catalog
    from app.models import SkillCode
    from app.services import catalog

    _, token = issue_token(client)
    resp = client.get("/v1/skill-codes", headers=auth(token))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items == [
        {"code": "java.concurrency", "label": "Java 并发"},
        {"code": "java.core", "label": "Java 基础"},
        {"code": "test.junit", "label": "JUnit 测试"},
    ]
    assert catalog.allowed_skill_codes(db) == {"java.core", "java.concurrency", "test.junit"}

    # 停用某项后，目录接口与允许集合同步变化；重复执行 seed 保留自定义名与停用状态
    row_junit = db.get(SkillCode, "test.junit")
    row_core = db.get(SkillCode, "java.core")
    row_conc = db.get(SkillCode, "java.concurrency")
    assert row_junit and row_core and row_conc
    try:
        row_junit.enabled = False
        row_junit.label = "自定义 JUnit"
        row_core.label = "java.core"  # 旧占位名应被补全
        row_conc.label = ""  # 空值应被补全
        db.commit()

        seed_catalog(db, sample_enabled=True)
        db.expire_all()
        assert db.get(SkillCode, "java.core").label == "Java 基础"
        assert db.get(SkillCode, "java.concurrency").label == "Java 并发"
        assert db.get(SkillCode, "test.junit").label == "自定义 JUnit"
        assert db.get(SkillCode, "test.junit").enabled is False

        filtered = client.get("/v1/skill-codes", headers=auth(token))
        assert filtered.status_code == 200
        assert [item["code"] for item in filtered.json()["items"]] == [
            "java.concurrency",
            "java.core",
        ]
        assert catalog.allowed_skill_codes(db) == {"java.concurrency", "java.core"}

        # 全部停用时返回 200 与空数组
        for row in (row_core, row_conc, row_junit):
            row.enabled = False
        db.commit()
        empty = client.get("/v1/skill-codes", headers=auth(token))
        assert empty.status_code == 200
        assert empty.json() == {"items": []}
        assert catalog.allowed_skill_codes(db) == set()
    finally:
        row_core.label = "Java 基础"
        row_core.enabled = True
        row_conc.label = "Java 并发"
        row_conc.enabled = True
        row_junit.label = "JUnit 测试"
        row_junit.enabled = True
        db.commit()


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
