from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import (
    auth,
    create_confirmed_resume,
    create_confirmed_template_jd,
    new_key,
    wait_job,
)


def make_preparation(client: TestClient, token: str, *, count: int) -> dict:
    resume = create_confirmed_resume(client, token)
    jd = create_confirmed_template_jd(client, token)
    response = client.post(
        "/v1/preparations",
        json={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": count,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    return response.json()


def run_match(client: TestClient, token: str, worker, prep_id: str) -> dict:
    response = client.post(
        f"/v1/preparations/{prep_id}/match",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, token, response.json()["id"], worker)
    assert job["state"] == "succeeded", job
    return job


def create_confirmed_plan(
    client: TestClient, token: str, worker, *, count: int = 6
) -> tuple[dict, dict]:
    prep = make_preparation(client, token, count=count)
    run_match(client, token, worker, prep["id"])
    response = client.post(
        f"/v1/preparations/{prep['id']}/generate-plan",
        json={"revision": 1, "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 202, response.text
    job = wait_job(client, token, response.json()["id"], worker)
    assert job["state"] == "succeeded", job
    plan_id = job["result_ref"]["id"]
    plan = client.get(f"/v1/plans/{plan_id}", headers=auth(token))
    assert plan.status_code == 200, plan.text
    plan_payload = plan.json()
    confirm = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"revision": plan_payload["revision"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 200, confirm.text
    return prep, confirm.json()


def start_interview(client: TestClient, token: str, plan: dict) -> dict:
    response = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code in (200, 201), response.text
    return response.json()


def detail(client: TestClient, token: str, session_id: str) -> dict:
    response = client.get(f"/v1/interviews/{session_id}", headers=auth(token))
    assert response.status_code == 200, response.text
    return response.json()


def answer(
    client: TestClient,
    token: str,
    session_id: str,
    *,
    revision: int,
    turn_id: str,
    text: str,
    key: str | None = None,
):
    return client.post(
        f"/v1/interviews/{session_id}/answers",
        json={"revision": revision, "turn_id": turn_id, "text": text},
        headers={**auth(token), "Idempotency-Key": key or new_key()},
    )


def skip(client: TestClient, token: str, session_id: str, *, revision: int, turn_id: str):
    return client.post(
        f"/v1/interviews/{session_id}/skip",
        json={"revision": revision, "turn_id": turn_id},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def pause(client: TestClient, token: str, session_id: str, revision: int):
    return client.post(
        f"/v1/interviews/{session_id}/pause",
        json={"revision": revision},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def resume(client: TestClient, token: str, session_id: str, revision: int):
    return client.post(
        f"/v1/interviews/{session_id}/resume",
        json={"revision": revision},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


def finish(client: TestClient, token: str, session_id: str, revision: int):
    return client.post(
        f"/v1/interviews/{session_id}/finish",
        json={"revision": revision},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )


LONG_ANSWER = (
    "我先检查是否已有相同键，再决定更新还是插入。因为课程项目里的借阅记录要求同一本书只保留一条当前借阅，"
    "所以先用键查询状态；如果不存在就插入，存在且未归还就拒绝重复借阅。验证时我会构造空集合、缺失键、"
    "已归还和未归还四组输入，并检查返回值和列表长度是否符合预期。"
)


def complete_six_question_interview(client: TestClient, token: str, worker, session_id: str) -> dict:
    """按 test provider 的确定性追问样例走完六题；返回最终详情。"""
    for _ in range(12):
        current = detail(client, token, session_id)
        if current["status"] == "ended":
            return current
        turn = current["current_turn"]
        assert turn is not None, current
        response = answer(
            client,
            token,
            session_id,
            revision=current["revision"],
            turn_id=turn["turn_id"],
            text=LONG_ANSWER,
        )
        assert response.status_code in (200, 202), response.text
        if response.status_code == 202:
            job = wait_job(client, token, response.json()["job_id"], worker, attempts=6)
            assert job["state"] == "succeeded", job
    raise AssertionError("面试未在预期回合内结束")
