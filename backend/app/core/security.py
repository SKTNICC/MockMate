"""密码哈希、不透明 token 与用户名规范化。"""

from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

USERNAME_INPUT_RE = re.compile(r"^[a-zA-Z0-9_]{3,32}$")
USERNAME_NORMALIZED_RE = re.compile(r"^[a-z0-9_]{3,32}$")

_hasher = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2)


def normalize_username(raw: str) -> str:
    value = raw.strip().lower()
    if not USERNAME_NORMALIZED_RE.match(value):
        raise ValueError("用户名需为 3–32 位小写字母、数字或下划线")
    return value


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, ValueError):
        return False


def create_session_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_expiry(ttl_days: int) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=ttl_days)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
