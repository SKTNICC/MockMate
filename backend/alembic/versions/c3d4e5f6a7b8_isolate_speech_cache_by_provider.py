"""isolate speech cache by provider

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-11-20
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 已有 speech_assets.provider 如实保存 test/deepseek；把 provider 纳入唯一缓存键，
    # 旧数据无需改写即可继续按原 provider 命中。
    op.drop_constraint("uq_speech_cache_key", "speech_assets", type_="unique")
    op.create_unique_constraint(
        "uq_speech_cache_key",
        "speech_assets",
        [
            "session_id",
            "segment_key",
            "provider",
            "voice_id",
            "format",
            "adapter_version",
            "content_hash",
        ],
    )


def downgrade() -> None:
    op.drop_constraint("uq_speech_cache_key", "speech_assets", type_="unique")
    op.create_unique_constraint(
        "uq_speech_cache_key",
        "speech_assets",
        [
            "session_id",
            "segment_key",
            "voice_id",
            "format",
            "adapter_version",
            "content_hash",
        ],
    )
