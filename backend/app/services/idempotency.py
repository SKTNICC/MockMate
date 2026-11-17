"""业务写接口幂等：user_id + method + path + key 唯一，同 key 同 body 回放首次结果。"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.models import IdempotencyRecord


def digest_json_body(body: bytes) -> str:
    return hashlib.sha256(body or b"").hexdigest()


def digest_file_input(kind: str, title: str, file_sha256: str) -> str:
    canonical = f"{kind}\x1f{title.strip()}\x1f{file_sha256}"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def require_idempotency_key(raw_key: str | None) -> str:
    if not raw_key:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "业务写操作必须携带 Idempotency-Key 请求头",
            details=["Idempotency-Key"],
        )
    try:
        return str(uuid.UUID(raw_key))
    except ValueError:
        raise AppError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key 必须是 UUID",
            details=["Idempotency-Key"],
        )


@dataclass
class IdempotencyHandle:
    user_id: uuid.UUID
    method: str
    path: str
    key: str
    request_hash: str
    replay: JSONResponse | None = None


def begin(
    db: Session, *, user_id: uuid.UUID, method: str, path: str, raw_key: str | None, request_hash: str
) -> IdempotencyHandle:
    key = require_idempotency_key(raw_key)
    stmt = (
        pg_insert(IdempotencyRecord)
        .values(
            id=uuid.uuid4(),
            user_id=user_id,
            method=method.upper(),
            path=path,
            key=key,
            request_hash=request_hash,
        )
        .on_conflict_do_nothing(constraint="uq_idem_user_method_path_key")
        .returning(IdempotencyRecord.id)
    )
    inserted_id = db.execute(stmt).scalar_one_or_none()
    created = inserted_id is not None

    if not created:
        record = db.scalars(
            select(IdempotencyRecord)
            .where(
                IdempotencyRecord.user_id == user_id,
                IdempotencyRecord.method == method.upper(),
                IdempotencyRecord.path == path,
                IdempotencyRecord.key == key,
            )
            .with_for_update()
        ).one_or_none()
        if record is None:  # 首个事务回滚，重新占用
            db.execute(stmt)
        else:
            if record.request_hash != request_hash:
                raise AppError(
                    "IDEMPOTENCY_CONFLICT",
                    "相同 Idempotency-Key 对应的请求内容不同",
                )
            if record.status_code is not None:
                if record.status_code == 204:
                    return IdempotencyHandle(
                        user_id=user_id,
                        method=method.upper(),
                        path=path,
                        key=key,
                        request_hash=request_hash,
                        replay=JSONResponse(status_code=204, content=None),
                    )
                return IdempotencyHandle(
                    user_id=user_id,
                    method=method.upper(),
                    path=path,
                    key=key,
                    request_hash=request_hash,
                    replay=JSONResponse(
                        status_code=record.status_code, content=record.response or {}
                    ),
                )
            raise AppError(
                "IDEMPOTENCY_CONFLICT",
                "相同请求正在处理中，请稍后重试",
                retryable=True,
            )

    return IdempotencyHandle(
        user_id=user_id, method=method.upper(), path=path, key=key, request_hash=request_hash
    )


def complete(
    db: Session, handle: IdempotencyHandle, status_code: int, payload: dict | None
) -> JSONResponse:
    record = db.scalars(
        select(IdempotencyRecord)
        .where(
            IdempotencyRecord.user_id == handle.user_id,
            IdempotencyRecord.method == handle.method,
            IdempotencyRecord.path == handle.path,
            IdempotencyRecord.key == handle.key,
        )
        .with_for_update()
    ).one_or_none()
    if record is not None:
        record.status_code = status_code
        record.response = payload
    db.commit()
    if status_code == 204:
        return JSONResponse(status_code=204, content=None)
    return JSONResponse(status_code=status_code, content=payload)
