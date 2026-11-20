"""为 M3 隔离模拟器联调准备合成目录/题库（test provider，不接触业务库）。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.cli import seed_catalog, seed_content  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal  # noqa: E402


def main() -> None:
    settings = get_settings()
    if settings.app_env != "test" or not settings.allow_test_fixtures:
        raise SystemExit("拒绝在非隔离 test 环境写入合成数据")
    db = SessionLocal()
    try:
        seed_catalog(db, sample_enabled=True)
        seed_content(db, test_mode=True)
    finally:
        db.close()
    print("seeded synthetic M3 isolated database")


if __name__ == "__main__":
    main()
