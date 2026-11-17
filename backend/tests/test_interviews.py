from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.models import InterviewAnswer, InterviewQuestion, InterviewSession, InterviewTurn, Job
from tests.conftest import auth, issue_token, new_key, wait_job
from tests.m2_helpers import (
    LONG_ANSWER,
    answer,
    complete_six_question_interview,
    create_confirmed_plan,
    detail,
    finish,
    pause,
    resume,
    skip,
    start_interview,
)


def test_start_opens_same_session_and_hides_future_questions(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=6)

    first = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert first.status_code == 201, first.text
    session_id = first.json()["session_id"]
    assert first.json()["revision"] == 1

    again = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert again.status_code == 200, again.text
    assert again.json()["session_id"] == session_id
    assert again.json()["revision"] == 1

    body = detail(client, token, session_id)
    assert body["status"] == "active"
    assert body["phase"] == "awaiting_answer"
    assert body["plan_summary"]["source_counts"] == {"bank": 3, "generated": 3}
    assert body["plan_summary"]["sample_only"] is True
    assert len(body["plan_summary"]["material_versions"]) == 2
    assert body["opening_text"]
    assert body["closing_text"] is None
    assert body["current_main_position"] == 1
    assert body["current_turn"]["kind"] == "main"
    assert body["current_turn"]["position"] == 1
    assert body["current_turn"]["answer"] is None
    assert len(body["transcript"]) == 1
    assert body["allowed_actions"] == ["pause", "finish", "answer", "skip"]

    serialized = json.dumps(body, ensure_ascii=False)
    for forbidden in ("reference_approach", "key_points", "rubric", "evaluation", "plan_snapshot"):
        assert forbidden not in serialized
    # 只曝光第一题，后续题干不得出现。
    assert serialized.count("prompt") == 2  # current_turn + transcript
    assert body["transcript"][0]["prompt"] == body["current_turn"]["prompt"]

    other_user, other_token = issue_token(client)
    denied = client.get(f"/v1/interviews/{session_id}", headers=auth(other_token))
    assert denied.status_code == 404
    denied_start = client.post(
        f"/v1/plans/{plan['id']}/interviews",
        json={"plan_revision": plan["revision"], "external_processing_consent": True},
        headers={**auth(other_token), "Idempotency-Key": new_key()},
    )
    assert denied_start.status_code == 404


def test_full_six_question_interview_and_report(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=6)
    started = start_interview(client, token, plan)
    session_id = started["session_id"]

    before = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert before.status_code == 409
    assert before.json()["error"]["code"] == "REPORT_NOT_AVAILABLE"

    final = complete_six_question_interview(client, token, worker, session_id)
    assert final["status"] == "ended"
    assert final["end_reason"] == "completed"
    assert final["phase"] == "closed"
    assert final["current_turn"] is None
    assert final["current_main_position"] is None
    assert final["answered_main_count"] == 6
    assert final["closing_text"]
    assert final["report_job"] is not None

    report_job = wait_job(client, token, final["report_job"]["id"], worker, attempts=8)
    assert report_job["state"] == "succeeded", report_job
    assert report_job["kind"] == "interview_report"

    report_response = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    assert report["quality"] == "full"
    assert report["end_reason"] == "completed"
    assert report["coverage"]["planned"] == 6
    assert report["coverage"]["presented"] == 6
    assert report["coverage"]["answered"] == 6
    assert report["coverage"]["scored"] == 6
    assert report["coverage"]["bank_planned"] == 3
    assert report["coverage"]["generated_planned"] == 3
    assert report["coverage"]["follow_up_presented"] >= 2
    assert report["coverage"]["follow_up_answered"] >= 2
    assert len(report["material_versions"]) == 2
    assert len(report["questions"]) == 6
    assert report["strengths"] or report["improvements"]

    answers_by_id = {}
    for question in report["questions"]:
        assert question["outcome"] == "answered"
        assert question["prompt"]
        assert question["turns"]
        dimension_ids = {dim["dimension_id"] for dim in question["dimensions"]}
        assert "expression_structure" in dimension_ids
        assert question["question_score"] is not None
        for turn in question["turns"]:
            if turn["answer"]:
                answers_by_id[turn["answer"]["answer_id"]] = turn["answer"]["text"]
        for dim in question["dimensions"]:
            if dim["verdict"] == "scored":
                assert dim["evidence"]
                for evidence in dim["evidence"]:
                    assert evidence["quote"] in answers_by_id[evidence["answer_id"]]
            else:
                assert dim["score"] is None
                assert dim["evidence"] == []

    serialized = json.dumps(report, ensure_ascii=False)
    for forbidden in ("reference_approach", "key_points", "plan_snapshot", "evaluation", "model_generated"):
        assert forbidden not in serialized

    # 历史记录可重开同一场。
    history = client.get(
        f"/v1/interviews?plan_id={plan['id']}&limit=1", headers=auth(token)
    )
    assert history.status_code == 200, history.text
    item = history.json()["items"][0]
    assert item["session_id"] == session_id
    assert item["status"] == "ended"
    assert item["report_state"] == "ready"
    assert item["main_question_count"] == 6
    assert item["answered_main_count"] == 6


def test_skip_pause_resume_and_zero_answer_finish(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]

    current = detail(client, token, session_id)
    paused = pause(client, token, session_id, current["revision"])
    assert paused.status_code == 200, paused.text
    paused_revision = paused.json()["revision"]
    paused_detail = detail(client, token, session_id)
    assert paused_detail["status"] == "paused"
    assert "resume" in paused_detail["allowed_actions"]
    assert "answer" not in paused_detail["allowed_actions"]

    blocked = answer(
        client,
        token,
        session_id,
        revision=paused_revision,
        turn_id=paused_detail["current_turn"]["turn_id"],
        text=LONG_ANSWER,
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "INTERVIEW_STATE_INVALID"

    resumed = resume(client, token, session_id, paused_revision)
    assert resumed.status_code == 200, resumed.text
    current = detail(client, token, session_id)
    first = skip(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
    )
    assert first.status_code == 200, first.text
    assert first.json()["job_id"] is None

    current = detail(client, token, session_id)
    assert current["current_main_position"] == 2
    assert current["skipped_main_count"] == 1
    second = skip(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
    )
    assert second.status_code == 200, second.text

    ended = detail(client, token, session_id)
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "completed"
    assert ended["report_job"] is None
    assert ended["report_state"] == "ready"

    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["quality"] == "no_answers"
    assert payload["coverage"]["planned"] == 2
    assert payload["coverage"]["skipped"] == 2
    assert payload["coverage"]["scored"] == 0
    assert payload["questions"][0]["dimensions"] == []
    assert payload["questions"][0]["question_score"] is None

    report_jobs = db.scalars(
        select(Job).where(
            Job.target_id == ended["session_id"], Job.kind == "interview_report"
        )
    ).all()
    assert report_jobs == []


def test_text_validation_revision_and_turn_conflicts(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    turn_id = current["current_turn"]["turn_id"]

    blank = answer(
        client, token, session_id, revision=current["revision"], turn_id=turn_id, text="   "
    )
    assert blank.status_code == 400
    assert blank.json()["error"]["code"] == "TEXT_INVALID"

    too_long = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=turn_id,
        text="字" * 4001,
    )
    assert too_long.status_code == 422
    assert too_long.json()["error"]["code"] == "VALIDATION_ERROR"

    wrong_revision = answer(
        client,
        token,
        session_id,
        revision=current["revision"] + 5,
        turn_id=turn_id,
        text=LONG_ANSWER,
    )
    assert wrong_revision.status_code == 409
    assert wrong_revision.json()["error"]["code"] == "REVISION_CONFLICT"

    wrong_turn = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=str(__import__("uuid").uuid4()),
        text=LONG_ANSWER,
    )
    assert wrong_turn.status_code == 409
    assert wrong_turn.json()["error"]["code"] == "TURN_NOT_CURRENT"

    accepted = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=turn_id,
        text=LONG_ANSWER,
    )
    assert accepted.status_code == 202, accepted.text

    duplicate = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=turn_id,
        text=LONG_ANSWER,
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "REVISION_CONFLICT"

    count = db.scalar(
        select(func.count())
        .select_from(InterviewAnswer)
        .join(InterviewTurn, InterviewAnswer.turn_id == InterviewTurn.id)
        .join(InterviewQuestion, InterviewTurn.question_id == InterviewQuestion.id)
        .where(InterviewQuestion.session_id == __import__("uuid").UUID(session_id))
    )
    assert count == 1


def test_idempotent_replay_and_conflict(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    key = new_key()
    first = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER,
        key=key,
    )
    assert first.status_code == 202, first.text
    replay = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER,
        key=key,
    )
    assert replay.status_code == 202, replay.text
    assert replay.json() == first.json()

    conflict = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER + "不同正文",
        key=key,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_early_finish_supersedes_pending_follow_up(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=4)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    accepted = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER,
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    deciding = detail(client, token, session_id)
    assert deciding["phase"] == "deciding_follow_up"
    assert deciding["current_turn"] is None
    assert deciding["current_main_position"] == 1
    finished = finish(client, token, session_id, deciding["revision"])
    assert finished.status_code == 200, finished.text
    ended = detail(client, token, session_id)
    assert ended["status"] == "ended"
    assert ended["end_reason"] == "early_finish"
    assert ended["report_job"] is not None

    # 已排队但未应用的追问任务被废弃；迟到的 worker 不会追加追问。
    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "superseded"
    worker.run_once()
    after = detail(client, token, session_id)
    assert after["transcript"] == ended["transcript"]
    assert all(turn["kind"] == "main" for turn in after["transcript"])

    report_job = wait_job(client, token, ended["report_job"]["id"], worker, attempts=8)
    assert report_job["state"] == "succeeded", report_job
    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["end_reason"] == "early_finish"
    assert payload["coverage"]["answered"] == 1
    assert payload["coverage"]["not_reached"] == 3
    assert payload["questions"][0]["outcome"] == "answered"
    assert payload["questions"][0]["follow_up_outcome"] == "cancelled"
    assert payload["questions"][1]["outcome"] == "not_reached"


def test_continue_without_follow_up_after_job_failure(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    accepted = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER,
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    job = db.get(Job, __import__("uuid").UUID(job_id))
    job.state = "failed"
    job.error_code = "AI_TIMEOUT"
    job.error = {
        "code": "AI_TIMEOUT",
        "message": "AI 服务超时，结果未知",
        "details": [],
        "request_id": "test",
    }
    job.lease_token = None
    job.lease_expires_at = None
    db.commit()

    failed = detail(client, token, session_id)
    assert "continue_without_follow_up" in failed["allowed_actions"]
    assert "retry_follow_up" in failed["allowed_actions"]

    continued = client.post(
        f"/v1/interviews/{session_id}/continue-without-follow-up",
        json={"revision": failed["revision"], "job_id": job_id},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert continued.status_code == 200, continued.text
    after = detail(client, token, session_id)
    assert after["current_main_position"] == 2
    assert after["transcript"][0]["status"] == "answered"
    superseded = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert superseded["state"] == "superseded"


def test_basic_report_after_failed_report_job(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    final = complete_six_question_interview(client, token, worker, session_id)
    # 该 helper 对 2 题同样适用；最后一题后应已有报告任务。
    assert final["status"] == "ended"
    assert final["report_job"] is not None
    report_job_id = final["report_job"]["id"]
    job = db.get(Job, __import__("uuid").UUID(report_job_id))
    job.state = "failed"
    job.error_code = "AI_INVALID_OUTPUT"
    job.error = {
        "code": "AI_INVALID_OUTPUT",
        "message": "AI 输出不符合契约",
        "details": [],
        "request_id": "test",
    }
    job.lease_token = None
    job.lease_expires_at = None
    db.commit()

    ended = detail(client, token, session_id)
    assert ended["report_state"] == "failed"
    assert "create_basic_report" in ended["allowed_actions"]
    assert "retry_report" in ended["allowed_actions"]
    pending = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert pending.status_code == 409
    assert pending.json()["error"]["code"] == "REPORT_FAILED"

    basic = client.post(
        f"/v1/interviews/{session_id}/report/basic",
        json={"revision": ended["revision"], "job_id": report_job_id},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert basic.status_code == 200, basic.text
    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    payload = report.json()
    assert payload["quality"] == "unscored"
    assert payload["coverage"]["answered"] == 2
    assert all(question["question_score"] is None for question in payload["questions"])
    superseded = client.get(f"/v1/jobs/{report_job_id}", headers=auth(token)).json()
    assert superseded["state"] == "superseded"


def test_history_pagination_and_cross_account_isolation(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    session_ids = []
    for _ in range(3):
        _prep, plan = create_confirmed_plan(client, token, worker, count=2)
        session_ids.append(start_interview(client, token, plan)["session_id"])

    first = client.get("/v1/interviews?limit=2", headers=auth(token))
    assert first.status_code == 200, first.text
    page = first.json()
    assert len(page["items"]) == 2
    assert page["next_cursor"]

    second = client.get(
        f"/v1/interviews?limit=2&cursor={page['next_cursor']}", headers=auth(token)
    )
    assert second.status_code == 200, second.text
    page2 = second.json()
    assert len(page2["items"]) == 1
    assert page2["next_cursor"] is None
    seen = [item["session_id"] for item in page["items"] + page2["items"]]
    assert len(seen) == len(set(seen)) == 3
    assert set(seen) == set(session_ids)

    _, other_token = issue_token(client)
    other = client.get("/v1/interviews", headers=auth(other_token))
    assert other.status_code == 200
    assert other.json()["items"] == []

    invalid = client.get("/v1/interviews?limit=0", headers=auth(token))
    assert invalid.status_code == 422
    bad_cursor = client.get("/v1/interviews?cursor=not-a-cursor", headers=auth(token))
    assert bad_cursor.status_code == 422


def test_pause_worker_advance_keeps_paused(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=4)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    accepted = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=current["current_turn"]["turn_id"],
        text=LONG_ANSWER,
    )
    assert accepted.status_code == 202, accepted.text
    deciding = detail(client, token, session_id)
    paused = pause(client, token, session_id, deciding["revision"])
    assert paused.status_code == 200, paused.text
    job = wait_job(client, token, accepted.json()["job_id"], worker, attempts=6)
    assert job["state"] == "succeeded", job
    after = detail(client, token, session_id)
    assert after["status"] == "paused"
    assert after["current_turn"] is not None
    assert after["current_turn"]["kind"] in ("follow_up", "main")
    assert "resume" in after["allowed_actions"]
    assert "answer" not in after["allowed_actions"]
