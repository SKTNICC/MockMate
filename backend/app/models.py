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
        CheckConstraint("kind in ('extract','parse','match','plan')", name="ck_job_kind"),
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
