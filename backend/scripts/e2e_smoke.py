"""M1 端到端冒烟脚本（真实 HTTP + 真实 PostgreSQL + 独立 worker）。

用途：在不依赖手机的情况下验证服务端完整链路，并可作为真机联调的对照。
执行前需要：数据库已迁移并 seed；API 与 worker 已启动；AI_PROVIDER 按环境配置。

示例：
    python scripts/e2e_smoke.py --base-url http://127.0.0.1:8000/v1
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from dataclasses import dataclass, field

import httpx

RESUME_TEXT = (
    "本科在读，计算机科学与技术专业。\n"
    "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。\n"
    "学习过 Java 集合基础和 JUnit 单元测试。\n"
    "没有工作经历。"
)

RESUME_PROFILE = {
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
    "projects": [
        {
            "item_id": "project-1",
            "name": "校园图书借阅系统",
            "description": "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。",
            "skill_codes": ["java.core", "test.junit"],
            "evidence": {
                "quote": "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。",
                "origin": "extracted",
            },
        }
    ],
    "skills": [
        {
            "item_id": "skill-1",
            "name": "Java 集合基础和 JUnit",
            "code": "java.core",
            "evidence": {"quote": "学习过 Java 集合基础和 JUnit 单元测试。", "origin": "extracted"},
        }
    ],
}

TEMPLATE_JD_TEXT = (
    "内置练习岗位：Java 后端开发实习生。\n"
    "职责：在指导下完成 Java 功能开发并说明实现思路。\n"
    "要求：理解 Java 集合的使用及基本边界。\n"
    "要求：能够为简单功能设计边界测试。"
)

JD_PROFILE = {
    "kind": "jd",
    "role_id": "java-backend",
    "level": "intern",
    "title": "Java 后端开发实习生",
    "responsibilities": [
        {
            "item_id": "responsibility-1",
            "text": "职责：在指导下完成 Java 功能开发并说明实现思路。",
            "evidence": {
                "quote": "职责：在指导下完成 Java 功能开发并说明实现思路。",
                "origin": "extracted",
            },
        }
    ],
    "requirements": [
        {
            "item_id": "req-1",
            "text": "理解 Java 集合的使用及基本边界",
            "skill_codes": ["java.core"],
            "importance": "must",
            "evidence": {"quote": "要求：理解 Java 集合的使用及基本边界。", "origin": "extracted"},
        },
        {
            "item_id": "req-2",
            "text": "能够为简单功能设计边界测试",
            "skill_codes": ["test.junit"],
            "importance": "must",
            "evidence": {"quote": "要求：能够为简单功能设计边界测试。", "origin": "extracted"},
        },
    ],
}

PUBLIC_PLAN_FIELDS = {
    "id",
    "preparation_id",
    "input_revision",
    "revision",
    "state",
    "role_id",
    "level",
    "main_question_count",
    "source_counts",
    "resume_snapshot_id",
    "jd_snapshot_id",
    "focus_areas",
    "limitations",
    "created_at",
    "confirmed_at",
}


@dataclass
class Report:
    steps: list[dict] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        self.steps.append({"step": name, "ok": ok, "detail": detail})
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")
        if not ok:
            self.failures.append(name)


def check(report: Report, name: str, condition: bool, detail: str = "") -> None:
    report.step(name, bool(condition), detail)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--output", default="")
    parser.add_argument("--account-prefix", default="m1e2e")
    args = parser.parse_args()

    report = Report()
    client = httpx.Client(base_url=args.base_url, timeout=30.0)
    suffix = uuid.uuid4().hex[:8]
    user_a = f"{args.account_prefix}_{suffix}_a"
    user_b = f"{args.account_prefix}_{suffix}_b"
    password = "MockMate-dev-2026"
    headers_a: dict[str, str] = {}

    def idem() -> dict[str, str]:
        return {"Idempotency-Key": str(uuid.uuid4())}

    def post(path: str, *, json_body=None, token=None, extra_headers=None, files=None, data=None):
        headers = dict(extra_headers or {})
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if files is not None:
            return client.post(path, data=data, files=files, headers=headers)
        return client.post(path, json=json_body, headers=headers)

    try:
        ready = client.get("/", timeout=5)
    except httpx.HTTPError:
        ready = None
    health = httpx.get(args.base_url.removesuffix("/v1") + "/readyz", timeout=10)
    check(report, "readiness", health.status_code == 200, health.text[:120])

    for name in (user_a, user_b):
        response = post("/auth/register", json_body={"username": name, "password": password})
        check(report, f"register {name[-1]}", response.status_code == 201, str(response.status_code))
    login_a = post("/auth/login", json_body={"username": user_a, "password": password})
    login_b = post("/auth/login", json_body={"username": user_b, "password": password})
    check(report, "login A", login_a.status_code == 200)
    check(report, "login B", login_b.status_code == 200)
    token_a = login_a.json()["access_token"]
    token_b = login_b.json()["access_token"]

    me = client.get("/me", headers={"Authorization": f"Bearer {token_a}"})
    check(report, "me", me.status_code == 200 and me.json()["username"] == user_a)

    missing_key = post(
        "/materials/text",
        json_body={"kind": "resume", "title": "无幂等键", "text": RESUME_TEXT},
        token=token_a,
    )
    check(
        report,
        "idempotency key required",
        missing_key.status_code == 400 and missing_key.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED",
    )

    resume_key = str(uuid.uuid4())
    created = post(
        "/materials/text",
        json_body={"kind": "resume", "title": "M1 冒烟简历", "text": RESUME_TEXT},
        token=token_a,
        extra_headers={"Idempotency-Key": resume_key},
    )
    check(report, "create resume text", created.status_code == 201, created.text[:160])
    material = created.json()

    replay = post(
        "/materials/text",
        json_body={"kind": "resume", "title": "M1 冒烟简历", "text": RESUME_TEXT},
        token=token_a,
        extra_headers={"Idempotency-Key": resume_key},
    )
    check(report, "idempotent replay", replay.status_code == 201 and replay.json()["id"] == material["id"])

    confirmed = post(
        f"/materials/{material['id']}/confirm",
        json_body={"revision": material["revision"], "profile": RESUME_PROFILE},
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "confirm resume snapshot", confirmed.status_code == 201, confirmed.text[:160])
    resume_snapshot = confirmed.json()["id"]
    check(
        report,
        "work_experience empty is legal",
        confirmed.json()["profile"]["work_experience"] == [],
    )

    other_read = client.get(
        f"/materials/{material['id']}", headers={"Authorization": f"Bearer {token_b}"}
    )
    check(report, "cross-account 404", other_read.status_code == 404)

    jd_created = post(
        "/materials/text",
        json_body={
            "kind": "jd",
            "title": "内置 JD",
            "text": TEMPLATE_JD_TEXT,
            "template_id": "java-backend-intern-v1",
            "template_version": 1,
        },
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "create jd from template", jd_created.status_code == 201, jd_created.text[:160])
    jd_material = jd_created.json()
    jd_confirmed = post(
        f"/materials/{jd_material['id']}/confirm",
        json_body={"revision": jd_material["revision"], "profile": JD_PROFILE},
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "confirm jd snapshot", jd_confirmed.status_code == 201, jd_confirmed.text[:160])
    jd_snapshot = jd_confirmed.json()["id"]

    draft = post(
        "/preparations",
        json_body={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": None,
            "jd_snapshot_id": None,
        },
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "preparation draft without materials", draft.status_code == 201)
    draft_prep = draft.json()
    blocked = post(
        f"/preparations/{draft_prep['id']}/match",
        json_body={"revision": 1, "external_processing_consent": True},
        token=token_a,
        extra_headers=idem(),
    )
    check(
        report,
        "materials gate blocks match",
        blocked.status_code == 409 and blocked.json()["error"]["code"] == "MATERIALS_REQUIRED",
    )

    prep = post(
        "/preparations",
        json_body={
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": 6,
            "resume_snapshot_id": resume_snapshot,
            "jd_snapshot_id": jd_snapshot,
        },
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "create preparation", prep.status_code == 201, prep.text[:160])
    prep_body = prep.json()

    match = post(
        f"/preparations/{prep_body['id']}/match",
        json_body={"revision": prep_body["revision"], "external_processing_consent": True},
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "match job accepted", match.status_code == 202, match.text[:160])

    def wait(job_id: str, label: str):
        deadline = time.time() + args.timeout
        state = None
        while time.time() < deadline:
            job = client.get(f"/jobs/{job_id}", headers={"Authorization": f"Bearer {token_a}"}).json()
            state = job["state"]
            if state in {"succeeded", "failed", "interrupted", "superseded"}:
                check(report, f"{label} job terminal", state == "succeeded", f"state={state} error={job.get('error')}")
                return job
            time.sleep(0.5)
        check(report, f"{label} job terminal", False, "timeout")
        return {"state": state}

    match_job = wait(match.json()["id"], "match")
    if match_job.get("state") == "succeeded":
        report_body = client.get(
            f"/match-reports/{match_job['result_ref']['id']}",
            headers={"Authorization": f"Bearer {token_a}"},
        ).json()
        check(report, "match rows complete", len(report_body["rows"]) >= 1)
    generate = post(
        f"/preparations/{prep_body['id']}/generate-plan",
        json_body={"revision": prep_body["revision"], "external_processing_consent": True},
        token=token_a,
        extra_headers=idem(),
    )
    check(report, "generate-plan accepted", generate.status_code == 202, generate.text[:200])
    plan_job = wait(generate.json()["id"], "plan") if generate.status_code == 202 else {}

    if plan_job.get("state") == "succeeded":
        plan_response = client.get(
            f"/plans/{plan_job['result_ref']['id']}",
            headers={"Authorization": f"Bearer {token_a}"},
        )
        plan = plan_response.json()
        check(report, "public plan fields", set(plan.keys()) == PUBLIC_PLAN_FIELDS)
        check(report, "3+3 quota", plan["source_counts"] == {"bank": 3, "generated": 3})
        serialized = json.dumps(plan, ensure_ascii=False)
        check(
            report,
            "plan does not leak stems",
            all(word not in serialized for word in ("stem", "questions", "reference_approach", "rubric")),
        )
        confirmed_plan = post(
            f"/plans/{plan['id']}/confirm",
            json_body={"revision": plan["revision"]},
            token=token_a,
            extra_headers=idem(),
        )
        check(
            report,
            "confirm plan",
            confirmed_plan.status_code == 200
            and confirmed_plan.json()["state"] == "confirmed"
            and confirmed_plan.json()["confirmed_at"],
            confirmed_plan.text[:160],
        )
        reopened = client.get(
            "/preparations", headers={"Authorization": f"Bearer {token_a}"}
        ).json()["items"]
        reopened_prep = next(item for item in reopened if item["id"] == prep_body["id"])
        same = client.get(
            f"/plans/{reopened_prep['current_plan_id']}",
            headers={"Authorization": f"Bearer {token_a}"},
        ).json()
        check(
            report,
            "reopen same plan",
            same["id"] == plan["id"] and same["state"] == "confirmed",
        )
        check(
            report,
            "plan order stable",
            same["created_at"] == plan["created_at"]
            and same["source_counts"] == plan["source_counts"],
        )

    logout = client.post("/auth/logout", headers={"Authorization": f"Bearer {token_b}"})
    check(report, "logout", logout.status_code == 204)
    after_logout = client.get("/me", headers={"Authorization": f"Bearer {token_b}"})
    check(report, "session revoked", after_logout.status_code == 401)

    summary = {
        "base_url": args.base_url,
        "account_a": user_a,
        "passed": len(report.steps) - len(report.failures),
        "failed": len(report.failures),
        "steps": report.steps,
        "failures": report.failures,
    }
    print("\nSUMMARY " + json.dumps(summary, ensure_ascii=False))
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
    return 0 if not report.failures else 1


if __name__ == "__main__":
    sys.exit(main())
