"""add M2 text interview tables and job kinds

Revision ID: a1b2c3d4e5f7
Revises: 9b2c3d4e5f6a
Create Date: 2026-11-17
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "a1b2c3d4e5f7"
down_revision: Union[str, None] = "9b2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind in ('extract','parse','match','plan','follow_up','interview_report')",
    )

    op.create_table(
        "interview_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("plan_id", sa.Uuid(), nullable=False),
        sa.Column("preparation_id", sa.Uuid(), nullable=False),
        sa.Column("plan_revision", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.String(length=120), nullable=False),
        sa.Column("level", sa.String(length=16), nullable=False),
        sa.Column("mode", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("phase", sa.String(length=24), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("opening_text", sa.Text(), nullable=False),
        sa.Column("closing_text", sa.Text(), nullable=True),
        sa.Column(
            "plan_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "external_processing_consented_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("consent_version", sa.String(length=32), nullable=False),
        sa.Column("current_turn_id", sa.Uuid(), nullable=True),
        sa.Column("follow_up_job_id", sa.Uuid(), nullable=True),
        sa.Column("follow_up_input_revision", sa.Integer(), nullable=True),
        sa.Column("report_job_id", sa.Uuid(), nullable=True),
        sa.Column("ended_revision", sa.Integer(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_reason", sa.String(length=24), nullable=True),
        sa.CheckConstraint("mode = 'text'", name="ck_interview_mode"),
        sa.CheckConstraint("level in ('intern','junior')", name="ck_interview_level"),
        sa.CheckConstraint(
            "status in ('active','paused','ended')", name="ck_interview_status"
        ),
        sa.CheckConstraint(
            "phase in ('awaiting_answer','deciding_follow_up','closed')",
            name="ck_interview_phase",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_interview_revision"),
        sa.CheckConstraint(
            "end_reason is null or end_reason in ('completed','early_finish')",
            name="ck_interview_end_reason",
        ),
        sa.CheckConstraint(
            "(status = 'ended') = (ended_at is not null) "
            "and (status = 'ended') = (ended_revision is not null) "
            "and (status = 'ended') = (end_reason is not null) "
            "and (status = 'ended') = (phase = 'closed')",
            name="ck_interview_ended_group",
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_interview_owner", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "plan_id", name="uq_interview_owner_plan"),
    )
    op.create_index(
        "ix_interview_sessions_owner_id",
        "interview_sessions",
        ["owner_id"],
        unique=False,
    )
    op.create_index(
        "ix_interview_owner_started",
        "interview_sessions",
        ["owner_id", "started_at", "id"],
        unique=False,
    )

    op.create_table(
        "interview_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("plan_question_id", sa.String(length=120), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("stem", sa.Text(), nullable=False),
        sa.Column("bank_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("anchors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "evaluation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("follow_up_outcome", sa.String(length=16), nullable=True),
        sa.CheckConstraint(
            "position between 1 and 6", name="ck_interview_question_position"
        ),
        sa.CheckConstraint(
            "source in ('bank','generated')", name="ck_interview_question_source"
        ),
        sa.CheckConstraint(
            "status in ('planned','current','answered','skipped','unanswered','not_reached')",
            name="ck_interview_question_status",
        ),
        sa.CheckConstraint(
            "follow_up_outcome is null or follow_up_outcome in "
            "('not_needed','asked','unavailable','cancelled')",
            name="ck_interview_question_follow_up",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name="fk_interview_question_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "position", name="uq_interview_question_position"
        ),
    )
    op.create_index(
        "ix_interview_questions_session_id",
        "interview_questions",
        ["session_id"],
        unique=False,
    )

    op.create_table(
        "interview_turns",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column(
            "presented_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('main','follow_up')", name="ck_interview_turn_kind"),
        sa.CheckConstraint(
            "status in ('pending','answered','skipped','unanswered')",
            name="ck_interview_turn_status",
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["interview_questions.id"],
            name="fk_interview_turn_question",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("question_id", "kind", name="uq_interview_turn_kind"),
    )
    op.create_index(
        "ix_interview_turns_question_id",
        "interview_turns",
        ["question_id"],
        unique=False,
    )

    op.create_table(
        "interview_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(text) between 1 and 4000", name="ck_interview_answer_len"
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["interview_turns.id"],
            name="fk_interview_answer_turn",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", name="uq_interview_answer_turn"),
    )

    op.create_table(
        "interview_reports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("rubric_policy_version", sa.String(length=64), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=120), nullable=False),
        sa.Column("quality", sa.String(length=24), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "quality in ('full','partial','no_answers','unscored')",
            name="ck_interview_report_quality",
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name="fk_interview_report_session",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_id", name="uq_interview_report_session"),
    )


def downgrade() -> None:
    op.drop_table("interview_reports")
    op.drop_table("interview_answers")
    op.drop_index("ix_interview_turns_question_id", table_name="interview_turns")
    op.drop_table("interview_turns")
    op.drop_index(
        "ix_interview_questions_session_id", table_name="interview_questions"
    )
    op.drop_table("interview_questions")
    op.drop_index("ix_interview_owner_started", table_name="interview_sessions")
    op.drop_index("ix_interview_sessions_owner_id", table_name="interview_sessions")
    op.drop_table("interview_sessions")

    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind", "jobs", "kind in ('extract','parse','match','plan')"
    )
