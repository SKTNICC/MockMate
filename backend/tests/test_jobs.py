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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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
    from concurrent.futures import ThreadPoolExecutor
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


def test_legacy_failed_parse_job_retry_and_boundaries(
    client: TestClient, worker, db, monkeypatch
) -> None:
    """旧版截图类 parse 失败任务可同 ID 重试；过期、手工确认、并发双击、耗尽与 JSON null 边界均正确。"""
    import app.workers.runner as runner
    from concurrent.futures import ThreadPoolExecutor
    from app.ai.provider import TestProvider
    from sqlalchemy import text as sql_text

    _, token = issue_token(client)
    resume_text = "工作经历：示例实验室｜开发实习生｜负责库存接口开发\n项目：库存工具，使用 Java。"
    mat = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "旧失败简历", "text": resume_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    parse_resp = client.post(
        f"/v1/materials/{mat['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert parse_resp.status_code == 202
    job_id = uuid.UUID(parse_resp.json()["id"])

    # 模拟已落库的截图类失败（无 stage=parse，含 JSON null）
    job = db.get(Job, job_id)
    assert job is not None
    job.state = "failed"
    job.attempt = 1
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": [
            "work_experience.0.organization: Field required",
            "work_experience.0.title: Field required",
            "work_experience.0.name: Extra inputs are not permitted",
            "work_experience.0.skill_codes: Extra inputs are not permitted",
        ],
        "request_id": "screenshot-legacy",
    }
    job.result = None
    # 同时验证数据库中存 JSON 'null'::jsonb 时不会误阻断重试
    db.execute(
        sql_text("UPDATE material_drafts SET profile = 'null'::jsonb WHERE id = :id"),
        {"id": uuid.UUID(mat["id"])},
    )
    db.commit()

    # 只读 GET 返回 retryable=True 且不调用 AI
    parse_calls = 0

    class CountingParseProvider(TestProvider):
        def parse_material(self, **kwargs):
            nonlocal parse_calls
            parse_calls += 1
            return super().parse_material(**kwargs)

    monkeypatch.setattr(runner, "get_provider", lambda _: CountingParseProvider())
    queried = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert queried.status_code == 200
    assert queried.json()["retryable"] is True
    assert parse_calls == 0

    # 并发双击重试同 Job 同 attempt：一个 202，一个 409 RETRY_NOT_ALLOWED
    def _do_retry():
        return client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = [f.result() for f in [pool.submit(_do_retry), pool.submit(_do_retry)]]
    assert sorted([r1.status_code, r2.status_code]) == [202, 409]

    # 旧 expected_attempt 再次重试被拒绝
    stale_attempt = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert stale_attempt.status_code == 409

    # worker 执行第 2 次尝试并成功写入
    done = wait_job(client, token, str(job_id), worker, attempts=4)
    assert done["state"] == "succeeded", done
    assert parse_calls == 1
    assert done["attempt"] == 2
    assert done["retryable"] is False

    # 确认后或达到上限或编辑产生新版本后均不可再重试
    mat_after = client.get(f"/v1/materials/{mat['id']}", headers=auth(token)).json()
    assert mat_after["state"] == "parsed"
    assert mat_after["profile"]["work_experience"][0]["organization"] == "示例实验室"

    # 再造一个第 1 次失败的任务，验证手工确认后立即变为 retryable=False
    mat_manual = client.post(
        "/v1/materials/text",
        json={"kind": "resume", "title": "手工确认后不可重试", "text": resume_text},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    p_manual = client.post(
        f"/v1/materials/{mat_manual['id']}/parse",
        json={
            "revision": 1,
            "external_processing_consent": True,
            "role_id": "java-backend",
            "level": "intern",
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    j_manual = db.get(Job, uuid.UUID(p_manual["id"]))
    assert j_manual is not None
    j_manual.state = "failed"
    j_manual.attempt = 1
    j_manual.error_code = "AI_INVALID_OUTPUT"
    j_manual.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": ["stage=parse", "reason=invalid_profile_schema"],
        "request_id": "manual-req",
    }
    db.commit()
    assert client.get(f"/v1/jobs/{j_manual.id}", headers=auth(token)).json()["retryable"] is True

    confirm_resp = client.post(
        f"/v1/materials/{mat_manual['id']}/confirm",
        json={
            "revision": 1,
            "profile": {
                "kind": "resume",
                "education": [],
                "work_experience": [],
                "projects": [
                    {
                        "item_id": "project-1",
                        "name": "库存工具",
                        "description": "使用 Java",
                        "skill_codes": [],
                        "evidence": {"quote": "项目：库存工具，使用 Java。", "origin": "user_confirmed"},
                    }
                ],
                "skills": [],
            },
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm_resp.status_code == 201
    assert client.get(f"/v1/jobs/{j_manual.id}", headers=auth(token)).json()["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{j_manual.id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )


def test_parse_empty_and_duplicate_id_retry_exhaustion_legacy_and_blank_text(
    client: TestClient, worker, db, monkeypatch
) -> None:
    import json as _json
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

    # 1) 两次失败 -> 同 Job 可只读查询(0 AI 调用)并显式重试成功；以及耗尽后不可再重试
    for bad_payload, expected_reason in (
        (empty_obj, "empty_profile"),
        (empty_arrays, "empty_profile"),
        (dup_payload, "duplicate_item_id"),
    ):
        call_count = 0
        fail_now = True

        def handler(_: httpx.Request, b=bad_payload) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            payload = b if fail_now else valid_payload
            return httpx.Response(
                200,
                json={
                    "id": f"req-{call_count}",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {"content": _json.dumps(payload, ensure_ascii=False)},
                        }
                    ],
                },
            )

        monkeypatch.setattr(
            runner,
            "get_provider",
            lambda _, h=handler: DeepSeekProvider(settings, transport=httpx.MockTransport(h)),
        )
        mat = _create_resume(client, token, text=resume_text)
        job_r = _parse(client, token, mat["id"])
        assert job_r.status_code == 202
        job_id = job_r.json()["id"]

        failed = wait_job(client, token, job_id, worker)
        assert call_count == 2
        assert failed["state"] == "failed"
        assert failed["error"]["code"] == "PROFILE_INVALID"
        assert failed["retryable"] is True
        assert f"reason={expected_reason}" in failed["error"]["details"]
        assert "source=provider_output" in failed["error"]["details"]

        # 状态查询不调用模型
        before_calls = call_count
        queried = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
        assert queried.status_code == 200
        assert queried.json()["retryable"] is True
        assert call_count == before_calls

        # 同 Job 显式重试成功
        fail_now = False
        retried = client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert retried.status_code == 202
        assert retried.json()["id"] == job_id
        assert retried.json()["attempt"] == 2
        succeeded = wait_job(client, token, job_id, worker)
        assert succeeded["state"] == "succeeded"

    # 耗尽：第 2 次显式重试仍返回空结果 -> attempt=2, retryable=False
    exhaust_calls = 0

    def always_empty(_: httpx.Request) -> httpx.Response:
        nonlocal exhaust_calls
        exhaust_calls += 1
        return httpx.Response(
            200,
            json={
                "id": f"req-ex-{exhaust_calls}",
                "choices": [{"finish_reason": "stop", "message": {"content": _json.dumps(empty_obj)}}],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(always_empty)),
    )
    mat_ex = _create_resume(client, token, text=resume_text)
    ex_job_id = _parse(client, token, mat_ex["id"]).json()["id"]
    fail1 = wait_job(client, token, ex_job_id, worker)
    assert fail1["retryable"] is True
    assert client.post(
        f"/v1/jobs/{ex_job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).status_code == 202
    fail2 = wait_job(client, token, ex_job_id, worker)
    assert fail2["attempt"] == 2
    assert fail2["state"] == "failed"
    assert fail2["retryable"] is False
    assert (
        client.post(
            f"/v1/jobs/{ex_job_id}/retry",
            json={"expected_attempt": 2},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 409
    )

    # 2) 空原文：走 FILE_TEXT_UNAVAILABLE，不调用模型且不可重试
    blank_calls = 0

    def counting_ok(_: httpx.Request) -> httpx.Response:
        nonlocal blank_calls
        blank_calls += 1
        return httpx.Response(
            200,
            json={
                "id": "req-ok",
                "choices": [{"finish_reason": "stop", "message": {"content": _json.dumps(valid_payload)}}],
            },
        )

    monkeypatch.setattr(
        runner,
        "get_provider",
        lambda _: DeepSeekProvider(settings, transport=httpx.MockTransport(counting_ok)),
    )
    mat_blank = _create_resume(client, token, text=resume_text)
    blank_job_id = _parse(client, token, mat_blank["id"]).json()["id"]
    draft_blank = db.get(MaterialDraft, uuid.UUID(mat_blank["id"]))
    assert draft_blank is not None
    draft_blank.text = "   \n  "
    db.commit()
    blank_done = wait_job(client, token, blank_job_id, worker)
    assert blank_done["state"] == "failed"
    assert blank_done["error"]["code"] == "FILE_TEXT_UNAVAILABLE"
    assert blank_done["retryable"] is False
    assert blank_calls == 0

    # 3) 历史任务兼容：未带 reason=empty_profile / duplicate_item_id 的旧失败任务仍可只读查出 retryable=True 并显式重试成功
    for legacy_msg, legacy_details in (
        ("简历至少需要一条教育、工作、项目或技能信息", ["education", "work_experience", "projects", "skills"]),
        ("item_id 不能重复", ["section=skills", "field=item_id", "duplicate_count=1"]),
    ):
        legacy_mat = _create_resume(client, token, text=resume_text)
        legacy_job_id = uuid.UUID(_parse(client, token, legacy_mat["id"]).json()["id"])
        legacy_job = db.get(Job, legacy_job_id)
        assert legacy_job is not None
        legacy_job.state = "failed"
        legacy_job.attempt = 1
        legacy_job.error_code = "PROFILE_INVALID"
        legacy_job.error = {
            "code": "PROFILE_INVALID",
            "message": legacy_msg,
            "details": legacy_details,
            "request_id": "legacy-empty-dup",
        }
        db.commit()

        before_q = blank_calls
        q = client.get(f"/v1/jobs/{legacy_job_id}", headers=auth(token))
        assert q.status_code == 200
        assert q.json()["retryable"] is True
        assert blank_calls == before_q

        r = client.post(
            f"/v1/jobs/{legacy_job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert r.status_code == 202
        ok_after = wait_job(client, token, str(legacy_job_id), worker)
        assert ok_after["state"] == "succeeded"


def test_legacy_job_message_reason_actual_and_ref_ids_redacted_across_api_and_records(
    client: TestClient, worker, db
) -> None:
    import json as _json
    from app.core.diagnostics import (
        UNRECOGNIZED_DETAIL_TOKEN,
        sanitize_error_details_for_records,
    )
    from app.workers.runner import JobWorker

    _, token = issue_token(client)
    mat = _create_resume(client, token, text="本科在读，计算机科学与技术")
    job_id = uuid.UUID(_parse(client, token, mat["id"]).json()["id"])
    job = db.get(Job, job_id)
    assert job is not None

    # 1) 合成历史消息、自由 reason/actual/expected/source/status 与伪造 item_id
    job.state = "failed"
    job.attempt = 1
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "历史内部错误：SYNTHETIC_HISTORY_MESSAGE_777",
        "details": [
            "stage=parse",
            "reason=SYNTHETIC_REASON_777",
            "actual=SYNTHETIC_ACTUAL_777",
            "expected=SYNTHETIC_EXPECTED_777",
            "source=SYNTHETIC_SOURCE_777",
            "status=SYNTHETIC_STATUS_777",
            "item_id=SYNTHETIC_ITEM_777",
            "work_experience.0.organization: Field required",
            "work_experience.0.title: Field required",
        ],
        "request_id": "legacy-synth-777",
    }
    db.commit()

    resp = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert resp.status_code == 200
    body = resp.json()
    body_text = _json.dumps(body, ensure_ascii=False)
    for marker in (
        "SYNTHETIC_HISTORY_MESSAGE_777",
        "SYNTHETIC_REASON_777",
        "SYNTHETIC_ACTUAL_777",
        "SYNTHETIC_EXPECTED_777",
        "SYNTHETIC_SOURCE_777",
        "SYNTHETIC_STATUS_777",
        "SYNTHETIC_ITEM_777",
    ):
        assert marker not in body_text
    assert body["error"]["message"] == "AI 输出不符合契约"
    assert "work_experience.0.organization: Field required" in body["error"]["details"]
    assert "work_experience.0.title: Field required" in body["error"]["details"]
    assert UNRECOGNIZED_DETAIL_TOKEN in body["error"]["details"]

    # 2) Worker 记录与 _safe_error_reason 过滤自由 ASCII reason=/actual=/item_id=
    assert JobWorker._safe_error_reason(["reason=SYNTHETIC_REASON_777"]) is None
    assert JobWorker._safe_error_reason(["reason=empty_profile"]) == "empty_profile"
    rec_details = sanitize_error_details_for_records(
        [
            "stage=parse",
            "reason=SYNTHETIC_REASON_777",
            "actual=SYNTHETIC_ACTUAL_777",
            "expected=SYNTHETIC_EXPECTED_777",
            "source=SYNTHETIC_SOURCE_777",
            "status=SYNTHETIC_STATUS_777",
            "item_id=SYNTHETIC_ITEM_777",
            "work_experience.0.organization: Field required",
        ],
        kind="parse",
    )
    rec_text = _json.dumps(rec_details, ensure_ascii=False)
    assert "SYNTHETIC_" not in rec_text
    assert rec_details == ["stage=parse", "work_experience.0.organization: Field required"]

    # 3) 兼容正例：缺失组织/职务旧记录仍可重试；出题槽位旧记录仍保留槽位与重试能力
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": [
            "work_experience.0.organization: Field required",
            "work_experience.0.title: Field required",
            "actual=SYNTHETIC_ACTUAL_COMPAT",
        ],
        "request_id": "legacy-compat-missing",
    }
    db.commit()
    compat_parse = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert compat_parse["retryable"] is True
    assert compat_parse["error"]["message"] == "AI 输出不符合契约结构"
    assert "work_experience.0.organization: Field required" in compat_parse["error"]["details"]
    assert "work_experience.0.title: Field required" in compat_parse["error"]["details"]
    assert "SYNTHETIC_ACTUAL_COMPAT" not in _json.dumps(compat_parse, ensure_ascii=False)

    # 4) 匹配/计划任务仅保留来自当前服务端对象的引用 ID，伪造 ID 被过滤
    from tests.conftest import create_confirmed_resume, create_confirmed_template_jd

    res_id = create_confirmed_resume(client, token)
    jd_id = create_confirmed_template_jd(client, token)
    prep = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 4,
            "resume_snapshot_id": res_id,
            "jd_snapshot_id": jd_id,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    ).json()
    match_resp = client.post(
        f"/v1/preparations/{prep['id']}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert match_resp.status_code == 202
    assert wait_job(client, token, match_resp.json()["id"], worker)["state"] == "succeeded"
    plan_job_resp = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert plan_job_resp.status_code == 202, plan_job_resp.text
    plan_job_id = uuid.UUID(plan_job_resp.json()["id"])
    from app.models import BankQuestion

    real_bank_qid = db.scalars(select(BankQuestion.question_id)).first()
    assert real_bank_qid is not None

    plan_job = db.get(Job, plan_job_id)
    assert plan_job is not None
    # 4a) 传统计划槽位失败记录：仍可重试且槽位可读
    plan_job.state = "failed"
    plan_job.attempt = 1
    plan_job.error_code = "AI_INVALID_OUTPUT"
    plan_job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": ["generated-1", "AI 返回内容不是合法 JSON"],
        "request_id": "legacy-plan-slot",
    }
    db.commit()
    legacy_plan_q = client.get(f"/v1/jobs/{plan_job_id}", headers=auth(token)).json()
    assert legacy_plan_q["retryable"] is True
    assert "generated-1" in legacy_plan_q["error"]["details"]
    assert "AI 返回内容不是合法 JSON" in legacy_plan_q["error"]["details"]

    # 4b) 结构化计划失败：仅保留来自当前服务端对象的引用 ID，伪造 ID 被过滤
    plan_job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "生成槽位在有限纠正后仍不合法",
        "details": [
            "stage=plan",
            "slot=generated-1",
            "reason=invalid_json",
            "item_id=skill-1",
            "item_id=skill-999",
            "requirement_id=req-1",
            "requirement_id=req-999",
            f"conflict_question_id={real_bank_qid}",
            "conflict_question_id=FAKE-Q-999",
        ],
        "request_id": "legacy-plan-refs",
    }
    db.commit()

    plan_q = client.get(f"/v1/jobs/{plan_job_id}", headers=auth(token)).json()
    assert plan_q["retryable"] is True
    assert "slot=generated-1" in plan_q["error"]["details"]
    assert "item_id=skill-1" in plan_q["error"]["details"]
    assert "requirement_id=req-1" in plan_q["error"]["details"]
    assert f"conflict_question_id={real_bank_qid}" in plan_q["error"]["details"]
    assert "item_id=skill-999" not in plan_q["error"]["details"]
    assert "requirement_id=req-999" not in plan_q["error"]["details"]
    assert "FAKE-Q-999" not in _json.dumps(plan_q, ensure_ascii=False)


def test_legacy_corrupt_error_envelope_types_code_and_request_id_are_sanitized(
    client: TestClient, worker, db
) -> None:
    import json as _json

    _, token = issue_token(client)
    mat = _create_resume(client, token, text="本科在读，计算机科学与技术")
    job_id = uuid.UUID(_parse(client, token, mat["id"]).json()["id"])
    job = db.get(Job, job_id)
    assert job is not None

    # 1) 非对象 error 体（字符串 / 整数 / 列表）：状态 GET 与 retryable 均不可崩溃，且不回显原文
    for non_obj_error, db_code, expected_code, expected_retryable in (
        ("SYNTHETIC_RAW_ERROR_STRING_801", None, "INTERNAL_ERROR", False),
        (12345, "SYNTHETIC_DB_CODE_801", "INTERNAL_ERROR", False),
        (["SYNTHETIC_LIST_ERR_801"], "AI_TIMEOUT", "AI_TIMEOUT", True),
    ):
        job.state = "failed"
        job.attempt = 1
        job.error_code = db_code
        job.error = non_obj_error  # type: ignore[assignment]
        db.commit()

        resp = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        body_text = _json.dumps(body, ensure_ascii=False)
        assert "SYNTHETIC_" not in body_text
        assert body["retryable"] is expected_retryable
        assert body["error"]["code"] == expected_code
        assert body["error"]["details"] == []
        assert body["error"]["request_id"] == str(job_id)

    # 2) details 为整数 123、error.code 与 error.request_id 含普通文本标记：不抛 TypeError 且标记被过滤
    job.state = "failed"
    job.attempt = 1
    job.error_code = "SYNTHETIC_CODE_MARKER_802"
    job.error = {
        "code": "SYNTHETIC_CODE_MARKER_802",
        "message": "SYNTHETIC_MSG_MARKER_802",
        "details": 123,
        "request_id": "SYNTHETIC_REQ_MARKER_802",
    }
    db.commit()

    int_resp = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert int_resp.status_code == 200, int_resp.text
    int_body = int_resp.json()
    int_text = _json.dumps(int_body, ensure_ascii=False)
    for marker in (
        "SYNTHETIC_CODE_MARKER_802",
        "SYNTHETIC_MSG_MARKER_802",
        "SYNTHETIC_REQ_MARKER_802",
    ):
        assert marker not in int_text
    assert int_body["error"]["code"] == "INTERNAL_ERROR"
    assert int_body["error"]["message"] == "任务执行失败"
    assert int_body["error"]["details"] == []
    assert int_body["error"]["request_id"] == str(job_id)
    assert int_body["retryable"] is False

    # 3) 混合元素 details + 合法 UUID request_id + 简历缺字段诊断仍可读且同一 Job 可显式重试
    valid_req_uuid = str(uuid.uuid4())
    job.state = "failed"
    job.attempt = 1
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": [
            123,
            None,
            {"injected": "SYNTHETIC_DICT_803"},
            "work_experience.0.organization: Field required",
            "work_experience.0.title: Field required",
        ],
        "request_id": valid_req_uuid,
    }
    db.commit()

    mixed_resp = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
    assert mixed_resp.status_code == 200
    mixed_body = mixed_resp.json()
    assert "SYNTHETIC_DICT_803" not in _json.dumps(mixed_body, ensure_ascii=False)
    assert mixed_body["error"]["code"] == "AI_INVALID_OUTPUT"
    assert mixed_body["error"]["request_id"] == valid_req_uuid
    assert mixed_body["error"]["details"] == [
        "work_experience.0.organization: Field required",
        "work_experience.0.title: Field required",
    ]
    assert mixed_body["retryable"] is True

    # 4) 各类正常错误码继续正确展示
    for normal_code in ("AI_TIMEOUT", "AI_UNAVAILABLE", "PROFILE_INVALID", "EVIDENCE_INVALID"):
        job.error_code = normal_code
        job.error = {
            "code": normal_code,
            "message": "SYNTHETIC_RAW_IGNORED",
            "details": ["stage=parse", "reason=empty_profile"] if normal_code == "PROFILE_INVALID" else [],
            "request_id": valid_req_uuid,
        }
        db.commit()
        r_norm = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
        assert r_norm.status_code == 200
        norm_err = r_norm.json()["error"]
        assert norm_err["code"] == normal_code
        assert norm_err["request_id"] == valid_req_uuid
        assert "SYNTHETIC_RAW_IGNORED" not in norm_err["message"]

    # 恢复为可重试的缺字段记录并执行同一 Job 显式重试
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约结构",
        "details": ["work_experience.0.organization: Field required"],
        "request_id": "legacy-plain-id",
    }
    db.commit()
    retry_resp = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry_resp.status_code == 202
    assert retry_resp.json()["id"] == str(job_id)
    assert retry_resp.json()["attempt"] == 2
    succeeded = wait_job(client, token, str(job_id), worker)
    assert succeeded["state"] == "succeeded", succeeded


def test_legacy_trusted_error_code_drives_projection_and_retry_for_empty_or_corrupt_json(
    client: TestClient, db
) -> None:
    import json as _json

    _, token = issue_token(client)
    unknown_request_id = str(uuid.uuid4())
    variants = [
        (
            "unknown-code",
            {
                "code": "SYNTHETIC_UNKNOWN_CODE_821",
                "message": "SYNTHETIC_MESSAGE_821",
                "details": ["SYNTHETIC_DETAIL_821"],
                "request_id": unknown_request_id,
            },
        ),
        ("null-error", None),
        ("empty-object", {}),
    ]

    for label, raw_error in variants:
        mat = _create_resume(client, token, text=f"{REVIEWED_RESUME_TEXT}\n{label}")
        job_id = uuid.UUID(_parse(client, token, mat["id"]).json()["id"])
        job = db.get(Job, job_id)
        assert job is not None
        job.state = "failed"
        job.attempt = 1
        job.error_code = "AI_TIMEOUT"
        job.error = raw_error
        db.commit()

        fetched = client.get(f"/v1/jobs/{job_id}", headers=auth(token))
        assert fetched.status_code == 200, fetched.text
        body = fetched.json()
        assert body["state"] == "failed"
        assert body["retryable"] is True
        error = body["error"]
        assert error["code"] == "AI_TIMEOUT"
        assert error["message"] == "AI 服务超时，结果未知"
        assert error["details"] == []
        assert "SYNTHETIC" not in _json.dumps(error, ensure_ascii=False)
        if label == "unknown-code":
            # 未知 JSON 错误码和消息被忽略，但合法历史追踪 ID 仍保留
            assert error["request_id"] == unknown_request_id
        else:
            assert error["request_id"] == str(job_id)

        retried = client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        assert retried.status_code == 202, retried.text
        assert retried.json()["attempt"] == 2


def test_conflicting_legacy_error_details_cannot_grant_format_retry(
    client: TestClient, db
) -> None:
    import json as _json

    _, token = issue_token(client)
    conflicting_variants = [
        # 可信列为 AI_INVALID_OUTPUT，旧 JSON 却声称是格式错误；详情必须整体失效。
        (
            "plan-conflict",
            {
                "code": "PROFILE_INVALID",
                "message": "SYNTHETIC_PROFILE_MESSAGE_831",
                "details": [
                    "stage=plan",
                    "slot=generated-1",
                    "reason=invalid_json",
                    "item_id=SYNTHETIC_ITEM_831",
                ],
                "request_id": "conflict-request-831",
            },
            {
                "code": "AI_INVALID_OUTPUT",
                "message": "AI 输出不符合契约",
                "details": [],
            },
            False,
        ),
        # 反向冲突：可信列为证据错误，旧 JSON 的格式原因不得授予重试。
        (
            "parse-conflict",
            {
                "code": "AI_INVALID_OUTPUT",
                "message": "SYNTHETIC_PARSE_MESSAGE_832",
                "details": [
                    "stage=parse",
                    "reason=invalid_json",
                    "item_id=SYNTHETIC_ITEM_832",
                ],
                "request_id": "conflict-request-832",
            },
            {
                "code": "EVIDENCE_INVALID",
                "message": "题目或匹配引用无依据",
                "details": [],
            },
            False,
        ),
        # 错误列缺省但旧 JSON 有可识别原因：真实历史任务仍可显式重试并保留详情。
        (
            "legacy-parse-no-column",
            {
                "code": "AI_INVALID_OUTPUT",
                "message": "AI 输出不符合契约",
                "details": ["stage=parse", "reason=invalid_json"],
                "request_id": "legacy-request-833",
            },
            {
                "code": "AI_INVALID_OUTPUT",
                "message": "AI 输出不符合契约",
                "details": ["stage=parse", "reason=invalid_json"],
            },
            True,
        ),
    ]

    for label, raw_error, expected, expected_retryable in conflicting_variants:
        material = _create_resume(client, token, text=f"{REVIEWED_RESUME_TEXT}\n{label}")
        job_id = uuid.UUID(_parse(client, token, material["id"]).json()["id"])
        job = db.get(Job, job_id)
        assert job is not None
        job.state = "failed"
        job.attempt = 1
        job.error_code = (
            None if label == "legacy-parse-no-column" else expected["code"]
        )
        job.error = raw_error
        db.commit()

        body = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
        assert body["retryable"] is expected_retryable, (label, body)
        assert body["error"]["code"] == expected["code"], (label, body)
        assert body["error"]["details"] == expected["details"], (label, body)
        assert "SYNTHETIC_" not in _json.dumps(body, ensure_ascii=False)

        retried = client.post(
            f"/v1/jobs/{job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        )
        if expected_retryable:
            assert retried.status_code == 202, (label, retried.text)
            assert retried.json()["attempt"] == 2
        else:
            assert retried.status_code == 409, (label, retried.text)
            assert retried.json()["error"]["code"] == "RETRY_NOT_ALLOWED"

        # 两次尝试上限：即使错误码本身可判断，达到上限也不能再重试。
        job.attempt = 2
        db.commit()
        exhausted = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
        assert exhausted["retryable"] is False, (label, exhausted)
