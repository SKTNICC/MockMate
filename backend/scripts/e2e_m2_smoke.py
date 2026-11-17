"""M2 文字模拟面试端到端冒烟（真实 HTTP + PostgreSQL + 独立 worker）。

执行前：数据库已迁移并 seed，API 与 worker 已启动，AI_PROVIDER 为 test 或已配置的真实 provider。

示例：
    python scripts/e2e_m2_smoke.py --base-url http://127.0.0.1:8000/v1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_smoke import (  # noqa: E402
    JD_PROFILE,
    RESUME_PROFILE,
    RESUME_TEXT,
    TEMPLATE_JD_TEXT,
)

ANSWER_TEXT = (
    "在课堂项目里我先检查相同键是否存在，再决定更新还是插入。因为同一本书只应保留一条当前借阅记录，"
    "所以先用键查询状态；不存在就插入，存在且未归还就拒绝重复借阅。验证时我会构造空集合、缺失键、"
    "已归还和未归还四组输入，并检查返回值和列表长度是否符合预期。"
)


class Report:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.failures: list[str] = []
        self.ids: dict[str, str] = {}

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": bool(condition), "detail": detail})
        print(f"[{'PASS' if condition else 'FAIL'}] {name} {detail}")
        if not condition:
            self.failures.append(name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--account-prefix", default="m2e2e")
    args = parser.parse_args()

    report = Report()
    client = httpx.Client(base_url=args.base_url, timeout=30.0)
    suffix = uuid.uuid4().hex[:8]
    username = f"{args.account_prefix}_{suffix}"
    other = f"{args.account_prefix}_{suffix}_b"
    password = "MockMate-dev-2026"

    def idem() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def post(path: str, body: dict, token: str, extra: dict | None = None):
        headers = {**auth(token), **(extra or idem())}
        return client.post(path, json=body, headers=headers)

    def wait_job(job_id: str, token: str, label: str) -> dict:
        deadline = time.time() + args.timeout
        job: dict = {}
        while time.time() < deadline:
            response = client.get(f"/jobs/{job_id}", headers=auth(token))
            if response.status_code != 200:
                report.check(f"{label} job readable", False, response.text[:160])
                return {}
            job = response.json()
            if job["state"] in {"succeeded", "failed", "interrupted", "superseded"}:
                report.check(
                    f"{label} job terminal",
                    job["state"] == "succeeded",
                    f"state={job['state']} code={(job.get('error') or {}).get('code')}",
                )
                return job
            time.sleep(0.5)
        report.check(f"{label} job terminal", False, "timeout")
        return job

    def create_plan(token: str, count: int) -> tuple[str, dict]:
        resume = post(
            "/materials/text",
            {"kind": "resume", "title": "M2 冒烟简历", "text": RESUME_TEXT},
            token,
        )
        assert resume.status_code == 201, resume.text
        resume_id = resume.json()["id"]
        resume_snapshot = post(
            f"/materials/{resume_id}/confirm",
            {"revision": resume.json()["revision"], "profile": RESUME_PROFILE},
            token,
        )
        assert resume_snapshot.status_code == 201, resume_snapshot.text
        jd = post(
            "/materials/text",
            {
                "kind": "jd",
                "title": "内置 JD",
                "text": TEMPLATE_JD_TEXT,
                "template_id": "java-backend-intern-v1",
                "template_version": 1,
            },
            token,
        )
        assert jd.status_code == 201, jd.text
        jd_snapshot = post(
            f"/materials/{jd.json()['id']}/confirm",
            {"revision": jd.json()["revision"], "profile": JD_PROFILE},
            token,
        )
        assert jd_snapshot.status_code == 201, jd_snapshot.text
        prep = post(
            "/preparations",
            {
                "role_id": "java-backend",
                "level": "intern",
                "main_question_count": count,
                "resume_snapshot_id": resume_snapshot.json()["id"],
                "jd_snapshot_id": jd_snapshot.json()["id"],
            },
            token,
        )
        assert prep.status_code == 201, prep.text
        prep_body = prep.json()
        match = post(
            f"/preparations/{prep_body['id']}/match",
            {"revision": prep_body["revision"], "external_processing_consent": True},
            token,
        )
        assert match.status_code == 202, match.text
        wait_job(match.json()["id"], token, "match")
        generate = post(
            f"/preparations/{prep_body['id']}/generate-plan",
            {"revision": prep_body["revision"], "external_processing_consent": True},
            token,
        )
        assert generate.status_code == 202, generate.text
        plan_job = wait_job(generate.json()["id"], token, "plan")
        assert plan_job.get("state") == "succeeded", plan_job
        plan = client.get(
            f"/plans/{plan_job['result_ref']['id']}", headers=auth(token)
        ).json()
        confirmed = post(
            f"/plans/{plan['id']}/confirm", {"revision": plan["revision"]}, token
        )
        assert confirmed.status_code == 200, confirmed.text
        return prep_body["id"], confirmed.json()

    def answer_all(token: str, session_id: str) -> dict:
        final: dict = {}
        for _ in range(16):
            detail = client.get(f"/interviews/{session_id}", headers=auth(token)).json()
            final = detail
            if detail["status"] == "ended":
                return detail
            turn = detail["current_turn"]
            report.check("single open turn", turn is not None, f"phase={detail['phase']}")
            if turn is None:
                return detail
            response = post(
                f"/interviews/{session_id}/answers",
                {
                    "revision": detail["revision"],
                    "turn_id": turn["turn_id"],
                    "text": ANSWER_TEXT,
                },
                token,
            )
            expected = 202 if turn["kind"] == "main" else 200
            report.check(
                f"answer {turn['kind']} status",
                response.status_code == expected,
                str(response.status_code),
            )
            if response.status_code == 202 and response.json().get("job_id"):
                wait_job(response.json()["job_id"], token, "follow_up")
        return final

    try:
        ready = httpx.get(args.base_url.removesuffix("/v1") + "/readyz", timeout=10)
        report.check("readiness", ready.status_code == 200, ready.text[:120])

        for label, name in (("primary", username), ("other", other)):
            response = client.post(
                "/auth/register", json={"username": name, "password": password}
            )
            report.check(f"register {label}", response.status_code == 201)
        login = client.post(
            "/auth/login", json={"username": username, "password": password}
        )
        other_login = client.post(
            "/auth/login", json={"username": other, "password": password}
        )
        report.check("login", login.status_code == 200 and other_login.status_code == 200)
        token = login.json()["access_token"]
        other_token = other_login.json()["access_token"]

        prep_id, plan = create_plan(token, 6)
        report.ids["plan_id"] = plan["id"]
        start = post(
            f"/plans/{plan['id']}/interviews",
            {"plan_revision": plan["revision"], "external_processing_consent": True},
            token,
        )
        report.check("start interview 201", start.status_code == 201, start.text[:160])
        session_id = start.json()["session_id"]
        report.ids["session_id"] = session_id

        reopened = post(
            f"/plans/{plan['id']}/interviews",
            {"plan_revision": plan["revision"], "external_processing_consent": True},
            token,
        )
        report.check(
            "reopen same session 200",
            reopened.status_code == 200
            and reopened.json()["session_id"] == session_id,
        )
        detail = client.get(f"/interviews/{session_id}", headers=auth(token)).json()
        report.check(
            "opening and first turn",
            detail["current_main_position"] == 1
            and detail["current_turn"]["kind"] == "main"
            and bool(detail["opening_text"]),
        )
        serialized = json.dumps(detail, ensure_ascii=False)
        report.check(
            "only exposed turns in detail",
            all(word not in serialized for word in ("reference_approach", "key_points", "plan_snapshot")),
        )
        report.check(
            "cross-account 404",
            client.get(f"/interviews/{session_id}", headers=auth(other_token)).status_code
            == 404,
        )
        report.check(
            "report before end 409",
            client.get(f"/interviews/{session_id}/report", headers=auth(token)).status_code
            == 409,
        )

        final = answer_all(token, session_id)
        report.check(
            "six-question completion",
            final["status"] == "ended"
            and final["end_reason"] == "completed"
            and final["answered_main_count"] == 6,
            f"status={final['status']} answered={final['answered_main_count']}",
        )
        if final.get("report_job"):
            report.ids["report_job_id"] = final["report_job"]["id"]
            wait_job(final["report_job"]["id"], token, "report")
        report_response = client.get(
            f"/interviews/{session_id}/report", headers=auth(token)
        )
        report.check("report 200", report_response.status_code == 200, report_response.text[:160])
        if report_response.status_code == 200:
            payload = report_response.json()
            report.ids["report_id"] = payload["report_id"]
            coverage = payload["coverage"]
            report.check(
                "coverage 6 planned/answered and 3+3",
                coverage["planned"] == 6
                and coverage["answered"] == 6
                and coverage["bank_planned"] == 3
                and coverage["generated_planned"] == 3,
                json.dumps(coverage, ensure_ascii=False),
            )
            report.check(
                "follow-ups recorded separately",
                coverage["follow_up_presented"] >= 2
                and coverage["follow_up_answered"] >= 2,
            )
            answers = {}
            for question in payload["questions"]:
                for turn in question["turns"]:
                    if turn["answer"]:
                        answers[turn["answer"]["answer_id"]] = turn["answer"]["text"]
            evidence_ok = True
            for question in payload["questions"]:
                for dim in question["dimensions"]:
                    for evidence in dim["evidence"]:
                        if evidence["quote"] not in answers.get(evidence["answer_id"], ""):
                            evidence_ok = False
            report.check("evidence quotes locate verbatim", evidence_ok)
            report.check(
                "question score or limited result",
                all(
                    question["question_score"] is not None
                    or any(dim["verdict"] != "scored" for dim in question["dimensions"])
                    for question in payload["questions"]
                ),
            )
            report.check(
                "report hides internal evaluation",
                all(
                    word not in json.dumps(payload, ensure_ascii=False)
                    for word in ("reference_approach", "key_points", "plan_snapshot")
                ),
            )

        history = client.get(
            f"/interviews?plan_id={plan['id']}&limit=1", headers=auth(token)
        )
        report.check(
            "history reopens same session",
            history.status_code == 200
            and history.json()["items"][0]["session_id"] == session_id
            and history.json()["items"][0]["report_state"] == "ready",
        )

        # 第二场：零回答早退。
        _prep2, plan2 = create_plan(token, 2)
        report.ids["zero_answer_plan_id"] = plan2["id"]
        start2 = post(
            f"/plans/{plan2['id']}/interviews",
            {"plan_revision": plan2["revision"], "external_processing_consent": True},
            token,
        )
        session2 = start2.json()["session_id"]
        report.ids["zero_answer_session_id"] = session2
        detail2 = client.get(f"/interviews/{session2}", headers=auth(token)).json()
        finish2 = post(
            f"/interviews/{session2}/finish",
            {"revision": detail2["revision"]},
            token,
        )
        report.check("early finish 200", finish2.status_code == 200, finish2.text[:160])
        zero_report = client.get(
            f"/interviews/{session2}/report", headers=auth(token)
        )
        report.check(
            "zero-answer report is no_answers",
            zero_report.status_code == 200
            and zero_report.json()["quality"] == "no_answers"
            and zero_report.json()["coverage"]["scored"] == 0,
        )

        summary = {
            "base_url": args.base_url,
            "account": username,
            "passed": len(report.steps) - len(report.failures),
            "failed": len(report.failures),
            "ids": report.ids,
            "steps": report.steps,
            "failures": report.failures,
        }
        print("\nSUMMARY " + json.dumps(summary, ensure_ascii=False))
        if args.output:
            Path(args.output).write_text(
                json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return 0 if not report.failures else 1
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
