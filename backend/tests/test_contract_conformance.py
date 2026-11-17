from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker

from tests.conftest import (
    REVIEWED_RESUME_TEXT,
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    issue_token,
    new_key,
    wait_job,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((REPO_ROOT / "contracts" / "m1" / "m1.schema.json").read_text(encoding="utf-8"))
OPENAPI = json.loads((REPO_ROOT / "contracts" / "m1" / "openapi.json").read_text(encoding="utf-8"))
DEFS = SCHEMA["$defs"]
VALIDATOR = Draft202012Validator({"$ref": "#/$defs/_root", "$defs": {**DEFS, "_root": {}}}, format_checker=FormatChecker())


def validate(def_name: str, payload: dict) -> None:
    schema = {"$ref": f"#/$defs/{def_name}", "$defs": DEFS}
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(payload),
        key=lambda error: error.path,
    )
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors[:5])


def test_contract_paths_are_implemented() -> None:
    from app.main import app

    implemented = {
        (method.upper(), path.removeprefix("/v1"))
        for path, operations in app.openapi()["paths"].items()
        if path.startswith("/v1")
        for method in operations
    }
    contract = {
        (method.upper(), path)
        for path, operations in OPENAPI["paths"].items()
        for method in operations
    }
    missing = contract - implemented
    assert not missing, f"契约中缺少实现: {sorted(missing)}"


def test_error_envelope_matches_contract(client: TestClient) -> None:
    response = client.get("/v1/materials")
    validate("ErrorResponse", response.json())
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"
    assert response.json()["error"]["request_id"]


def test_auth_and_material_shapes(client: TestClient) -> None:
    username, password = __import__("tests.conftest", fromlist=["register"]).register(client)
    login = client.post("/v1/auth/login", json={"username": username, "password": password})
    validate("TokenResponse", login.json())
    token = login.json()["access_token"]
    validate("User", client.get("/v1/me", headers=auth(token)).json())

    material = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "简历", "text": REVIEWED_RESUME_TEXT},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("MaterialDraft", material.json())
    snapshot = client.post(
        f"/v1/materials/{material.json()['id']}/confirm",
        json={
            "revision": 1,
            "profile": {
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
                "projects": [],
                "skills": [],
            },
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("Snapshot", snapshot.json())
    validate("Roles", client.get("/v1/roles", headers=auth(token)).json())
    validate("JdTemplates", client.get("/v1/jd-templates", headers=auth(token)).json())


def test_preparation_match_plan_job_shapes(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("Preparation", prep.json())
    validate("Preparations", client.get("/v1/preparations", headers=auth(token)).json())

    match = client.post(
        f"/v1/preparations/{prep.json()['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("Job", match.json())
    match_job = wait_job(client, token, match.json()["id"], worker)
    validate("Job", match_job)
    report = client.get(f"/v1/match-reports/{match_job['result_ref']['id']}", headers=auth(token))
    validate("MatchReport", report.json())

    generate = client.post(
        f"/v1/preparations/{prep.json()['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("Job", generate.json())
    plan_job = wait_job(client, token, generate.json()["id"], worker)
    assert plan_job["state"] == "succeeded"
    validate("Job", plan_job)
    plan = client.get(f"/v1/plans/{plan_job['result_ref']['id']}", headers=auth(token))
    validate("PublicPlan", plan.json())

    confirmed = client.post(
        f"/v1/plans/{plan_job['result_ref']['id']}/confirm",
        json={"revision": plan.json()["revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    validate("PublicPlan", confirmed.json())
