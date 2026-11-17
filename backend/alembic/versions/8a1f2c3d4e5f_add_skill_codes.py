"""add enabled skill code vocabulary

Revision ID: 8a1f2c3d4e5f
Revises: 7cb306f2df86
Create Date: 2026-11-16
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "8a1f2c3d4e5f"
down_revision: Union[str, None] = "7cb306f2df86"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "skill_codes",
        sa.Column("code", sa.String(length=120), nullable=False),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )


def downgrade() -> None:
    op.drop_table("skill_codes")
