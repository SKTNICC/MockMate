"""M2 迁移验证：旧 M1 计划 → 升级 head → 用原 plan ID 开场。

需要一个可创建数据库的本机管理连接：
    $env:MOCKMATE_ADMIN_DATABASE_URL = "postgresql://postgres:...@127.0.0.1:5432/postgres"
    $env:DATABASE_URL = "postgresql+psycopg://mockmate:...@127.0.0.1:5432/mockmate_m2_dev"
    python scripts/verify_m2_migration.py

脚本默认创建带随机后缀的迁移专用临时库，只在本次创建成功且未传
``--keep`` 时回收该库。不会删除、覆盖或终止任何已有数据库的连接。
``--database`` 只接受 ``mockmate_m2_migration_`` 前缀，拒绝开发库、测试库
与维护库名。
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import uuid
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from sqlalchemy.engine.url import make_url  # noqa: E402

M1_HEAD = "9b2c3d4e5f6a"
MIGRATION_DB_PREFIX = "mockmate_m2_migration_"
MAX_DATABASE_NAME_LENGTH = 63
MIN_SUFFIX_LENGTH = 6
DATABASE_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
MAINTENANCE_DATABASE_NAMES = {"postgres", "template0", "template1"}


def _admin_dsn() -> str:
    raw = os.environ.get("MOCKMATE_ADMIN_DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("缺少 MOCKMATE_ADMIN_DATABASE_URL")
    return raw


def _normalize_host(host: str | None) -> str:
    value = (host or "").strip().lower()
    if value in ("localhost", "::1"):
        return "127.0.0.1"
    return value


def _normalize_port(port: int | None) -> int:
    return port or 5432


def _template_url() -> str:
    raw = os.environ.get("DATABASE_URL", "").strip()
    if not raw:
        raise SystemExit("缺少 DATABASE_URL（用作应用连接模板）")
    return raw


def _validate_template_host(admin_dsn: str, template_url: str) -> None:
    """只操作管理连接与应用模板共同指向的本机实例。"""
    admin = make_url(admin_dsn)
    template = make_url(template_url)
    for label, url in (("MOCKMATE_ADMIN_DATABASE_URL", admin), ("DATABASE_URL", template)):
        if url.get_backend_name() != "postgresql":
            raise SystemExit(f"{label} 必须是 PostgreSQL 连接")
        if _normalize_host(url.host) not in LOCAL_HOSTS:
            raise SystemExit(f"{label} 只接受本机回环地址")
    if _normalize_host(admin.host) != _normalize_host(template.host) or _normalize_port(
        admin.port
    ) != _normalize_port(template.port):
        raise SystemExit(
            "管理连接与应用模板必须指向同一个本机 PostgreSQL 实例（host/port 不一致）"
        )


def _test_database_name() -> str | None:
    raw = os.environ.get("MOCKMATE_TEST_DATABASE_URL", "").strip()
    if not raw:
        return None
    try:
        return make_url(raw).database
    except Exception:
        return None


def _resolve_database_name(
    explicit: str | None,
    *,
    dev_db: str | None = None,
    test_db: str | None = None,
) -> str:
    """返回本次验证使用的库名；只接受迁移专用前缀的显式名。"""
    if explicit is None or not explicit.strip():
        return f"{MIGRATION_DB_PREFIX}{uuid.uuid4().hex[:12]}"

    name = explicit.strip()
    if (
        name in MAINTENANCE_DATABASE_NAMES
        or (dev_db is not None and name == dev_db)
        or (test_db is not None and name == test_db)
    ):
        raise SystemExit(
            f"--database {name} 指向开发库、测试库或维护库名；脚本拒绝操作该库"
        )
    if not name.startswith(MIGRATION_DB_PREFIX):
        raise SystemExit(
            f"--database 只接受迁移专用前缀 {MIGRATION_DB_PREFIX}<随机后缀>，"
            "以明确本次临时库的用途"
        )
    suffix = name[len(MIGRATION_DB_PREFIX) :]
    if (
        len(name) > MAX_DATABASE_NAME_LENGTH
        or len(suffix) < MIN_SUFFIX_LENGTH
        or not DATABASE_NAME_PATTERN.fullmatch(name)
    ):
        raise SystemExit(
            f"--database 名称不合法：长度需不超过 {MAX_DATABASE_NAME_LENGTH}，"
            f"后缀至少 {MIN_SUFFIX_LENGTH} 位，且只含小写字母、数字与下划线"
        )
    return name


def _create_database(admin_dsn: str, name: str, owner: str) -> None:
    """只执行 CREATE DATABASE；同名冲突由数据库报告，绝不提前删库。"""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("CREATE DATABASE {} OWNER {}").format(
                sql.Identifier(name), sql.Identifier(owner)
            )
        )


def _drop_database(admin_dsn: str, name: str) -> None:
    """仅回收本次脚本创建的临时库；不终止其他连接。"""
    import psycopg
    from psycopg import sql

    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
        )


def _app_url_with_database(template_url: str, name: str) -> str:
    return make_url(template_url).set(database=name).render_as_string(hide_password=False)


def _run_verification(app_url: str, database: str) -> None:
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

    engine = None
    try:
        from alembic import command
        from alembic.config import Config
        from fastapi.testclient import TestClient
        from sqlalchemy import select, text

        from app.core.db import SessionLocal, engine as app_engine
        from app.main import app
        from app.models import Plan
        from app.workers.runner import JobWorker
        from scripts.e2e_smoke import (
            JD_PROFILE,
            RESUME_PROFILE,
            RESUME_TEXT,
            TEMPLATE_JD_TEXT,
        )

        engine = app_engine
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

        worker = JobWorker(
            __import__("app.core.config", fromlist=["get_settings"]).get_settings()
        )

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
                    if job["state"] in {
                        "succeeded",
                        "failed",
                        "interrupted",
                        "superseded",
                    }:
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
            print(f"[PASS] M2 tables and job kinds present; old plan preserved in {database}")

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
            detail = client.get(
                f"/v1/interviews/{session_id}", headers=auth(token)
            ).json()
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
    finally:
        if engine is not None:
            engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "在本次创建的一次性临时库中验证 M1 → M2 迁移和开场；"
            "不会删除或终止任何已有数据库。"
        )
    )
    parser.add_argument(
        "--database",
        default=None,
        help=(
            f"显式临时库名，必须使用 {MIGRATION_DB_PREFIX}<后缀>；"
            "缺省时自动生成随机后缀"
        ),
    )
    parser.add_argument("--keep", action="store_true", help="验证后保留本次创建的临时库")
    args = parser.parse_args(argv)

    try:
        admin = _admin_dsn()
        template_url = _template_url()
        _validate_template_host(admin, template_url)
        template = make_url(template_url)
        database = _resolve_database_name(
            args.database,
            dev_db=template.database,
            test_db=_test_database_name(),
        )
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    app_url = _app_url_with_database(template_url, database)
    owner = template.username or "mockmate"

    import psycopg

    try:
        _create_database(admin, database, owner)
    except psycopg.errors.DuplicateDatabase:
        print(
            f"[FAIL] 数据库 {database} 已存在；脚本不会删除或覆盖已有库。"
            "请换用缺省随机库名，或指定新的迁移专用库名。",
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            f"[FAIL] 无法创建临时库 {database}（{type(exc).__name__}）："
            "请核对管理连接、CREATEDB 权限和本机实例。",
            file=sys.stderr,
        )
        raise

    print(f"[INFO] created temporary database {database}")
    failed = False
    try:
        _run_verification(app_url, database)
        print("[PASS] migration verification complete")
        return 0
    except BaseException:
        failed = True
        raise
    finally:
        if args.keep:
            print(f"[INFO] kept temporary database {database}")
        else:
            try:
                _drop_database(admin, database)
                print(f"[INFO] dropped temporary database {database}")
            except Exception as exc:
                message = (
                    f"[WARN] 临时库 {database} 未能回收（{type(exc).__name__}）。"
                    f"请关闭残留连接后手动执行 DROP DATABASE {database};"
                )
                if failed:
                    # 主验证异常必须保留，清理错误只提示。
                    print(message, file=sys.stderr)
                else:
                    raise SystemExit(message) from exc


if __name__ == "__main__":
    sys.exit(main())
