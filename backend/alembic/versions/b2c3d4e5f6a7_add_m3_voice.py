"""add M3 voice interview tables, source columns and speech job kinds

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f7
Create Date: 2026-11-20
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind in ('extract','parse','match','plan','follow_up','interview_report','audio_asr','audio_tts')",
    )

    op.add_column(
        "interview_sessions",
        sa.Column("audio_processing_consented_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "interview_sessions",
        sa.Column("audio_consent_version", sa.String(length=32), nullable=True),
    )
    op.drop_constraint("ck_interview_mode", "interview_sessions", type_="check")
    op.create_check_constraint(
        "ck_interview_mode", "interview_sessions", "mode in ('text','voice')"
    )
    op.create_check_constraint(
        "ck_interview_audio_consent",
        "interview_sessions",
        "(mode = 'text') or (audio_processing_consented_at is not null "
        "and audio_consent_version is not null)",
    )

    op.create_table(
        "audio_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("take_id", sa.Uuid(), nullable=False),
        sa.Column("voice_revision", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_length", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("storage_path", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("voice_revision >= 1", name="ck_audio_revision"),
        sa.CheckConstraint("byte_length between 2 and 3840000", name="ck_audio_bytes"),
        sa.CheckConstraint("duration_ms between 1 and 120000", name="ck_audio_duration"),
        sa.CheckConstraint("format = 'pcm_s16le/16000/mono'", name="ck_audio_format"),
        sa.CheckConstraint("status in ('stored','expired')", name="ck_audio_status"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_audio_owner", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name="fk_audio_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["interview_turns.id"], name="fk_audio_turn", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner_id", "take_id", name="uq_audio_owner_take"),
        sa.UniqueConstraint(
            "session_id", "turn_id", "voice_revision", name="uq_audio_turn_revision"
        ),
    )
    op.create_index("ix_audio_assets_owner_id", "audio_assets", ["owner_id"], unique=False)
    op.create_index("ix_audio_assets_session_id", "audio_assets", ["session_id"], unique=False)
    op.create_index("ix_audio_assets_turn_id", "audio_assets", ["turn_id"], unique=False)

    op.create_table(
        "voice_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=False),
        sa.Column("voice_revision", sa.Integer(), nullable=False),
        sa.Column("active_take_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("voice_revision >= 0", name="ck_voice_draft_revision"),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_voice_draft_owner", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name="fk_voice_draft_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"],
            ["interview_turns.id"],
            name="fk_voice_draft_turn",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("turn_id", name="uq_voice_draft_turn"),
    )
    op.create_index("ix_voice_drafts_owner_id", "voice_drafts", ["owner_id"], unique=False)
    op.create_index("ix_voice_drafts_session_id", "voice_drafts", ["session_id"], unique=False)

    op.create_table(
        "audio_transcripts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("audio_asset_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=True),
        sa.Column("request_id", sa.String(length=120), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("revision >= 1", name="ck_transcript_revision"),
        sa.CheckConstraint("status in ('ready','empty','failed')", name="ck_transcript_status"),
        sa.CheckConstraint("char_length(text) <= 20000", name="ck_transcript_len"),
        sa.ForeignKeyConstraint(
            ["audio_asset_id"],
            ["audio_assets.id"],
            name="fk_transcript_asset",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("audio_asset_id", "revision", name="uq_transcript_asset_revision"),
    )
    op.create_index(
        "ix_audio_transcripts_audio_asset_id", "audio_transcripts", ["audio_asset_id"], unique=False
    )

    op.create_table(
        "speech_assets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("owner_id", sa.Uuid(), nullable=False),
        sa.Column("session_id", sa.Uuid(), nullable=False),
        sa.Column("segment", sa.String(length=16), nullable=False),
        sa.Column("segment_key", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.Uuid(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("voice_id", sa.String(length=16), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("adapter_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("storage_path", sa.String(length=255), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("synthetic", sa.Boolean(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.CheckConstraint("segment in ('opening','turn','closing')", name="ck_speech_segment"),
        sa.CheckConstraint(
            "status in ('queued','generating','ready','failed','expired')",
            name="ck_speech_status",
        ),
        sa.CheckConstraint(
            "(segment = 'turn') = (turn_id is not null)", name="ck_speech_turn_group"
        ),
        sa.ForeignKeyConstraint(
            ["owner_id"], ["users.id"], name="fk_speech_owner", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["session_id"],
            ["interview_sessions.id"],
            name="fk_speech_session",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["interview_turns.id"], name="fk_speech_turn", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id",
            "segment_key",
            "voice_id",
            "format",
            "adapter_version",
            "content_hash",
            name="uq_speech_cache_key",
        ),
    )
    op.create_index("ix_speech_assets_owner_id", "speech_assets", ["owner_id"], unique=False)
    op.create_index("ix_speech_assets_session_id", "speech_assets", ["session_id"], unique=False)

    op.add_column(
        "interview_answers",
        sa.Column("input_source", sa.String(length=16), server_default="text", nullable=False),
    )
    op.add_column(
        "interview_answers", sa.Column("audio_asset_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "interview_answers", sa.Column("transcript_revision", sa.Integer(), nullable=True)
    )
    op.create_foreign_key(
        "fk_interview_answer_audio",
        "interview_answers",
        "audio_assets",
        ["audio_asset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_interview_answer_source",
        "interview_answers",
        "input_source in ('text','asr','asr_corrected')",
    )
    op.create_check_constraint(
        "ck_interview_answer_source_refs",
        "interview_answers",
        "(input_source = 'text') = (audio_asset_id is null and transcript_revision is null)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_interview_answer_source_refs", "interview_answers", type_="check")
    op.drop_constraint("ck_interview_answer_source", "interview_answers", type_="check")
    op.drop_constraint("fk_interview_answer_audio", "interview_answers", type_="foreignkey")
    op.drop_column("interview_answers", "transcript_revision")
    op.drop_column("interview_answers", "audio_asset_id")
    op.drop_column("interview_answers", "input_source")

    op.drop_index("ix_speech_assets_session_id", table_name="speech_assets")
    op.drop_index("ix_speech_assets_owner_id", table_name="speech_assets")
    op.drop_table("speech_assets")
    op.drop_index(
        "ix_audio_transcripts_audio_asset_id", table_name="audio_transcripts"
    )
    op.drop_table("audio_transcripts")
    op.drop_index("ix_voice_drafts_session_id", table_name="voice_drafts")
    op.drop_index("ix_voice_drafts_owner_id", table_name="voice_drafts")
    op.drop_table("voice_drafts")
    op.drop_index("ix_audio_assets_turn_id", table_name="audio_assets")
    op.drop_index("ix_audio_assets_session_id", table_name="audio_assets")
    op.drop_index("ix_audio_assets_owner_id", table_name="audio_assets")
    op.drop_table("audio_assets")

    op.drop_constraint("ck_interview_audio_consent", "interview_sessions", type_="check")
    op.drop_constraint("ck_interview_mode", "interview_sessions", type_="check")
    op.create_check_constraint("ck_interview_mode", "interview_sessions", "mode = 'text'")
    op.drop_column("interview_sessions", "audio_consent_version")
    op.drop_column("interview_sessions", "audio_processing_consented_at")

    op.drop_constraint("ck_job_kind", "jobs", type_="check")
    op.create_check_constraint(
        "ck_job_kind",
        "jobs",
        "kind in ('extract','parse','match','plan','follow_up','interview_report')",
    )
