package com.mockmate.app.core.network

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.JsonObject

// 字段名与 contracts/m1/openapi.json 一致；多余字段由 Json 配置忽略。

@Serializable
data class ErrorInfo(
    val code: String,
    val message: String,
    val retryable: Boolean = false,
    val details: List<String> = emptyList(),
    val request_id: String = "",
)

@Serializable
data class ErrorEnvelope(val error: ErrorInfo)

@Serializable
data class Credentials(val username: String, val password: String)

@Serializable
data class UserDto(val id: String, val username: String)

@Serializable
data class TokenResponse(
    val access_token: String,
    val token_type: String,
    val expires_at: String,
    val user: UserDto,
)

@Serializable
data class RoleDto(
    val id: String,
    val name: String,
    val levels: List<String>,
    val enabled: Boolean,
    val sample_only: Boolean,
)

@Serializable
data class RolesResponse(val items: List<RoleDto>)

@Serializable
data class SkillCodeDto(
    val code: String,
    val label: String,
)

@Serializable
data class SkillCodesResponse(val items: List<SkillCodeDto>)

@Serializable
data class EvidenceDto(val quote: String, val origin: String = "extracted")

@Serializable
data class AnchorDto(
    val snapshot_id: String,
    val item_id: String,
    val quote: String,
)

@Serializable
data class ResponsibilityDto(
    val item_id: String,
    val text: String,
    val evidence: EvidenceDto,
)

@Serializable
data class RequirementDto(
    val item_id: String,
    val text: String,
    val skill_codes: List<String> = emptyList(),
    val importance: String = "must",
    val evidence: EvidenceDto,
)

@Serializable
data class JdProfileDto(
    val kind: String = "jd",
    val role_id: String,
    val level: String,
    val title: String,
    val responsibilities: List<ResponsibilityDto> = emptyList(),
    val requirements: List<RequirementDto> = emptyList(),
)

@Serializable
data class JdTemplateDto(
    val id: String,
    val version: Int,
    val role_id: String,
    val level: String,
    val title: String,
    val label: String = "内置练习岗位",
    val text: String,
    val profile: JdProfileDto,
)

@Serializable
data class JdTemplatesResponse(val items: List<JdTemplateDto>)

@Serializable
data class MaterialDraftDto(
    val id: String,
    val revision: Int,
    val kind: String,
    val title: String,
    val text: String? = null,
    val profile: JsonObject? = null,
    val state: String,
    val template_id: String? = null,
    val template_version: Int? = null,
    val latest_job_id: String? = null,
    val created_at: String,
    val updated_at: String,
)

@Serializable
data class MaterialsResponse(val items: List<MaterialDraftDto>)

@Serializable
data class SnapshotDto(
    val id: String,
    val material_id: String,
    val source_revision: Int,
    val kind: String,
    val title: String,
    val text: String,
    val profile: JsonObject,
    val template_id: String? = null,
    val template_version: Int? = null,
    val confirmed_at: String,
)

@Serializable
data class SnapshotsResponse(val items: List<SnapshotDto>)

@Serializable
data class PreparationDto(
    val id: String,
    val revision: Int,
    val role_id: String,
    val level: String,
    val main_question_count: Int,
    val resume_snapshot_id: String? = null,
    val jd_snapshot_id: String? = null,
    val match_report_id: String? = null,
    val current_plan_id: String? = null,
    val created_at: String,
    val updated_at: String,
)

@Serializable
data class PreparationsResponse(val items: List<PreparationDto>)

@Serializable
data class CreatePreparationRequest(
    val role_id: String,
    val level: String,
    val main_question_count: Int,
    val resume_snapshot_id: String? = null,
    val jd_snapshot_id: String? = null,
)

@Serializable
data class EditPreparationRequest(
    val revision: Int,
    val role_id: String,
    val level: String,
    val main_question_count: Int,
    val resume_snapshot_id: String? = null,
    val jd_snapshot_id: String? = null,
)

@Serializable
data class RevisionRequest(val revision: Int)

@Serializable
data class AiJobRequest(val revision: Int, val external_processing_consent: Boolean)

@Serializable
data class ParseRequest(
    val revision: Int,
    val external_processing_consent: Boolean,
    val role_id: String,
    val level: String,
)

@Serializable
data class ConfirmMaterialRequest(val revision: Int, val profile: JsonObject)

@Serializable
data class TextMaterialRequest(
    val kind: String,
    val title: String,
    val text: String,
    val template_id: String? = null,
    val template_version: Int? = null,
)

@Serializable
data class EditMaterialRequest(
    val revision: Int,
    val title: String,
    val text: String,
)

@Serializable
data class RetryRequest(val expected_attempt: Int)

@Serializable
data class MatchRowDto(
    val requirement_id: String,
    val status: String,
    val resume_evidence: List<AnchorDto> = emptyList(),
    val explanation: String,
    val suggested_focus: String,
)

@Serializable
data class MatchReportDto(
    val id: String,
    val preparation_id: String,
    val input_revision: Int,
    val resume_snapshot_id: String,
    val jd_snapshot_id: String,
    val rows: List<MatchRowDto>,
    val created_at: String,
)

@Serializable
data class ResultRefDto(val entity_type: String, val id: String)

@Serializable
data class JobDto(
    val id: String,
    val kind: String,
    val target_id: String,
    val input_revision: Int,
    val state: String,
    val attempt: Int,
    val retryable: Boolean = false,
    val result_ref: ResultRefDto? = null,
    val error: ErrorInfo? = null,
    val created_at: String,
    val updated_at: String,
)

@Serializable
data class SourceCountsDto(val bank: Int, val generated: Int)

@Serializable
data class PublicPlanDto(
    val id: String,
    val preparation_id: String,
    val input_revision: Int,
    val revision: Int,
    val state: String,
    val role_id: String,
    val level: String,
    val main_question_count: Int,
    val source_counts: SourceCountsDto,
    val resume_snapshot_id: String,
    val jd_snapshot_id: String,
    val focus_areas: List<String> = emptyList(),
    val limitations: List<String> = emptyList(),
    val created_at: String,
    val confirmed_at: String? = null,
)

// ------------------------------------------------------------------ M2 文字面试

@Serializable
data class StartInterviewRequest(
    val plan_revision: Int,
    val external_processing_consent: Boolean,
    val mode: String = "text",
    val audio_processing_consent: Boolean = false,
)

@Serializable
data class InterviewStartReceiptDto(
    val session_id: String,
    val revision: Int,
    val mode: String = "text",
)

@Serializable
data class InterviewAnswerRequest(
    val revision: Int,
    val turn_id: String,
    val text: String,
    val input_source: String = "text",
    val audio_asset_id: String? = null,
    val transcript_revision: Int? = null,
)

@Serializable
data class InterviewSkipRequest(val revision: Int, val turn_id: String)

@Serializable
data class InterviewContinueRequest(val revision: Int, val job_id: String)

@Serializable
data class InterviewBasicReportRequest(val revision: Int, val job_id: String)

@Serializable
data class InterviewReceiptDto(
    val session_id: String,
    val revision: Int,
    val answer_id: String? = null,
    val job_id: String? = null,
)

@Serializable
data class InterviewMaterialVersionDto(
    val kind: String,
    val snapshot_id: String,
    val material_id: String,
    val title: String,
    val source_revision: Int,
)

@Serializable
data class InterviewPlanSummaryDto(
    val plan_id: String,
    val main_question_count: Int,
    val source_counts: SourceCountsDto,
    val material_versions: List<InterviewMaterialVersionDto> = emptyList(),
    val sample_only: Boolean = false,
)

@Serializable
data class InterviewAnswerDto(
    val answer_id: String,
    val text: String,
    val submitted_at: String,
    val input_source: String = "text",
    val audio_asset_id: String? = null,
    val transcript_revision: Int? = null,
)

@Serializable
data class InterviewTurnDto(
    val turn_id: String,
    val question_id: String,
    val position: Int,
    val kind: String,
    val prompt: String,
    val status: String,
    val answer: InterviewAnswerDto? = null,
)

@Serializable
data class InterviewJobSummaryDto(
    val id: String,
    val state: String,
    val attempt: Int,
    val retryable: Boolean = false,
    val error_code: String? = null,
)

@Serializable
data class InterviewDetailDto(
    val session_id: String,
    val plan_id: String,
    val preparation_id: String,
    val role_id: String,
    val level: String,
    val mode: String,
    val status: String,
    val phase: String,
    val revision: Int,
    val started_at: String,
    val ended_at: String? = null,
    val end_reason: String? = null,
    val plan_summary: InterviewPlanSummaryDto,
    val opening_text: String,
    val closing_text: String? = null,
    val current_main_position: Int? = null,
    val answered_main_count: Int = 0,
    val skipped_main_count: Int = 0,
    val current_turn: InterviewTurnDto? = null,
    val allowed_actions: List<String> = emptyList(),
    val transcript: List<InterviewTurnDto> = emptyList(),
    val follow_up_job: InterviewJobSummaryDto? = null,
    val report_job: InterviewJobSummaryDto? = null,
    val report_state: String = "not_requested",
)

@Serializable
data class InterviewHistoryItemDto(
    val session_id: String,
    val plan_id: String,
    val preparation_id: String,
    val role_id: String,
    val level: String,
    val mode: String = "text",
    val status: String,
    val started_at: String,
    val ended_at: String? = null,
    val end_reason: String? = null,
    val main_question_count: Int,
    val answered_main_count: Int,
    val report_state: String,
)

@Serializable
data class InterviewHistoryDto(
    val items: List<InterviewHistoryItemDto> = emptyList(),
    val next_cursor: String? = null,
)

@Serializable
data class InterviewEvidenceDto(val answer_id: String, val quote: String)

@Serializable
data class InterviewDimensionResultDto(
    val dimension_id: String,
    val name: String,
    val weight: Double,
    val verdict: String,
    val score: Int? = null,
    val evidence: List<InterviewEvidenceDto> = emptyList(),
    val explanation: String = "",
    val improvement: String = "",
)

@Serializable
data class InterviewReportQuestionDto(
    val question_id: String,
    val position: Int,
    val source: String,
    val outcome: String,
    val prompt: String? = null,
    val bank_version: Int? = null,
    val turns: List<InterviewTurnDto> = emptyList(),
    val assessment_trust: String,
    val rubric_version: String,
    val question_score: Int? = null,
    val dimensions: List<InterviewDimensionResultDto> = emptyList(),
    val follow_up_outcome: String? = null,
)

@Serializable
data class InterviewCoverageDto(
    val planned: Int,
    val presented: Int,
    val answered: Int,
    val skipped: Int,
    val unanswered: Int,
    val not_reached: Int,
    val scored: Int,
    val bank_planned: Int,
    val bank_answered: Int,
    val generated_planned: Int,
    val generated_answered: Int,
    val follow_up_presented: Int,
    val follow_up_answered: Int,
)

@Serializable
data class InterviewFindingDto(
    val question_id: String,
    val position: Int,
    val dimension_id: String,
    val text: String,
    val evidence: List<InterviewEvidenceDto> = emptyList(),
)

@Serializable
data class InterviewReportDto(
    val report_id: String,
    val session_id: String,
    val role_id: String,
    val level: String,
    val material_versions: List<InterviewMaterialVersionDto> = emptyList(),
    val quality: String,
    val rubric_policy_version: String,
    val created_at: String,
    val end_reason: String,
    val coverage: InterviewCoverageDto,
    val strengths: List<InterviewFindingDto> = emptyList(),
    val improvements: List<InterviewFindingDto> = emptyList(),
    val questions: List<InterviewReportQuestionDto> = emptyList(),
)

// ------------------------------------------------------------------ M3 语音面试

@Serializable
data class VoiceConfigDto(
    val available: Boolean,
    val max_answer_seconds: Int,
    val format: String,
    val tts_voice_id: String,
    val speech_provider: String,
)

@Serializable
data class VoiceAudioAssetDto(
    val id: String,
    val session_id: String,
    val turn_id: String,
    val take_id: String,
    val sha256: String,
    val byte_length: Int,
    val duration_ms: Int,
    val format: String,
    val status: String,
    val expires_at: String,
    val created_at: String,
)

@Serializable
data class VoiceTranscriptDto(
    val audio_asset_id: String,
    val revision: Int,
    val status: String,
    val text: String? = null,
    val provider: String? = null,
    val request_id: String? = null,
    val error_code: String? = null,
    val updated_at: String,
)

@Serializable
data class VoiceDraftDto(
    val session_id: String,
    val turn_id: String,
    val voice_revision: Int,
    val active_audio: VoiceAudioAssetDto? = null,
    val transcript: VoiceTranscriptDto? = null,
    val asr_job: InterviewJobSummaryDto? = null,
)

@Serializable
data class VoiceUploadReceiptDto(
    val session_id: String,
    val turn_id: String,
    val voice_revision: Int,
    val audio: VoiceAudioAssetDto,
)

@Serializable
data class TranscribeRequest(val expected_voice_revision: Int)

@Serializable
data class SpeechRequest(
    val segment: String,
    val turn_id: String? = null,
)

@Serializable
data class SpeechAssetDto(
    val id: String,
    val session_id: String,
    val segment: String,
    val turn_id: String? = null,
    val status: String,
    val voice_id: String,
    val provider: String,
    val format: String,
    val duration_ms: Int? = null,
    val sha256: String? = null,
    val job_id: String? = null,
    val error_code: String? = null,
    val synthetic: Boolean = false,
    val created_at: String,
    val updated_at: String,
)
