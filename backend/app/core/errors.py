"""统一错误对象与 HTTP 映射。

对外只暴露受控 code + 中文 message + request_id；不返回数据库异常或 provider 原始正文。
"""

from __future__ import annotations

from dataclasses import dataclass, field

# 文档列出的关键业务 code，加上内部校验使用但同样受控的细分 code。
ERROR_STATUS: dict[str, int] = {
    "INVALID_INPUT": 400,
    "MATERIALS_REQUIRED": 409,
    "PROFILE_INVALID": 400,
    "EVIDENCE_INVALID": 409,
    "ROLE_LEVEL_MISMATCH": 409,
    "JD_LEVEL_CONFLICT": 409,
    "ROLE_NOT_AVAILABLE": 409,
    "MATCH_REQUIRED": 409,
    "BANK_INSUFFICIENT": 409,
    "DUPLICATE_QUESTION": 409,
    "DEDUP_UNCERTAIN": 409,
    "EVALUATION_INVALID": 409,
    "GROUNDING_INVALID": 409,
    "REVISION_CONFLICT": 409,
    "PLAN_STALE": 409,
    "PLAN_STATE_INVALID": 409,
    "INTERVIEW_STATE_INVALID": 409,
    "TURN_NOT_CURRENT": 409,
    "REPORT_NOT_AVAILABLE": 409,
    "REPORT_PENDING": 409,
    "REPORT_FAILED": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "IDEMPOTENCY_KEY_REQUIRED": 400,
    "MATERIAL_IN_USE": 409,
    "JOB_INPUT_CONFLICT": 409,
    "RETRY_NOT_ALLOWED": 409,
    "TEMPLATE_MISMATCH": 409,
    "CONSENT_REQUIRED": 400,
    "AI_NOT_CONFIGURED": 503,
    "AI_INVALID_OUTPUT": 502,
    "AI_TIMEOUT": 502,
    "AI_UNAVAILABLE": 503,
    "AI_RATE_LIMITED": 503,
    "AI_AUTH_FAILED": 503,
    "AI_REQUEST_REJECTED": 502,
    "FILE_TEXT_UNAVAILABLE": 400,
    "UNSUPPORTED_FILE_TYPE": 415,
    "FILE_TOO_LARGE": 413,
    "TEXT_INVALID": 400,
    "UNAUTHENTICATED": 401,
    "NOT_FOUND": 404,
    "USERNAME_TAKEN": 409,
    "RATE_LIMITED": 429,
    "VALIDATION_ERROR": 422,
    "CONTENT_IMPORT_INVALID": 422,
    "INTERNAL_ERROR": 500,
}

# 客户端可自行重试的错误；业务冲突一般不能原样重试。
RETRYABLE_CODES = {"AI_TIMEOUT", "AI_UNAVAILABLE", "AI_RATE_LIMITED", "RATE_LIMITED"}


@dataclass
class AppError(Exception):
    code: str
    message: str
    details: list[str] = field(default_factory=list)
    retryable: bool | None = None
    status_code: int | None = None

    def __post_init__(self) -> None:
        if self.status_code is None:
            self.status_code = ERROR_STATUS.get(self.code, 400)
        if self.retryable is None:
            self.retryable = self.code in RETRYABLE_CODES

    def to_payload(self, request_id: str) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": bool(self.retryable),
                "details": list(self.details),
                "request_id": request_id,
            }
        }


def not_found(message: str = "对象不存在") -> AppError:
    return AppError("NOT_FOUND", message)


def materials_required(missing: list[str]) -> AppError:
    return AppError(
        "MATERIALS_REQUIRED",
        "请确认简历与岗位 JD 后继续",
        details=missing,
    )
