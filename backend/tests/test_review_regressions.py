"""独立验收 R01–R10 的正式回归测试。

这些用例断言修复后的正确业务行为，不读取或依赖诊断脚本的退出码。
数据库用例使用测试库（conftest 已隔离）；不写正式内容，不调用真实外部模型。
"""

from __future__ import annotations

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.ai.provider import DeepSeekProvider, TestProvider
from app.core.config import Settings, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import BankQuestion, MaterialDraft
from app.schemas import EditMaterialInput
from app.services import materials as materials_service
from app.services.planning import (
    PlanValidationContext,
    parse_internal_plan,
    stem_similarity,
    validate_internal_plan,
)
from tests.conftest import (
    REVIEWED_RESUME_PROFILE,
    REVIEWED_RESUME_TEXT,
    TEMPLATE_JD_PROFILE,
    TEMPLATE_JD_TEXT,
    auth,
    issue_token,
    new_key,
    wait_job,
)

BASE_PLAN = json.loads(
    (Path(__file__).resolve().parents[2] / "fixtures/m1/internal-plan.example.json").read_text(
        encoding="utf-8"
    )
)


def _plan_and_validate(mutate) -> None:
    value = copy.deepcopy(BASE_PLAN)
    mutate(value)
    validate_internal_plan(parse_internal_plan(value), PlanValidationContext(True))


def _snapshots() -> tuple[SimpleNamespace, SimpleNamespace]:
    resume = SimpleNamespace(**BASE_PLAN["material_snapshots"][0])
    jd = SimpleNamespace(**BASE_PLAN["material_snapshots"][1])
    return resume, jd


# --------------------------------------------------------------------------- R01–R04


def test_r01_bank_stem_must_match_reviewed_content() -> None:
    def mutate(plan: dict) -> None:
        plan["questions"][0]["stem"] = (
            "请说明一个与审核原题完全不同的新问题该如何回答，并给出推理过程。"
        )

    with pytest.raises(AppError) as caught:
        _plan_and_validate(mutate)
    assert caught.value.code == "BANK_INELIGIBLE"


def test_r02_unresolved_uncertainty_cannot_be_ready() -> None:
    def mutate(plan: dict) -> None:
        plan["questions"][3]["evaluation"]["uncertainties"] = ["关键技术结论尚未核验"]

    with pytest.raises(AppError) as caught:
        _plan_and_validate(mutate)
    assert caught.value.code == "EVALUATION_INVALID"


def test_r03_anchor_quote_must_belong_to_referenced_item() -> None:
    def mutate(plan: dict) -> None:
        plan["questions"][3]["anchors"][0].update(item_id="edu-1")

    with pytest.raises(AppError) as caught:
        _plan_and_validate(mutate)
    assert caught.value.code == "EVIDENCE_INVALID"


def test_r04_near_duplicate_uses_agreed_threshold() -> None:
    original = BASE_PLAN["questions"][0]["stem"]
    changed = original + "请说明理由与验证步骤。"
    assert stem_similarity(original, changed) >= 0.70

    def mutate(plan: dict) -> None:
        plan["questions"][3]["stem"] = changed

    with pytest.raises(AppError) as caught:
        _plan_and_validate(mutate)
    assert caught.value.code == "DUPLICATE_QUESTION"


# --------------------------------------------------------------------------- R05 / R06


def test_r05_jd_only_hypothetical_scenario_is_allowed(worker) -> None:
    resume, jd = _snapshots()
    generated = {
        key: copy.deepcopy(value)
        for key, value in BASE_PLAN["questions"][3].items()
        if key not in {"id", "position", "source", "bank_ref"}
    }
    requirement = jd.profile["requirements"][0]
    generated["stem"] = (
        "假设你要根据岗位要求实现一个 Java 基础功能，你会如何分析边界并验证自己的实现？"
    )
    generated["anchors"] = [
        {
            "snapshot_id": jd.id,
            "item_id": requirement["item_id"],
            "quote": requirement["evidence"]["quote"],
        }
    ]
    generated["evaluation"]["basis"] = [
        {
            "kind": "material",
            "ref_id": f"{jd.id}/{requirement['item_id']}",
            "note": "明确假设场景，不声称做过项目",
        },
        {"kind": "reasoning", "ref_id": "hypothetical-analysis", "note": "只评价分析过程"},
    ]
    generated["evaluation"]["uncertainties"] = []

    worker._validate_generated(generated, resume, jd)


def test_r06_supported_requires_resume_evidence(worker) -> None:
    resume, jd = _snapshots()
    rows = [
        {
            "requirement_id": entry["item_id"],
            "status": "supported",
            "resume_evidence": [],
            "explanation": "无证据结论",
            "suggested_focus": "核实依据",
        }
        for entry in jd.profile["requirements"]
    ]
    with pytest.raises(AppError) as caught:
        worker._validate_match_rows(rows, resume, jd)
    assert caught.value.code == "AI_INVALID_OUTPUT"


# --------------------------------------------------------------------------- R07


def test_r07_stale_material_edit_is_rejected(client: TestClient, settings) -> None:
    _, token = issue_token(client)
    created = client.post(
        "/v1/materials/text",
        json={
            "kind": "resume",
            "title": "并发材料",
            "text": REVIEWED_RESUME_TEXT,
            "template_id": None,
            "template_version": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created.status_code == 201, created.text
    material_id = uuid.UUID(created.json()["id"])
    owner_id = uuid.UUID(client.get("/v1/me", headers=auth(token)).json()["id"])

    first, second = SessionLocal(), SessionLocal()
    try:
        row1 = materials_service.get_material(first, owner_id, material_id)
        row2 = materials_service.get_material(second, owner_id, material_id)
        # 两个会话都读到 revision=1，模拟真实的旧版本并发写。
        assert row1.revision == row2.revision == 1
        materials_service.edit_material(
            first,
            row1,
            EditMaterialInput(revision=1, title="并发A", text="第一次修改"),
            settings,
        )
        first.commit()

        with pytest.raises(AppError) as caught:
            materials_service.edit_material(
                second,
                row2,
                EditMaterialInput(revision=1, title="并发B", text="第二次旧版本修改"),
                settings,
            )
            second.commit()
        assert caught.value.code == "REVISION_CONFLICT"
        second.rollback()
    finally:
        first.close()
        second.close()

    with SessionLocal() as check:
        final = check.get(MaterialDraft, material_id)
        assert final is not None
        assert final.revision == 2
        assert final.text == "第一次修改"


# --------------------------------------------------------------------------- R08


def test_r08_deprecated_bank_cannot_be_published(
    client: TestClient, worker, settings, monkeypatch
) -> None:
    import app.workers.runner as runner

    _, token = issue_token(client)

    def create_material(kind: str, title: str, text: str, template_id=None, template_version=None):
        response = client.post(
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
        assert response.status_code == 201, response.text
        return response.json()

    resume = create_material("resume", "简历", REVIEWED_RESUME_TEXT)
    resume_snapshot = client.post(
        f"/v1/materials/{resume['id']}/confirm",
        json={"revision": 1, "profile": REVIEWED_RESUME_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert resume_snapshot.status_code == 201, resume_snapshot.text

    jd = create_material(
        "jd", "内置 JD", TEMPLATE_JD_TEXT, "java-backend-intern-v1", 1
    )
    jd_snapshot = client.post(
        f"/v1/materials/{jd['id']}/confirm",
        json={"revision": 1, "profile": TEMPLATE_JD_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert jd_snapshot.status_code == 201, jd_snapshot.text

    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_snapshot.json()["id"],
            "jd_snapshot_id": jd_snapshot.json()["id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert prep.status_code == 201, prep.text

    match = client.post(
        f"/v1/preparations/{prep.json()['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert match.status_code == 202, match.text
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    plan_job = client.post(
        f"/v1/preparations/{prep.json()['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert plan_job.status_code == 202, plan_job.text

    revoked: list[tuple[str, int]] = []

    class RevokingProvider(TestProvider):
        def review_questions(self, *, questions, material_texts, fact_cards=None):
            result = super().review_questions(
                questions=questions,
                material_texts=material_texts,
                fact_cards=fact_cards,
            )
            with SessionLocal() as other:
                bank = other.scalars(
                    select(BankQuestion)
                    .where(BankQuestion.status == "approved")
                    .order_by(BankQuestion.question_id)
                ).first()
                assert bank is not None
                revoked.append((bank.question_id, bank.version))
                bank.status = "deprecated"
                other.commit()
            return result

    monkeypatch.setattr(runner, "get_provider", lambda _: RevokingProvider())
    try:
        for _ in range(6):
            state = client.get(
                f"/v1/jobs/{plan_job.json()['id']}", headers=auth(token)
            ).json()["state"]
            if state in {"succeeded", "failed", "interrupted", "superseded"}:
                break
            worker.run_once()
    finally:
        with SessionLocal() as restore:
            for question_id, version in revoked:
                row = restore.get(BankQuestion, (question_id, version))
                if row is not None:
                    row.status = "approved"
            restore.commit()

    job = client.get(f"/v1/jobs/{plan_job.json()['id']}", headers=auth(token)).json()
    assert revoked, "测试必须在生成与发布之间真的撤销一条题库题"
    assert job["state"] == "failed", job
    assert job["error"]["code"] == "BANK_INSUFFICIENT"


# --------------------------------------------------------------------------- R09 / R10


def test_r09_unexpected_provider_error_becomes_controlled_failure(
    client: TestClient, worker, monkeypatch
) -> None:
    import app.workers.runner as runner

    _, token = issue_token(client)
    draft = client.post(
        "/v1/materials/text",
        json={
            "kind": "resume",
            "title": "异常解析材料",
            "text": REVIEWED_RESUME_TEXT,
            "template_id": None,
            "template_version": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert draft.status_code == 201, draft.text
    job = client.post(
        f"/v1/materials/{draft.json()['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert job.status_code == 202, job.text

    class BrokenProvider(TestProvider):
        def parse_material(self, **kwargs):
            raise RuntimeError("audit-injected-unexpected-error")

    monkeypatch.setattr(runner, "get_provider", lambda _: BrokenProvider())
    for _ in range(6):
        state = client.get(f"/v1/jobs/{job.json()['id']}", headers=auth(token)).json()["state"]
        if state in {"succeeded", "failed", "interrupted", "superseded"}:
            break
        worker.run_once()  # 不允许把异常抛给调用方

    result = client.get(f"/v1/jobs/{job.json()['id']}", headers=auth(token)).json()
    assert result["state"] == "failed", result
    assert result["error"]["code"] == "INTERNAL_ERROR"


def test_r10_non_json_http_envelope_is_invalid_output() -> None:
    settings = Settings(
        APP_ENV="test",
        DATABASE_URL=get_settings().database_url,
        PRIVATE_FILE_ROOT=str(get_settings().private_file_root),
        AI_PROVIDER="deepseek",
        AI_MODEL="audit-model",
        AI_API_KEY="AUDIT-FAKE-NOT-A-KEY",
        AI_BASE_URL="https://api.deepseek.test",
        ALLOW_TEST_FIXTURES=False,
    )
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not-json", request=request)
    )
    provider = DeepSeekProvider(settings, transport=transport)
    with pytest.raises(AppError) as caught:
        provider._chat_json("audit", "audit")
    assert caught.value.code == "AI_INVALID_OUTPUT"
