"""登录速率限制（单进程内存实现；多实例部署时需替换为共享存储）。"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from app.core.config import Settings
from app.core.errors import AppError

_lock = threading.Lock()
_attempts: dict[tuple[str, str], deque[float]] = defaultdict(deque)


def check_login_rate_limit(settings: Settings, username: str, client_ip: str) -> None:
    window = settings.login_rate_limit_window_seconds
    limit = settings.login_rate_limit_attempts
    now = time.monotonic()
    key = (username, client_ip)
    with _lock:
        bucket = _attempts[key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            raise AppError(
                "RATE_LIMITED",
                "登录尝试过于频繁，请稍后再试",
                retryable=True,
                status_code=429,
            )
        bucket.append(now)


def reset_login_rate_limit(username: str, client_ip: str) -> None:
    with _lock:
        _attempts.pop((username, client_ip), None)
