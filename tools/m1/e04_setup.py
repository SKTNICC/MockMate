"""E04 真机测试账号与准备数据准备（仅合成材料、test provider）。

用法：
    .work\\venv\\Scripts\\python.exe tools\\m1\\e04_setup.py --base-url http://127.0.0.1:8000/v1

输出 outputs/m1/e04-setup.json（含测试账号与会话 token，属于本机测试证据，不提交/不公开）。
"""

from __future__ import annotations

import argparse
import json
import secrets
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "outputs" / "m1" / "e04-setup.json"
PASSWORD = secrets.token_urlsafe(18)

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


def key() -> str:
    return str(uuid.uuid4())


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


class Api:
    def __init__(self, base: str) -> None:
        self.client = httpx.Client(base_url=base.rstrip("/"), timeout=30)

    def close(self) -> None:
        self.client.close()

    def post(self, path: str, token: str | None, body: dict | None = None) -> httpx.Response:
        headers = {"Idempotency-Key": key()}
        if token:
            headers.update(auth(token))
        return self.client.post(path, json=body or {}, headers=headers)

    def get(self, path: str, token: str | None) -> httpx.Response:
        return self.client.get(path, headers=auth(token) if token else {})


def wait_job(api: Api, token: str, job_id: str, limit: float = 120.0) -> dict:
    deadline = time.time() + limit
    while time.time() < deadline:
        response = api.get(f"/jobs/{job_id}", token)
        response.raise_for_status()
        job = response.json()
        if job["state"] in {"succeeded", "failed", "interrupted", "superseded"}:
            return job
        time.sleep(0.5)
    raise RuntimeError(f"job {job_id} did not reach terminal state in {limit}s")


def register_and_login(api: Api, username: str) -> str:
    response = api.client.post(
        "/auth/register", json={"username": username, "password": PASSWORD}
    )
    if response.status_code not in (201, 409):
        raise RuntimeError(f"register failed: {response.status_code} {response.text}")
    response = api.client.post(
        "/auth/login", json={"username": username, "password": PASSWORD}
    )
    response.raise_for_status()
    return response.json()["access_token"]


def confirmed_snapshots(api: Api, token: str) -> tuple[str, str]:
    response = api.post(
        "/materials/text",
        token,
        {"kind": "resume", "title": "E04 合成简历", "text": RESUME_TEXT},
    )
    response.raise_for_status()
    material = response.json()
    response = api.post(
        f"/materials/{material['id']}/confirm",
        token,
        {"revision": material["revision"], "profile": RESUME_PROFILE},
    )
    response.raise_for_status()
    resume_snapshot = response.json()["id"]

    templates = api.get("/jd-templates", token).json()["items"]
    template = next(
        item for item in templates if item["role_id"] == "java-backend" and item["level"] == "intern"
    )
    response = api.post(
        "/materials/text",
        token,
        {
            "kind": "jd",
            "title": template["title"],
            "text": template["text"],
            "template_id": template["id"],
            "template_version": template["version"],
        },
    )
    response.raise_for_status()
    material = response.json()
    response = api.post(
        f"/materials/{material['id']}/confirm",
        token,
        {"revision": material["revision"], "profile": template["profile"]},
    )
    response.raise_for_status()
    jd_snapshot = response.json()["id"]
    return resume_snapshot, jd_snapshot


def create_preparation(
    api: Api, token: str, resume: str, jd: str, count: int
) -> dict:
    response = api.post(
        "/preparations",
        token,
        {
            "role_id": "java-backend",
            "level": "intern",
            "main_question_count": count,
            "resume_snapshot_id": resume,
            "jd_snapshot_id": jd,
        },
    )
    response.raise_for_status()
    return response.json()


def run_match(api: Api, token: str, prep_id: str) -> dict:
    response = api.post(
        f"/preparations/{prep_id}/match",
        token,
        {"revision": 1, "external_processing_consent": True},
    )
    response.raise_for_status()
    job = wait_job(api, token, response.json()["id"])
    if job["state"] != "succeeded":
        raise RuntimeError(f"match failed: {job}")
    return api.get(f"/preparations/{prep_id}", token).json()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
    args = parser.parse_args()
    api = Api(args.base_url)
    try:
        suffix = str(int(time.time()))
        lifecycle_user = f"e04life{suffix}"
        isolation_user = f"e04iso{suffix}"

        lifecycle_token = register_and_login(api, lifecycle_user)
        resume, jd = confirmed_snapshots(api, lifecycle_token)
        lifecycle_prep = create_preparation(api, lifecycle_token, resume, jd, 6)
        lifecycle_prep = run_match(api, lifecycle_token, lifecycle_prep["id"])
        if not lifecycle_prep.get("match_report_id"):
            raise RuntimeError("lifecycle preparation has no match report")

        isolation_token = register_and_login(api, isolation_user)
        resume2, jd2 = confirmed_snapshots(api, isolation_token)
        # 先建 P2（2 题），再建 P1（6 题）并完成匹配：列表按 updated_at 倒序，P1 稳定在前。
        p2 = create_preparation(api, isolation_token, resume2, jd2, 2)
        p1 = create_preparation(api, isolation_token, resume2, jd2, 6)
        p1 = run_match(api, isolation_token, p1["id"])
        if not p1.get("match_report_id"):
            raise RuntimeError("isolation P1 has no match report")

        payload = {
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "base_url": args.base_url,
            "password": PASSWORD,
            "lifecycle": {
                "username": lifecycle_user,
                "token": lifecycle_token,
                "preparation_id": lifecycle_prep["id"],
                "revision": lifecycle_prep["revision"],
            },
            "isolation": {
                "username": isolation_user,
                "token": isolation_token,
                "p1": {
                    "id": p1["id"],
                    "revision": p1["revision"],
                    "count": 6,
                },
                "p2": {
                    "id": p2["id"],
                    "revision": p2["revision"],
                    "count": 2,
                },
            },
        }
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({k: payload[k] for k in ("generated_at", "lifecycle", "isolation")}, ensure_ascii=False, indent=2))
    finally:
        api.close()


if __name__ == "__main__":
    main()
