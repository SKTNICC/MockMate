"""与 contracts/m1/m1.schema.json 对应的 Pydantic 模型。

服务端只在这些模型上构造响应；PublicPlan 是显式白名单，禁止把 InternalPlan 直接返回客户端。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr


def _require_non_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


NonBlankStrictStr = Annotated[StrictStr, AfterValidator(_require_non_blank)]


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- 账户


class Credentials(ContractModel):
    username: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_]{3,32}$")
    password: str = Field(min_length=10, max_length=128)


class User(ContractModel):
    id: uuid.UUID
    username: str = Field(min_length=1, pattern=r"^[a-z0-9_]{3,32}$")


class TokenResponse(ContractModel):
    access_token: str = Field(min_length=1)
    token_type: Literal["bearer"] = "bearer"
    expires_at: datetime
    user: User


# --------------------------------------------------------------------------- 材料结构


class Evidence(ContractModel):
    quote: str = Field(min_length=1)
    origin: Literal["extracted", "user_confirmed"]


class Education(ContractModel):
    item_id: str = Field(min_length=1)
    degree: str | None = Field(default=None, min_length=1)
    major: str | None = Field(default=None, min_length=1)
    evidence: Evidence


class Experience(ContractModel):
    item_id: str = Field(min_length=1)
    organization: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    evidence: Evidence


class Project(ContractModel):
    item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    description: str = Field(min_length=1)
    skill_codes: list[str] = Field(default_factory=list)
    evidence: Evidence


class Skill(ContractModel):
    item_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    code: str | None = Field(default=None, min_length=1)
    evidence: Evidence


class Responsibility(ContractModel):
    item_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence: Evidence


class Requirement(ContractModel):
    item_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    skill_codes: list[str] = Field(default_factory=list)
    importance: Literal["must", "nice"]
    evidence: Evidence


class ResumeProfile(ContractModel):
    kind: Literal["resume"]
    education: list[Education] = Field(default_factory=list)
    work_experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[Skill] = Field(default_factory=list)


class JdProfile(ContractModel):
    kind: Literal["jd"]
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    title: str = Field(min_length=1)
    responsibilities: list[Responsibility] = Field(min_length=1)
    requirements: list[Requirement] = Field(min_length=1)


Profile = Annotated[Union[ResumeProfile, JdProfile], Field(discriminator="kind")]


# --------------------------------------------------------------------------- 材料接口


class TextMaterialInput(ContractModel):
    kind: Literal["resume", "jd"]
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=20000)
    template_id: str | None = Field(default=None, min_length=1)
    template_version: int | None = Field(default=None, ge=1)


class FileMaterialInput(ContractModel):
    kind: Literal["resume", "jd"]
    title: str = Field(min_length=1, max_length=120)


class EditMaterialInput(ContractModel):
    revision: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=20000)


class ParseMaterialInput(ContractModel):
    revision: int = Field(ge=1)
    external_processing_consent: bool
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]


class ConfirmMaterialInput(ContractModel):
    revision: int = Field(ge=1)
    profile: Profile


class RevisionInput(ContractModel):
    revision: int = Field(ge=1)


class AiJobInput(ContractModel):
    revision: int = Field(ge=1)
    external_processing_consent: bool


class RetryInput(ContractModel):
    expected_attempt: int = Field(ge=1, le=2)


class MaterialDraft(ContractModel):
    id: uuid.UUID
    revision: int = Field(ge=1)
    kind: Literal["resume", "jd"]
    title: str = Field(min_length=1, max_length=120)
    text: str | None = Field(default=None, min_length=1, max_length=20000)
    profile: Profile | None = None
    state: Literal["uploaded", "text_ready", "parsed"]
    template_id: str | None = None
    template_version: int | None = None
    latest_job_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class Materials(ContractModel):
    items: list[MaterialDraft] = Field(default_factory=list)


class Snapshot(ContractModel):
    id: uuid.UUID
    material_id: uuid.UUID
    source_revision: int = Field(ge=1)
    kind: Literal["resume", "jd"]
    title: str = Field(min_length=1, max_length=120)
    text: str = Field(min_length=1, max_length=20000)
    profile: Profile
    template_id: str | None = None
    template_version: int | None = None
    confirmed_at: datetime


class Snapshots(ContractModel):
    items: list[Snapshot] = Field(default_factory=list)


# --------------------------------------------------------------------------- 岗位与模板


class Role(ContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    levels: list[Literal["intern", "junior"]] = Field(min_length=1)
    enabled: bool
    sample_only: bool


class Roles(ContractModel):
    items: list[Role] = Field(default_factory=list)


class SkillCode(ContractModel):
    code: str = Field(min_length=1)
    label: str = Field(min_length=1)


class SkillCodes(ContractModel):
    items: list[SkillCode] = Field(default_factory=list)


class JdTemplate(ContractModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    title: str = Field(min_length=1)
    label: Literal["内置练习岗位"] = "内置练习岗位"
    text: str = Field(min_length=1, max_length=20000)
    profile: JdProfile


class JdTemplates(ContractModel):
    items: list[JdTemplate] = Field(default_factory=list)


# --------------------------------------------------------------------------- 准备与匹配


class CreatePreparationInput(ContractModel):
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    main_question_count: Literal[2, 4, 6]
    resume_snapshot_id: uuid.UUID | None = None
    jd_snapshot_id: uuid.UUID | None = None


class EditPreparationInput(ContractModel):
    revision: int = Field(ge=1)
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    main_question_count: Literal[2, 4, 6]
    resume_snapshot_id: uuid.UUID | None = None
    jd_snapshot_id: uuid.UUID | None = None


class Preparation(ContractModel):
    id: uuid.UUID
    revision: int = Field(ge=1)
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    main_question_count: Literal[2, 4, 6]
    resume_snapshot_id: uuid.UUID | None = None
    jd_snapshot_id: uuid.UUID | None = None
    match_report_id: uuid.UUID | None = None
    current_plan_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime


class Preparations(ContractModel):
    items: list[Preparation] = Field(default_factory=list)


class Anchor(ContractModel):
    snapshot_id: uuid.UUID
    item_id: str = Field(min_length=1)
    quote: str = Field(min_length=1)


class MatchRow(ContractModel):
    requirement_id: str = Field(min_length=1)
    status: Literal["supported", "to_verify", "not_evidenced"]
    resume_evidence: list[Anchor] = Field(default_factory=list)
    explanation: str = Field(min_length=1)
    suggested_focus: str = Field(min_length=1)


class MatchReport(ContractModel):
    id: uuid.UUID
    preparation_id: uuid.UUID
    input_revision: int = Field(ge=1)
    resume_snapshot_id: uuid.UUID
    jd_snapshot_id: uuid.UUID
    rows: list[MatchRow] = Field(min_length=1)
    created_at: datetime


# --------------------------------------------------------------------------- 题目评价


class KeyPoint(ContractModel):
    point: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)


class RubricDimension(ContractModel):
    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)
    low_anchor: str = Field(min_length=1)
    high_anchor: str = Field(min_length=1)


class Rubric(ContractModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    dimensions: list[RubricDimension] = Field(min_length=1)


class Basis(ContractModel):
    kind: Literal["material", "fact_card", "reasoning"]
    ref_id: str = Field(min_length=1)
    note: str = Field(min_length=1)


class Evaluation(ContractModel):
    goal: str = Field(min_length=1)
    reference_approach: str = Field(min_length=1)
    key_points: list[KeyPoint] = Field(min_length=2)
    acceptable_alternatives: list[str] = Field(default_factory=list)
    common_mistakes: list[str] = Field(default_factory=list)
    rubric: Rubric
    basis: list[Basis] = Field(min_length=1)
    answer_status: Literal["verified", "model_generated"]
    trust: Literal["reviewed", "provisional"]
    uncertainties: list[str] = Field(default_factory=list)


class BankRef(ContractModel):
    question_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    canonical_id: str = Field(min_length=1)
    review_status: Literal["approved"]
    answer_status: Literal["verified"]
    reviewer_ids: list[str] = Field(min_length=2)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    license_status: Literal["clear"]


class FactCard(ContractModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: Literal["draft", "approved"]
    reviewer_ids: list[str] = Field(default_factory=list)
    url: str
    summary: str = Field(min_length=1)
    license_status: Literal["pending_owner_review", "clear"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class BankCandidate(ContractModel):
    question_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    canonical_id: str = Field(min_length=1)
    status: Literal["draft", "review", "approved", "deprecated"]
    answer_status: Literal["model_generated", "verified"]
    title: str = Field(min_length=1)
    role_ids: list[str] = Field(min_length=1)
    levels: list[str] = Field(min_length=1)
    stem: str = Field(min_length=1)
    focus: str = Field(min_length=1)
    topic_key: str = Field(min_length=1)
    skill_codes: list[str] = Field(default_factory=list)
    evaluation: Evaluation
    provenance: Literal["authored"]
    source_urls: list[str] = Field(min_length=1)
    license_status: Literal["pending_owner_review", "clear"]
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class InternalQuestion(ContractModel):
    id: str
    position: int = Field(ge=1, le=6)
    source: Literal["bank", "generated"]
    bank_ref: BankRef | None = None
    question_type: Literal["experience", "knowledge"]
    canonical_id: str = Field(min_length=1)
    stem: str = Field(min_length=8, max_length=500)
    focus: str = Field(min_length=1)
    topic_key: str = Field(min_length=1)
    skill_codes: list[str] = Field(default_factory=list)
    anchors: list[Anchor] = Field(min_length=1)
    evaluation: Evaluation


class SourceCounts(ContractModel):
    bank: int = Field(ge=1, le=3)
    generated: int = Field(ge=1, le=3)


class SemanticReview(ContractModel):
    duplicate_pairs: list[dict] = Field(default_factory=list)
    grounding_issues: list[dict] = Field(default_factory=list)
    uncertain: bool = False
    reviewed_at: datetime


class InternalPlan(ContractModel):
    id: uuid.UUID
    preparation_id: uuid.UUID
    input_revision: int = Field(ge=1)
    revision: int = Field(ge=1)
    state: Literal["ready", "confirmed"]
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    main_question_count: Literal[2, 4, 6]
    source_counts: SourceCounts
    resume_snapshot_id: uuid.UUID
    jd_snapshot_id: uuid.UUID
    focus_areas: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime
    confirmed_at: datetime | None = None
    test_only: bool
    material_snapshots: list[Snapshot] = Field(min_length=2)
    bank_source_snapshots: list[BankCandidate] = Field(min_length=1)
    fact_cards: list[FactCard] = Field(default_factory=list)
    content_reviews: list[ReviewDecision] = Field(default_factory=list)
    questions: list[InternalQuestion] = Field(min_length=2)
    prompt_version: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    semantic_review: SemanticReview


class ReviewDecision(ContractModel):
    resource_type: Literal["bank_question", "fact_card"]
    resource_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    reviewer_id: str = Field(min_length=1)
    decision: Literal["approve", "reject"]
    reviewed_at: datetime
    note: str = Field(min_length=1)


# --------------------------------------------------------------------------- 计划接口


class PublicPlan(ContractModel):
    id: uuid.UUID
    preparation_id: uuid.UUID
    input_revision: int = Field(ge=1)
    revision: int = Field(ge=1)
    state: Literal["ready", "confirmed"]
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    main_question_count: Literal[2, 4, 6]
    source_counts: SourceCounts
    resume_snapshot_id: uuid.UUID
    jd_snapshot_id: uuid.UUID
    focus_areas: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    created_at: datetime
    confirmed_at: datetime | None = None


# --------------------------------------------------------------------------- 任务接口


class ResultRef(ContractModel):
    entity_type: Literal[
        "material", "match_report", "plan", "interview_session", "interview_report",
        "audio_asset", "speech_asset",
    ]
    id: uuid.UUID


class ErrorInfo(ContractModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool
    details: list[str] = Field(default_factory=list)
    request_id: str


class ErrorResponse(ContractModel):
    error: ErrorInfo


class Job(ContractModel):
    id: uuid.UUID
    kind: Literal[
        "extract", "parse", "match", "plan", "follow_up", "interview_report",
        "audio_asr", "audio_tts",
    ]
    target_id: uuid.UUID
    input_revision: int = Field(ge=1)
    state: Literal["queued", "running", "succeeded", "failed", "interrupted", "superseded"]
    attempt: int = Field(ge=1, le=2)
    retryable: bool
    result_ref: ResultRef | None = None
    error: ErrorInfo | None = None
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- 模型内部输出


class MatchRowsOutput(ContractModel):
    rows: list[MatchRow] = Field(min_length=1)


class GeneratedQuestionOutput(ContractModel):
    question_type: Literal["experience", "knowledge"]
    canonical_id: str = Field(min_length=1)
    stem: str = Field(min_length=8, max_length=500)
    focus: str = Field(min_length=1)
    topic_key: str = Field(min_length=1)
    skill_codes: list[str] = Field(default_factory=list)
    anchors: list[Anchor] = Field(min_length=1)
    evaluation: Evaluation


class DuplicatePair(ContractModel):
    left_id: NonBlankStrictStr
    right_id: NonBlankStrictStr
    reason: NonBlankStrictStr


class GroundingIssue(ContractModel):
    question_id: NonBlankStrictStr
    claim: NonBlankStrictStr
    reason: NonBlankStrictStr


class DuplicateReviewOutput(ContractModel):
    duplicate_pairs: list[DuplicatePair]
    grounding_issues: list[GroundingIssue]
    uncertain: StrictBool


# --------------------------------------------------------------------------- M2 文字面试


class StartInterviewInput(ContractModel):
    plan_revision: int = Field(ge=1)
    external_processing_consent: bool
    mode: Literal["text", "voice"] = "text"
    audio_processing_consent: bool = False


class InterviewStartReceipt(ContractModel):
    session_id: uuid.UUID
    revision: int = Field(ge=1)
    mode: Literal["text", "voice"] = "text"


class InterviewAnswerInput(ContractModel):
    revision: int = Field(ge=1)
    turn_id: uuid.UUID
    # 4000 是 Unicode 码点上限；空白字符串另由服务端校验。
    text: str = Field(min_length=1, max_length=4000)
    input_source: Literal["text", "asr", "asr_corrected"] = "text"
    audio_asset_id: uuid.UUID | None = None
    transcript_revision: int | None = Field(default=None, ge=1)


class InterviewSkipInput(ContractModel):
    revision: int = Field(ge=1)
    turn_id: uuid.UUID


class InterviewContinueInput(ContractModel):
    revision: int = Field(ge=1)
    job_id: uuid.UUID


class InterviewBasicReportInput(ContractModel):
    revision: int = Field(ge=1)
    job_id: uuid.UUID


class InterviewReceipt(ContractModel):
    session_id: uuid.UUID
    revision: int = Field(ge=1)
    answer_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None


class InterviewMaterialVersion(ContractModel):
    kind: Literal["resume", "jd"]
    snapshot_id: uuid.UUID
    material_id: uuid.UUID
    title: str = Field(min_length=1, max_length=120)
    source_revision: int = Field(ge=1)


class InterviewPlanSummary(ContractModel):
    plan_id: uuid.UUID
    main_question_count: Literal[2, 4, 6]
    source_counts: SourceCounts
    material_versions: list[InterviewMaterialVersion] = Field(min_length=2)
    sample_only: bool


class InterviewAnswerView(ContractModel):
    answer_id: uuid.UUID
    text: str = Field(min_length=1, max_length=4000)
    submitted_at: datetime
    input_source: Literal["text", "asr", "asr_corrected"] = "text"
    audio_asset_id: uuid.UUID | None = None
    transcript_revision: int | None = Field(default=None, ge=1)


class InterviewTurnView(ContractModel):
    turn_id: uuid.UUID
    question_id: uuid.UUID
    position: int = Field(ge=1, le=6)
    kind: Literal["main", "follow_up"]
    prompt: str = Field(min_length=1)
    status: Literal["pending", "answered", "skipped", "unanswered"]
    answer: InterviewAnswerView | None = None


class InterviewJobSummary(ContractModel):
    id: uuid.UUID
    state: Literal["queued", "running", "succeeded", "failed", "interrupted", "superseded"]
    attempt: int = Field(ge=1, le=2)
    retryable: bool
    error_code: str | None = None


class InterviewDetail(ContractModel):
    session_id: uuid.UUID
    plan_id: uuid.UUID
    preparation_id: uuid.UUID
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    mode: Literal["text", "voice"]
    status: Literal["active", "paused", "ended"]
    phase: Literal["awaiting_answer", "deciding_follow_up", "closed"]
    revision: int = Field(ge=1)
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: Literal["completed", "early_finish"] | None = None
    plan_summary: InterviewPlanSummary
    opening_text: str = Field(min_length=1)
    closing_text: str | None = None
    current_main_position: int | None = Field(default=None, ge=1, le=6)
    answered_main_count: int = Field(ge=0, le=6)
    skipped_main_count: int = Field(ge=0, le=6)
    current_turn: InterviewTurnView | None = None
    allowed_actions: list[
        Literal[
            "answer",
            "skip",
            "pause",
            "resume",
            "finish",
            "continue_without_follow_up",
            "retry_follow_up",
            "retry_report",
            "create_basic_report",
        ]
    ] = Field(default_factory=list)
    transcript: list[InterviewTurnView] = Field(default_factory=list)
    follow_up_job: InterviewJobSummary | None = None
    report_job: InterviewJobSummary | None = None
    report_state: Literal["not_requested", "pending", "failed", "ready"]


class InterviewHistoryItem(ContractModel):
    session_id: uuid.UUID
    plan_id: uuid.UUID
    preparation_id: uuid.UUID
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    mode: Literal["text", "voice"] = "text"
    status: Literal["active", "paused", "ended"]
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: Literal["completed", "early_finish"] | None = None
    main_question_count: int = Field(ge=2, le=6)
    answered_main_count: int = Field(ge=0, le=6)
    report_state: Literal["not_requested", "pending", "failed", "ready"]


class InterviewHistory(ContractModel):
    items: list[InterviewHistoryItem] = Field(default_factory=list)
    next_cursor: str | None = None


class InterviewEvidence(ContractModel):
    answer_id: uuid.UUID
    quote: str = Field(min_length=1, max_length=240)


class InterviewDimensionResult(ContractModel):
    dimension_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    weight: float = Field(gt=0, le=1)
    verdict: Literal["scored", "insufficient_evidence", "uncertain"]
    score: int | None = Field(default=None, ge=0, le=4)
    evidence: list[InterviewEvidence] = Field(default_factory=list)
    explanation: str = ""
    improvement: str = ""


class InterviewReportQuestion(ContractModel):
    question_id: uuid.UUID
    position: int = Field(ge=1, le=6)
    source: Literal["bank", "generated"]
    outcome: Literal["answered", "skipped", "unanswered", "not_reached"]
    prompt: str | None = None
    bank_version: int | None = None
    turns: list[InterviewTurnView] = Field(default_factory=list)
    assessment_trust: Literal["reviewed", "provisional"]
    rubric_version: str = Field(min_length=1)
    question_score: int | None = Field(default=None, ge=0, le=100)
    dimensions: list[InterviewDimensionResult] = Field(default_factory=list)
    follow_up_outcome: Literal["not_needed", "asked", "unavailable", "cancelled"] | None = None


class InterviewCoverage(ContractModel):
    planned: int = Field(ge=0, le=6)
    presented: int = Field(ge=0, le=6)
    answered: int = Field(ge=0, le=6)
    skipped: int = Field(ge=0, le=6)
    unanswered: int = Field(ge=0, le=6)
    not_reached: int = Field(ge=0, le=6)
    scored: int = Field(ge=0, le=6)
    bank_planned: int = Field(ge=0, le=3)
    bank_answered: int = Field(ge=0, le=3)
    generated_planned: int = Field(ge=0, le=3)
    generated_answered: int = Field(ge=0, le=3)
    follow_up_presented: int = Field(ge=0, le=6)
    follow_up_answered: int = Field(ge=0, le=6)


class InterviewFinding(ContractModel):
    question_id: uuid.UUID
    position: int = Field(ge=1, le=6)
    dimension_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence: list[InterviewEvidence] = Field(default_factory=list)


class InterviewReport(ContractModel):
    report_id: uuid.UUID
    session_id: uuid.UUID
    role_id: str = Field(min_length=1)
    level: Literal["intern", "junior"]
    material_versions: list[InterviewMaterialVersion] = Field(min_length=2)
    quality: Literal["full", "partial", "no_answers", "unscored"]
    rubric_policy_version: str = Field(min_length=1)
    created_at: datetime
    end_reason: Literal["completed", "early_finish"]
    coverage: InterviewCoverage
    strengths: list[InterviewFinding] = Field(default_factory=list)
    improvements: list[InterviewFinding] = Field(default_factory=list)
    questions: list[InterviewReportQuestion] = Field(default_factory=list)


# --------------------------------------------------------------------------- M3 语音面试

VoiceFormat = Literal["pcm_s16le/16000/mono"]


class VoiceConfig(ContractModel):
    available: bool
    max_answer_seconds: int = Field(ge=1, le=120)
    format: VoiceFormat
    tts_voice_id: str = Field(min_length=1)
    speech_provider: str = Field(min_length=1)


class VoiceAudioAsset(ContractModel):
    id: uuid.UUID
    session_id: uuid.UUID
    turn_id: uuid.UUID
    take_id: uuid.UUID
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    byte_length: int = Field(ge=2, le=3_840_000)
    duration_ms: int = Field(ge=1, le=120_000)
    format: VoiceFormat
    status: Literal["stored", "expired"]
    expires_at: datetime
    created_at: datetime


class VoiceTranscript(ContractModel):
    audio_asset_id: uuid.UUID
    revision: int = Field(ge=1)
    status: Literal["processing", "ready", "empty", "failed"]
    text: str | None = Field(default=None, max_length=20000)
    provider: str | None = None
    request_id: str | None = None
    error_code: str | None = None
    updated_at: datetime


class VoiceDraft(ContractModel):
    session_id: uuid.UUID
    turn_id: uuid.UUID
    voice_revision: int = Field(ge=0)
    active_audio: VoiceAudioAsset | None = None
    transcript: VoiceTranscript | None = None
    asr_job: InterviewJobSummary | None = None


class TranscribeInput(ContractModel):
    expected_voice_revision: int = Field(ge=0)


class VoiceUploadReceipt(ContractModel):
    session_id: uuid.UUID
    turn_id: uuid.UUID
    voice_revision: int = Field(ge=1)
    audio: VoiceAudioAsset


class SpeechRequestInput(ContractModel):
    segment: Literal["opening", "turn", "closing"]
    turn_id: uuid.UUID | None = None


class SpeechAsset(ContractModel):
    id: uuid.UUID
    session_id: uuid.UUID
    segment: Literal["opening", "turn", "closing"]
    turn_id: uuid.UUID | None = None
    status: Literal["queued", "generating", "ready", "failed", "expired"]
    voice_id: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    format: str = Field(min_length=1)
    duration_ms: int | None = Field(default=None, ge=1)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    job_id: uuid.UUID | None = None
    error_code: str | None = None
    synthetic: bool
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- 模型内部输出（M2）


class FollowUpDecisionOutput(ContractModel):
    decision: Literal["ask", "advance"]
    prompt: StrictStr | None = None
    answer_quote: StrictStr | None = None
    target: StrictStr | None = None


class ScoreDimensionOutput(ContractModel):
    dimension_id: NonBlankStrictStr
    verdict: Literal["scored", "insufficient_evidence", "uncertain"]
    score: StrictInt | None = Field(default=None, ge=0, le=4)
    evidence: list[InterviewEvidence] = Field(default_factory=list)
    explanation: StrictStr = ""
    improvement: StrictStr = ""


class ScoreQuestionOutput(ContractModel):
    question_id: uuid.UUID
    dimensions: list[ScoreDimensionOutput] = Field(min_length=1)


class CachedScoreDimensionItem(ContractModel):
    dimension_id: NonBlankStrictStr
    name: NonBlankStrictStr
    weight: float = Field(gt=0, le=1)
    verdict: Literal["scored", "insufficient_evidence", "uncertain"]
    score: StrictInt | None = Field(default=None, ge=0, le=4)
    evidence: list[InterviewEvidence] = Field(default_factory=list)
    explanation: StrictStr = ""
    improvement: StrictStr = ""


class CachedScoreSlotPayload(ContractModel):
    question_id: uuid.UUID
    content_hash: NonBlankStrictStr
    dimensions: list[CachedScoreDimensionItem] = Field(min_length=1)


InternalPlan.model_rebuild()
