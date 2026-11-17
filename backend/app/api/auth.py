"""账户接口：注册、登录、退出、本人信息。"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_session, get_current_user
from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import AppError
from app.core.security import (
    create_session_token,
    hash_password,
    hash_token,
    normalize_username,
    session_expiry,
    verify_password,
)
from app.models import AuthSession, User
from app.schemas import Credentials, TokenResponse, User as UserSchema
from app.services.rate_limit import check_login_rate_limit, reset_login_rate_limit

router = APIRouter(tags=["auth"])


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _user_schema(user: User) -> UserSchema:
    return UserSchema(id=user.id, username=user.username_normalized)


@router.post("/auth/register", response_model=UserSchema, status_code=201, operation_id="register")
def register(payload: Credentials, db: Session = Depends(get_db)) -> UserSchema:
    try:
        username = normalize_username(payload.username)
    except ValueError as exc:
        raise AppError("INVALID_INPUT", str(exc), details=["username"])
    existing = db.scalars(select(User).where(User.username_normalized == username)).one_or_none()
    if existing is not None:
        raise AppError("USERNAME_TAKEN", "该用户名已被占用", status_code=409)
    user = User(username_normalized=username, password_hash=hash_password(payload.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise AppError("USERNAME_TAKEN", "该用户名已被占用", status_code=409)
    db.refresh(user)
    return _user_schema(user)


@router.post("/auth/login", response_model=TokenResponse, operation_id="login")
def login(payload: Credentials, request: Request, db: Session = Depends(get_db)) -> TokenResponse:
    settings = get_settings()
    try:
        username = normalize_username(payload.username)
    except ValueError:
        username = payload.username.strip().lower()[:32]
    check_login_rate_limit(settings, username, _client_ip(request))

    user = db.scalars(select(User).where(User.username_normalized == username)).one_or_none()
    if user is None or not verify_password(payload.password, user.password_hash):
        raise AppError("UNAUTHENTICATED", "用户名或密码不正确", status_code=401)

    token = create_session_token()
    expires_at = session_expiry(settings.session_ttl_days)
    session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(token),
        expires_at=expires_at,
        created_at=datetime.now(timezone.utc),
    )
    db.add(session)
    db.commit()
    reset_login_rate_limit(username, _client_ip(request))
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        expires_at=expires_at,
        user=_user_schema(user),
    )


@router.post("/auth/logout", status_code=204, operation_id="logout")
def logout(
    db: Session = Depends(get_db), session: AuthSession = Depends(get_current_session)
) -> Response:
    session.revoked_at = datetime.now(timezone.utc)
    db.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserSchema, operation_id="getMe")
def me(user: User = Depends(get_current_user)) -> UserSchema:
    return _user_schema(user)
