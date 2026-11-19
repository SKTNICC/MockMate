from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.ai.provider import TestProvider
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models import (
    InterviewAnswer,
    InterviewQuestion,
    InterviewSession,
    InterviewTurn,
    Job,
    JobWorkItem,
)
from app.services import interviews as interviews_service
from app.services import jobs as jobs_service
from tests.conftest import auth, issue_token, new_key, wait_job
from tests.m2_helpers import (
    LONG_ANSWER,
    answer,
    complete_six_question_interview,
    create_confirmed_plan,
    detail,
    finish,
    start_interview,
)


class StubProvider:
    name = "stub"
    model_id = "stub-model"
    prompt_version = "stub"
    test_only = True

    def __init__(self, *, decide=None, score=None):
        self._decide = decide
        self._score = score
        self.last_call = {
            "provider": self.name,
            "model": self.model_id,
            "request_id": "stub-request",
            "usage": None,
        }
        self.score_calls: list[str] = []
        self.score_arguments: list[tuple[dict, list[dict], list[dict]]] = []

    def decide_follow_up(self, *, question, answer_text, context=None):
        self.last_call = {**self.last_call, "request_id": "stub-decide"}
        return self._decide(question=question, answer_text=answer_text, context=context)

    def score_interview_question(self, *, question, answers, dimensions):
        self.last_call = {**self.last_call, "request_id": f"stub-score-{question['id']}"}
        self.score_calls.append(question["id"])
        self.score_arguments.append((question, answers, dimensions))
        return self._score(question=question, answers=answers, dimensions=dimensions)


def _valid_decision(question, answer_text, context=None):
    quote = answer_text[:12]
    return {
        "decision": "ask",
        "prompt": f"你提到「{quote}」，能进一步说明判断依据和适用边界吗？",
        "answer_quote": quote,
        "target": "边界",
    }


def test_follow_up_invalid_quote_fails_job_and_retry_recovers(
    client: TestClient, worker, monkeypatch, db
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

    def bad_decision(**_kwargs):
        return {
            "decision": "ask",
            "prompt": "你能进一步说明这个判断的依据和边界吗？",
            "answer_quote": "这段引文不在原回答中",
            "target": "依据",
        }

    stub = StubProvider(decide=bad_decision)
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: stub)
    worker.run_once()

    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "failed"
    assert job["error"]["code"] == "AI_INVALID_OUTPUT"
    assert job["retryable"] is True

    failed = detail(client, token, session_id)
    assert "continue_without_follow_up" in failed["allowed_actions"]
    assert "retry_follow_up" in failed["allowed_actions"]

    retry = client.post(
        f"/v1/jobs/{job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry.status_code == 202, retry.text
    assert retry.json()["state"] == "queued"

    good = StubProvider(decide=_valid_decision)
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: good)
    job = wait_job(client, token, job_id, worker, attempts=4)
    assert job["state"] == "succeeded", job
    after = detail(client, token, session_id)
    assert after["phase"] == "awaiting_answer"
    assert after["current_turn"]["kind"] == "follow_up"
    assert after["current_turn"]["prompt"].startswith("你提到")


def test_report_scoring_quote_validation_and_dimension_contract(
    client: TestClient, worker, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    complete_six_question_interview(client, token, worker, session_id)

    session_uuid = uuid.UUID(session_id)
    questions = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.session_id == session_uuid)
        .order_by(InterviewQuestion.position)
    ).all()
    turns = db.scalars(
        select(InterviewTurn).where(
            InterviewTurn.question_id.in_([q.id for q in questions])
        )
    ).all()
    answers = {
        row.turn_id: row
        for row in db.scalars(
            select(InterviewAnswer).where(
                InterviewAnswer.turn_id.in_([t.id for t in turns])
            )
        ).all()
    }
    q1, q2 = questions[0], questions[1]
    q1_turns = [t for t in turns if t.question_id == q1.id]
    q1_answers = {t.id: answers[t.id] for t in q1_turns if t.id in answers}
    q2_answers = [answers[t.id] for t in turns if t.question_id == q2.id and t.id in answers]
    assert q1_answers and q2_answers

    dims = interviews_service._expected_dimensions(q1)
    cross_question = {
        "question_id": str(q1.id),
        "dimensions": [
            {
                "dimension_id": dim["id"],
                "verdict": "scored",
                "score": 3,
                "evidence": [
                    {"answer_id": str(q2_answers[0].id), "quote": q2_answers[0].text[:20]}
                ],
                "explanation": "跨题引用",
                "improvement": "无",
            }
            for dim in dims
        ],
    }
    validated = interviews_service.validate_question_score(
        q1, q1_turns, q1_answers, cross_question
    )
    assert len(validated) == len(dims)
    assert all(dim.verdict == "uncertain" for dim in validated)
    assert all(
        dim.score is None and dim.evidence == [] and dim.improvement == ""
        for dim in validated
    )

    fake_quote = {
        "question_id": str(q1.id),
        "dimensions": [
            {
                "dimension_id": dim["id"],
                "verdict": "scored",
                "score": 2,
                "evidence": [
                    {"answer_id": str(q1_answers[q1_turns[0].id].id), "quote": "不存在的原文片段"}
                ],
                "explanation": "伪引文",
                "improvement": "无",
            }
            for dim in dims
        ],
    }
    downgraded = interviews_service.validate_question_score(
        q1, q1_turns, q1_answers, fake_quote
    )
    assert all(dim.verdict == "uncertain" for dim in downgraded)
    assert all(
        dim.score is None and dim.evidence == [] and dim.improvement == ""
        for dim in downgraded
    )

    missing = {
        "question_id": str(q1.id),
        "dimensions": cross_question["dimensions"][:-1],
    }
    with pytest.raises(AppError) as exc:
        interviews_service.validate_question_score(q1, q1_turns, q1_answers, missing)
    assert exc.value.code == "AI_INVALID_OUTPUT"

    duplicate = {
        "question_id": str(q1.id),
        "dimensions": cross_question["dimensions"]
        + [cross_question["dimensions"][0]],
    }
    with pytest.raises(AppError) as exc:
        interviews_service.validate_question_score(q1, q1_turns, q1_answers, duplicate)
    assert exc.value.code == "AI_INVALID_OUTPUT"


def test_report_retry_reuses_valid_slots(
    client: TestClient, worker, monkeypatch, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=4)
    session_id = start_interview(client, token, plan)["session_id"]
    final = complete_six_question_interview(client, token, worker, session_id)
    assert final["report_job"] is not None
    report_job_id = final["report_job"]["id"]

    base = TestProvider()
    call_count = {"n": 0}

    def fail_second(*, question, answers, dimensions):
        call_count["n"] += 1
        if call_count["n"] >= 2:
            raise AppError("AI_TIMEOUT", "AI 服务超时，结果未知")
        return base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )

    failing = StubProvider(score=fail_second)
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: failing)
    worker.run_once()

    job = client.get(f"/v1/jobs/{report_job_id}", headers=auth(token)).json()
    assert job["state"] == "failed", job
    assert job["error"]["code"] == "AI_TIMEOUT"
    assert job["retryable"] is True

    saved = db.scalars(
        select(Job)
        .where(Job.id == uuid.UUID(report_job_id))
    ).one()
    work_items = db.execute(
        select(func.count())
        .select_from(JobWorkItem)
        .where(JobWorkItem.job_id == saved.id)
    ).scalar_one()
    assert work_items == 1

    retry = client.post(
        f"/v1/jobs/{report_job_id}/retry",
        json={"expected_attempt": 1},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert retry.status_code == 202, retry.text

    second_base = TestProvider()

    def score_ok(*, question, answers, dimensions):
        return second_base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )

    recovering = StubProvider(score=score_ok)
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: recovering)
    job = wait_job(client, token, report_job_id, worker, attempts=6)
    assert job["state"] == "succeeded", job
    assert len(recovering.score_calls) == 3  # 第一题复用已验证 slot

    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    assert report.json()["quality"] == "full"


def test_concurrent_start_only_one_session(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)

    def start():
        with TestClient(client.app) as thread_client:
            return thread_client.post(
                f"/v1/plans/{plan['id']}/interviews",
                json={
                    "plan_revision": plan["revision"],
                    "external_processing_consent": True,
                },
                headers={**auth(token), "Idempotency-Key": new_key()},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: start(), range(2)))
    assert sorted(response.status_code for response in responses) == [200, 201]
    session_ids = {response.json()["session_id"] for response in responses}
    assert len(session_ids) == 1
    count = db.scalar(
        select(func.count())
        .select_from(InterviewSession)
        .where(InterviewSession.plan_id == uuid.UUID(plan["id"]))
    )
    assert count == 1


def test_concurrent_double_answer_one_answer(client: TestClient, worker, db) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    current = detail(client, token, session_id)
    body = {
        "revision": current["revision"],
        "turn_id": current["current_turn"]["turn_id"],
        "text": LONG_ANSWER,
    }

    def submit():
        with TestClient(client.app) as thread_client:
            return thread_client.post(
                f"/v1/interviews/{session_id}/answers",
                json=body,
                headers={**auth(token), "Idempotency-Key": new_key()},
            )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: submit(), range(2)))
    assert sorted(response.status_code for response in responses) == [202, 409]
    count = db.scalar(
        select(func.count())
        .select_from(InterviewAnswer)
        .join(InterviewTurn, InterviewAnswer.turn_id == InterviewTurn.id)
        .join(InterviewQuestion, InterviewTurn.question_id == InterviewQuestion.id)
        .where(InterviewQuestion.session_id == uuid.UUID(session_id))
    )
    assert count == 1


def test_late_follow_up_after_early_finish_is_not_applied(
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
    job_id = uuid.UUID(accepted.json()["job_id"])

    claim_db = SessionLocal()
    try:
        claimed = jobs_service.claim_next_job(claim_db, "late-worker")
        assert claimed is not None and claimed.id == job_id
    finally:
        claim_db.close()

    deciding = detail(client, token, session_id)
    finished = finish(client, token, session_id, deciding["revision"])
    assert finished.status_code == 200, finished.text

    worker._handle_follow_up(claimed)

    after = detail(client, token, session_id)
    assert after["status"] == "ended"
    assert all(turn["kind"] == "main" for turn in after["transcript"])
    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    assert job["state"] == "superseded"


def test_score_input_carries_each_turn_prompt_and_hash_is_versioned(
    client: TestClient, worker, monkeypatch, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    final = complete_six_question_interview(client, token, worker, session_id)
    assert final["status"] == "ended"
    assert final["report_job"] is not None

    session_uuid = uuid.UUID(session_id)
    questions = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.session_id == session_uuid)
        .order_by(InterviewQuestion.position)
    ).all()
    turns = db.scalars(
        select(InterviewTurn).where(
            InterviewTurn.question_id.in_([q.id for q in questions])
        )
    ).all()
    answers = {
        row.turn_id: row
        for row in db.scalars(
            select(InterviewAnswer).where(
                InterviewAnswer.turn_id.in_([t.id for t in turns])
            )
        ).all()
    }
    expected: dict[str, list[tuple[str, str, str, str]]] = {}
    for question in questions:
        ordered = interviews_service._ordered_question_turns(
            [t for t in turns if t.question_id == question.id]
        )
        expected[str(question.id)] = [
            (str(answers[t.id].id), t.kind, t.prompt, answers[t.id].text)
            for t in ordered
            if t.id in answers
        ]

    base = TestProvider()
    capturing = StubProvider(
        score=lambda *, question, answers, dimensions: base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )
    )
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: capturing)
    job = wait_job(client, token, final["report_job"]["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job

    captured = {question["id"]: answers for question, answers, _ in capturing.score_arguments}
    for question_id, rows in expected.items():
        assert captured[question_id] == [
            {
                "answer_id": answer_id,
                "kind": kind,
                "prompt": prompt,
                "text": text,
            }
            for answer_id, kind, prompt, text in rows
        ]

    followed = [
        rows for rows in expected.values() if any(kind == "follow_up" for _, kind, _, _ in rows)
    ]
    assert followed, "test provider 应至少为两类来源各产生一次追问"
    for rows in followed:
        main_prompt = next(prompt for _, kind, prompt, _ in rows if kind == "main")
        follow_prompt = next(prompt for _, kind, prompt, _ in rows if kind == "follow_up")
        assert main_prompt != follow_prompt

    q1 = questions[0]
    q1_turns = [t for t in turns if t.question_id == q1.id]
    q1_answers = {t.id: answers[t.id] for t in q1_turns if t.id in answers}
    first = interviews_service.question_slot_hash(q1, q1_turns, q1_answers)
    again = interviews_service.question_slot_hash(q1, list(reversed(q1_turns)), q1_answers)
    assert first == again
    monkeypatch.setattr(
        interviews_service, "SCORE_PROMPT_VERSION", "m2-score-v1"
    )
    old_version = interviews_service.question_slot_hash(q1, q1_turns, q1_answers)
    assert old_version != first


def test_legacy_score_slot_is_not_reused_after_prompt_version_change(
    client: TestClient, worker, monkeypatch, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    final = complete_six_question_interview(client, token, worker, session_id)
    report_job_id = uuid.UUID(final["report_job"]["id"])
    q1 = db.scalars(
        select(InterviewQuestion)
        .where(
            InterviewQuestion.session_id == uuid.UUID(session_id),
            InterviewQuestion.position == 1,
        )
    ).one()
    db.add(
        JobWorkItem(
            job_id=report_job_id,
            slot_key=str(q1.id),
            payload={"dimensions": []},
            content_hash="legacy-m2-score-v1-hash",
            validated=True,
        )
    )
    db.commit()

    base = TestProvider()
    capturing = StubProvider(
        score=lambda *, question, answers, dimensions: base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )
    )
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: capturing)
    job = wait_job(client, token, str(report_job_id), worker, attempts=4)
    assert job["state"] == "succeeded", job
    assert str(q1.id) in capturing.score_calls
    item = db.scalars(
        select(JobWorkItem).where(
            JobWorkItem.job_id == report_job_id, JobWorkItem.slot_key == str(q1.id)
        )
    ).one()
    assert item.content_hash != "legacy-m2-score-v1-hash"
    report = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report.status_code == 200, report.text
    assert report.json()["quality"] == "full"


def test_invalid_evidence_clears_improvement_and_overview_finding(
    client: TestClient, worker, monkeypatch, db
) -> None:
    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]
    final = complete_six_question_interview(client, token, worker, session_id)
    assert final["report_job"] is not None

    base = TestProvider()
    bad_improvement = "用户应使用未在回答中出现的 React 框架"

    def score_with_bad_quote(*, question, answers, dimensions):
        output = base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )
        output["dimensions"][0]["verdict"] = "scored"
        output["dimensions"][0]["score"] = 3
        output["dimensions"][0]["evidence"] = [
            {
                "answer_id": answers[0]["answer_id"],
                "quote": "这段引文完全没有出现在原回答中",
            }
        ]
        output["dimensions"][0]["explanation"] = "基于失效引文的结论"
        output["dimensions"][0]["improvement"] = bad_improvement
        return output

    stub = StubProvider(score=score_with_bad_quote)
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: stub)
    job = wait_job(client, token, final["report_job"]["id"], worker, attempts=4)
    assert job["state"] == "succeeded", job

    report_response = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert report_response.status_code == 200, report_response.text
    report = report_response.json()
    invalid_dimension = report["questions"][0]["dimensions"][0]
    assert invalid_dimension["verdict"] == "uncertain"
    assert invalid_dimension["score"] is None
    assert invalid_dimension["evidence"] == []
    assert invalid_dimension["improvement"] == ""
    assert "引文" in invalid_dimension["explanation"] or "定位" in invalid_dimension["explanation"]
    assert any(
        dim["verdict"] == "scored" and dim["improvement"]
        for dim in report["questions"][0]["dimensions"][1:]
    )
    assert bad_improvement not in str(report["strengths"])
    assert bad_improvement not in str(report["improvements"])


def test_follow_up_blank_fields_and_score_strict_types_and_corrupt_cache_recovery(
    client: TestClient, worker, monkeypatch, db
) -> None:
    """追问拒绝空白引文/目标与非空 advance；评分拒绝 bool/str/float，接受 0/4，坏报告槽只重做该题。"""
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
    assert accepted.status_code == 202
    follow_job_id = accepted.json()["job_id"]

    # 追问：空白引文、空白目标、advance 携带非 null 字段均拒绝
    bad_decisions = [
        {"decision": "ask", "prompt": "你能说明这里的具体实现细节与边界条件吗？", "answer_quote": " ", "target": "细节"},
        {"decision": "ask", "prompt": "你能说明这里的具体实现细节与边界条件吗？", "answer_quote": LONG_ANSWER[:10], "target": " "},
        {"decision": "advance", "prompt": "", "answer_quote": None, "target": None},
        {"decision": "advance", "prompt": None, "answer_quote": " ", "target": None},
    ]
    for idx, bad_dec in enumerate(bad_decisions):
        # 直接测试 _validate_follow_up_decision
        with pytest.raises(AppError) as err:
            worker._validate_follow_up_decision(bad_dec, {"stem": "原主问题？"}, LONG_ANSWER)
        assert err.value.code == "AI_INVALID_OUTPUT", (idx, bad_dec)

    # 通过任务失败与合法恢复验证一次完整追问拒绝 -> 重试 -> advance（省略可空字段）
    monkeypatch.setattr(
        "app.workers.runner.get_provider",
        lambda _settings: StubProvider(decide=lambda **_kw: bad_decisions[0]),
    )
    worker.run_once()
    failed_fu = client.get(f"/v1/jobs/{follow_job_id}", headers=auth(token)).json()
    assert failed_fu["state"] == "failed"
    assert failed_fu["error"]["code"] == "AI_INVALID_OUTPUT"
    assert failed_fu["retryable"] is True

    assert (
        client.post(
            f"/v1/jobs/{follow_job_id}/retry",
            json={"expected_attempt": 1},
            headers={**auth(token), "Idempotency-Key": new_key()},
        ).status_code
        == 202
    )
    monkeypatch.setattr(
        "app.workers.runner.get_provider",
        lambda _settings: StubProvider(decide=lambda **_kw: {"decision": "advance"}),
    )
    assert wait_job(client, token, follow_job_id, worker, attempts=4)["state"] == "succeeded"

    # 完成剩余面试并拿到 report_job
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _settings: TestProvider())
    final = complete_six_question_interview(client, token, worker, session_id)
    report_job_id = uuid.UUID(final["report_job"]["id"])

    session_uuid = uuid.UUID(session_id)
    questions = db.scalars(
        select(InterviewQuestion)
        .where(InterviewQuestion.session_id == session_uuid)
        .order_by(InterviewQuestion.position)
    ).all()
    turns = db.scalars(
        select(InterviewTurn).where(InterviewTurn.question_id.in_([q.id for q in questions]))
    ).all()
    answers = {
        row.turn_id: row
        for row in db.scalars(
            select(InterviewAnswer).where(InterviewAnswer.turn_id.in_([t.id for t in turns]))
        ).all()
    }
    q1, q2 = questions[0], questions[1]
    q1_turns = interviews_service._ordered_question_turns([t for t in turns if t.question_id == q1.id])
    q1_answers = {t.id: answers[t.id] for t in q1_turns if t.id in answers}
    q2_turns = interviews_service._ordered_question_turns([t for t in turns if t.question_id == q2.id])
    q2_answers = {t.id: answers[t.id] for t in q2_turns if t.id in answers}
    dims_q1 = interviews_service._expected_dimensions(q1)
    q1_ans = q1_answers[q1_turns[0].id]

    # 评分严格整数：拒绝 True、"3"、3.0
    for bad_score in (True, "3", 3.0):
        bad_out = {
            "question_id": str(q1.id),
            "dimensions": [
                {
                    "dimension_id": dim["id"],
                    "verdict": "scored",
                    "score": bad_score,
                    "evidence": [{"answer_id": str(q1_ans.id), "quote": q1_ans.text[:10]}],
                    "explanation": "说明",
                    "improvement": "改进",
                }
                for dim in dims_q1
            ],
        }
        with pytest.raises(AppError) as score_err:
            interviews_service.validate_question_score(q1, q1_turns, q1_answers, bad_out)
        assert score_err.value.code == "AI_INVALID_OUTPUT", bad_score

    # 合法 0 分和 4 分允许；纯空白引文降为 uncertain
    mixed_out = {
        "question_id": str(q1.id),
        "dimensions": [
            {
                "dimension_id": dim["id"],
                "verdict": "scored",
                "score": 0 if idx == 0 else 4,
                "evidence": [
                    {
                        "answer_id": str(q1_ans.id),
                        "quote": " " if idx == len(dims_q1) - 1 else q1_ans.text[:10],
                    }
                ],
                "explanation": "说明",
                "improvement": "应被撤回的建议" if idx == len(dims_q1) - 1 else "改进",
            }
            for idx, dim in enumerate(dims_q1)
        ],
    }
    validated_mixed = interviews_service.validate_question_score(
        q1, q1_turns, q1_answers, mixed_out
    )
    assert validated_mixed[0].verdict == "scored" and validated_mixed[0].score == 0
    if len(dims_q1) > 2:
        assert validated_mixed[1].verdict == "scored" and validated_mixed[1].score == 4
    assert validated_mixed[-1].verdict == "uncertain"
    assert validated_mixed[-1].score is None
    assert validated_mixed[-1].evidence == []
    assert validated_mixed[-1].improvement == ""

    # 造一个“哈希正确但载荷残缺（dimensions 元素为标量）”的 q1 坏槽，和有效的 q2 好槽
    q1_hash = interviews_service.question_slot_hash(q1, q1_turns, q1_answers)
    q2_hash = interviews_service.question_slot_hash(q2, q2_turns, q2_answers)
    base = TestProvider()
    q2_output = base.score_interview_question(
        question={"id": str(q2.id)},
        answers=[
            {"answer_id": str(ans.id), "kind": t.kind, "prompt": t.prompt, "text": ans.text}
            for t in q2_turns
            if (ans := q2_answers.get(t.id)) is not None
        ],
        dimensions=interviews_service._expected_dimensions(q2),
    )
    q2_valid = interviews_service.validate_question_score(q2, q2_turns, q2_answers, q2_output)
    db.add(
        JobWorkItem(
            job_id=report_job_id,
            slot_key=str(q1.id),
            payload={"question_id": str(q1.id), "content_hash": q1_hash, "dimensions": ["broken-scalar"]},
            content_hash=q1_hash,
            validated=True,
        )
    )
    db.add(
        JobWorkItem(
            job_id=report_job_id,
            slot_key=str(q2.id),
            payload={
                "question_id": str(q2.id),
                "content_hash": q2_hash,
                "dimensions": [d.model_dump(mode="json") for d in q2_valid],
            },
            content_hash=q2_hash,
            validated=True,
        )
    )
    db.commit()

    score_calls: list[str] = []

    def score_only_missing(*, question, answers, dimensions):
        score_calls.append(question["id"])
        return base.score_interview_question(
            question=question, answers=answers, dimensions=dimensions
        )

    monkeypatch.setattr(
        "app.workers.runner.get_provider",
        lambda _settings: StubProvider(score=score_only_missing),
    )
    done_report = wait_job(client, token, str(report_job_id), worker, attempts=4)
    assert done_report["state"] == "succeeded", done_report
    # q2 有效缓存被复用，只补做了坏槽 q1（及任何其他未缓存的已答题目）
    assert str(q1.id) in score_calls
    assert str(q2.id) not in score_calls
    rep = client.get(f"/v1/interviews/{session_id}/report", headers=auth(token))
    assert rep.status_code == 200, rep.text
    assert rep.json()["quality"] == "full"


def test_score_unknown_dimension_id_redacts_marker_and_null_char_rejected_in_follow_up(
    client: TestClient, worker, monkeypatch, db, caplog
) -> None:
    import json
    import logging
    import httpx
    from app.ai.provider import DeepSeekProvider
    from app.core.config import Settings

    _, token = issue_token(client)
    _prep, plan = create_confirmed_plan(client, token, worker, count=2)
    session_id = start_interview(client, token, plan)["session_id"]

    # 1) 追问共同入口 _chat_json 拦截 \u0000 并保留已提交主回答
    current = detail(client, token, session_id)
    turn = current["current_turn"]
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
    null_fu_provider = DeepSeekProvider(
        settings,
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "id": "req-fu-null",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "content": (
                                    '{"decision":"ask","prompt":"你在回答中提到\\u0000内容，能展开吗？",'
                                    '"answer_quote":"先说明场景前提","target":"边界"}'
                                )
                            },
                        }
                    ],
                },
            )
        ),
    )
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _s: null_fu_provider)
    ans_res = answer(
        client,
        token,
        session_id,
        revision=current["revision"],
        turn_id=turn["turn_id"],
        text=LONG_ANSWER,
    ).json()
    fu_job = wait_job(client, token, ans_res["job_id"], worker, attempts=4)
    assert fu_job["state"] == "failed"
    assert fu_job["retryable"] is True
    assert fu_job["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "reason=invalid_json_text" in fu_job["error"]["details"]

    after_fu_fail = detail(client, token, session_id)
    assert after_fu_fail["transcript"][0]["answer"]["text"] == LONG_ANSWER

    # 切回正常 provider 完成剩余面试，然后测评分未知 dimension_id 的脱敏
    monkeypatch.setattr("app.workers.runner.get_provider", lambda _s: TestProvider())
    cont = client.post(
        f"/v1/interviews/{session_id}/continue-without-follow-up",
        json={"revision": after_fu_fail["revision"], "job_id": ans_res["job_id"]},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert cont.status_code == 200
    ended = complete_six_question_interview(client, token, worker, session_id)
    report_job_id = ended["report_job"]["id"]

    def bad_dimension_score(*, question, answers, dimensions):
        return {
            "question_id": question["id"],
            "dimensions": [
                {
                    "dimension_id": "SYNTHETIC_DIM_5512",
                    "verdict": "insufficient_evidence",
                    "score": None,
                    "evidence": [],
                    "explanation": "x",
                    "improvement": "y",
                }
            ],
        }

    monkeypatch.setattr(
        "app.workers.runner.get_provider",
        lambda _s: StubProvider(score=bad_dimension_score),
    )
    with caplog.at_level(logging.INFO):
        failed_score = wait_job(client, token, report_job_id, worker, attempts=4)

    assert failed_score["state"] == "failed"
    assert failed_score["error"]["code"] == "AI_INVALID_OUTPUT"
    assert "SYNTHETIC_DIM_5512" not in json.dumps(failed_score, ensure_ascii=False)
    assert "field=dimension_id" in failed_score["error"]["details"]
    assert "unknown_count=1" in failed_score["error"]["details"]
    job_row = db.get(Job, uuid.UUID(report_job_id))
    assert job_row is not None
    assert "SYNTHETIC_DIM_5512" not in json.dumps(job_row.ai_call_records, ensure_ascii=False)
    assert "SYNTHETIC_DIM_5512" not in caplog.text
