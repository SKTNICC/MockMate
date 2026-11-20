"""为 R07 真机受控小样本创建一个虚构 2 题语音计划，并输出账号。

只使用虚构材料；真实 DeepSeek 计划调用次数与结果写入输出。不打印 Key。
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
import uuid
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from e2e_smoke import JD_PROFILE, RESUME_PROFILE, RESUME_TEXT, TEMPLATE_JD_TEXT  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--output", default="")
    args = parser.parse_args()
    client = httpx.Client(base_url=args.base_url, timeout=60.0)
    username = f"realm3_{uuid.uuid4().hex[:8]}"
    password = secrets.token_urlsafe(18)

    def idem() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def auth(token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def post(path: str, body: dict, token: str):
        return client.post(path, json=body, headers={**auth(token), **idem()})

    register = client.post(
        "/auth/register", json={"username": username, "password": password}
    )
    if register.status_code != 201:
        raise SystemExit(f"register failed: {register.status_code} {register.text[:200]}")
    login = client.post("/auth/login", json={"username": username, "password": password})
    login.raise_for_status()
    token = login.json()["access_token"]

    resume = post(
        "/materials/text",
        {"kind": "resume", "title": "R07 虚构简历", "text": RESUME_TEXT},
        token,
    )
    resume.raise_for_status()
    resume_snapshot = post(
        f"/materials/{resume.json()['id']}/confirm",
        {"revision": resume.json()["revision"], "profile": RESUME_PROFILE},
        token,
    )
    resume_snapshot.raise_for_status()
    jd = post(
        "/materials/text",
        {
            "kind": "jd",
            "title": "R07 虚构 JD",
            "text": TEMPLATE_JD_TEXT,
            "template_id": "java-backend-intern-v1",
            "template_version": 1,
        },
        token,
    )
    jd.raise_for_status()
    jd_snapshot = post(
        f"/materials/{jd.json()['id']}/confirm",
        {"revision": jd.json()["revision"], "profile": JD_PROFILE},
        token,
    )
    jd_snapshot.raise_for_status()
    prep = post(
        "/preparations",
        {
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 2,
            "resume_snapshot_id": resume_snapshot.json()["id"],
            "jd_snapshot_id": jd_snapshot.json()["id"],
        },
        token,
    )
    prep.raise_for_status()
    prep_body = prep.json()
    match = post(
        f"/preparations/{prep_body['id']}/match",
        {"revision": prep_body["revision"], "external_processing_consent": True},
        token,
    )
    match.raise_for_status()
    match_deadline = time.time() + 180
    match_job = {}
    while time.time() < match_deadline:
        match_job = client.get(
            f"/jobs/{match.json()['id']}", headers=auth(token)
        ).json()
        if match_job["state"] in {"succeeded", "failed", "interrupted", "superseded"}:
            break
        time.sleep(0.5)
    if match_job.get("state") != "succeeded":
        raise SystemExit(f"match job failed: {match_job}")
    generate = post(
        f"/preparations/{prep_body['id']}/generate-plan",
        {"revision": prep_body["revision"], "external_processing_consent": True},
        token,
    )
    generate.raise_for_status()

    jobs = [match.json()["id"], generate.json()["id"]]
    states = {}
    deadline = time.time() + 180
    while time.time() < deadline:
        states = {
            job: client.get(f"/jobs/{job}", headers=auth(token)).json()
            for job in jobs
        }
        if all(job["state"] in {"succeeded", "failed", "interrupted", "superseded"} for job in states.values()):
            break
        time.sleep(0.5)
    plan_job = states[jobs[1]]
    if plan_job.get("state") != "succeeded":
        raise SystemExit(f"plan job failed: {plan_job}")
    plan = client.get(
        f"/plans/{plan_job['result_ref']['id']}", headers=auth(token)
    )
    plan.raise_for_status()
    plan_body = plan.json()
    confirmed = post(
        f"/plans/{plan_body['id']}/confirm", {"revision": plan_body["revision"]}, token
    )
    confirmed.raise_for_status()
    summary = {
        "username": username,
        "password": password,
        "plan_id": plan_body["id"],
        "main_question_count": plan_body["main_question_count"],
        "jobs": {
            job: {
                "state": value["state"],
                "attempt": value["attempt"],
                "error_code": (value.get("error") or {}).get("code"),
            }
            for job, value in states.items()
        },
    }
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
