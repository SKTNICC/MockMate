package com.mockmate.app.core.network

import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.engine.HttpClientEngine
import io.ktor.client.engine.okhttp.OkHttp
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.delete
import io.ktor.client.request.forms.MultiPartFormDataContent
import io.ktor.client.request.forms.formData
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.request.post
import io.ktor.client.request.put
import io.ktor.client.request.setBody
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.http.ContentType
import io.ktor.http.Headers
import io.ktor.http.HttpHeaders
import io.ktor.http.contentType
import io.ktor.http.isSuccess
import io.ktor.serialization.kotlinx.json.json
import java.io.IOException
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.json.Json

class ApiException(
    val code: String,
    override val message: String,
    val httpStatus: Int,
    val retryable: Boolean = false,
    val details: List<String> = emptyList(),
    /** 请求是否已经发出；false 表示连接前失败，可按确定失败处理。 */
    val requestSent: Boolean = true,
) : Exception(message)

class NetworkException(
    message: String,
    cause: Throwable? = null,
    val requestSent: Boolean = true,
) : Exception(message, cause)

interface TokenProvider {
    fun token(): String?
}

/**
 * 真实契约调用：所有业务写操作都带新的 Idempotency-Key；
 * 超时或网络失败时由调用方用同一 key 与同一 body 重试。
 */
class MockMateApi(
    private val tokenProvider: TokenProvider,
    baseUrl: String,
    engine: HttpClientEngine? = null,
) {
    private val root = baseUrl.trimEnd('/')

    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = true
    }

    private val configuration: io.ktor.client.HttpClientConfig<*>.() -> Unit = {
        install(ContentNegotiation) { json(json) }
        install(HttpTimeout) {
            requestTimeoutMillis = 30_000
            connectTimeoutMillis = 15_000
            socketTimeoutMillis = 30_000
        }
    }

    private val client = if (engine == null) {
        HttpClient(OkHttp, configuration)
    } else {
        HttpClient(engine, configuration)
    }

    private fun authHeader(): Pair<String, String>? =
        tokenProvider.token()?.let { "Authorization" to "Bearer $it" }

    private suspend fun ensureOk(response: HttpResponse) {
        if (response.status.isSuccess()) return
        val raw = runCatching { response.bodyAsText() }.getOrDefault("")
        val envelope = runCatching { json.decodeFromString<ErrorEnvelope>(raw) }.getOrNull()
        throw ApiException(
            code = envelope?.error?.code ?: "HTTP_${response.status.value}",
            message = envelope?.error?.message ?: "请求失败（HTTP ${response.status.value}）",
            httpStatus = response.status.value,
            retryable = envelope?.error?.retryable ?: (response.status.value >= 500),
            details = envelope?.error?.details ?: emptyList(),
        )
    }

    private suspend inline fun <reified T> getJson(path: String): T {
        try {
            val response = client.get(root + path) {
                authHeader()?.let { (name, value) -> header(name, value) }
            }
            ensureOk(response)
            return response.body()
        } catch (e: IOException) {
            throw NetworkException("网络不可达，请检查后端是否启动或连接是否可用", e)
        }
    }

    /** 服务端已返回 2xx 但回执无法解析：请求结果未知，必须保留原操作记录。 */
    private fun unreadableReceipt(status: Int, cause: Throwable): ApiException = ApiException(
        code = "RECEIPT_UNREADABLE",
        message = "服务端已处理请求，但客户端无法解析回执；将按原 key 恢复，不会新建重复操作",
        httpStatus = status,
        retryable = true,
        requestSent = true,
    )

    private fun beforeSendFailure(e: IOException): Boolean {
        var current: Throwable? = e
        while (current != null) {
            if (current is java.net.ConnectException || current is java.net.UnknownHostException) {
                return true
            }
            current = current.cause
        }
        return false
    }

    private suspend inline fun <reified T> writeJson(
        path: String,
        method: String,
        body: Any?,
        key: String,
    ): T {
        val response = try {
            when (method) {
                "POST" -> client.post(root + path) {
                    authHeader()?.let { (name, value) -> header(name, value) }
                    header("Idempotency-Key", key)
                    if (body != null) {
                        contentType(ContentType.Application.Json)
                        setBody(body)
                    }
                }
                "PUT" -> client.put(root + path) {
                    authHeader()?.let { (name, value) -> header(name, value) }
                    header("Idempotency-Key", key)
                    if (body != null) {
                        contentType(ContentType.Application.Json)
                        setBody(body)
                    }
                }
                else -> throw IllegalArgumentException("不支持的写方法 $method")
            }
        } catch (e: IOException) {
            throw NetworkException("网络不可达，请稍后重试", e, requestSent = !beforeSendFailure(e))
        }
        ensureOk(response)
        val text = try {
            response.bodyAsText()
        } catch (e: IOException) {
            throw NetworkException("回执读取中断", e, requestSent = true)
        }
        return try {
            json.decodeFromString<T>(text)
        } catch (e: Exception) {
            throw unreadableReceipt(response.status.value, e)
        }
    }

    private suspend fun deleteNoContent(path: String, operationKey: String) {
        try {
            val response = client.delete(root + path) {
                authHeader()?.let { (name, value) -> header(name, value) }
                header("Idempotency-Key", operationKey)
            }
            ensureOk(response)
        } catch (e: IOException) {
            throw NetworkException("网络不可达，请稍后重试", e)
        }
    }

    // ---------------------------------------------------------------- 账户

    suspend fun register(username: String, password: String): UserDto =
        try {
            val response = client.post(root + "/auth/register") {
                contentType(ContentType.Application.Json)
                setBody(Credentials(username, password))
            }
            ensureOk(response)
            response.body()
        } catch (e: IOException) {
            throw NetworkException("网络不可达，请检查后端是否启动", e)
        }

    suspend fun login(username: String, password: String): TokenResponse =
        try {
            val response = client.post(root + "/auth/login") {
                contentType(ContentType.Application.Json)
                setBody(Credentials(username, password))
            }
            ensureOk(response)
            response.body()
        } catch (e: IOException) {
            throw NetworkException("网络不可达，请检查后端是否启动", e)
        }

    suspend fun logout(explicitToken: String? = null) {
        try {
            val response = client.post(root + "/auth/logout") {
                val token = explicitToken ?: tokenProvider.token()
                token?.let { header("Authorization", "Bearer $it") }
            }
            ensureOk(response)
        } catch (e: IOException) {
            throw NetworkException("网络不可达，未能在服务端撤销会话", e)
        }
    }

    suspend fun me(): UserDto = getJson("/me")

    // ---------------------------------------------------------------- 目录

    suspend fun roles(): RolesResponse = getJson("/roles")

    suspend fun skillCodes(): SkillCodesResponse = getJson("/skill-codes")

    suspend fun jdTemplates(): JdTemplatesResponse = getJson("/jd-templates")

    suspend fun jdTemplate(id: String): JdTemplateDto = getJson("/jd-templates/$id")

    // ---------------------------------------------------------------- 材料

    suspend fun materials(): MaterialsResponse = getJson("/materials")

    suspend fun material(id: String): MaterialDraftDto = getJson("/materials/$id")

    suspend fun createTextMaterial(
        request: TextMaterialRequest,
        operationKey: String,
    ): MaterialDraftDto =
        writeJson("/materials/text", "POST", request, operationKey)

    suspend fun uploadFile(
        kind: String,
        title: String,
        fileName: String,
        mimeType: String,
        bytes: ByteArray,
        operationKey: String,
    ): MaterialDraftDto {
        try {
            val response = client.post(root + "/materials/file") {
                authHeader()?.let { (name, value) -> header(name, value) }
                header("Idempotency-Key", operationKey)
                setBody(
                    MultiPartFormDataContent(
                        formData {
                            append("kind", kind)
                            append("title", title)
                            append(
                                "file",
                                bytes,
                                Headers.build {
                                    append(HttpHeaders.ContentType, mimeType)
                                    append(
                                        HttpHeaders.ContentDisposition,
                                        "filename=\"${fileName.replace("\"", "")}\"",
                                    )
                                },
                            )
                        },
                    ),
                )
            }
            ensureOk(response)
            val text = try {
                response.bodyAsText()
            } catch (e: IOException) {
                throw NetworkException("文件上传回执读取中断", e, requestSent = true)
            }
            return try {
                json.decodeFromString<MaterialDraftDto>(text)
            } catch (e: Exception) {
                throw unreadableReceipt(response.status.value, e)
            }
        } catch (e: IOException) {
            throw NetworkException("文件上传失败：网络不可达", e, requestSent = !beforeSendFailure(e))
        }
    }

    suspend fun editMaterial(
        id: String,
        request: EditMaterialRequest,
        operationKey: String,
    ): MaterialDraftDto = writeJson("/materials/$id", "PUT", request, operationKey)

    suspend fun deleteMaterial(id: String, operationKey: String) =
        deleteNoContent("/materials/$id", operationKey)

    suspend fun extractMaterial(id: String, revision: Int, operationKey: String): JobDto =
        writeJson("/materials/$id/extract", "POST", RevisionRequest(revision), operationKey)

    suspend fun parseMaterial(id: String, request: ParseRequest, operationKey: String): JobDto =
        writeJson("/materials/$id/parse", "POST", request, operationKey)

    suspend fun confirmMaterial(
        id: String,
        request: ConfirmMaterialRequest,
        operationKey: String,
    ): SnapshotDto = writeJson("/materials/$id/confirm", "POST", request, operationKey)

    suspend fun snapshots(materialId: String): SnapshotsResponse =
        getJson("/materials/$materialId/snapshots")

    suspend fun snapshot(id: String): SnapshotDto = getJson("/snapshots/$id")

    // ---------------------------------------------------------------- 准备与匹配

    suspend fun preparations(): PreparationsResponse = getJson("/preparations")

    suspend fun preparation(id: String): PreparationDto = getJson("/preparations/$id")

    suspend fun createPreparation(
        request: CreatePreparationRequest,
        operationKey: String,
    ): PreparationDto = writeJson("/preparations", "POST", request, operationKey)

    suspend fun editPreparation(
        id: String,
        request: EditPreparationRequest,
        operationKey: String,
    ): PreparationDto = writeJson("/preparations/$id", "PUT", request, operationKey)

    suspend fun deletePreparation(id: String, operationKey: String) =
        deleteNoContent("/preparations/$id", operationKey)

    suspend fun match(id: String, request: AiJobRequest, operationKey: String): JobDto =
        writeJson("/preparations/$id/match", "POST", request, operationKey)

    suspend fun matchReport(id: String): MatchReportDto = getJson("/match-reports/$id")

    // ---------------------------------------------------------------- 计划

    suspend fun generatePlan(id: String, request: AiJobRequest, operationKey: String): JobDto =
        writeJson("/preparations/$id/generate-plan", "POST", request, operationKey)

    suspend fun plan(id: String): PublicPlanDto = getJson("/plans/$id")

    suspend fun confirmPlan(id: String, revision: Int, operationKey: String): PublicPlanDto =
        writeJson("/plans/$id/confirm", "POST", RevisionRequest(revision), operationKey)

    // ---------------------------------------------------------------- M2 文字面试

    suspend fun startInterview(
        planId: String,
        request: StartInterviewRequest,
        operationKey: String,
    ): InterviewStartReceiptDto =
        writeJson("/plans/$planId/interviews", "POST", request, operationKey)

    suspend fun interviews(
        limit: Int = 20,
        cursor: String? = null,
        planId: String? = null,
    ): InterviewHistoryDto {
        val params = mutableListOf("limit=$limit")
        cursor?.let {
            params += "cursor=${java.net.URLEncoder.encode(it, "UTF-8")}"
        }
        planId?.let {
            params += "plan_id=${java.net.URLEncoder.encode(it, "UTF-8")}"
        }
        return getJson("/interviews?" + params.joinToString("&"))
    }

    suspend fun interview(sessionId: String): InterviewDetailDto =
        getJson("/interviews/$sessionId")

    suspend fun submitInterviewAnswer(
        sessionId: String,
        request: InterviewAnswerRequest,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/answers", "POST", request, operationKey)

    suspend fun skipInterviewTurn(
        sessionId: String,
        request: InterviewSkipRequest,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/skip", "POST", request, operationKey)

    suspend fun pauseInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/pause", "POST", RevisionRequest(revision), operationKey)

    suspend fun resumeInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/resume", "POST", RevisionRequest(revision), operationKey)

    suspend fun continueWithoutFollowUp(
        sessionId: String,
        request: InterviewContinueRequest,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson(
            "/interviews/$sessionId/continue-without-follow-up",
            "POST",
            request,
            operationKey,
        )

    suspend fun finishInterview(
        sessionId: String,
        revision: Int,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/finish", "POST", RevisionRequest(revision), operationKey)

    suspend fun interviewReport(sessionId: String): InterviewReportDto =
        getJson("/interviews/$sessionId/report")

    suspend fun createBasicInterviewReport(
        sessionId: String,
        request: InterviewBasicReportRequest,
        operationKey: String,
    ): InterviewReceiptDto =
        writeJson("/interviews/$sessionId/report/basic", "POST", request, operationKey)

    // ---------------------------------------------------------------- 任务

    suspend fun job(id: String): JobDto = getJson("/jobs/$id")

    suspend fun retryJob(id: String, expectedAttempt: Int, operationKey: String): JobDto =
        writeJson("/jobs/$id/retry", "POST", RetryRequest(expectedAttempt), operationKey)

    fun close() {
        client.close()
    }
}
