from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent

LEGACY_TEST_DB_NAME = "mockmate_test"
LEGACY_TEST_DATABASE_URL = f"postgresql+psycopg://mockmate@127.0.0.1:55432/{LEGACY_TEST_DB_NAME}"


def _psycopg_dsn(url: str) -> str:
    return url.replace("postgresql+psycopg://", "postgresql://", 1)


def _resolve_test_database() -> tuple[str, str, str | None, str | None]:
    """返回 (app DSN, 测试库名, 维护库 DSN, 开发库名)。

    显式 MOCKMATE_TEST_DATABASE_URL 只接受回环主机与以 _test 结尾的库名；
    同时拒绝与当前 DATABASE_URL 指向同一个库，避免测试重建开发数据。
    """
    from sqlalchemy.engine.url import make_url

    explicit = os.environ.get("MOCKMATE_TEST_DATABASE_URL", "").strip()
    dev_url = os.environ.get("DATABASE_URL", "").strip()
    dev_db = make_url(dev_url).database if dev_url else None
    if not explicit:
        return LEGACY_TEST_DATABASE_URL, LEGACY_TEST_DB_NAME, None, dev_db

    parsed = make_url(explicit)
    if parsed.drivername not in ("postgresql", "postgresql+psycopg"):
        raise RuntimeError("MOCKMATE_TEST_DATABASE_URL 必须是 PostgreSQL URL")
    if (parsed.host or "") not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("MOCKMATE_TEST_DATABASE_URL 只接受回环主机")
    if not parsed.database or not parsed.database.endswith("_test"):
        raise RuntimeError("MOCKMATE_TEST_DATABASE_URL 的库名必须以 _test 结尾")
    if dev_db and parsed.database == dev_db:
        raise RuntimeError("测试库不能与当前 DATABASE_URL 指向同一个数据库")
    app_url = parsed.set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )
    maintenance = parsed.set(database="postgres").render_as_string(hide_password=False)
    return app_url, parsed.database, maintenance, dev_db


TEST_DATABASE_URL, TEST_DB_NAME, TEST_MAINTENANCE_URL, DEV_DB_NAME = _resolve_test_database()

# 必须在导入 app.* 之前设置：配置在模块导入期读取。
os.environ.update(
    {
        "APP_ENV": "test",
        "DATABASE_URL": TEST_DATABASE_URL,
        "PRIVATE_FILE_ROOT": str(BACKEND_ROOT / "runtime" / "test-private"),
        "AI_PROVIDER": "test",
        "ALLOW_TEST_FIXTURES": "true",
        "M1_SAMPLE_CATALOG": "true",
        "MAX_JOB_ATTEMPTS": "2",
        "LOGIN_RATE_LIMIT_ATTEMPTS": "1000",
    }
)

import psycopg  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text, update  # noqa: E402

from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Job  # noqa: E402
from app.workers.runner import JobWorker  # noqa: E402


def _ensure_test_database() -> None:
    if TEST_MAINTENANCE_URL is not None:
        with psycopg.connect(_psycopg_dsn(TEST_MAINTENANCE_URL), autocommit=True) as conn:
            exists = conn.execute(
                "select 1 from pg_database where datname = %s", (TEST_DB_NAME,)
            ).fetchone()
            if not exists:
                try:
                    conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')
                except psycopg.Error as exc:
                    raise RuntimeError(
                        f"测试库 {TEST_DB_NAME} 不存在且当前角色无权创建；"
                        "请由本机管理员预创建后重试"
                    ) from exc
        return

    dsn = "postgresql://mockmate@127.0.0.1:55432/postgres"
    with psycopg.connect(dsn, autocommit=True) as conn:
        exists = conn.execute(
            "select 1 from pg_database where datname = %s", (TEST_DB_NAME,)
        ).fetchone()
        if not exists:
            conn.execute(f'CREATE DATABASE "{TEST_DB_NAME}"')


def _reset_schema() -> None:
    with engine.connect() as conn:
        current = conn.execute(text("select current_database()")).scalar_one()
    if current != TEST_DB_NAME:
        raise RuntimeError(f"测试目标库不一致：期望 {TEST_DB_NAME}，实际 {current}")
    if DEV_DB_NAME and current == DEV_DB_NAME:
        raise RuntimeError("拒绝重建与开发库同名的数据库")
    Base.metadata.drop_all(bind=engine)
    for table in ("alembic_version",):
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {table}"))
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    command.upgrade(config, "head")


def _seed() -> None:
    from app.cli import seed_catalog, seed_content

    db = SessionLocal()
    try:
        seed_catalog(db, sample_enabled=True)
        seed_content(db, test_mode=True)
    finally:
        db.close()


@pytest.fixture(scope="session", autouse=True)
def prepared_database():
    _ensure_test_database()
    _reset_schema()
    _seed()
    yield
    engine.dispose()


@pytest.fixture(autouse=True)
def _clear_queued_jobs():
    """测试之间不共享排队任务，避免 worker.run_once 领到上一用例留下的任务。"""
    db = SessionLocal()
    try:
        db.execute(
            update(Job)
            .where(Job.state == "queued")
            .values(state="superseded", lease_token=None, lease_expires_at=None)
        )
        db.commit()
    finally:
        db.close()
    yield


@pytest.fixture()
def settings():
    return get_settings()


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def worker(settings):
    return JobWorker(settings)


def new_key() -> str:
    return str(uuid.uuid4())


def register(client: TestClient, username: str | None = None, password: str = "password12345"):
    username = username or f"user_{uuid.uuid4().hex[:10]}"
    response = client.post("/v1/auth/register", json={"username": username, "password": password})
    assert response.status_code == 201, response.text
    return username, password


def login(client: TestClient, username: str, password: str = "password12345") -> str:
    response = client.post("/v1/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def issue_token(client: TestClient) -> tuple[str, str]:
    username, password = register(client)
    return username, login(client, username, password)


REVIEWED_RESUME_TEXT = (
    "本科在读，计算机科学与技术专业。\n"
    "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。\n"
    "学习过 Java 集合基础和 JUnit 单元测试。\n"
    "没有工作经历。"
)

REVIEWED_RESUME_PROFILE = {
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

TEMPLATE_JD_PROFILE = {
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


def create_confirmed_resume(client: TestClient, token: str) -> str:
    response = client.post(
        "/v1/materials/text",
        json={
            "kind": "resume",
            "title": "我的简历",
            "text": REVIEWED_RESUME_TEXT,
            "template_id": None,
            "template_version": None,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    material = response.json()
    confirm = client.post(
        f"/v1/materials/{material['id']}/confirm",
        json={"revision": material["revision"], "profile": REVIEWED_RESUME_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 201, confirm.text
    return confirm.json()["id"]


def create_confirmed_template_jd(client: TestClient, token: str) -> str:
    response = client.post(
        "/v1/materials/text",
        json={
            "kind": "jd",
            "title": "内置JD",
            "text": TEMPLATE_JD_TEXT,
            "template_id": "java-backend-intern-v1",
            "template_version": 1,
        },
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert response.status_code == 201, response.text
    material = response.json()
    confirm = client.post(
        f"/v1/materials/{material['id']}/confirm",
        json={"revision": material["revision"], "profile": TEMPLATE_JD_PROFILE},
        headers={**auth(token), "Idempotency-Key": new_key()},
    )
    assert confirm.status_code == 201, confirm.text
    return confirm.json()["id"]


def wait_job(client: TestClient, token: str, job_id: str, worker: JobWorker, attempts: int = 5) -> dict:
    job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    for _ in range(attempts):
        if job["state"] in {"succeeded", "failed", "interrupted", "superseded"}:
            return job
        worker.run_once()
        job = client.get(f"/v1/jobs/{job_id}", headers=auth(token)).json()
    return job


def make_pdf(lines: list[str]) -> bytes:
    """生成一个最小可提取文本的 PDF（ASCII only，用于真实 pypdf 提取测试）。"""
    content = "BT /F1 12 Tf 72 720 Td 14 TL\n"
    for line in lines:
        escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content += f"({escaped}) Tj T*\n"
    content += "ET"
    stream = content.encode("utf-8")

    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_offset = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n"
    ).encode()
    return bytes(out)


def make_docx(paragraphs: list[str]) -> bytes:
    import io

    import docx

    document = docx.Document()
    for paragraph in paragraphs:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
