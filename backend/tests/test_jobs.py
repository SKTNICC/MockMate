from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Job, MaterialDraft
from app.services import jobs as jobs_service
from tests.conftest import (
    REVIEWED_RESUME_TEXT,
    auth,
    issue_token,
    new_key,
    wait_job,
)


def _create_resume(client, token, text=REVIEWED_RESUME_TEXT):
    response = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "简历", "text": text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201
    return response.json()


def _parse(client, token, material_id, revision=1, role="java-backend", level="intern"):
    return client.post(
        f"/v1/materials/{material_id}/parse",
        json={
            "revision": revision,
            "external_processing_consent": True,
            "role_id": role,
            "level": level,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def test_parse_job_is_shared_for_same_revision(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    first = _parse(client, token, material["id"])
    second = _parse(client, token, material["id"])
    assert first.status_code == 202 and second.status_code == 202
    assert first.json()["id"] == second.json()["id"]


def test_parse_job_input_conflict_on_role_change(client: TestClient) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    assert _parse(client, token, material["id"], level="intern").status_code == 202
    conflict = _parse(client, token, material["id"], level="junior")
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "JOB_INPUT_CONFLICT"


def test_expired_lease_interrupted_and_explicit_retry(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_response = _parse(client, token, material["id"])
    job_id = uuid.UUID(job_response.json()["id"])

    # 模拟 worker 领取后失联：直接改租约过期并回收
    job = db.get(Job, job_id)
    job.state = "running"
    job.lease_token = uuid.uuid4()
    job.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    db.commit()

    reaped = jobs_service.reap_expired_leases(db)
    assert reaped >= 1
    interrupted = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert interrupted["state"] == "interrupted"
    assert interrupted["error"]["code"] == "JOB_INTERRUPTED"
    assert interrupted["retryable"] is True
    assert interrupted["result_ref"] is None

    # 不会自动重发外部调用
    worker.run_once()
    still = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert still["state"] == "interrupted"
    assert still["attempt"] == 1

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["attempt"] == 2
    assert retried.json()["state"] == "queued"

    final = wait_job(client, token, str(job_id), worker, attempts=3)
    assert final["state"] == "succeeded"

    too_late = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert too_late.status_code == 409
    assert too_late.json()["error"]["code"] == "RETRY_NOT_ALLOWED"


def test_retry_rejected_when_not_allowed(client: TestClient, worker) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = _parse(client, token, material["id"]).json()["id"]
    final = wait_job(client, token, job_id, worker)
    assert final["state"] == "succeeded"

    response = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RETRY_NOT_ALLOWED"

    wrong_attempt = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wrong_attempt.status_code == 409


def test_old_worker_cannot_submit_after_lease_lost(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = uuid.UUID(_parse(client, token, material["id"]).json()["id"])

    claimed = jobs_service.claim_next_job(db, "test-worker")
    while claimed is not None and claimed.id != job_id:
        claimed = jobs_service.claim_next_job(db, "test-worker")
    assert claimed is not None
    assert claimed.id == job_id
    stale_token = claimed.lease_token
    stale_attempt = claimed.attempt

    # 另一进程回收并重新入队（模拟新尝试）
    claimed.state = "interrupted"
    claimed.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db.commit()

    fenced = jobs_service.fence_job(db, job_id, stale_attempt, stale_token)
    assert fenced is None


def test_job_visibility_is_owner_only(client: TestClient) -> None:
    _, token_a = issue_token(client)
    _, token_b = issue_token(client)
    material = _create_resume(client, token_a)
    job_id = _parse(client, token_a, material["id"]).json()["id"]
    assert client.get(f"/v1/jobs/{job_id}", headers=auth(token_b)).status_code == 404
    assert (
        client.get(f"/v1/jobs/{uuid.uuid4()}", headers=auth(token_a)).status_code == 404
    )


def test_edit_supersedes_open_parse_job(client: TestClient, db) -> None:
    _, token = issue_token(client)
    material = _create_resume(client, token)
    job_id = uuid.UUID(_parse(client, token, material["id"]).json()["id"])

    draft = db.get(MaterialDraft, uuid.UUID(material["id"]))
    assert draft is not None
    edited = client.put(
        f"/v1/materials/{material['id']}",
        json={"revision": 1, "title": "简历2", "text": REVIEWED_RESUME_TEXT + "\n补充内容。"},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200

    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "superseded"
    assert job["retryable"] is False


def test_plan_retry_reuses_validated_work_items(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """真实失败后显式 retry：已合格槽位复用，只重做失败槽位。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    class FlakyProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_two = True
            self.calls: list[str] = []

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            if slot == "generated-2" and self.fail_slot_two:
                raise AppError("AI_TIMEOUT", "模拟第二槽外部超时，结果未知")
            return super().generate_questions(context=context, **kwargs)

    provider = FlakyProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=3)
    assert failed["state"] == "failed", failed
    assert failed["error"]["code"] == "AI_TIMEOUT"
    assert provider.calls == ["generated-1", "generated-2"]

    saved = db.scalars(
        select(JobWorkItem).where(
            JobWorkItem.job_id == job_id, JobWorkItem.slot_key == "generated-1"
        )
    ).one_or_none()
    assert saved is not None and saved.validated is True
    assert saved.payload["validation_version"] == "m1-work-item-v2"
    assert saved.payload["context_hash"]
    first_question = saved.payload["question"]
    assert first_question["stem"]

    provider.fail_slot_two = False
    provider.calls.clear()
    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202, retried.text
    final = wait_job(client, token, str(job_id), worker, attempts=3)
    assert final["state"] == "succeeded", final
    assert provider.calls == ["generated-2"], "已保存槽位不得再次调用模型"

    plan_row = db.scalars(select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))).one()
    plan = client.get(f"/v1/plans/{plan_row.id}", headers=auth(token)).json()
    assert plan["source_counts"] == {"bank": 2, "generated": 2}
    generated = [
        q for q in plan_row.internal_payload["questions"] if q["source"] == "generated"
    ]
    assert generated[0]["stem"] == first_question["stem"]


def test_failed_match_job_explicit_retry_and_boundaries(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """旧任务恢复：第 1 次因 AI_INVALID_OUTPUT 失败的匹配任务可显式重试一次，过期或他账号不可重试。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import MatchReport, Preparation
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    _, other_token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()

    match_calls = 0

    class RecoverableMatchProvider(TestProvider):
        def match_requirements(self, *, resume_snapshot, jd_snapshot):
            nonlocal match_calls
            match_calls += 1
            return super().match_requirements(
                resume_snapshot=resume_snapshot, jd_snapshot=jd_snapshot
            )

    monkeypatch.setattr(runner, "get_provider", lambda settings: RecoverableMatchProvider())

    # 预置一条第 1 次尝试以旧错误消息失败的匹配任务（未生成报告，准备仍在 revision=1）。
    created_match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created_match.status_code == 202
    job_id = uuid.UUID(created_match.json()["id"])

    job = db.get(Job, job_id)
    assert job is not None
    job.state = "failed"
    job.attempt = 1
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "引用原文不属于所引用的材料条目",
        "details": ["skill-1", "坏引文"],
        "request_id": "legacy-req",
    }
    job.result = None
    db.commit()

    # 只读查询 GET 动态返回 retryable=True，且不会自动触发模型调用。
    queried = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert queried.status_code == 200
    assert queried.json()["retryable"] is True
    assert queried.json()["attempt"] == 1
    assert queried.json()["result_ref"] is None
    assert worker.run_once() is False
    assert match_calls == 0

    # 重复点击「开始匹配分析」只复用同一失败任务，不会重跑模型。
    repeated_start = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert repeated_start.status_code == 202
    assert repeated_start.json()["id"] == str(job_id)
    assert repeated_start.json()["state"] == "failed"
    assert repeated_start.json()["attempt"] == 1
    assert match_calls == 0

    # 其他账号不可读取或重试该任务。
    assert client.get(f"/v1/jobs/{job_id}", headers=auth(other_token)).status_code == 404
    assert (
        client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(other_token), "Idempotency-Key": new_key()},
        ).status_code
        == 404
    )

    # 显式重试：同一任务 ID 入队并把 attempt 加到 2，正常生成报告。
    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202, retried.text
    assert retried.json()["id"] == str(job_id)
    assert retried.json()["attempt"] == 2
    assert retried.json()["state"] == "queued"

    final = wait_job(client, token, str(job_id), worker)
    assert final["state"] == "succeeded", final
    assert match_calls == 1
    report = client.get(
        f"/v1/match-reports/{final['result_ref']['id']}", headers=auth(token)
    ).json()
    assert report["input_revision"] == 1
    assert report["rows"]

    # 测试第 2 次仍失败时达到尝试上限（retryable=False），且输入版本过期后不可重试。
    _, token2 = issue_token(client)
    resume2 = create_confirmed_resume(client, token2)
    jd2 = create_confirmed_template_jd(client, token2)
    prep2 = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume2,
            "jd_snapshot_id": jd2,
        },
        headers={**auth(token2), "Idempotency-Key": new_key()},
    ).json()

    class AlwaysInvalidMatchProvider(TestProvider):
        def match_requirements(self, *, resume_snapshot, jd_snapshot):
            raise AppError(
                "AI_INVALID_OUTPUT",
                "匹配结果引用了不存在的条目",
                details=["stage=match", "reason=invalid_evidence_selection", "item_id=bad-1"],
            )

    monkeypatch.setattr(runner, "get_provider", lambda settings: AlwaysInvalidMatchProvider())
    m2 = client.post(
        f"/v1/preparations/{prep2['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token2), "Idempotency-Key": new_key()},
    )
    job2_id = m2.json()["id"]
    first_fail = wait_job(client, token2, job2_id, worker)
    assert first_fail["state"] == "failed"
    assert first_fail["retryable"] is True

    retry2 = client.post(
        f"/v1/jobs/{job2_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token2), "Idempotency-Key": new_key()},
    )
    assert retry2.status_code == 202
    second_fail = wait_job(client, token2, job2_id, worker)
    assert second_fail["state"] == "failed"
    assert second_fail["attempt"] == 2
    assert second_fail["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{job2_id}/retry",
            json={"expected_attempt": 2},
            headers={**auth(token2), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )

    # 新建一个仅失败 1 次的任务，编辑准备导致输入版本过期后，旧任务不可重试。
    prep3 = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume2,
            "jd_snapshot_id": jd2,
        },
        headers={**auth(token2), "Idempotency-Key": new_key()},
    ).json()
    m3 = client.post(
        f"/v1/preparations/{prep3['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token2), "Idempotency-Key": new_key()},
    )
    job3_id = m3.json()["id"]
    fail3 = wait_job(client, token2, job3_id, worker)
    assert fail3["retryable"] is True

    edited = client.put(
        f"/v1/preparations/{prep3['id']}",
        json={
            "revision": 1,
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume2,
            "jd_snapshot_id": jd2,
        },
        headers={**auth(token2), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 2

    stale_job = client.get(f"/v1/jobs/{job3_id}", headers=auth(token2)).json()
    assert stale_job["retryable"] is False
    stale_retry = client.post(
        f"/v1/jobs/{job3_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token2), "Idempotency-Key": new_key()},
    )
    assert stale_retry.status_code == 409
    assert stale_retry.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert db.get(Preparation, uuid.UUID(prep3["id"])).match_report_id is None
    assert (
        db.scalars(
            select(MatchReport).where(MatchReport.preparation_id == uuid.UUID(prep3["id"]))
        ).all()
        == []
    )


def test_failed_plan_job_explicit_retry_and_boundaries(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """旧截图形状失败与新结构化格式失败可限次显式重试；非格式/过期/越权/超限均不可重试。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan, Preparation
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    _, other_token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    plan_calls: list[str] = []

    class RecoverablePlanProvider(TestProvider):
        def generate_questions(self, *, context=None, **kwargs):
            plan_calls.append((context or {}).get("slot_key", "?"))
            return super().generate_questions(context=context, **kwargs)

    monkeypatch.setattr(runner, "get_provider", lambda settings: RecoverablePlanProvider())

    created_plan = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert created_plan.status_code == 202
    job_id = uuid.UUID(created_plan.json()["id"])

    # 预置截图里的旧格式失败记录（attempt=1, generated-1, AI 返回内容不是合法 JSON）
    job = db.get(Job, job_id)
    assert job is not None
    job.state = "failed"
    job.attempt = 1
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["generated-1", "AI 返回内容不是合法 JSON"],
        "request_id": "legacy-plan-req",
    }
    job.result = None
    db.commit()

    # 只读 GET 返回 retryable=True，不自动触发模型调用
    queried = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert queried.status_code == 200
    assert queried.json()["retryable"] is True
    assert queried.json()["attempt"] == 1
    assert worker.run_once() is False
    assert plan_calls == []

    # 重复点击「生成计划」只复用原失败任务，不重跑模型
    repeated = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert repeated.status_code == 202
    assert repeated.json()["id"] == str(job_id)
    assert repeated.json()["state"] == "failed"
    assert plan_calls == []

    # 其他账号不可读取或重试
    assert client.get(f"/v1/jobs/{job_id}", headers=auth(other_token)).status_code == 404
    assert (
        client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(other_token), "Idempotency-Key": new_key()},
        ).status_code
        == 404
    )

    # 显式重试：原任务 ID attempt 加到 2，成功发布计划
    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    assert retried.json()["id"] == str(job_id)
    assert retried.json()["attempt"] == 2
    final = wait_job(client, token, str(job_id), worker, attempts=4)
    assert final["state"] == "succeeded", final
    assert plan_calls == ["generated-1", "generated-2"]

    # 结构化格式失败 + 已保存槽位复用 + 次数上限与非格式/过期边界
    prep2 = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    m2 = client.post(
        f"/v1/preparations/{prep2['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, m2.json()["id"], worker)["state"] == "succeeded"

    slot2_calls: list[str] = []

    class SlotTwoTruncatedProvider(TestProvider):
        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            slot2_calls.append(slot)
            if slot == "generated-2":
                raise AppError(
                    "AI_INVALID_OUTPUT",
                    "AI 输出因达到长度上限而未完整结束",
                    details=["reason=output_truncated", "finish_reason=length"],
                )
            return super().generate_questions(context=context, **kwargs)

    monkeypatch.setattr(runner, "get_provider", lambda settings: SlotTwoTruncatedProvider())
    p2 = client.post(
        f"/v1/preparations/{prep2['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job2_id = p2.json()["id"]
    first_fail = wait_job(client, token, job2_id, worker, attempts=3)
    assert first_fail["state"] == "failed"
    assert first_fail["retryable"] is True
    assert "stage=plan" in first_fail["error"]["details"]
    assert "slot=generated-2" in first_fail["error"]["details"]
    assert "reason=output_truncated" in first_fail["error"]["details"]
    assert "finish_reason=length" in first_fail["error"]["details"]
    assert slot2_calls == ["generated-1", "generated-2", "generated-2"]

    # 重试后若仍失败，第 2 次达到上限，不再允许重试
    slot2_calls.clear()
    retry2 = client.post(
        f"/v1/jobs/{job2_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry2.status_code == 202
    second_fail = wait_job(client, token, job2_id, worker, attempts=3)
    assert second_fail["state"] == "failed"
    assert second_fail["attempt"] == 2
    assert second_fail["retryable"] is False
    # generated-1 已校验槽位复用，未再次调用
    assert slot2_calls == ["generated-2", "generated-2"]
    assert (
        client.post(
            f"/v1/jobs/{job2_id}/retry",
            json={"expected_attempt": 2},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )

    # 非格式业务校验失败（如 schema/语义复核/去重）以及过期版本均不可重试
    job2_row = db.get(Job, uuid.UUID(job2_id))
    assert job2_row is not None
    non_format_cases = [
        ("DUPLICATE_QUESTION", "存在重复主问题", ["generated-1"]),
        (
            "AI_INVALID_OUTPUT",
            "生成槽位在有限纠正后仍不合法",
            ["stage=plan", "slot=generated-1", "reason=invalid_question_schema", "finish_reason=stop"],
        ),
        (
            "AI_INVALID_OUTPUT",
            "生成槽位在有限纠正后仍不合法",
            ["generated-1", "生成题缺少合理替代解"],
        ),
        (
            "AI_INVALID_OUTPUT",
            "语义复核包含未知或非法题目引用",
            [],
        ),
    ]
    for code, msg, details in non_format_cases:
        job2_row.attempt = 1
        job2_row.state = "failed"
        job2_row.error_code = code
        job2_row.error = {"code": code, "message": msg, "details": details, "request_id": "x"}
        db.commit()
        assert client.get(f"/v1/jobs/{job2_id}", headers=auth(token)).json()["retryable"] is False

    # 格式失败但在编辑准备使输入版本过期后不可重试
    job2_row.attempt = 1
    job2_row.state = "failed"
    job2_row.error_code = "AI_INVALID_OUTPUT"
    job2_row.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["stage=plan", "slot=generated-1", "reason=invalid_json", "finish_reason=stop"],
        "request_id": "x",
    }
    db.commit()
    assert client.get(f"/v1/jobs/{job2_id}", headers=auth(token)).json()["retryable"] is True

    edited = client.put(
        f"/v1/preparations/{prep2['id']}",
        json={
            "revision": 1,
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert edited.status_code == 200
    assert client.get(f"/v1/jobs/{job2_id}", headers=auth(token)).json()["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{job2_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )
    assert db.get(Preparation, uuid.UUID(prep2["id"])).current_plan_id is None
    assert db.scalars(select(Plan).where(Plan.preparation_id == uuid.UUID(prep2["id"]))).all() == []


def test_plan_retry_includes_reused_slot_summaries_for_subsequent_slot(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """缓存复用的合格槽位也必须纳入后续槽位的 previous_summaries，且不重复调用模型。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    captured_contexts: list[dict] = []

    class SlotThreeTimeoutProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_three = True
            self.calls: list[str] = []

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            captured_contexts.append({"slot": slot, "context": dict(context or {})})
            if slot == "generated-3" and self.fail_slot_three:
                raise AppError("AI_TIMEOUT", "模拟第 3 槽超时")
            return super().generate_questions(context=context, **kwargs)

    provider = SlotThreeTimeoutProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=4)
    assert failed["state"] == "failed"
    assert failed["retryable"] is True
    assert provider.calls == ["generated-1", "generated-2", "generated-3"]

    saved_slots = {
        item.slot_key: item
        for item in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_id)).all()
    }
    assert set(saved_slots.keys()) == {"generated-1", "generated-2"}

    provider.fail_slot_three = False
    provider.calls.clear()
    captured_contexts.clear()

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    final = wait_job(client, token, str(job_id), worker, attempts=4)
    assert final["state"] == "succeeded", final
    assert provider.calls == ["generated-3"]
    assert len(captured_contexts) == 1
    summaries = captured_contexts[0]["context"].get("previous_summaries") or []
    assert [s["slot"] for s in summaries] == ["generated-1", "generated-2"]
    assert all(s["focus"] and s["topic_key"] and s["anchor_refs"] for s in summaries)


def _independent_legacy_context_hash(context: dict) -> str:
    """独立构造旧版 Worker 的规范上下文摘要（不含 previous_summaries）。"""
    import hashlib
    import json

    legacy_payload = {k: v for k, v in context.items() if k != "previous_summaries"}
    serialized = json.dumps(
        legacy_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def test_plan_retry_reuses_legacy_work_items_without_previous_summaries(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """升级前写入（无 previous_summaries 键）的合格槽位在合法重试时可复用，且仅请求第 3 槽。"""
    import copy

    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan
    from app.services.preparations import generation_context_hash
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    captured_contexts: list[dict] = []

    class CrossVersionSlotThreeTimeoutProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_three = True
            self.calls: list[str] = []
            self.review_calls = 0

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            captured_contexts.append({"slot": slot, "context": copy.deepcopy(context or {})})
            if slot == "generated-3" and self.fail_slot_three:
                raise AppError("AI_TIMEOUT", "模拟第 3 槽超时")
            return super().generate_questions(context=context, **kwargs)

        def review_questions(self, **kwargs):
            self.review_calls += 1
            return super().review_questions(**kwargs)

    provider = CrossVersionSlotThreeTimeoutProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=4)
    assert failed["state"] == "failed"
    assert failed["attempt"] == 1
    assert failed["retryable"] is True
    assert provider.calls == ["generated-1", "generated-2", "generated-3"]
    assert provider.review_calls == 0

    ctx1 = captured_contexts[0]["context"]
    ctx2 = captured_contexts[1]["context"]
    assert ctx1["previous_summaries"] == []
    assert [s["slot"] for s in ctx2["previous_summaries"]] == ["generated-1"]

    legacy_hash_1 = _independent_legacy_context_hash(ctx1)
    legacy_hash_2 = _independent_legacy_context_hash(ctx2)
    current_hash_1 = generation_context_hash(ctx1)
    current_hash_2 = generation_context_hash(ctx2)
    assert legacy_hash_1 != current_hash_1
    assert legacy_hash_2 != current_hash_2

    saved_slots = {
        item.slot_key: item
        for item in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_id)).all()
    }
    assert set(saved_slots.keys()) == {"generated-1", "generated-2"}
    assert saved_slots["generated-1"].payload["context_hash"] == current_hash_1
    assert saved_slots["generated-2"].payload["context_hash"] == current_hash_2

    slot1_question = copy.deepcopy(saved_slots["generated-1"].payload["question"])
    slot2_question = copy.deepcopy(saved_slots["generated-2"].payload["question"])

    # 将前两槽改写为旧版 Worker 保存的 context_hash（去掉 previous_summaries）
    saved_slots["generated-1"].payload = {
        **saved_slots["generated-1"].payload,
        "context_hash": legacy_hash_1,
    }
    saved_slots["generated-2"].payload = {
        **saved_slots["generated-2"].payload,
        "context_hash": legacy_hash_2,
    }
    db.commit()

    refreshed = {
        item.slot_key: item
        for item in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_id)).all()
    }
    assert refreshed["generated-1"].payload["context_hash"] == legacy_hash_1
    assert refreshed["generated-1"].payload["context_hash"] != current_hash_1
    assert refreshed["generated-2"].payload["context_hash"] == legacy_hash_2
    assert refreshed["generated-2"].payload["context_hash"] != current_hash_2

    provider.fail_slot_three = False
    provider.calls.clear()
    captured_contexts.clear()

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    assert retried.json()["id"] == str(job_id)

    final = wait_job(client, token, str(job_id), worker, attempts=4)
    assert final["state"] == "succeeded", final
    assert provider.calls == ["generated-3"]
    assert provider.review_calls == 1
    assert len(captured_contexts) == 1

    slot3_ctx = captured_contexts[0]["context"]
    summaries = slot3_ctx.get("previous_summaries") or []
    assert [s["slot"] for s in summaries] == ["generated-1", "generated-2"]
    assert all(s["focus"] and s["topic_key"] and s["anchor_refs"] for s in summaries)
    assert slot1_question["stem"] in slot3_ctx["avoid_stems"]
    assert slot2_question["stem"] in slot3_ctx["avoid_stems"]

    plans = db.scalars(
        select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))
    ).all()
    assert len(plans) == 1
    plan_questions = plans[0].internal_payload["questions"]
    assert len(plan_questions) == 6
    bank_questions = [q for q in plan_questions if q["source"] == "bank"]
    generated_questions = [q for q in plan_questions if q["source"] == "generated"]
    assert len(bank_questions) == 3
    assert len(generated_questions) == 3
    assert generated_questions[0]["stem"] == slot1_question["stem"]
    assert generated_questions[0]["focus"] == slot1_question["focus"]
    assert generated_questions[0]["topic_key"] == slot1_question["topic_key"]
    assert generated_questions[1]["stem"] == slot2_question["stem"]
    assert generated_questions[1]["focus"] == slot2_question["focus"]
    assert generated_questions[1]["topic_key"] == slot2_question["topic_key"]


def test_plan_retry_skips_legacy_cache_on_input_mismatch_or_validation_failure(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """历史键业务输入不同、校验失败或尝试耗尽时，不误用缓存或破坏重试限制。"""
    import copy

    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan
    from app.services.bank import eligible_bank_questions
    from app.services.preparations import generation_context_hash
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    captured_contexts: list[dict] = []

    class RecordingTimeoutProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_three = True
            self.calls: list[str] = []

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            captured_contexts.append({"slot": slot, "context": copy.deepcopy(context or {})})
            if slot == "generated-3" and self.fail_slot_three:
                raise AppError("AI_TIMEOUT", "模拟第 3 槽超时")
            return super().generate_questions(context=context, **kwargs)

    provider = RecordingTimeoutProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=4)
    assert failed["state"] == "failed"
    assert failed["retryable"] is True

    ctx1 = captured_contexts[0]["context"]
    ctx2 = captured_contexts[1]["context"]
    legacy_hash_1 = _independent_legacy_context_hash(ctx1)
    # 槽 2 构造一个“业务输入变化”的历史键（例如 revision 不符）
    mismatched_ctx2 = copy.deepcopy(ctx2)
    mismatched_ctx2["preparation_revision"] = 999
    mismatched_legacy_hash_2 = _independent_legacy_context_hash(mismatched_ctx2)

    saved_slots = {
        item.slot_key: item
        for item in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_id)).all()
    }
    bank_stem = eligible_bank_questions(db, worker.settings, "java-backend", "intern")[0].content[
        "stem"
    ]
    # 槽 1：旧键完全匹配，但缓存题目题干与题库冲突，必须无法通过当前 _validate_generated
    bad_question_1 = copy.deepcopy(saved_slots["generated-1"].payload["question"])
    bad_question_1["stem"] = bank_stem
    saved_slots["generated-1"].payload = {
        **saved_slots["generated-1"].payload,
        "context_hash": legacy_hash_1,
        "question": bad_question_1,
    }
    # 槽 2：旧键业务输入不符，必须跳过缓存
    saved_slots["generated-2"].payload = {
        **saved_slots["generated-2"].payload,
        "context_hash": mismatched_legacy_hash_2,
    }
    db.commit()

    provider.fail_slot_three = False
    provider.calls.clear()
    captured_contexts.clear()

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    final = wait_job(client, token, str(job_id), worker, attempts=4)
    assert final["state"] == "succeeded", final
    assert provider.calls == ["generated-1", "generated-2", "generated-3"]

    db.expire_all()
    plans = db.scalars(
        select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))
    ).all()
    assert len(plans) == 1
    generated_questions = [
        q for q in plans[0].internal_payload["questions"] if q["source"] == "generated"
    ]
    assert len(generated_questions) == 3
    assert all(q["stem"] != bank_stem for q in generated_questions)

    refreshed_slot1 = db.scalars(
        select(JobWorkItem).where(
            JobWorkItem.job_id == job_id, JobWorkItem.slot_key == "generated-1"
        )
    ).one()
    assert refreshed_slot1.payload["context_hash"] != legacy_hash_1
    assert refreshed_slot1.payload["context_hash"] == generation_context_hash(ctx1)

    # 已耗尽两次尝试的旧缓存任务仍不可重试，且沿用材料新建准备不继承旧槽位
    exhausted_job = db.get(Job, job_id)
    assert exhausted_job is not None
    exhausted_job.state = "failed"
    exhausted_job.attempt = 2
    exhausted_job.error_code = "AI_TIMEOUT"
    exhausted_job.error = {"code": "AI_TIMEOUT", "message": "超时", "details": [], "request_id": "x"}
    refreshed_slot1.payload = {**refreshed_slot1.payload, "context_hash": legacy_hash_1}
    db.commit()

    assert client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 2},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )
    redo_prep = client.post(
        "/v1/preparations",
        json={
            "role_id": prep["role_id"],
            "level": prep["level"],
            "main_question_count": prep["main_question_count"],
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert redo_prep.status_code == 201
    assert redo_prep.json()["id"] != prep["id"]
    assert redo_prep.json()["current_plan_id"] is None


@pytest.mark.parametrize(
    ("hash_mode", "corruption_mode"),
    [
        ("current", "missing_evaluation"),
        ("legacy", "missing_evaluation"),
        ("current", "wrong_type_anchors"),
    ],
)
def test_plan_retry_regenerates_malformed_cached_slot_on_validation_error(
    client: TestClient,
    worker,
    db,
    monkeypatch,
    hash_mode: str,
    corruption_mode: str,
) -> None:
    """缓存键匹配但缓存题目缺字段或类型错误触发 ValidationError 时，仅重建该槽位并正常发布 3+3 计划。"""
    import copy

    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.core.errors import AppError
    from app.models import JobWorkItem, Plan
    from app.services.bank import content_digest
    from app.services.preparations import generation_context_hash
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    captured_contexts: list[dict] = []
    rebuilt_slot2_stem = (
        f"围绕已审依据卡与岗位边界，重建后的第 2 槽题目如何设计对照验证？（{hash_mode}-{corruption_mode}）"
    )

    class MalformedCacheRetryProvider(TestProvider):
        def __init__(self) -> None:
            super().__init__()
            self.fail_slot_three = True
            self.override_slot_two_stem = False
            self.calls: list[str] = []
            self.review_calls = 0

        def generate_questions(self, *, context=None, **kwargs):
            slot = (context or {}).get("slot_key", "?")
            self.calls.append(slot)
            captured_contexts.append({"slot": slot, "context": copy.deepcopy(context or {})})
            if slot == "generated-3" and self.fail_slot_three:
                raise AppError("AI_TIMEOUT", "模拟第 3 槽超时")
            items = super().generate_questions(context=context, **kwargs)
            if self.override_slot_two_stem and slot == "generated-2":
                items = copy.deepcopy(items)
                items[0]["stem"] = rebuilt_slot2_stem
            return items

        def review_questions(self, **kwargs):
            self.review_calls += 1
            return super().review_questions(**kwargs)

    provider = MalformedCacheRetryProvider()
    monkeypatch.setattr(runner, "get_provider", lambda settings: provider)

    generate = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(generate.json()["id"])
    failed = wait_job(client, token, str(job_id), worker, attempts=4)
    assert failed["state"] == "failed"
    assert failed["attempt"] == 1
    assert failed["retryable"] is True
    assert provider.calls == ["generated-1", "generated-2", "generated-3"]
    assert provider.review_calls == 0

    ctx2 = captured_contexts[1]["context"]
    current_hash_2 = generation_context_hash(ctx2)
    legacy_hash_2 = _independent_legacy_context_hash(ctx2)
    assert legacy_hash_2 != current_hash_2

    saved_slots = {
        item.slot_key: item
        for item in db.scalars(select(JobWorkItem).where(JobWorkItem.job_id == job_id)).all()
    }
    assert set(saved_slots.keys()) == {"generated-1", "generated-2"}
    assert saved_slots["generated-1"].validated is True
    assert saved_slots["generated-2"].validated is True

    slot1_question = copy.deepcopy(saved_slots["generated-1"].payload["question"])
    old_slot2_question = copy.deepcopy(saved_slots["generated-2"].payload["question"])
    old_slot2_digest = saved_slots["generated-2"].content_hash
    assert old_slot2_question["stem"] != rebuilt_slot2_stem

    bad_question_2 = copy.deepcopy(old_slot2_question)
    if corruption_mode == "missing_evaluation":
        bad_question_2.pop("evaluation", None)
        assert "evaluation" not in bad_question_2
    elif corruption_mode == "wrong_type_anchors":
        bad_question_2["anchors"] = "not-a-list"
    else:
        raise AssertionError(f"unsupported corruption_mode: {corruption_mode}")

    assert isinstance(bad_question_2, dict)
    target_hash_2 = current_hash_2 if hash_mode == "current" else legacy_hash_2
    saved_slots["generated-2"].payload = {
        **saved_slots["generated-2"].payload,
        "context_hash": target_hash_2,
        "question": bad_question_2,
    }
    db.commit()

    provider.fail_slot_three = False
    provider.override_slot_two_stem = True
    provider.calls.clear()
    captured_contexts.clear()

    retried = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retried.status_code == 202
    assert retried.json()["id"] == str(job_id)

    final = wait_job(client, token, str(job_id), worker, attempts=4)
    assert final["state"] == "succeeded", final
    assert final["id"] == str(job_id)
    assert final["attempt"] == 2
    assert final["retryable"] is False
    assert provider.calls == ["generated-2", "generated-3"]
    assert provider.review_calls == 1
    assert len(captured_contexts) == 2
    assert captured_contexts[0]["slot"] == "generated-2"
    assert captured_contexts[1]["slot"] == "generated-3"

    slot3_ctx = captured_contexts[1]["context"]
    summaries = slot3_ctx.get("previous_summaries") or []
    assert [s["slot"] for s in summaries] == ["generated-1", "generated-2"]
    assert all(s["focus"] and s["topic_key"] and s["anchor_refs"] for s in summaries)
    assert slot1_question["stem"] in slot3_ctx["avoid_stems"]
    assert rebuilt_slot2_stem in slot3_ctx["avoid_stems"]
    assert old_slot2_question["stem"] not in slot3_ctx["avoid_stems"]

    db.expire_all()
    refreshed_slot2 = db.scalars(
        select(JobWorkItem).where(
            JobWorkItem.job_id == job_id, JobWorkItem.slot_key == "generated-2"
        )
    ).one()
    assert refreshed_slot2.validated is True
    assert refreshed_slot2.payload["context_hash"] == current_hash_2
    assert refreshed_slot2.payload["question"]["stem"] == rebuilt_slot2_stem
    assert isinstance(refreshed_slot2.payload["question"].get("evaluation"), dict)
    assert refreshed_slot2.content_hash == content_digest(refreshed_slot2.payload["question"])
    assert refreshed_slot2.content_hash != old_slot2_digest

    plans = db.scalars(
        select(Plan).where(Plan.preparation_id == uuid.UUID(prep["id"]))
    ).all()
    assert len(plans) == 1
    plan_questions = plans[0].internal_payload["questions"]
    assert len(plan_questions) == 6
    bank_questions = [q for q in plan_questions if q["source"] == "bank"]
    generated_questions = [q for q in plan_questions if q["source"] == "generated"]
    assert len(bank_questions) == 3
    assert len(generated_questions) == 3
    assert generated_questions[0]["stem"] == slot1_question["stem"]
    assert generated_questions[1]["stem"] == rebuilt_slot2_stem
    assert generated_questions[1]["stem"] != old_slot2_question["stem"]
    assert isinstance(generated_questions[1].get("evaluation"), dict)

    assert (
        client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 2},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )


def test_exhausted_plan_job_and_redo_preparation_preserves_old_and_isolates_work(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """尝试用尽的失败任务只读可查不可重试；沿用材料新建准备只复制配置、不触发 AI、不跨准备移植槽位。"""
    import app.workers.runner as runner
    from app.ai.provider import TestProvider
    from app.models import JobWorkItem, Plan, Preparation
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    _, token = issue_token(client)
    resume_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep1 = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match = client.post(
        f"/v1/preparations/{prep1['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert wait_job(client, token, match.json()["id"], worker)["state"] == "succeeded"

    ai_calls = 0

    class CountingProvider(TestProvider):
        def match_requirements(self, **kwargs):
            nonlocal ai_calls
            ai_calls += 1
            return super().match_requirements(**kwargs)

        def generate_questions(self, **kwargs):
            nonlocal ai_calls
            ai_calls += 1
            return super().generate_questions(**kwargs)

    monkeypatch.setattr(runner, "get_provider", lambda settings: CountingProvider())

    gen = client.post(
        f"/v1/preparations/{prep1['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    job_id = uuid.UUID(gen.json()["id"])
    job = db.get(Job, job_id)
    assert job is not None
    job.state = "failed"
    job.attempt = 2
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": [
            "stage=plan",
            "slot=generated-3",
            "reason=duplicate_stem",
            "duplicate_kind=exact",
            "conflict_source=prior_generated",
            "conflict_slot=generated-1",
        ],
        "request_id": "exhausted-req",
    }
    job.result = None
    db.add(
        JobWorkItem(
            job_id=job_id,
            slot_key="generated-1",
            payload={"question": {"stem": "old-slot-1"}, "validation_version": "m1-work-item-v2"},
            content_hash="h1",
            validated=True,
        )
    )
    db.commit()

    # 只读查询：retryable=False，不可重试，且无 AI 调用
    queried = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert queried.status_code == 200
    assert queried.json()["attempt"] == 2
    assert queried.json()["retryable"] is False
    retry_resp = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 2},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry_resp.status_code == 409
    assert retry_resp.json()["error"]["code"] == "RETRY_NOT_ALLOWED"
    assert ai_calls == 0

    # 沿用原快照与配置新建准备：无 AI 调用，不继承旧匹配报告或旧槽位
    prep2_resp = client.post(
        "/v1/preparations",
        json={
            "role_id": prep1["role_id"],
            "level": prep1["level"],
            "main_question_count": prep1["main_question_count"],
            "resume_snapshot_id": prep1["resume_snapshot_id"],
            "jd_snapshot_id": prep1["jd_snapshot_id"],
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert prep2_resp.status_code == 201, prep2_resp.text
    prep2 = prep2_resp.json()
    assert prep2["id"] != prep1["id"]
    assert prep2["revision"] == 1
    assert prep2["role_id"] == "java-backend"
    assert prep2["level"] == "intern"
    assert prep2["main_question_count"] == 6
    assert prep2["resume_snapshot_id"] == resume_id
    assert prep2["jd_snapshot_id"] == jd_id
    assert prep2["match_report_id"] is None
    assert prep2["current_plan_id"] is None
    assert ai_calls == 0

    # 旧准备与旧失败任务保留
    old_prep_row = db.get(Preparation, uuid.UUID(prep1["id"]))
    assert old_prep_row is not None and old_prep_row.current_plan_id is None
    assert db.get(Job, job_id).state == "failed"
    assert db.scalars(select(Plan).where(Plan.preparation_id == uuid.UUID(prep1["id"]))).all() == []
