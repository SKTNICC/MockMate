"""运行时配置。

所有配置集中在此读取；缺失必需项时启动即失败，避免半配置状态把错误推迟到业务请求。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_env: str = Field(default="development", alias="APP_ENV")
    database_url: str = Field(alias="DATABASE_URL")
    session_ttl_days: int = Field(default=7, alias="SESSION_TTL_DAYS")
    private_file_root: Path = Field(alias="PRIVATE_FILE_ROOT")

    ai_provider: str = Field(default="disabled", alias="AI_PROVIDER")
    ai_base_url: str = Field(default="https://api.deepseek.com", alias="AI_BASE_URL")
    ai_model: str = Field(default="", alias="AI_MODEL")
    ai_api_key: str = Field(default="", alias="AI_API_KEY")
    ai_timeout_seconds: int = Field(default=60, alias="AI_TIMEOUT_SECONDS")

    max_upload_bytes: int = Field(default=10 * 1024 * 1024, alias="MAX_UPLOAD_BYTES")
    max_material_characters: int = Field(default=20000, alias="MAX_MATERIAL_CHARACTERS")
    max_job_attempts: int = Field(default=2, alias="MAX_JOB_ATTEMPTS")
    allow_test_fixtures: bool = Field(default=False, alias="ALLOW_TEST_FIXTURES")

    # 开发样板岗位开关。默认关闭；开启时角色带 sample_only=true，界面必须醒目标注。
    sample_catalog_enabled: bool = Field(default=False, alias="M1_SAMPLE_CATALOG")
    login_rate_limit_attempts: int = Field(default=10, alias="LOGIN_RATE_LIMIT_ATTEMPTS")
    login_rate_limit_window_seconds: int = Field(default=300, alias="LOGIN_RATE_LIMIT_WINDOW_SECONDS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")

    # 仅限隔离测试的故障注入：对指定写路径丢弃一次 2xx 回执（服务端已完成）。
    # 生产环境禁止设置；正常业务不会走该分支。
    test_drop_receipt_path: str = Field(default="", alias="M1_TEST_DROP_RECEIPT_PATH")

    @field_validator("private_file_root", mode="after")
    @classmethod
    def _expand_root(cls, value: Path) -> Path:
        return value.expanduser()

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    def validate_startup(self) -> None:
        problems: list[str] = []
        if not self.database_url:
            problems.append("DATABASE_URL 未配置")
        if not str(self.private_file_root):
            problems.append("PRIVATE_FILE_ROOT 未配置")
        if self.ai_provider not in {"disabled", "deepseek", "test"}:
            problems.append(f"AI_PROVIDER 取值非法: {self.ai_provider}")
        if self.ai_provider == "deepseek" and not (self.ai_api_key and self.ai_model):
            problems.append("AI_PROVIDER=deepseek 但 AI_API_KEY/AI_MODEL 不完整")
        if self.ai_provider == "test" and not self.allow_test_fixtures:
            problems.append("AI_PROVIDER=test 必须同时显式设置 ALLOW_TEST_FIXTURES=true")
        if self.is_production and self.allow_test_fixtures:
            problems.append("生产环境禁止 ALLOW_TEST_FIXTURES=true")
        if self.is_production and self.sample_catalog_enabled:
            problems.append("生产环境禁止启用开发样板岗位 M1_SAMPLE_CATALOG")
        if self.is_production and self.test_drop_receipt_path:
            problems.append("生产环境禁止使用测试故障注入 M1_TEST_DROP_RECEIPT_PATH")
        if self.session_ttl_days < 1:
            problems.append("SESSION_TTL_DAYS 必须为正")
        if problems:
            raise RuntimeError("配置校验失败: " + "; ".join(problems))

    def ensure_directories(self) -> None:
        self.private_file_root.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.validate_startup()
    return settings


def reload_settings_for_tests() -> None:
    get_settings.cache_clear()


def database_url_from_env() -> str | None:
    return os.environ.get("DATABASE_URL")
