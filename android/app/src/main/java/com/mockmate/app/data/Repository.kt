package com.mockmate.app.data

import com.mockmate.app.core.network.AiJobRequest
import com.mockmate.app.core.network.ApiException
import com.mockmate.app.core.network.ConfirmMaterialRequest
import com.mockmate.app.core.network.CreatePreparationRequest
import com.mockmate.app.core.network.EditMaterialRequest
import com.mockmate.app.core.network.EditPreparationRequest
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.MockMateApi
import com.mockmate.app.core.network.NetworkException
import com.mockmate.app.core.network.ParseRequest
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.RoleDto
import com.mockmate.app.core.network.SkillCodeDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.TextMaterialRequest
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.network.InterviewAnswerRequest
import com.mockmate.app.core.network.InterviewBasicReportRequest
import com.mockmate.app.core.network.InterviewContinueRequest
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewHistoryDto
import com.mockmate.app.core.network.InterviewReceiptDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewSkipRequest
import com.mockmate.app.core.network.InterviewStartReceiptDto
import com.mockmate.app.core.network.StartInterviewRequest
import com.mockmate.app.core.network.SpeechAssetDto
import com.mockmate.app.core.network.SpeechRequest
import com.mockmate.app.core.network.VoiceConfigDto
import com.mockmate.app.core.network.VoiceDraftDto
import com.mockmate.app.core.network.VoiceTranscriptDto
import com.mockmate.app.core.network.VoiceUploadReceiptDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.serialization.json.JsonObject

sealed interface ApiResult<out T> {
    data class Ok<T>(val value: T) : ApiResult<T>
    data class Err(
        val code: String?,
        val message: String,
        val retryable: Boolean,
        val details: List<String> = emptyList(),
        val httpStatus: Int? = null,
        /** 请求是否已发出。连接前失败为 false，可按确定失败处理。 */
        val requestSent: Boolean = true,
    ) : ApiResult<Nothing>
}

/**
 * 只调用真实 HTTP 契约；不内置 fixture，不在客户端伪造成功结果。
 */
class M1Repository(private val api: MockMateApi) {

    private suspend fun <T> call(block: suspend () -> T): ApiResult<T> =
        try {
            ApiResult.Ok(block())
        } catch (e: CancellationException) {
            throw e
        } catch (e: ApiException) {
            ApiResult.Err(e.code, e.message, e.retryable, e.details, e.httpStatus, e.requestSent)
        } catch (e: NetworkException) {
            ApiResult.Err(
                "NETWORK_UNAVAILABLE",
                e.message ?: "网络不可达",
                true,
                requestSent = e.requestSent,
            )
        } catch (e: Exception) {
            ApiResult.Err("CLIENT_ERROR", e.message ?: "本地请求失败", false)
        }

    suspend fun register(username: String, password: String): ApiResult<UserDto> =
        call { api.register(username, password) }

    suspend fun login(username: String, password: String): ApiResult<com.mockmate.app.core.network.TokenResponse> =
        call { api.login(username, password) }

    suspend fun logout(explicitToken: String? = null): ApiResult<Unit> =
        call { api.logout(explicitToken) }

    suspend fun me(): ApiResult<UserDto> = call { api.me() }

    suspend fun roles(): ApiResult<List<RoleDto>> = call { api.roles().items }

    suspend fun skillCodes(): ApiResult<List<SkillCodeDto>> = call { api.skillCodes().items }

    suspend fun jdTemplates(): ApiResult<List<com.mockmate.app.core.network.JdTemplateDto>> =
        call { api.jdTemplates().items }

    suspend fun materials(): ApiResult<List<MaterialDraftDto>> = call { api.materials().items }

    suspend fun material(id: String): ApiResult<MaterialDraftDto> = call { api.material(id) }

    suspend fun createTextMaterial(
        kind: String,
        title: String,
        text: String,
        templateId: String? = null,
        templateVersion: Int? = null,
        operationKey: String,
    ): ApiResult<MaterialDraftDto> = call {
        api.createTextMaterial(
            TextMaterialRequest(kind, title, text, templateId, templateVersion),
            operationKey,
        )
    }

    suspend fun uploadFile(
        kind: String,
        title: String,
        fileName: String,
        mimeType: String,
        bytes: ByteArray,
        operationKey: String,
    ): ApiResult<MaterialDraftDto> = call {
        api.uploadFile(kind, title, fileName, mimeType, bytes, operationKey)
    }

    suspend fun editMaterial(
        id: String,
        revision: Int,
        title: String,
        text: String,
        operationKey: String,
    ): ApiResult<MaterialDraftDto> =
        call { api.editMaterial(id, EditMaterialRequest(revision, title, text), operationKey) }

    suspend fun deleteMaterial(id: String, operationKey: String): ApiResult<Unit> =
        call { api.deleteMaterial(id, operationKey) }

    suspend fun extractMaterial(id: String, revision: Int, operationKey: String): ApiResult<JobDto> =
        call { api.extractMaterial(id, revision, operationKey) }

    suspend fun parseMaterial(
        id: String,
        revision: Int,
        roleId: String,
        level: String,
        operationKey: String,
    ): ApiResult<JobDto> =
        call { api.parseMaterial(id, ParseRequest(revision, true, roleId, level), operationKey) }

    suspend fun confirmMaterial(
        id: String,
        revision: Int,
        profile: JsonObject,
        operationKey: String,
    ): ApiResult<SnapshotDto> =
        call { api.confirmMaterial(id, ConfirmMaterialRequest(revision, profile), operationKey) }

    suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>> =
        call { api.snapshots(materialId).items }

    suspend fun snapshot(id: String): ApiResult<SnapshotDto> = call { api.snapshot(id) }

    suspend fun preparations(): ApiResult<List<PreparationDto>> = call { api.preparations().items }

    suspend fun preparation(id: String): ApiResult<PreparationDto> = call { api.preparation(id) }

    suspend fun createPreparation(
        roleId: String,
        level: String,
        count: Int,
        resumeSnapshotId: String?,
        jdSnapshotId: String?,
        operationKey: String,
    ): ApiResult<PreparationDto> = call {
        api.createPreparation(
            CreatePreparationRequest(roleId, level, count, resumeSnapshotId, jdSnapshotId),
            operationKey,
        )
    }

    suspend fun editPreparation(
        id: String,
        revision: Int,
        roleId: String,
        level: String,
        count: Int,
        resumeSnapshotId: String?,
        jdSnapshotId: String?,
        operationKey: String,
    ): ApiResult<PreparationDto> = call {
        api.editPreparation(
            id,
            EditPreparationRequest(revision, roleId, level, count, resumeSnapshotId, jdSnapshotId),
            operationKey,
        )
    }

    suspend fun deletePreparation(id: String, operationKey: String): ApiResult<Unit> =
        call { api.deletePreparation(id, operationKey) }

    suspend fun match(preparationId: String, revision: Int, operationKey: String): ApiResult<JobDto> =
        call { api.match(preparationId, AiJobRequest(revision, true), operationKey) }

    suspend fun matchReport(id: String): ApiResult<MatchReportDto> = call { api.matchReport(id) }

    suspend fun generatePlan(
        preparationId: String,
        revision: Int,
        operationKey: String,
    ): ApiResult<JobDto> =
        call { api.generatePlan(preparationId, AiJobRequest(revision, true), operationKey) }

    suspend fun plan(id: String): ApiResult<PublicPlanDto> = call { api.plan(id) }

    suspend fun confirmPlan(id: String, revision: Int, operationKey: String): ApiResult<PublicPlanDto> =
        call { api.confirmPlan(id, revision, operationKey) }

    // ---------------------------------------------------------------- M2 文字面试

    suspend fun startInterview(
        planId: String,
        planRevision: Int,
        operationKey: String,
        mode: String = "text",
        audioProcessingConsent: Boolean = false,
    ): ApiResult<InterviewStartReceiptDto> = call {
        api.startInterview(
            planId,
            StartInterviewRequest(planRevision, true, mode, audioProcessingConsent),
            operationKey,
        )
    }

    suspend fun interviews(
        limit: Int = 20,
        cursor: String? = null,
        planId: String? = null,
    ): ApiResult<InterviewHistoryDto> = call { api.interviews(limit, cursor, planId) }

    suspend fun interview(sessionId: String): ApiResult<InterviewDetailDto> =
        call { api.interview(sessionId) }

    suspend fun submitInterviewAnswer(
        sessionId: String,
        revision: Int,
        turnId: String,
        text: String,
        operationKey: String,
        inputSource: String = "text",
        audioAssetId: String? = null,
        transcriptRevision: Int? = null,
    ): ApiResult<InterviewReceiptDto> = call {
        api.submitInterviewAnswer(
            sessionId,
            InterviewAnswerRequest(
                revision, turnId, text, inputSource, audioAssetId, transcriptRevision
            ),
            operationKey,
        )
    }

    suspend fun skipInterviewTurn(
        sessionId: String,
        revision: Int,
        turnId: String,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.skipInterviewTurn(sessionId, InterviewSkipRequest(revision, turnId), operationKey)
    }

    suspend fun pauseInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.pauseInterview(sessionId, revision, operationKey)
    }

    suspend fun resumeInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.resumeInterview(sessionId, revision, operationKey)
    }

    suspend fun continueWithoutFollowUp(
        sessionId: String,
        revision: Int,
        jobId: String,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.continueWithoutFollowUp(
            sessionId,
            InterviewContinueRequest(revision, jobId),
            operationKey,
        )
    }

    suspend fun finishInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.finishInterview(sessionId, revision, operationKey)
    }

    suspend fun interviewReport(sessionId: String): ApiResult<InterviewReportDto> =
        call { api.interviewReport(sessionId) }

    suspend fun createBasicInterviewReport(
        sessionId: String,
        revision: Int,
        jobId: String,
        operationKey: String,
    ): ApiResult<InterviewReceiptDto> = call {
        api.createBasicInterviewReport(
            sessionId,
            InterviewBasicReportRequest(revision, jobId),
            operationKey,
        )
    }

    // ---------------------------------------------------------------- M3 语音面试

    suspend fun voiceConfig(): ApiResult<VoiceConfigDto> = call { api.voiceConfig() }

    suspend fun voiceDraft(sessionId: String, turnId: String): ApiResult<VoiceDraftDto> =
        call { api.voiceDraft(sessionId, turnId) }

    suspend fun uploadVoiceAudio(
        sessionId: String,
        turnId: String,
        takeId: String,
        sessionRevision: Int,
        expectedVoiceRevision: Int,
        sha256: String,
        bytes: ByteArray,
        operationKey: String,
    ): ApiResult<VoiceUploadReceiptDto> = call {
        api.uploadVoiceAudio(
            sessionId,
            turnId,
            takeId,
            sessionRevision,
            expectedVoiceRevision,
            sha256,
            "pcm_s16le/16000/mono",
            bytes,
            operationKey,
        )
    }

    suspend fun createAudioTranscription(
        assetId: String,
        expectedVoiceRevision: Int,
        operationKey: String,
    ): ApiResult<JobDto> = call {
        api.createAudioTranscription(assetId, expectedVoiceRevision, operationKey)
    }

    suspend fun audioTranscript(assetId: String): ApiResult<VoiceTranscriptDto> =
        call { api.audioTranscript(assetId) }

    suspend fun requestSpeech(
        sessionId: String,
        segment: String,
        turnId: String?,
        operationKey: String,
    ): ApiResult<SpeechAssetDto> = call {
        api.requestSpeech(sessionId, SpeechRequest(segment, turnId), operationKey)
    }

    suspend fun speechAsset(assetId: String): ApiResult<SpeechAssetDto> =
        call { api.speechAsset(assetId) }

    suspend fun speechContent(assetId: String): ApiResult<ByteArray> =
        call { api.speechContent(assetId) }

    suspend fun job(id: String): ApiResult<JobDto> = call { api.job(id) }

    suspend fun retryJob(
        id: String,
        expectedAttempt: Int,
        operationKey: String,
    ): ApiResult<JobDto> =
        call { api.retryJob(id, expectedAttempt, operationKey) }

    /**
     * 轮询任务直到终态；只读 GET，不重新触发生成。
     * shouldPoll 为 false（页面/App 不可见或账号已切换）时立即停止并返回暂停，不写业务状态。
     */
    suspend fun awaitJob(
        jobId: String,
        timeoutMillis: Long = 180_000,
        shouldPoll: () -> Boolean = { true },
    ): ApiResult<JobDto> {
        val deadline = System.currentTimeMillis() + timeoutMillis
        var last: JobDto? = null
        while (System.currentTimeMillis() < deadline) {
            if (!currentCoroutineContext().isActive) {
                throw CancellationException("job polling cancelled")
            }
            if (!shouldPoll()) {
                return ApiResult.Err(
                    "JOB_POLL_PAUSED",
                    "页面不可见或已切换，任务轮询已暂停；返回后可继续查询，不会重复提交",
                    true,
                )
            }
            when (val result = job(jobId)) {
                is ApiResult.Err -> return result
                is ApiResult.Ok -> {
                    last = result.value
                    if (result.value.state in TERMINAL_STATES) return ApiResult.Ok(result.value)
                }
            }
            delay(900)
        }
        return ApiResult.Err(
            "JOB_POLL_TIMEOUT",
            if (last == null) "任务状态查询超时，可稍后重新打开页面继续查询"
            else "任务仍在执行（${stateLabel(last.state)}），可稍后继续查询，不会重复提交",
            true,
        )
    }

    companion object {
        val TERMINAL_STATES = setOf("succeeded", "failed", "interrupted", "superseded")

        fun stateLabel(state: String): String = when (state) {
            "queued" -> "排队中"
            "running" -> "执行中"
            "succeeded" -> "已完成"
            "failed" -> "失败"
            "interrupted" -> "执行中断"
            "superseded" -> "已失效"
            else -> state
        }
    }
}
