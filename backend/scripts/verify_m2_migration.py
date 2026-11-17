"""M2 迁移验证：旧 M1 计划 → 升级 head → 用原 plan ID 开场。

需要一个可创建数据库的本机管理连接：
    $env:MOCKMATE_ADMIN_DATABASE_URL = "postgresql://postgres:...@127.0.0.1:5432/postgres"
    $env:DATABASE_URL = "postgresql+psycopg://mockmate:...@127.0.0.1:5432/mockmate_m2_dev"
    python scripts/verify_m2_migration.py

脚本会创建并删除临时库（默认 mockmate_m2_migration），不会触碰开发库或测试库。
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.engine.url import make_url  # noqa: E402

M1_HEAD = "9b2c3d4e5f6a"


def _admin_dsn() -> str:
    raw = os.environ.get("MOCKMATE_ADMIN_DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("缺少 MOCKMATE_ADMIN_DATABASE_URL")
    return raw


def _app_url_with_database(name: str) -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("缺少 DATABASE_URL（用作应用连接模板）")
    return make_url(raw).set(database=name).render_as_string(hide_password=False)


def _create_database(admin_dsn: str, name: str, owner: str) -> None:
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        conn.execute(f'CREATE DATABASE "{name}" OWNER "{owner}"')


def _drop_database(admin_dsn: str, name: str) -> None:
    import psycopg

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            "select pg_terminate_backend(pid) from pg_stat_activity where datname=%s",
            (name,),
        )
        conn.execute(f'DROP DATABASE IF EXISTS "{name}"')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default="mockmate_m2_migration")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    admin = _admin_dsn()
    app_url = _app_url_with_database(args.database)
    owner = make_url(app_url).username or "mockmate"
    os.environ.update(
        {
            "DATABASE_URL": app_url,
            "APP_ENV": "test",
            "AI_PROVIDER": "test",
            "ALLOW_TEST_FIXTURES": "true",
            "M1_SAMPLE_CATALOG": "true",
            "PRIVATE_FILE_ROOT": str(BACKEND_ROOT / "runtime" / "migration-private"),
            "MAX_JOB_ATTEMPTS": "2",
            "LOGIN_RATE_LIMIT_ATTEMPTS": "1000",
        }
    )

    _create_database(admin, args.database, owner)
    try:
        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient
        from sqlalchemy import select, text

        from app.core.db import SessionLocal, engine
        from app.main import app
        from app.models import Plan
        from app.workers.runner import JobWorker
        from scripts.e2e_smoke import JD_PROFILE, RESUME_PROFILE, RESUME_TEXT, TEMPLATE_JD_TEXT

        config = Config(str(BACKEND_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

        def upgrade(revision: str) -> None:
            command.upgrade(config, revision)
            print(f"[PASS] alembic upgrade {revision}")

        def new_key() -> str:
            return str(uuid.uuid4())

        def auth(token: str) -> dict:
            return {"Authorization": f"Bearer {token}"}

        upgrade(M1_HEAD)

        from app.cli import seed_catalog, seed_content

        db = SessionLocal()
        try:
            seed_catalog(db, sample_enabled=True)
            seed_content(db, test_mode=True)
        finally:
            db.close()

        worker = JobWorker(__import__("app.core.config", fromlist=["get_settings"]).get_settings())

        with TestClient(app) as client:
            username = f"migration_{uuid.uuid4().hex[:8]}"
            password = "MockMate-dev-2026"
            assert client.post(
                "/v1/auth/register", json={"username": username, "password": password}
            ).status_code == 201
            login = client.post(
                "/v1/auth/login", json={"username": username, "password": password}
            )
            assert login.status_code == 200, login.text
            token = login.json()["access_token"]

            resume = client.post(
                "/v1/materials/text",
                json={"kind": "resume", "title": "迁移验证简历", "text": RESUME_TEXT},
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert resume.status_code == 201, resume.text
            resume_snapshot = client.post(
                f"/v1/materials/{resume.json()['id']}/confirm",
                json={"revision": resume.json()["revision"], "profile": RESUME_PROFILE},
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert resume_snapshot.status_code == 201, resume_snapshot.text

            jd = client.post(
                "/v1/materials/text",
                json={
                    "kind": "jd",
                    "title": "内置 JD",
                    "text": TEMPLATE_JD_TEXT,
                    "template_id": "java-backend-intern-v1",
                    "template_version": 1,
                },
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert jd.status_code == 201, jd.text
            jd_snapshot = client.post(
                f"/v1/materials/{jd.json()['id']}/confirm",
                json={"revision": jd.json()["revision"], "profile": JD_PROFILE},
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

            def wait(job_id: str) -> dict:
                job = {}
                for _ in range(8):
                    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
                    if job["state"] in {"succeeded", "failed", "interrupted", "superseded"}:
                        return job
                    worker.run_once()
                return job

            match = client.post(
                f"/v1/preparations/{prep.json()['id']}/match",
                json={"revision": 1, "external_processing_consent": True},
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert match.status_code == 202, match.text
            assert wait(match.json()["id"])["state"] == "succeeded"
            generate = client.post(
                f"/v1/preparations/{prep.json()['id']}/generate-plan",
                json={"revision": 1, "external_processing_consent": True},
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert generate.status_code == 202, generate.text
            plan_job = wait(generate.json()["id"])
            assert plan_job["state"] == "succeeded", plan_job
            plan_id = plan_job["result_ref"]["id"]
            plan = client.get(f"/v1/plans/{plan_id}", headers=auth(token)).json()
            confirmed = client.post(
                f"/v1/plans/{plan_id}/confirm",
                json={"revision": plan["revision"]},
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert confirmed.status_code == 200, confirmed.text
            print(f"[PASS] M1 plan created before upgrade plan_id={plan_id}")

            db = SessionLocal()
            try:
                assert db.scalar(select(Plan).where(Plan.id == uuid.UUID(plan_id))) is not None
            finally:
                db.close()

            upgrade("head")

            db = SessionLocal()
            try:
                table = db.execute(
                    text("select to_regclass('public.interview_sessions')")
                ).scalar_one()
                assert table is not None
                constraint = db.execute(
                    text(
                        "select pg_get_constraintdef(oid) from pg_constraint "
                        "where conname='ck_job_kind'"
                    )
                ).scalar_one()
                assert "follow_up" in constraint and "interview_report" in constraint
                assert db.scalar(select(Plan).where(Plan.id == uuid.UUID(plan_id))) is not None
            finally:
                db.close()
            print("[PASS] M2 tables and job kinds present; old plan preserved")

            started = client.post(
                f"/v1/plans/{plan_id}/interviews",
                json={
                    "plan_revision": confirmed.json()["revision"],
                    "external_processing_consent": True,
                },
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert started.status_code == 201, started.text
            session_id = started.json()["session_id"]
            detail = client.get(f"/v1/interviews/{session_id}", headers=auth(token)).json()
            assert detail["current_turn"]["kind"] == "main"
            print(f"[PASS] old plan starts after upgrade session_id={session_id}")

            answer = client.post(
                f"/v1/interviews/{session_id}/answers",
                json={
                    "revision": detail["revision"],
                    "turn_id": detail["current_turn"]["turn_id"],
                    "text": "我先检查相同键是否存在，再决定更新还是插入，并用边界用例验证。",
                },
                headers={**auth(token), "Idempotency-Key": new_key()},
            )
            assert answer.status_code == 202, answer.text
            assert answer.json()["job_id"]
            follow = wait(answer.json()["job_id"])
            assert follow["state"] == "succeeded", follow
            print("[PASS] follow_up job executes on migrated schema")
        engine.dispose()
        print("[PASS] migration verification complete")
        return 0
    finally:
        if args.keep:
            print(f"[INFO] kept temporary database {args.database}")
        else:
            _drop_database(admin, args.database)
            print(f"[INFO] dropped temporary database {args.database}")


if __name__ == "__main__":
    sys.exit(main())
