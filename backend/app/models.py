"""PostgreSQL 表定义（SQLAlchemy 2.0 声明式）。

规则：owner／状态／版本／唯一键单独成列，复杂结构放 JSONB；跨行配额在服务层事务内校验。
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username_normalized: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("char_length(username_normalized) between 3 and 32", name="ck_users_username_len"),
    )


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MaterialDraft(Base):
    __tablename__ = "material_drafts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="text_ready")
    file_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    template_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latest_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("kind in ('resume','jd')", name="ck_material_kind"),
        CheckConstraint("state in ('uploaded','text_ready','parsed')", name="ck_material_state"),
        CheckConstraint("revision >= 1", name="ck_material_revision"),
    )


class MaterialSnapshot(Base):
    __tablename__ = "material_snapshots"

    id: Mapped[uuid.UUID] = _uuid_pk()
    material_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("material_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    template_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    confirmed_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("material_id", "source_revision", name="uq_snapshot_material_revision"),
        CheckConstraint("kind in ('resume','jd')", name="ck_snapshot_kind"),
    )


class Preparation(Base):
    __tablename__ = "preparations"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    role_id: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    main_question_count: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("material_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    jd_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("material_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    # 循环指针不加外键，由服务层事务内校验；删除 preparation 时显式清理。
    match_report_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    current_plan_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("main_question_count in (2,4,6)", name="ck_prep_count"),
        CheckConstraint("level in ('intern','junior')", name="ck_prep_level"),
        CheckConstraint("revision >= 1", name="ck_prep_revision"),
    )


class MatchReport(Base):
    __tablename__ = "match_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    resume_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    jd_snapshot_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("preparation_id", "input_revision", name="uq_match_prep_revision"),
    )


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    preparation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("preparations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="ready")
    internal_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("preparation_id", "input_revision", name="uq_plan_prep_revision"),
        CheckConstraint("state in ('ready','confirmed')", name="ck_plan_state"),
        CheckConstraint("revision >= 1", name="ck_plan_revision"),
    )


class PlanItem(Base):
    __tablename__ = "plan_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    plan_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("plans.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    question_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    canonical_id: Mapped[str] = mapped_column(String(120), nullable=False)
    normalized_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    __table_args__ = (
        UniqueConstraint("plan_id", "position", name="uq_plan_item_position"),
        UniqueConstraint("plan_id", "canonical_id", name="uq_plan_item_canonical"),
        UniqueConstraint("plan_id", "normalized_hash", name="uq_plan_item_hash"),
        CheckConstraint("source in ('bank','generated')", name="ck_plan_item_source"),
    )


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    input_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="queued", index=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Job 私有调用记录：stage/slot/attempt、provider request ID、model、usage、已知/未知结果。
    # 不保存 Key、Prompt 或材料正文。
    ai_call_records: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("target_id", "kind", "input_revision", name="uq_job_target_kind_revision"),
        CheckConstraint(
            "kind in ('extract','parse','match','plan','follow_up','interview_report','audio_asr','audio_tts')",
            name="ck_job_kind",
        ),
        CheckConstraint(
            "state in ('queued','running','succeeded','failed','interrupted','superseded')",
            name="ck_job_state",
        ),
        CheckConstraint("attempt between 1 and 2", name="ck_job_attempt"),
        Index("ix_jobs_claim", "state", "created_at"),
    )


class JobWorkItem(Base):
    __tablename__ = "job_work_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    slot_key: Mapped[str] = mapped_column(String(120), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("job_id", "slot_key", name="uq_work_item_slot"),)


class BankQuestion(Base):
    __tablename__ = "bank_questions"

    question_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_id: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    role_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    levels: Mapped[list] = mapped_column(JSONB, nullable=False)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    source_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    test_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "status in ('draft','review','approved','deprecated')", name="ck_bank_status"
        ),
    )


class ContentReview(Base):
    __tablename__ = "content_reviews"

    id: Mapped[uuid.UUID] = _uuid_pk()
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(120), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reviewer_id: Mapped[str] = mapped_column(String(120), nullable=False)
    reviewer_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="human")
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint(
            "resource_type",
            "resource_id",
            "version",
            "reviewer_id",
            "content_sha256",
            name="uq_review_resource_reviewer_hash",
        ),
        CheckConstraint("resource_type in ('bank_question','fact_card')", name="ck_review_type"),
        CheckConstraint("decision in ('approve','reject')", name="ck_review_decision"),
        CheckConstraint("reviewer_kind in ('human','synthetic_test')", name="ck_review_kind"),
        Index("ix_reviews_resource", "resource_type", "resource_id", "version"),
    )


class FactCard(Base):
    __tablename__ = "fact_cards"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    test_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (CheckConstraint("status in ('draft','approved')", name="ck_fact_status"),)


class SkillCode(Base):
    __tablename__ = "skill_codes"

    code: Mapped[str] = mapped_column(String(120), primary_key=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    levels: Mapped[list] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sample_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class JdTemplate(Base):
    __tablename__ = "jd_templates"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_id: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    profile: Mapped[dict] = mapped_column(JSONB, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("user_id", "method", "path", "key", name="uq_idem_user_method_path_key"),
    )


# --------------------------------------------------------------------------- M2 文字面试


class InterviewSession(Base):
    __tablename__ = "interview_sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 历史关联值：M1 删除准备会删除 Plan，旧场次仍凭冻结快照保留。
    plan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    preparation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    plan_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    role_id: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[str] = mapped_column(String(16), nullable=False)
    mode: Mapped[str] = mapped_column(String(8), nullable=False, default="text")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    phase: Mapped[str] = mapped_column(String(24), nullable=False, default="awaiting_answer")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    opening_text: Mapped[str] = mapped_column(Text, nullable=False)
    closing_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    plan_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    external_processing_consented_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    consent_version: Mapped[str] = mapped_column(String(32), nullable=False)
    audio_processing_consented_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    audio_consent_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 循环指针不加外键，由服务层事务内校验属于本场且符合阶段。
    current_turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    follow_up_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    follow_up_input_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    ended_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    started_at: Mapped[datetime] = _created_at()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)

    __table_args__ = (
        UniqueConstraint("owner_id", "plan_id", name="uq_interview_owner_plan"),
        Index(
            "ix_interview_owner_started",
            "owner_id",
            "started_at",
            "id",
        ),
        CheckConstraint("mode in ('text','voice')", name="ck_interview_mode"),
        CheckConstraint("level in ('intern','junior')", name="ck_interview_level"),
        CheckConstraint("status in ('active','paused','ended')", name="ck_interview_status"),
        CheckConstraint(
            "(mode = 'text') or (audio_processing_consented_at is not null "
            "and audio_consent_version is not null)",
            name="ck_interview_audio_consent",
        ),
        CheckConstraint(
            "phase in ('awaiting_answer','deciding_follow_up','closed')",
            name="ck_interview_phase",
        ),
        CheckConstraint("revision >= 1", name="ck_interview_revision"),
        CheckConstraint(
            "end_reason is null or end_reason in ('completed','early_finish')",
            name="ck_interview_end_reason",
        ),
        CheckConstraint(
            "(status = 'ended') = (ended_at is not null) "
            "and (status = 'ended') = (ended_revision is not null) "
            "and (status = 'ended') = (end_reason is not null) "
            "and (status = 'ended') = (phase = 'closed')",
            name="ck_interview_ended_group",
        ),
    )


class InterviewQuestion(Base):
    __tablename__ = "interview_questions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_question_id: Mapped[str] = mapped_column(String(120), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    stem: Mapped[str] = mapped_column(Text, nullable=False)
    bank_ref: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    anchors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    evaluation: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned")
    follow_up_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)

    __table_args__ = (
        UniqueConstraint("session_id", "position", name="uq_interview_question_position"),
        CheckConstraint("position between 1 and 6", name="ck_interview_question_position"),
        CheckConstraint("source in ('bank','generated')", name="ck_interview_question_source"),
        CheckConstraint(
            "status in ('planned','current','answered','skipped','unanswered','not_reached')",
            name="ck_interview_question_status",
        ),
        CheckConstraint(
            "follow_up_outcome is null or follow_up_outcome in "
            "('not_needed','asked','unavailable','cancelled')",
            name="ck_interview_question_follow_up",
        ),
    )


class InterviewTurn(Base):
    __tablename__ = "interview_turns"

    id: Mapped[uuid.UUID] = _uuid_pk()
    question_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_questions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    presented_at: Mapped[datetime] = _created_at()
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("question_id", "kind", name="uq_interview_turn_kind"),
        CheckConstraint("kind in ('main','follow_up')", name="ck_interview_turn_kind"),
        CheckConstraint(
            "status in ('pending','answered','skipped','unanswered')",
            name="ck_interview_turn_status",
        ),
    )


class InterviewAnswer(Base):
    __tablename__ = "interview_answers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_turns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    text: Mapped[str] = mapped_column(Text, nullable=False)
    input_source: Mapped[str] = mapped_column(String(16), nullable=False, default="text")
    audio_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("audio_assets.id", ondelete="SET NULL"), nullable=True
    )
    transcript_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    submitted_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("char_length(text) between 1 and 4000", name="ck_interview_answer_len"),
        CheckConstraint(
            "input_source in ('text','asr','asr_corrected')", name="ck_interview_answer_source"
        ),
        CheckConstraint(
            "(input_source = 'text') = (audio_asset_id is null and transcript_revision is null)",
            name="ck_interview_answer_source_refs",
        ),
    )


class VoiceDraft(Base):
    __tablename__ = "voice_drafts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_turns.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    voice_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    active_take_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("voice_revision >= 0", name="ck_voice_draft_revision"),
    )


class AudioAsset(Base):
    __tablename__ = "audio_assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    turn_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_turns.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    take_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    voice_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_length: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="stored")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("owner_id", "take_id", name="uq_audio_owner_take"),
        UniqueConstraint("session_id", "turn_id", "voice_revision", name="uq_audio_turn_revision"),
        CheckConstraint("voice_revision >= 1", name="ck_audio_revision"),
        CheckConstraint("byte_length between 2 and 3840000", name="ck_audio_bytes"),
        CheckConstraint("duration_ms between 1 and 120000", name="ck_audio_duration"),
        CheckConstraint("format = 'pcm_s16le/16000/mono'", name="ck_audio_format"),
        CheckConstraint("status in ('stored','expired')", name="ck_audio_status"),
    )


class AudioTranscript(Base):
    __tablename__ = "audio_transcripts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    audio_asset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("audio_assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("audio_asset_id", "revision", name="uq_transcript_asset_revision"),
        CheckConstraint("revision >= 1", name="ck_transcript_revision"),
        CheckConstraint("status in ('ready','empty','failed')", name="ck_transcript_status"),
        CheckConstraint("char_length(text) <= 20000", name="ck_transcript_len"),
    )


class SpeechAsset(Base):
    __tablename__ = "speech_assets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    segment: Mapped[str] = mapped_column(String(16), nullable=False)
    segment_key: Mapped[str] = mapped_column(String(64), nullable=False)
    turn_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("interview_turns.id", ondelete="CASCADE"), nullable=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    voice_id: Mapped[str] = mapped_column(String(16), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    format: Mapped[str] = mapped_column(String(32), nullable=False)
    adapter_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="queued")
    storage_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    job_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    synthetic: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint(
            "session_id",
            "segment_key",
            "provider",
            "voice_id",
            "format",
            "adapter_version",
            "content_hash",
            name="uq_speech_cache_key",
        ),
        CheckConstraint("segment in ('opening','turn','closing')", name="ck_speech_segment"),
        CheckConstraint(
            "status in ('queued','generating','ready','failed','expired')",
            name="ck_speech_status",
        ),
        CheckConstraint(
            "(segment = 'turn') = (turn_id is not null)", name="ck_speech_turn_group"
        ),
    )


class InterviewReport(Base):
    __tablename__ = "interview_reports"

    id: Mapped[uuid.UUID] = _uuid_pk()
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("interview_sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    rubric_policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(64), nullable=False)
    model_id: Mapped[str] = mapped_column(String(120), nullable=False)
    quality: Mapped[str] = mapped_column(String(24), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (
        CheckConstraint(
            "quality in ('full','partial','no_answers','unscored')",
            name="ck_interview_report_quality",
        ),
    )
