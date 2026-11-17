"""鉴权与请求上下文依赖。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import hash_token
from app.models import AuthSession, User


def _extract_bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    parts = header.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise AppError("UNAUTHENTICATED", "需要有效的登录会话", status_code=401)
    return parts[1].strip()


def get_current_session(request: Request, db: Session = Depends(get_db)) -> AuthSession:
    token = _extract_bearer(request)
    session = db.scalars(
        select(AuthSession).where(AuthSession.token_hash == hash_token(token))
    ).one_or_none()
    if session is None or session.revoked_at is not None:
        raise AppError("UNAUTHENTICATED", "登录会话无效或已退出", status_code=401)
    expires_at = session.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if expires_at <= datetime.now(timezone.utc):
        raise AppError("UNAUTHENTICATED", "登录会话已过期", status_code=401)
    request.state.user_id = session.user_id
    return session


def get_current_user(
    request: Request,
    db: Session = Depends(get_db),
    session: AuthSession = Depends(get_current_session),
) -> User:
    user = db.get(User, session.user_id)
    if user is None:
        raise AppError("UNAUTHENTICATED", "登录会话无效", status_code=401)
    return user
