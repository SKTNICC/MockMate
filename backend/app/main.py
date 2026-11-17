"""FastAPI 应用装配：request_id、统一错误、健康检查、/v1 路由。"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from starlette.middleware.base import BaseHTTPMiddleware

from app.api import auth, catalog_routes, jobs_routes, materials_routes, plans_routes, preparations_routes
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError

LOG = logging.getLogger("mockmate.api")


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id
        LOG.info(
            "request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            duration_ms,
        )
        return response


_DROP_LOCK = threading.Lock()
_DROPPED_ONCE = False


class TestDropReceiptMiddleware(BaseHTTPMiddleware):
    """隔离测试专用：让服务端完成写入但丢弃一次 2xx 回执，验证客户端未知回执恢复。

    只在显式配置 M1_TEST_DROP_RECEIPT_PATH 且非生产环境时生效，且进程内只丢一次。
    """

    async def dispatch(self, request: Request, call_next):
        global _DROPPED_ONCE
        settings = get_settings()
        target = settings.test_drop_receipt_path
        should_drop = False
        if target and not settings.is_production and request.method == "POST":
            if request.url.path == target or request.url.path.endswith(target):
                with _DROP_LOCK:
                    if not _DROPPED_ONCE:
                        _DROPPED_ONCE = True
                        should_drop = True
        response = await call_next(request)
        if should_drop and 200 <= response.status_code < 300:
            LOG.warning(
                "test fault injection: dropped receipt path=%s status=%s",
                request.url.path,
                response.status_code,
            )
            return Response(
                status_code=response.status_code,
                content=b"",
                media_type="application/json",
            )
        return response


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", str(uuid.uuid4()))


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.validate_startup()
    settings.ensure_directories()
    LOG.info("mockmate api started env=%s ai_provider=%s", settings.app_env, settings.ai_provider)
    yield


def create_app() -> FastAPI:
    app = FastAPI(
        title="MockMate M1 API",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs",
        openapi_url="/openapi.json",
    )
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(TestDropReceiptMiddleware)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code or 400,
            content=exc.to_payload(_request_id(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        details = [
            f"{'.'.join(str(part) for part in error.get('loc', []))}: {error.get('msg', '')}"
            for error in exc.errors()[:8]
        ]
        payload = AppError(
            "VALIDATION_ERROR", "请求参数不符合契约", details=details, status_code=422
        ).to_payload(_request_id(request))
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        LOG.exception("unhandled error request_id=%s path=%s", _request_id(request), request.url.path)
        payload = AppError(
            "INTERNAL_ERROR", "服务处理失败，请稍后重试", status_code=500
        ).to_payload(_request_id(request))
        return JSONResponse(status_code=500, content=payload)

    app.include_router(auth.router, prefix="/v1")
    app.include_router(catalog_routes.router, prefix="/v1")
    app.include_router(materials_routes.router, prefix="/v1")
    app.include_router(preparations_routes.router, prefix="/v1")
    app.include_router(plans_routes.router, prefix="/v1")
    app.include_router(jobs_routes.router, prefix="/v1")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz() -> JSONResponse:
        settings = get_settings()
        try:
            with SessionLocal() as db:
                db.execute(text("select 1"))
            settings.private_file_root.mkdir(parents=True, exist_ok=True)
            probe = settings.private_file_root / ".ready"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except Exception:
            LOG.exception("readiness failed")
            return JSONResponse(
                status_code=503, content={"status": "not-ready", "database": False}
            )
        return JSONResponse(status_code=200, content={"status": "ready", "database": True})

    return app


app = create_app()
