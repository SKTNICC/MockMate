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
