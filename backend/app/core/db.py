from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings

_settings = get_settings()
engine = create_engine(
    _settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def get_db() -> Iterator[Session]:
    """FastAPI 依赖：同步 Session，路由在线程池中执行，不在 event loop 阻塞。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


@contextmanager
def read_snapshot_session() -> Iterator[Session]:
    """详情读取专用的一致性快照。

    鉴权查询已经在请求自己的 Session 里开启事务，不能在同一个事务上再改隔离级别。
    这里用独立连接从首条查询之前建立 REPEATABLE READ 只读事务；快照内的会话、回合、
    回答、任务和报告来自同一时点，避免 worker 提交时混读旧阶段与新任务。
    """
    db = SessionLocal()
    try:
        db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        db.execute(text("SET TRANSACTION READ ONLY"))
        yield db
    finally:
        db.close()
