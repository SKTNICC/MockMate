"""add private AI call records to jobs

Revision ID: 9b2c3d4e5f6a
Revises: 8a1f2c3d4e5f
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "9b2c3d4e5f6a"
down_revision: Union[str, None] = "8a1f2c3d4e5f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "ai_call_records",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "ai_call_records")
