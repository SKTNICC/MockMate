package com.mockmate.app

import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.RetryRequest
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.ErrorDiagnosticsMapper
import com.mockmate.app.ui.InterviewUiMerge
import com.mockmate.app.ui.MaterialJobLookupStatus
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.components.JobRecoveryPolicy
import com.mockmate.app.ui.screens.ReviewScreenPolicy
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpMethod
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class ParseRecoveryAndDiagnosticsTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }
    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")

    private fun MockRequestHandleScope.okJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.OK, jsonHeaders)

    private fun MockRequestHandleScope.acceptedJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.Accepted, jsonHeaders)

    private fun TestScope.drainAll(until: () -> Boolean = { true }) {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            testScheduler.runCurrent()
            if (until()) {
                Thread.sleep(10)
                testScheduler.runCurrent()
                if (until()) return
            } else {
                testScheduler.advanceTimeBy(100)
                Thread.sleep(5)
            }
        }
    }

    private fun materialDto(
        id: String = "mat-1",
        revision: Int = 1,
        state: String = "text_ready",
        text: String? = "示例实验室｜开发实习生｜负责库存接口开发",
        latestJobId: String? = "job-parse-1",
        hasProfile: Boolean = false,
    ) = MaterialDraftDto(
        id = id,
        revision = revision,
        kind = "resume",
        title = "实习简历",
        text = text,
        profile = if (hasProfile) buildJsonObject { put("kind", "resume") } else null,
        state = state,
        latest_job_id = latestJobId,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    private fun parseJobDto(
        id: String = "job-parse-1",
        revision: Int = 1,
        state: String = "failed",
        attempt: Int = 1,
        retryable: Boolean = true,
        error: ErrorInfo? = null,
        resultRef: ResultRefDto? = null,
        targetId: String = "mat-1",
    ) = JobDto(
        id = id,
        kind = "parse",
        target_id = targetId,
        input_revision = revision,
        state = state,
        attempt = attempt,
        retryable = retryable,
        result_ref = resultRef,
        error = error,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    @Test
    fun reviewScreenPolicyAndDiagnosticsCoverParseRetryExhaustedAndSanitization() {
        val mat = materialDto()
        val retryableJob = parseJobDto(
            retryable = true,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "AI 输出不符合契约结构",
                retryable = true,
                details = listOf(
                    "stage=parse",
                    "reason=invalid_profile_schema",
                    "work_experience.0.organization: Field required",
                    "SENTINEL_MODEL_SNIPPET",
                    "forbidden=SENTINEL_MATERIAL_TEXT",
                ),
                request_id = "req-parse-1",
            ),
        )
        val retryPolicy = ReviewScreenPolicy.evaluate(mat, retryableJob)
        assertTrue(retryPolicy.hasCurrentFailedParse)
        assertFalse(retryPolicy.showStartParseAction)
        assertFalse(retryPolicy.parseExhausted)
        assertTrue(retryPolicy.showManualHint)
        assertEquals("重试 AI 解析", JobRecoveryPolicy.retryButtonText(retryableJob))

        val friendlyRetry = ErrorDiagnosticsMapper.fromJob(retryableJob)
        assertTrue(friendlyRetry.userExplanation.contains("原文已保留，可以手动核对"))
        assertTrue(friendlyRetry.suggestion.orEmpty().contains("重试 AI 解析"))
        assertFalse(friendlyRetry.suggestion.orEmpty().contains("更换岗位"))
        val diagText = friendlyRetry.formatDiagnosticText()
        assertTrue(diagText.contains("stage=parse"))
        assertTrue(diagText.contains("work_experience.0.organization: Field required"))
        assertTrue(diagText.contains("attempt=1"))
        assertFalse(diagText.contains("SENTINEL_MODEL_SNIPPET"))
        assertFalse(diagText.contains("SENTINEL_MATERIAL_TEXT"))

        val exhaustedJob = parseJobDto(
            attempt = 2,
            retryable = false,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "AI 输出不符合契约结构",
                retryable = false,
                details = listOf("stage=parse", "reason=invalid_profile_schema"),
                request_id = "req-parse-2",
            ),
        )
        val exhaustedPolicy = ReviewScreenPolicy.evaluate(mat, exhaustedJob)
        assertTrue(exhaustedPolicy.hasCurrentFailedParse)
        assertTrue(exhaustedPolicy.parseExhausted)
        assertFalse(exhaustedPolicy.showStartParseAction)
        val friendlyExhausted = ErrorDiagnosticsMapper.fromJob(exhaustedJob)
        assertTrue(friendlyExhausted.suggestion.orEmpty().contains("手动添加并确认"))
        assertFalse(friendlyExhausted.suggestion.orEmpty().contains("调整设置"))

        // 旧版计划/匹配诊断中夹带的引文或题干片段也必须被过滤
        val legacyWithRawText = ErrorDiagnosticsMapper.fromJob(
            JobDto(
                id = "job-legacy-plan",
                kind = "plan",
                target_id = "prep-1",
                input_revision = 1,
                state = "failed",
                attempt = 1,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-3",
                        "重复题干：SENTINEL_USER_ANSWER",
                        "forbidden=SENTINEL_QUOTE",
                    ),
                ),
                created_at = "2026-11-18T00:00:00Z",
                updated_at = "2026-11-18T00:00:00Z",
            ),
        )
        val legacyDiag = legacyWithRawText.formatDiagnosticText()
        assertTrue(legacyDiag.contains("slot=generated-3"))
        assertFalse(legacyDiag.contains("SENTINEL_USER_ANSWER"))
        assertFalse(legacyDiag.contains("SENTINEL_QUOTE"))

        // 工作经历表格行无法对齐列时的受控中文错误消息应原样保留并给出解析恢复引导
        val unalignableTableJob = parseJobDto(
            retryable = true,
            error = ErrorInfo(
                code = "EVIDENCE_INVALID",
                message = "工作经历表格行无法按表头可靠对齐列，请核对原文",
                retryable = true,
                details = listOf(
                    "stage=parse",
                    "reason=ungrounded_work_experience",
                    "source=provider_output",
                    "section=work_experience",
                    "field=quote",
                ),
                request_id = "req-parse-table-align",
            ),
        )
        val friendlyTableErr = ErrorDiagnosticsMapper.fromJob(unalignableTableJob)
        assertEquals(
            "工作经历表格行无法按表头可靠对齐列，请核对原文",
            friendlyTableErr.rawServerMessage,
        )
        assertTrue(friendlyTableErr.userExplanation.contains("原文已保留，可以手动核对"))
        assertTrue(friendlyTableErr.suggestion.orEmpty().contains("重试 AI 解析"))
        val tableDiag = friendlyTableErr.formatDiagnosticText()
        assertTrue(tableDiag.contains("field=quote"))
        assertTrue(tableDiag.contains("工作经历表格行无法按表头可靠对齐列，请核对原文"))
    }

    @Test
    fun openReviewRestoresFailedParseJobReadOnlyAndRetryRecoversWithoutExtraStartCalls() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val parseStartCalls = AtomicInteger(0)
        val getJobCalls = AtomicInteger(0)
        val retryCalls = mutableListOf<RetryRequest>()
        var parsedNow = false
        var retriedYet = false

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson(
                        json.encodeToString(
                            mapOf(
                                "items" to listOf(
                                    materialDto(
                                        state = if (parsedNow) "parsed" else "text_ready",
                                        hasProfile = parsedNow,
                                    ),
                                ),
                            ),
                        ),
                    )
                    "/v1/materials/mat-1" -> okJson(
                        json.encodeToString(
                            materialDto(
                                state = if (parsedNow) "parsed" else "text_ready",
                                hasProfile = parsedNow,
                            ),
                        ),
                    )
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1/parse" -> {
                        parseStartCalls.incrementAndGet()
                        error("离开页面返回时禁止自动重新触发 AI 解析")
                    }
                    "/v1/jobs/job-parse-1" -> {
                        getJobCalls.incrementAndGet()
                        val dto = if (!retriedYet) {
                            parseJobDto(
                                state = "failed",
                                attempt = 1,
                                retryable = true,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "AI 输出不符合契约结构",
                                    retryable = true,
                                    details = listOf("stage=parse", "reason=invalid_profile_schema"),
                                    request_id = "req-1",
                                ),
                            )
                        } else {
                            parsedNow = true
                            parseJobDto(
                                state = "succeeded",
                                attempt = 2,
                                retryable = false,
                                resultRef = ResultRefDto("material", "mat-1"),
                            )
                        }
                        okJson(json.encodeToString(dto))
                    }
                    "/v1/jobs/job-parse-1/retry" -> {
                        assertEquals(HttpMethod.Post, request.method)
                        val bodyText = (request.body as TextContent).text
                        retryCalls += json.decodeFromString<RetryRequest>(bodyText)
                        retriedYet = true
                        acceptedJson(
                            json.encodeToString(
                                parseJobDto(state = "queued", attempt = 2, retryable = false),
                            ),
                        )
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 离开再进入核对页：只读 GET 恢复失败任务，不重新发起 AI 解析
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.job?.id == "job-parse-1" && getJobCalls.get() >= 1 }
            assertEquals(0, parseStartCalls.get())
            assertEquals("failed", vm.state.value.job?.state)
            assertTrue(vm.state.value.job?.retryable == true)

            vm.back()
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.job?.id == "job-parse-1" && getJobCalls.get() >= 2 }
            assertEquals(0, parseStartCalls.get())

            // 用户显式点击重试同一任务
            vm.retryCurrentJob()
            drainAll { vm.state.value.material?.state == "parsed" }
            assertEquals(listOf(RetryRequest(expected_attempt = 1)), retryCalls)
            assertEquals(0, parseStartCalls.get())
            assertEquals("parsed", vm.state.value.material?.state)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun diagnosticsRedactsUnknownSchemaKeysAndShowsGenericUnknownDetailWhileKeepingLegacyReadable() {
        val info = ErrorDiagnosticsMapper.fromJob(
            parseJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "AI 输出不符合契约结构",
                    retryable = true,
                    details = listOf(
                        "stage=parse",
                        "field=work_experience.0.SYNTHETIC_MARKER_7469",
                        "work_experience.0.SYNTHETIC_MARKER_7469: Extra inputs are not permitted",
                        "work_experience.0.organization: Field required",
                        "work_experience.0.title: Field required",
                        "AI 返回内容不是合法 JSON",
                        "未知自定义提示_SYNTHETIC_RAW_TEXT",
                    ),
                    request_id = "req-redact",
                ),
            ),
        )
        val diagText = info.formatDiagnosticText()
        assertFalse(diagText.contains("SYNTHETIC_MARKER_7469"))
        assertFalse(diagText.contains("SYNTHETIC_RAW_TEXT"))
        assertFalse(info.details.any { it.contains("SYNTHETIC") })
        assertTrue(diagText.contains("work_experience.0.organization: Field required"))
        assertTrue(diagText.contains("work_experience.0.title: Field required"))
        assertTrue(diagText.contains("AI 返回内容不是合法 JSON"))
        assertTrue(diagText.contains("未知诊断明细（已隐藏原始内容）"))
        assertTrue(info.details.contains("未知诊断明细（已隐藏原始内容）"))
    }

    @Test
    fun openReviewDelayedAndFailedJobLookupPausesStartAndRecoversReadOnly() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val parseStartCalls = AtomicInteger(0)
        val getJobCalls = AtomicInteger(0)
        val firstJobEntered = CompletableDeferred<Unit>()
        val firstJobRelease = CompletableDeferred<Unit>()
        var failJobLookup = true

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> okJson(
                        json.encodeToString(
                            materialDto(
                                state = "text_ready",
                                text = "示例实验室｜后端实习生｜维护库存接口",
                                latestJobId = "job-parse-1",
                                hasProfile = false,
                            ),
                        ),
                    )
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1/parse" -> {
                        parseStartCalls.incrementAndGet()
                        error("任务状态未明或已失败时禁止触发 POST /parse")
                    }
                    "/v1/jobs/job-parse-1" -> {
                        val idx = getJobCalls.incrementAndGet()
                        if (idx == 1) {
                            firstJobEntered.complete(Unit)
                            firstJobRelease.await()
                        }
                        if (failJobLookup) {
                            respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                        } else {
                            okJson(
                                json.encodeToString(
                                    parseJobDto(
                                        state = "failed",
                                        attempt = 1,
                                        retryable = true,
                                        error = ErrorInfo(
                                            code = "AI_INVALID_OUTPUT",
                                            message = "AI 输出不符合契约结构",
                                            retryable = true,
                                            details = listOf("stage=parse", "reason=invalid_profile_schema"),
                                        ),
                                    ),
                                ),
                            )
                        }
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { firstJobEntered.isCompleted }

            // 1) 延迟 Job 查询返回前：暂停启动按钮，保留原文与手动填写入口
            val pendingState = vm.state.value
            val pendingPolicy = ReviewScreenPolicy.evaluate(
                pendingState.material,
                pendingState.job,
                pendingState.materialJobLookup,
            )
            assertEquals(MaterialJobLookupStatus.LOADING, pendingState.materialJobLookup?.status)
            assertTrue(pendingPolicy.isJobLookupPending)
            assertFalse(pendingPolicy.showStartParseAction)
            assertTrue(pendingPolicy.showManualHint)
            assertEquals("示例实验室｜后端实习生｜维护库存接口", pendingState.material?.text)

            // 尝试在查询尚未返回时点击启动解析，不能发送 POST /parse
            vm.parseMaterial("mat-1", "java-backend", "intern")
            drainAll()
            assertEquals(0, parseStartCalls.get())

            // 2) Job 查询失败：显示「查询任务状态」，仍暂停启动按钮
            firstJobRelease.complete(Unit)
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            val failedLookupState = vm.state.value
            val failedLookupPolicy = ReviewScreenPolicy.evaluate(
                failedLookupState.material,
                failedLookupState.job,
                failedLookupState.materialJobLookup,
            )
            assertTrue(failedLookupPolicy.showQueryJobStatusAction)
            assertFalse(failedLookupPolicy.showStartParseAction)
            assertTrue(failedLookupPolicy.showManualHint)

            // 3) 显式点击「查询任务状态」只发 GET；恢复出可重试失败任务后展示「重试 AI 解析」
            failJobLookup = false
            vm.queryLatestMaterialJob("mat-1")
            vm.queryLatestMaterialJob("mat-1")
            drainAll { vm.state.value.job?.id == "job-parse-1" }
            assertEquals(2, getJobCalls.get())
            assertEquals(0, parseStartCalls.get())
            val recoveredPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(recoveredPolicy.hasCurrentFailedParse)
            assertFalse(recoveredPolicy.showStartParseAction)
            assertEquals("重试 AI 解析", JobRecoveryPolicy.retryButtonText(vm.state.value.job!!))
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun openReviewRecoversUploadedMaterialWhenExtractSucceededBeforeJobGetAndGuardsSwitches() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val mat1GetCount = AtomicInteger(0)
        val parsePostCount = AtomicInteger(0)
        val mat2JobEntered = CompletableDeferred<Unit>()
        val mat2JobRelease = CompletableDeferred<Unit>()

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> {
                        val n = mat1GetCount.incrementAndGet()
                        if (n == 1) {
                            // 首次 GET 仍为 uploaded（无原文），但 latest_job_id 已指向 extract 任务
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "uploaded",
                                        text = null,
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                        } else {
                            // 第二次 GET 返回提取完成后的正文
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "text_ready",
                                        text = "提取出的简历原文：示例甲实验室｜后端实习生",
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-1" -> okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-ext-1",
                                kind = "extract",
                                target_id = "mat-1",
                                input_revision = 1,
                                state = "succeeded",
                                attempt = 1,
                                retryable = false,
                                result_ref = ResultRefDto("material", "mat-1"),
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    "/v1/materials/mat-1/parse" -> {
                        parsePostCount.incrementAndGet()
                        acceptedJson(
                            json.encodeToString(
                                parseJobDto(id = "job-parse-new", state = "queued", attempt = 1, retryable = false),
                            ),
                        )
                    }
                    "/v1/jobs/job-parse-new" -> okJson(
                        json.encodeToString(
                            parseJobDto(id = "job-parse-new", state = "queued", attempt = 1, retryable = false),
                        ),
                    )
                    "/v1/materials/mat-2" -> okJson(
                        json.encodeToString(
                            materialDto(
                                id = "mat-2",
                                revision = 2,
                                state = "text_ready",
                                text = "材料二正文",
                                latestJobId = "job-mat2-old",
                                hasProfile = false,
                            ),
                        ),
                    )
                    "/v1/materials/mat-2/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-mat2-old" -> {
                        mat2JobEntered.complete(Unit)
                        mat2JobRelease.await()
                        okJson(
                            json.encodeToString(
                                JobDto(
                                    id = "job-mat2-old",
                                    kind = "parse",
                                    target_id = "mat-2",
                                    input_revision = 2,
                                    state = "failed",
                                    attempt = 1,
                                    retryable = true,
                                    created_at = "2026-11-18T00:00:00Z",
                                    updated_at = "2026-11-18T00:00:00Z",
                                ),
                            ),
                        )
                    }
                    "/v1/materials/mat-3" -> okJson(
                        json.encodeToString(
                            materialDto(
                                id = "mat-3",
                                revision = 1,
                                state = "text_ready",
                                text = "材料三正文",
                                latestJobId = null,
                                hasProfile = false,
                            ),
                        ),
                    )
                    "/v1/materials/mat-3/snapshots" -> okJson("""{"items":[]}""")
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) uploaded -> extract succeeded -> 重新拉取材料后自动进入可解析状态
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.material?.state == "text_ready" }
            assertEquals(2, mat1GetCount.get())
            assertEquals("提取出的简历原文：示例甲实验室｜后端实习生", vm.state.value.material?.text)
            val readyPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(readyPolicy.showStartParseAction)
            assertFalse(readyPolicy.showExtractAction)

            // 连续点击 parseMaterial 只会排队一次
            vm.parseMaterial("mat-1", "java-backend", "intern")
            vm.parseMaterial("mat-1", "java-backend", "intern")
            drainAll { vm.state.value.job?.id == "job-parse-new" }
            assertEquals(1, parsePostCount.get())

            // 2) 切换材料后，mat-2 的迟到 Job 回调不得覆盖 mat-3 状态
            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll { mat2JobEntered.isCompleted }
            vm.back()
            vm.openReview("mat-3", "prep-1")
            drainAll { vm.state.value.material?.id == "mat-3" }

            mat2JobRelease.complete(Unit)
            drainAll()
            assertTrue(vm.state.value.stack.last() == Screen.Review("mat-3", "prep-1"))
            assertEquals("mat-3", vm.state.value.material?.id)
            assertEquals(null, vm.state.value.job)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reopenSameMaterialWhileFirstJobGetIsInFlightDoesNotStayLoadingAndDiscardsLateGet() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val firstJobEntered = CompletableDeferred<Unit>()
        val firstJobRelease = CompletableDeferred<Unit>()
        val getJobCalls = AtomicInteger(0)
        val parsePostCalls = AtomicInteger(0)

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> okJson(
                        json.encodeToString(
                            materialDto(
                                id = "mat-1",
                                revision = 1,
                                state = "text_ready",
                                text = "公司：示例甲实验室\n职位：后端实习生",
                                latestJobId = "job-parse-1",
                                hasProfile = false,
                            ),
                        ),
                    )
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1/parse" -> {
                        parsePostCalls.incrementAndGet()
                        error("重开同一材料不得发送 POST /parse")
                    }
                    "/v1/jobs/job-parse-1" -> {
                        val idx = getJobCalls.incrementAndGet()
                        if (idx == 1) {
                            firstJobEntered.complete(Unit)
                            firstJobRelease.await()
                            // 第一次迟到的请求即使返回了另一种状态，也不得覆盖重开后的新页面
                            okJson(
                                json.encodeToString(
                                    parseJobDto(
                                        id = "job-parse-1",
                                        state = "interrupted",
                                        attempt = 1,
                                        retryable = false,
                                    ),
                                ),
                            )
                        } else {
                            okJson(
                                json.encodeToString(
                                    parseJobDto(
                                        id = "job-parse-1",
                                        state = "failed",
                                        attempt = 1,
                                        retryable = true,
                                        error = ErrorInfo(
                                            code = "AI_INVALID_OUTPUT",
                                            message = "AI 输出不符合契约结构",
                                            retryable = true,
                                            details = listOf("stage=parse", "reason=empty_profile"),
                                        ),
                                    ),
                                ),
                            )
                        }
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 第一次打开材料 A，卡在第一次 Job GET
            vm.openReview("mat-1", "prep-1")
            drainAll { firstJobEntered.isCompleted }
            assertEquals(1, getJobCalls.get())

            // 退出后立即重开同一材料 A
            vm.back()
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED && vm.state.value.job != null }
            assertEquals(2, getJobCalls.get())

            // 放行第一次旧 Job GET
            firstJobRelease.complete(Unit)
            drainAll()

            assertEquals(0, parsePostCalls.get())
            assertEquals("job-parse-1", vm.state.value.job?.id)
            assertEquals("failed", vm.state.value.job?.state)
            assertTrue(vm.state.value.job?.retryable == true)
            assertEquals(MaterialJobLookupStatus.LOADED, vm.state.value.materialJobLookup?.status)
            val policy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertFalse(policy.isJobLookupPending)
            assertTrue(policy.hasCurrentFailedParse)
            assertFalse(policy.showStartParseAction)
            assertEquals("重试 AI 解析", JobRecoveryPolicy.retryButtonText(vm.state.value.job!!))
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun completedExtractAndParseMaterialReread503EntersRecoverableFailedLookupAndRecoversReadOnly() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val mat1GetCount = AtomicInteger(0)
        val mat2GetCount = AtomicInteger(0)
        val mat2JobGetCount = AtomicInteger(0)
        val postCalls = AtomicInteger(0)

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> {
                        when (mat1GetCount.incrementAndGet()) {
                            1 -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "uploaded",
                                        text = null,
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                            2 -> respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r1"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                            else -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "text_ready",
                                        text = "恢复出的简历正文",
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-1" -> okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-ext-1",
                                kind = "extract",
                                target_id = "mat-1",
                                input_revision = 1,
                                state = "succeeded",
                                attempt = 1,
                                retryable = false,
                                result_ref = ResultRefDto("material", "mat-1"),
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    "/v1/materials/mat-2" -> {
                        when (mat2GetCount.incrementAndGet()) {
                            1 -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-2",
                                        state = "text_ready",
                                        text = "材料二正文",
                                        latestJobId = "job-parse-2",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                            2 -> respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r2"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                            else -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-2",
                                        state = "parsed",
                                        text = "材料二正文",
                                        latestJobId = "job-parse-2",
                                        hasProfile = true,
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-2/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-parse-2" -> {
                        mat2JobGetCount.incrementAndGet()
                        okJson(
                            json.encodeToString(
                                JobDto(
                                    id = "job-parse-2",
                                    kind = "parse",
                                    target_id = "mat-2",
                                    input_revision = 1,
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    result_ref = ResultRefDto("material", "mat-2"),
                                    created_at = "2026-11-18T00:00:00Z",
                                    updated_at = "2026-11-18T00:00:00Z",
                                ),
                            ),
                        )
                    }
                    "/v1/materials/mat-1/extract", "/v1/materials/mat-1/parse", "/v1/materials/mat-2/parse" -> {
                        postCalls.incrementAndGet()
                        error("只读恢复禁止发 POST")
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) 提取已完成但第一次回读材料 HTTP 503：不能提前标为 LOADED，进入 FAILED 并提供只读查询入口
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            val extFailedPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(extFailedPolicy.showQueryJobStatusAction)
            assertFalse(extFailedPolicy.showExtractAction)
            assertFalse(extFailedPolicy.showStartParseAction)
            assertFalse(extFailedPolicy.isJobLookupPending)

            // 下一次只读恢复取回正文
            vm.queryLatestMaterialJob("mat-1")
            drainAll { vm.state.value.material?.state == "text_ready" }
            assertEquals("恢复出的简历正文", vm.state.value.material?.text)
            assertEquals(MaterialJobLookupStatus.LOADED, vm.state.value.materialJobLookup?.status)
            val extRecoveredPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(extRecoveredPolicy.showStartParseAction)
            assertFalse(extRecoveredPolicy.showQueryJobStatusAction)

            // 2) 解析已完成但第一次回读材料 HTTP 503：保持可查询恢复并保留手动填写提示
            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            val parseFailedPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(parseFailedPolicy.showQueryJobStatusAction)
            assertFalse(parseFailedPolicy.showStartParseAction)
            assertTrue(parseFailedPolicy.showManualHint)

            // 连续点击同一页面只读查询只发一次 GET
            val beforeJobs = mat2JobGetCount.get()
            vm.queryLatestMaterialJob("mat-2")
            vm.queryLatestMaterialJob("mat-2")
            drainAll { vm.state.value.material?.state == "parsed" }
            assertEquals(beforeJobs + 1, mat2JobGetCount.get())
            assertTrue(vm.state.value.material?.profile != null)
            assertEquals(MaterialJobLookupStatus.LOADED, vm.state.value.materialJobLookup?.status)
            assertEquals(0, postCalls.get())
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun legacyJobMessageAndSyntheticDetailTokensAreRedactedAcrossUiToastAndClipboard() = runTest {
        val legacyJob = parseJobDto(
            retryable = true,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "历史原始异常：SYNTHETIC_LEGACY_MESSAGE_901",
                retryable = true,
                details = listOf(
                    "stage=parse",
                    "reason=SYNTHETIC_REASON_901",
                    "actual=SYNTHETIC_ACTUAL_901",
                    "expected=SYNTHETIC_EXPECTED_901",
                    "source=SYNTHETIC_SOURCE_901",
                    "status=SYNTHETIC_STATUS_901",
                    "item_id=SYNTHETIC_ITEM_901",
                    "work_experience.0.organization: Field required",
                    "work_experience.0.title: Field required",
                ),
                request_id = "req-legacy-redact",
            ),
        )
        val friendly = ErrorDiagnosticsMapper.fromJob(legacyJob)
        val clipboardText = friendly.formatDiagnosticText()
        for (marker in listOf(
            "SYNTHETIC_LEGACY_MESSAGE_901",
            "SYNTHETIC_REASON_901",
            "SYNTHETIC_ACTUAL_901",
            "SYNTHETIC_EXPECTED_901",
            "SYNTHETIC_SOURCE_901",
            "SYNTHETIC_STATUS_901",
            "SYNTHETIC_ITEM_901",
        )) {
            assertFalse(friendly.rawServerMessage.contains(marker))
            assertFalse(friendly.userExplanation.contains(marker))
            assertFalse(friendly.details.any { it.contains(marker) })
            assertFalse(clipboardText.contains(marker))
        }
        assertEquals("AI 输出不符合契约", friendly.rawServerMessage)
        assertTrue(clipboardText.contains("work_experience.0.organization: Field required"))
        assertTrue(clipboardText.contains("work_experience.0.title: Field required"))
        assertTrue(friendly.details.contains("未知诊断明细（已隐藏原始内容）"))

        // 验证出题槽位、合理数字及用户确认表单的合法 item_id 仍可读
        val slotFriendly = ErrorDiagnosticsMapper.fromJob(
            JobDto(
                id = "job-plan-slot",
                kind = "plan",
                target_id = "prep-1",
                input_revision = 1,
                state = "failed",
                attempt = 1,
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = true,
                    details = listOf("stage=plan", "slot=generated-2", "reason=output_truncated", "finish_reason=length"),
                ),
                created_at = "2026-11-18T00:00:00Z",
                updated_at = "2026-11-18T00:00:00Z",
            ),
        )
        assertTrue(slotFriendly.formatDiagnosticText().contains("slot=generated-2"))

        // 验证通过 ViewModel 恢复时 Toast 消息也不包含原始未识别历史消息
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> okJson(json.encodeToString(materialDto(latestJobId = "job-parse-1")))
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-parse-1" -> okJson(json.encodeToString(legacyJob))
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.job?.id == "job-parse-1" }
            vm.queryCurrentJob()
            drainAll { vm.state.value.message != null }
            val toastText = vm.state.value.message?.text.orEmpty()
            assertFalse(toastText.contains("SYNTHETIC_LEGACY_MESSAGE_901"))
            assertEquals("AI 输出不符合契约", toastText)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun extractedMaterialParseQueuedPollSucceededReread503RecoversReadOnlyViaNewParseJob() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val parsePostCalls = AtomicInteger(0)
        val extJobGetCalls = AtomicInteger(0)
        val parseJobGetCalls = AtomicInteger(0)
        var parseFinished = false
        var failMaterialReread = true

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[{"code":"java.core","label":"Java 基础"}]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/materials/mat-1" -> {
                        if (!parseFinished) {
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "text_ready",
                                        text = "公司：示例甲实验室\n职位：后端实习生",
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                        } else if (failMaterialReread) {
                            respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r503"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                        } else {
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "parsed",
                                        text = "公司：示例甲实验室\n职位：后端实习生",
                                        latestJobId = "job-parse-new",
                                        hasProfile = true,
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-1" -> {
                        extJobGetCalls.incrementAndGet()
                        okJson(
                            json.encodeToString(
                                JobDto(
                                    id = "job-ext-1",
                                    kind = "extract",
                                    target_id = "mat-1",
                                    input_revision = 1,
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    result_ref = ResultRefDto("material", "mat-1"),
                                    created_at = "2026-11-18T00:00:00Z",
                                    updated_at = "2026-11-18T00:00:00Z",
                                ),
                            ),
                        )
                    }
                    "/v1/materials/mat-1/parse" -> {
                        parsePostCalls.incrementAndGet()
                        acceptedJson(
                            json.encodeToString(
                                parseJobDto(
                                    id = "job-parse-new",
                                    state = "queued",
                                    attempt = 1,
                                    retryable = false,
                                ),
                            ),
                        )
                    }
                    "/v1/jobs/job-parse-new" -> {
                        parseJobGetCalls.incrementAndGet()
                        parseFinished = true
                        okJson(
                            json.encodeToString(
                                parseJobDto(
                                    id = "job-parse-new",
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    resultRef = ResultRefDto("material", "mat-1"),
                                ),
                            ),
                        )
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.job?.id == "job-ext-1" && vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED }
            val beforeRecoveryExtGets = extJobGetCalls.get()

            // 在核对页直接发起 AI 解析：收到 queued 回执后立即同步 latest_job_id；
            // 轮询 succeeded 后回读材料 503，页面进入可恢复状态而非永久加载。
            vm.parseMaterial("mat-1", "java-backend", "intern")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }

            assertEquals(1, parsePostCalls.get())
            assertEquals("job-parse-new", vm.state.value.material?.latest_job_id)
            assertEquals("job-parse-new", vm.state.value.materialJobLookup?.jobId)
            assertEquals(MaterialJobLookupStatus.FAILED, vm.state.value.materialJobLookup?.status)
            assertEquals("公司：示例甲实验室\n职位：后端实习生", vm.state.value.material?.text)

            val failedPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(failedPolicy.showQueryJobStatusAction)
            assertFalse(failedPolicy.isJobLookupPending)
            assertFalse(failedPolicy.hasCurrentRunningJob)
            assertFalse(failedPolicy.showStartParseAction)
            assertTrue(failedPolicy.showManualHint)

            // 点击只读查询：查询的是新解析 Job，且不重复 POST /parse
            failMaterialReread = false
            val parseGetsBefore = parseJobGetCalls.get()
            vm.queryLatestMaterialJob("mat-1")
            drainAll { vm.state.value.material?.profile != null }

            assertEquals(1, parsePostCalls.get())
            assertEquals(beforeRecoveryExtGets, extJobGetCalls.get())
            assertTrue(parseJobGetCalls.get() > parseGetsBefore)
            assertEquals("parsed", vm.state.value.material?.state)
            assertTrue(vm.state.value.material?.profile != null)
            assertEquals(MaterialJobLookupStatus.LOADED, vm.state.value.materialJobLookup?.status)
            val recoveredPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertFalse(recoveredPolicy.showQueryJobStatusAction)
            assertFalse(recoveredPolicy.isJobLookupPending)
            assertFalse(recoveredPolicy.showStartParseAction)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun parseImmediateTerminalReceiptsAndExpiredExtractCallbackAreIsolated() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val parsePostMat1 = AtomicInteger(0)
        val parsePostMat2 = AtomicInteger(0)
        val parseGetMat1 = AtomicInteger(0)
        val retryCalls = mutableListOf<RetryRequest>()
        var failMat1Reread = true
        var mat1Parsed = false
        var mat2Retried = false
        val mat3ExtCount = AtomicInteger(0)
        var mat3Extracted = false
        val mat3SlowQueryEntered = CompletableDeferred<Unit>()
        val mat3SlowQueryRelease = CompletableDeferred<Unit>()

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")

                    // 材料 1：首次回执直接为 succeeded（幂等返回），回读 503 后只读恢复
                    "/v1/materials/mat-1" -> {
                        if (parsePostMat1.get() == 0) {
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "text_ready",
                                        text = "材料一正文",
                                        latestJobId = "job-ext-1",
                                        hasProfile = false,
                                    ),
                                ),
                            )
                        } else if (failMat1Reread) {
                            respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r1"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                        } else {
                            mat1Parsed = true
                            okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-1",
                                        state = "parsed",
                                        text = "材料一正文",
                                        latestJobId = "job-parse-imm-ok",
                                        hasProfile = true,
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-1" -> okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-ext-1",
                                kind = "extract",
                                target_id = "mat-1",
                                input_revision = 1,
                                state = "succeeded",
                                attempt = 1,
                                retryable = false,
                                result_ref = ResultRefDto("material", "mat-1"),
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    "/v1/materials/mat-1/parse" -> {
                        parsePostMat1.incrementAndGet()
                        acceptedJson(
                            json.encodeToString(
                                parseJobDto(
                                    id = "job-parse-imm-ok",
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    resultRef = ResultRefDto("material", "mat-1"),
                                ),
                            ),
                        )
                    }
                    "/v1/jobs/job-parse-imm-ok" -> {
                        parseGetMat1.incrementAndGet()
                        okJson(
                            json.encodeToString(
                                parseJobDto(
                                    id = "job-parse-imm-ok",
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    resultRef = ResultRefDto("material", "mat-1"),
                                ),
                            ),
                        )
                    }

                    // 材料 2：首次回执直接为 failed
                    "/v1/materials/mat-2" -> okJson(
                        json.encodeToString(
                            materialDto(
                                id = "mat-2",
                                state = if (mat2Retried) "parsed" else "text_ready",
                                text = "材料二正文",
                                latestJobId = if (mat2Retried) "job-parse-imm-fail" else "job-ext-2",
                                hasProfile = mat2Retried,
                            ),
                        ),
                    )
                    "/v1/materials/mat-2/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-2" -> okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-ext-2",
                                kind = "extract",
                                target_id = "mat-2",
                                input_revision = 1,
                                state = "succeeded",
                                attempt = 1,
                                retryable = false,
                                result_ref = ResultRefDto("material", "mat-2"),
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    "/v1/materials/mat-2/parse" -> {
                        parsePostMat2.incrementAndGet()
                        acceptedJson(
                            json.encodeToString(
                                JobDto(
                                    id = "job-parse-imm-fail",
                                    kind = "parse",
                                    target_id = "mat-2",
                                    input_revision = 1,
                                    state = "failed",
                                    attempt = 1,
                                    retryable = true,
                                    error = ErrorInfo(
                                        code = "AI_INVALID_OUTPUT",
                                        message = "AI 输出不符合契约结构",
                                        retryable = true,
                                        details = listOf("stage=parse", "reason=empty_profile"),
                                    ),
                                    created_at = "2026-11-18T00:00:00Z",
                                    updated_at = "2026-11-18T00:00:00Z",
                                ),
                            ),
                        )
                    }
                    "/v1/jobs/job-parse-imm-fail/retry" -> {
                        val bodyText = (request.body as TextContent).text
                        retryCalls += json.decodeFromString<RetryRequest>(bodyText)
                        mat2Retried = true
                        acceptedJson(
                            json.encodeToString(
                                JobDto(
                                    id = "job-parse-imm-fail",
                                    kind = "parse",
                                    target_id = "mat-2",
                                    input_revision = 1,
                                    state = "succeeded",
                                    attempt = 2,
                                    retryable = false,
                                    result_ref = ResultRefDto("material", "mat-2"),
                                    created_at = "2026-11-18T00:00:00Z",
                                    updated_at = "2026-11-18T00:00:00Z",
                                ),
                            ),
                        )
                    }

                    // 材料 3：迟到/过期的 extract 回调不得反向覆盖新发起的 parse Job
                    "/v1/materials/mat-3" -> okJson(
                        json.encodeToString(
                            materialDto(
                                id = "mat-3",
                                state = if (mat3Extracted) "text_ready" else "uploaded",
                                text = if (mat3Extracted) "材料三正文" else null,
                                latestJobId = "job-ext-3",
                                hasProfile = false,
                            ),
                        ),
                    )
                    "/v1/materials/mat-3/snapshots" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-ext-3" -> {
                        val n = mat3ExtCount.incrementAndGet()
                        when (n) {
                            1 -> okJson(
                                json.encodeToString(
                                    JobDto(
                                        id = "job-ext-3",
                                        kind = "extract",
                                        target_id = "mat-3",
                                        input_revision = 1,
                                        state = "queued",
                                        attempt = 1,
                                        retryable = false,
                                        created_at = "2026-11-18T00:00:00Z",
                                        updated_at = "2026-11-18T00:00:00Z",
                                    ),
                                ),
                            )
                            2 -> {
                                // 手动 queryCurrentJob() 发起的迟到请求，挂起直到 parse 已启动后返回 503
                                mat3SlowQueryEntered.complete(Unit)
                                mat3SlowQueryRelease.await()
                                respond(
                                    """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r3"}}""",
                                    HttpStatusCode.ServiceUnavailable,
                                    jsonHeaders,
                                )
                            }
                            else -> {
                                mat3Extracted = true
                                okJson(
                                    json.encodeToString(
                                        JobDto(
                                            id = "job-ext-3",
                                            kind = "extract",
                                            target_id = "mat-3",
                                            input_revision = 1,
                                            state = "succeeded",
                                            attempt = 1,
                                            retryable = false,
                                            result_ref = ResultRefDto("material", "mat-3"),
                                            created_at = "2026-11-18T00:00:00Z",
                                            updated_at = "2026-11-18T00:00:00Z",
                                        ),
                                    ),
                                )
                            }
                        }
                    }
                    "/v1/materials/mat-3/parse" -> acceptedJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-parse-3",
                                kind = "parse",
                                target_id = "mat-3",
                                input_revision = 1,
                                state = "queued",
                                attempt = 1,
                                retryable = false,
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    "/v1/jobs/job-parse-3" -> okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-parse-3",
                                kind = "parse",
                                target_id = "mat-3",
                                input_revision = 1,
                                state = "queued",
                                attempt = 1,
                                retryable = false,
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:00Z",
                            ),
                        ),
                    )
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) 立即 succeeded 回执 + 回读 503 -> 同步 latest_job_id 并可只读恢复
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.job?.id == "job-ext-1" }
            vm.parseMaterial("mat-1", "java-backend", "intern")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            assertEquals(1, parsePostMat1.get())
            assertEquals("job-parse-imm-ok", vm.state.value.material?.latest_job_id)
            assertTrue(
                ReviewScreenPolicy.evaluate(
                    vm.state.value.material,
                    vm.state.value.job,
                    vm.state.value.materialJobLookup,
                ).showQueryJobStatusAction,
            )
            failMat1Reread = false
            vm.queryLatestMaterialJob("mat-1")
            drainAll { mat1Parsed && vm.state.value.material?.profile != null }
            assertTrue(parseGetMat1.get() >= 1)
            assertEquals(1, parsePostMat1.get())

            // 2) 立即 failed 回执 -> 同步 latest_job_id 并可显式重试
            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll { vm.state.value.job?.id == "job-ext-2" }
            vm.parseMaterial("mat-2", "java-backend", "intern")
            drainAll { vm.state.value.job?.id == "job-parse-imm-fail" }
            assertEquals(1, parsePostMat2.get())
            assertEquals("job-parse-imm-fail", vm.state.value.material?.latest_job_id)
            assertEquals("failed", vm.state.value.job?.state)
            val failPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(failPolicy.hasCurrentFailedParse)
            assertFalse(failPolicy.showStartParseAction)
            assertFalse(failPolicy.isJobLookupPending)
            vm.retryCurrentJob()
            drainAll { vm.state.value.material?.state == "parsed" }
            assertEquals(listOf(RetryRequest(expected_attempt = 1)), retryCalls)

            // 3) 过期 extract 回调不得反向替换新 parse Job 或污染状态
            val parseJobMat3 = JobDto(
                id = "job-parse-3",
                kind = "parse",
                target_id = "mat-3",
                input_revision = 1,
                state = "queued",
                attempt = 1,
                retryable = false,
                created_at = "2026-11-18T00:00:00Z",
                updated_at = "2026-11-18T00:00:00Z",
            )
            val staleExtractMat3 = JobDto(
                id = "job-ext-3",
                kind = "extract",
                target_id = "mat-3",
                input_revision = 1,
                state = "succeeded",
                attempt = 1,
                retryable = false,
                created_at = "2026-11-18T00:00:00Z",
                updated_at = "2026-11-18T00:00:00Z",
            )
            assertFalse(InterviewUiMerge.shouldApplyJob(parseJobMat3, staleExtractMat3))

            vm.back()
            vm.openReview("mat-3", "prep-1")
            drainAll { vm.state.value.job?.id == "job-ext-3" && vm.state.value.job?.state == "queued" }
            // 触发一次挂起的旧 extract 查询，随后让 JobWatch 完成 extract 并启动新 parse
            vm.queryCurrentJob()
            drainAll { mat3SlowQueryEntered.isCompleted }
            drainAll { vm.state.value.material?.state == "text_ready" }
            vm.parseMaterial("mat-3", "java-backend", "intern")
            drainAll { vm.state.value.job?.id == "job-parse-3" }
            // 放行过期的 extract 失败回调
            mat3SlowQueryRelease.complete(Unit)
            drainAll()
            assertEquals("job-parse-3", vm.state.value.job?.id)
            assertEquals("job-parse-3", vm.state.value.material?.latest_job_id)
            assertEquals("job-parse-3", vm.state.value.materialJobLookup?.jobId)
            assertFalse(vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED)
            assertFalse(vm.state.value.message?.isError == true)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun syntheticJobErrorCodeAndRequestIdDoNotLeakIntoAndroidDiagnostics() {
        val syntheticJob = parseJobDto(
            retryable = false,
            error = ErrorInfo(
                code = "SYNTHETIC_CODE_903",
                message = "SYNTHETIC_MSG_903",
                retryable = false,
                details = listOf(
                    "SYNTHETIC_DETAIL_903",
                    "work_experience.0.organization: Field required",
                ),
                request_id = "SYNTHETIC_REQ_903",
            ),
        )
        val friendly = ErrorDiagnosticsMapper.fromJob(syntheticJob)
        val diagText = friendly.formatDiagnosticText()
        for (marker in listOf(
            "SYNTHETIC_CODE_903",
            "SYNTHETIC_MSG_903",
            "SYNTHETIC_DETAIL_903",
            "SYNTHETIC_REQ_903",
        )) {
            assertFalse(friendly.code.orEmpty().contains(marker))
            assertFalse(friendly.rawServerMessage.contains(marker))
            assertFalse(friendly.userExplanation.contains(marker))
            assertFalse(friendly.details.any { it.contains(marker) })
            assertFalse(diagText.contains(marker))
        }
        assertEquals("INTERNAL_ERROR", friendly.code)
        assertTrue(diagText.contains("work_experience.0.organization: Field required"))
        assertTrue(diagText.contains("错误码 (Code): INTERNAL_ERROR"))

        // 正常错误码仍正确展示
        val normalTimeout = ErrorDiagnosticsMapper.fromJob(
            parseJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_TIMEOUT",
                    message = "AI 服务超时，结果未知",
                    retryable = true,
                    details = emptyList(),
                ),
            ),
        )
        assertEquals("AI_TIMEOUT", normalTimeout.code)
        assertTrue(normalTimeout.formatDiagnosticText().contains("错误码 (Code): AI_TIMEOUT"))
    }

    @Test
    fun materialRereadHigherRevisionClearsOldJobAndRestoresAuthoritativeLatestJob() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val oldJobGets = mapOf(
            "job-a-old" to AtomicInteger(0),
            "job-b-old" to AtomicInteger(0),
            "job-c-old" to AtomicInteger(0),
            "job-d-old" to AtomicInteger(0),
            "job-e-old" to AtomicInteger(0),
        )
        val materialGets = mapOf(
            "mat-a" to AtomicInteger(0),
            "mat-b" to AtomicInteger(0),
            "mat-c" to AtomicInteger(0),
            "mat-d" to AtomicInteger(0),
            "mat-e" to AtomicInteger(0),
        )
        val newJobDGets = AtomicInteger(0)
        val staleJobEGets = AtomicInteger(0)
        val parsePostCalls = AtomicInteger(0)

        fun succeededMaterialJob(id: String, targetId: String): JobDto = parseJobDto(
            id = id,
            revision = 1,
            state = "succeeded",
            attempt = 1,
            retryable = false,
            resultRef = ResultRefDto("material", targetId),
            targetId = targetId,
        )

        fun rereadMaterial(
            id: String,
            firstText: String,
            nextText: String,
            firstJobId: String?,
            nextJobId: String?,
        ): MaterialDraftDto {
            val count = materialGets.getValue(id).incrementAndGet()
            return materialDto(
                id = id,
                revision = if (count == 1) 1 else 2,
                state = "text_ready",
                text = if (count == 1) firstText else nextText,
                latestJobId = if (count == 1) firstJobId else nextJobId,
                hasProfile = false,
            )
        }

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")

                    "/v1/materials/mat-a", "/v1/materials/mat-b",
                    "/v1/materials/mat-c", "/v1/materials/mat-d",
                    "/v1/materials/mat-e" -> {
                        val id = request.url.encodedPath.substringAfterLast('/')
                        val dto = when (id) {
                            "mat-a" -> rereadMaterial(
                                id, "版本一正文A", "版本二正文A", "job-a-old", "job-a-new",
                            )
                            "mat-b" -> rereadMaterial(
                                id, "版本一正文B", "版本二正文B", "job-b-old", null,
                            )
                            "mat-c" -> rereadMaterial(
                                id, "版本一正文C", "版本二正文C", "job-c-old", "job-c-old",
                            )
                            "mat-d" -> rereadMaterial(
                                id, "版本一正文D", "版本二正文D", "job-d-old", "job-d-new",
                            )
                            else -> rereadMaterial(
                                id, "版本一正文E", "版本二正文E", "job-e-old", "job-e-stale",
                            )
                        }
                        okJson(json.encodeToString(dto))
                    }
                    "/v1/materials/mat-a/snapshots", "/v1/materials/mat-b/snapshots",
                    "/v1/materials/mat-c/snapshots", "/v1/materials/mat-d/snapshots",
                    "/v1/materials/mat-e/snapshots" -> okJson("""{"items":[]}""")

                    "/v1/jobs/job-a-old", "/v1/jobs/job-b-old",
                    "/v1/jobs/job-c-old", "/v1/jobs/job-d-old",
                    "/v1/jobs/job-e-old" -> {
                        val jobId = request.url.encodedPath.substringAfterLast('/')
                        oldJobGets.getValue(jobId).incrementAndGet()
                        val targetId = "mat-" + jobId.removePrefix("job-").removeSuffix("-old")
                        okJson(json.encodeToString(succeededMaterialJob(jobId, targetId)))
                    }
                    "/v1/jobs/job-a-new" -> okJson(
                        json.encodeToString(
                            parseJobDto(
                                id = "job-a-new",
                                revision = 2,
                                state = "failed",
                                attempt = 1,
                                retryable = true,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "AI 输出不符合契约结构",
                                    retryable = true,
                                    details = listOf("stage=parse", "reason=invalid_profile_schema"),
                                ),
                                targetId = "mat-a",
                            ),
                        ),
                    )
                    "/v1/jobs/job-d-new" -> {
                        newJobDGets.incrementAndGet()
                        respond(
                            """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"r-d"}}""",
                            HttpStatusCode.ServiceUnavailable,
                            jsonHeaders,
                        )
                    }
                    "/v1/jobs/job-e-stale" -> {
                        staleJobEGets.incrementAndGet()
                        okJson(
                            json.encodeToString(
                                parseJobDto(
                                    id = "job-e-stale",
                                    revision = 1,
                                    state = "superseded",
                                    attempt = 1,
                                    retryable = false,
                                    targetId = "mat-e",
                                ),
                            ),
                        )
                    }
                    "/v1/materials/mat-a/parse", "/v1/materials/mat-b/parse",
                    "/v1/materials/mat-c/parse", "/v1/materials/mat-d/parse",
                    "/v1/materials/mat-e/parse" -> {
                        parsePostCalls.incrementAndGet()
                        error("高版本只读恢复禁止 POST /parse")
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) 旧 Job succeeded + 材料回读返回 revision 2 和新 Job：读取新 Job 并展示其状态
            vm.openReview("mat-a", "prep-1")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.job?.id == "job-a-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("版本二正文A", vm.state.value.material?.text)
            assertEquals("failed", vm.state.value.job?.state)
            assertFalse(vm.state.value.message?.text.orEmpty().contains("解析完成"))
            assertEquals(1, oldJobGets.getValue("job-a-old").get())
            val policyA = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyA.hasCurrentFailedParse)
            assertFalse(policyA.showStartParseAction)
            assertFalse(policyA.isJobLookupPending)

            // 2) 新版本没有任务：清除旧 Job 与完成提示，直接按材料现状显示可用操作
            vm.back()
            vm.openReview("mat-b", "prep-1")
            drainAll {
                vm.state.value.material?.id == "mat-b" &&
                    vm.state.value.material?.revision == 2 &&
                    vm.state.value.job == null &&
                    vm.state.value.materialJobLookup == null
            }
            assertEquals("版本二正文B", vm.state.value.material?.text)
            assertEquals(1, oldJobGets.getValue("job-b-old").get())
            val policyB = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyB.showStartParseAction)
            assertFalse(policyB.isJobLookupPending)

            // 3) 服务器仍携带旧任务指针：确认属于旧版本后结束恢复，不循环查询也不阻断操作
            vm.back()
            vm.openReview("mat-c", "prep-1")
            drainAll {
                vm.state.value.material?.id == "mat-c" &&
                    vm.state.value.material?.revision == 2 &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("版本二正文C", vm.state.value.material?.text)
            assertEquals(null, vm.state.value.job)
            assertEquals("job-c-old", vm.state.value.materialJobLookup?.jobId)
            assertEquals(1, oldJobGets.getValue("job-c-old").get())
            val policyC = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyC.showStartParseAction)
            assertFalse(policyC.isJobLookupPending)
            assertFalse(policyC.showQueryJobStatusAction)

            // 4) 新任务查询 503：保留新版本原文，展示只读重查入口
            vm.back()
            vm.openReview("mat-d", "prep-1")
            drainAll {
                vm.state.value.material?.id == "mat-d" &&
                    vm.state.value.material?.revision == 2 &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED
            }
            assertEquals("版本二正文D", vm.state.value.material?.text)
            assertEquals(null, vm.state.value.job)
            assertEquals("job-d-new", vm.state.value.materialJobLookup?.jobId)
            assertTrue(newJobDGets.get() >= 1)
            val policyD = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyD.showQueryJobStatusAction)
            assertFalse(policyD.isJobLookupPending)
            assertFalse(policyD.showStartParseAction)

            // 5) 不同旧任务指针：只读查明属于旧版本后结束恢复，不展示旧任务也不循环查询
            vm.back()
            vm.openReview("mat-e", "prep-1")
            drainAll {
                vm.state.value.material?.id == "mat-e" &&
                    vm.state.value.material?.revision == 2 &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("版本二正文E", vm.state.value.material?.text)
            assertEquals(null, vm.state.value.job)
            assertEquals("job-e-stale", vm.state.value.materialJobLookup?.jobId)
            assertEquals(1, staleJobEGets.get())
            assertEquals(1, oldJobGets.getValue("job-e-old").get())
            val policyE = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyE.showStartParseAction)
            assertFalse(policyE.isJobLookupPending)
            assertFalse(policyE.showQueryJobStatusAction)
            assertEquals(0, parsePostCalls.get())
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun supersededCurrentMaterialJobRereadsMaterialReadOnlyOnOpenQueryPollAndAfterFailure() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val materialGets = mapOf(
            "mat-a" to AtomicInteger(0),
            "mat-b" to AtomicInteger(0),
            "mat-c" to AtomicInteger(0),
            "mat-d" to AtomicInteger(0),
        )
        val oldJobGets = mapOf(
            "job-a-old" to AtomicInteger(0),
            "job-b-old" to AtomicInteger(0),
            "job-c-old" to AtomicInteger(0),
            "job-d-old" to AtomicInteger(0),
        )
        val newJobGets = mapOf(
            "job-a-new" to AtomicInteger(0),
            "job-b-new" to AtomicInteger(0),
            "job-c-new" to AtomicInteger(0),
            "job-d-new" to AtomicInteger(0),
        )
        val snapshotsGets = AtomicInteger(0)
        val postCalls = AtomicInteger(0)

        fun rereadMaterial(
            id: String,
            firstText: String,
            nextText: String,
            firstJobId: String,
            nextJobId: String,
        ): MaterialDraftDto {
            val count = materialGets.getValue(id).incrementAndGet()
            return materialDto(
                id = id,
                revision = if (count == 1) 1 else 2,
                state = "text_ready",
                text = if (count == 1) firstText else nextText,
                latestJobId = if (count == 1) firstJobId else nextJobId,
                hasProfile = false,
            )
        }

        fun failedNewJob(id: String, targetId: String): JobDto = parseJobDto(
            id = id,
            revision = 2,
            state = "failed",
            attempt = 1,
            retryable = true,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "AI 输出不符合契约结构",
                retryable = true,
                details = listOf("stage=parse", "reason=invalid_profile_schema"),
            ),
            targetId = targetId,
        )

        fun supersededOldJob(id: String, targetId: String): JobDto = parseJobDto(
            id = id,
            revision = 1,
            state = "superseded",
            attempt = 1,
            retryable = false,
            targetId = targetId,
        )

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")

                    "/v1/materials/mat-a" -> okJson(
                        json.encodeToString(
                            rereadMaterial("mat-a", "旧正文A", "新正文A", "job-a-old", "job-a-new"),
                        ),
                    )
                    "/v1/materials/mat-b" -> {
                        val count = materialGets.getValue("mat-b").incrementAndGet()
                        when (count) {
                            1 -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-b",
                                        revision = 1,
                                        text = "旧正文B",
                                        latestJobId = "job-b-old",
                                    ),
                                ),
                            )
                            else -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-b",
                                        revision = 2,
                                        text = "新正文B",
                                        latestJobId = "job-b-new",
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-c" -> okJson(
                        json.encodeToString(
                            rereadMaterial("mat-c", "旧正文C", "新正文C", "job-c-old", "job-c-new"),
                        ),
                    )
                    "/v1/materials/mat-d" -> {
                        when (materialGets.getValue("mat-d").incrementAndGet()) {
                            1 -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-d",
                                        revision = 1,
                                        text = "旧正文D",
                                        latestJobId = "job-d-old",
                                    ),
                                ),
                            )
                            2 -> respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"rd"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                            else -> okJson(
                                json.encodeToString(
                                    materialDto(
                                        id = "mat-d",
                                        revision = 2,
                                        text = "新正文D",
                                        latestJobId = "job-d-new",
                                    ),
                                ),
                            )
                        }
                    }
                    "/v1/materials/mat-a/snapshots", "/v1/materials/mat-b/snapshots",
                    "/v1/materials/mat-c/snapshots", "/v1/materials/mat-d/snapshots" -> {
                        snapshotsGets.incrementAndGet()
                        okJson("""{"items":[]}""")
                    }

                    "/v1/jobs/job-a-old", "/v1/jobs/job-d-old" -> {
                        val jobId = request.url.encodedPath.substringAfterLast('/')
                        oldJobGets.getValue(jobId).incrementAndGet()
                        val targetId = "mat-" + jobId.removePrefix("job-").removeSuffix("-old")
                        okJson(json.encodeToString(supersededOldJob(jobId, targetId)))
                    }
                    "/v1/jobs/job-b-old" -> {
                        val count = oldJobGets.getValue("job-b-old").incrementAndGet()
                        if (count == 1) {
                            respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"rb"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                        } else {
                            okJson(json.encodeToString(supersededOldJob("job-b-old", "mat-b")))
                        }
                    }
                    "/v1/jobs/job-c-old" -> {
                        val count = oldJobGets.getValue("job-c-old").incrementAndGet()
                        if (count == 1) {
                            okJson(
                                json.encodeToString(
                                    parseJobDto(
                                        id = "job-c-old",
                                        revision = 1,
                                        state = "running",
                                        attempt = 1,
                                        retryable = false,
                                        targetId = "mat-c",
                                    ),
                                ),
                            )
                        } else {
                            okJson(json.encodeToString(supersededOldJob("job-c-old", "mat-c")))
                        }
                    }
                    "/v1/jobs/job-a-new" -> {
                        newJobGets.getValue("job-a-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-a-new", "mat-a")))
                    }
                    "/v1/jobs/job-b-new" -> {
                        newJobGets.getValue("job-b-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-b-new", "mat-b")))
                    }
                    "/v1/jobs/job-c-new" -> {
                        newJobGets.getValue("job-c-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-c-new", "mat-c")))
                    }
                    "/v1/jobs/job-d-new" -> {
                        newJobGets.getValue("job-d-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-d-new", "mat-d")))
                    }
                    "/v1/materials/mat-a/parse", "/v1/materials/mat-b/parse",
                    "/v1/materials/mat-c/parse", "/v1/materials/mat-d/parse" -> {
                        postCalls.incrementAndGet()
                        error("失效任务恢复只能发送 GET")
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) 首次打开：GET revision 1 -> Job superseded -> GET revision 2 -> 查询新 Job
            vm.openReview("mat-a", "prep-1")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.job?.id == "job-a-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("新正文A", vm.state.value.material?.text)
            assertEquals("failed", vm.state.value.job?.state)
            assertEquals(1, oldJobGets.getValue("job-a-old").get())
            assertEquals(1, newJobGets.getValue("job-a-new").get())
            assertEquals(2, materialGets.getValue("mat-a").get())
            val policyA = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(policyA.hasCurrentFailedParse)
            assertFalse(policyA.showStartParseAction)
            assertFalse(policyA.isJobLookupPending)

            // 2) 主动查询：首次 Job GET 失败，点击只读查询后发现 superseded 并回读新版
            vm.back()
            vm.openReview("mat-b", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            assertEquals("旧正文B", vm.state.value.material?.text)
            vm.queryLatestMaterialJob("mat-b")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.job?.id == "job-b-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("新正文B", vm.state.value.material?.text)
            assertEquals(2, oldJobGets.getValue("job-b-old").get())
            assertEquals(1, newJobGets.getValue("job-b-new").get())
            assertEquals(2, materialGets.getValue("mat-b").get())

            // 3) 运行中轮询：Job 变为 superseded 后同样自动回读新版，不需要退出页面
            vm.back()
            vm.openReview("mat-c", "prep-1")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.job?.id == "job-c-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("新正文C", vm.state.value.material?.text)
            assertTrue(oldJobGets.getValue("job-c-old").get() >= 2)
            assertEquals(1, newJobGets.getValue("job-c-new").get())
            assertEquals(2, materialGets.getValue("mat-c").get())

            // 4) 回读失败：保留原文与只读刷新入口；再次查询时重试回读并恢复新版
            vm.back()
            vm.openReview("mat-d", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            assertEquals("旧正文D", vm.state.value.material?.text)
            assertEquals(null, vm.state.value.job)
            val failedPolicy = ReviewScreenPolicy.evaluate(
                vm.state.value.material,
                vm.state.value.job,
                vm.state.value.materialJobLookup,
            )
            assertTrue(failedPolicy.showQueryJobStatusAction)
            assertFalse(failedPolicy.showStartParseAction)
            assertFalse(failedPolicy.isJobLookupPending)

            vm.queryLatestMaterialJob("mat-d")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.job?.id == "job-d-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals("新正文D", vm.state.value.material?.text)
            assertEquals(3, materialGets.getValue("mat-d").get())
            assertEquals(1, newJobGets.getValue("job-d-new").get())
            assertEquals(0, postCalls.get())
            assertTrue(snapshotsGets.get() >= 4)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun supersededRereadAllowsReopenedPageToTakeOverAndDeduplicatesSamePageQuery() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val materialGets = mapOf(
            "mat-e" to AtomicInteger(0),
            "mat-f" to AtomicInteger(0),
        )
        val oldJobGets = mapOf(
            "job-e-old" to AtomicInteger(0),
            "job-f-old" to AtomicInteger(0),
        )
        val newJobGets = mapOf(
            "job-e-new" to AtomicInteger(0),
            "job-f-new" to AtomicInteger(0),
        )
        val parsePostCalls = AtomicInteger(0)
        val oldRereadEntered = CompletableDeferred<Unit>()
        val oldRereadRelease = CompletableDeferred<Unit>()

        fun material(id: String, revision: Int, text: String, jobId: String): MaterialDraftDto =
            materialDto(
                id = id,
                revision = revision,
                state = "text_ready",
                text = text,
                latestJobId = jobId,
                hasProfile = false,
            )

        fun supersededJob(id: String, targetId: String): JobDto = parseJobDto(
            id = id,
            revision = 1,
            state = "superseded",
            attempt = 1,
            retryable = false,
            targetId = targetId,
        )

        fun failedNewJob(id: String, targetId: String): JobDto = parseJobDto(
            id = id,
            revision = 2,
            state = "failed",
            attempt = 1,
            retryable = true,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "AI 输出不符合契约结构",
                retryable = true,
                details = listOf("stage=parse", "reason=invalid_profile_schema"),
            ),
            targetId = targetId,
        )

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = MockEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> okJson(
                        json.encodeToString(
                            TokenResponse(
                                access_token = "tok-a",
                                token_type = "bearer",
                                expires_at = "2026-12-01T00:00:00Z",
                                user = UserDto("acc-a", "userA"),
                            ),
                        ),
                    )
                    "/v1/roles" -> okJson("""{"items":[]}""")
                    "/v1/skill-codes" -> okJson("""{"items":[]}""")
                    "/v1/preparations" -> okJson("""{"items":[]}""")
                    "/v1/materials" -> okJson("""{"items":[]}""")

                    "/v1/materials/mat-e" -> {
                        when (materialGets.getValue("mat-e").incrementAndGet()) {
                            1 -> okJson(
                                json.encodeToString(material("mat-e", 1, "旧正文E", "job-e-old")),
                            )
                            2 -> {
                                // 第一页的失效回读尚未返回；新页面此时重开同一材料。
                                oldRereadEntered.complete(Unit)
                                oldRereadRelease.await()
                                okJson(
                                    json.encodeToString(
                                        material("mat-e", 2, "新正文E", "job-e-new"),
                                    ),
                                )
                            }
                            3 -> okJson(
                                json.encodeToString(material("mat-e", 1, "旧正文E", "job-e-old")),
                            )
                            else -> okJson(
                                json.encodeToString(material("mat-e", 2, "新正文E", "job-e-new")),
                            )
                        }
                    }
                    "/v1/materials/mat-f" -> {
                        if (materialGets.getValue("mat-f").incrementAndGet() == 1) {
                            okJson(
                                json.encodeToString(material("mat-f", 1, "旧正文F", "job-f-old")),
                            )
                        } else {
                            okJson(
                                json.encodeToString(material("mat-f", 2, "新正文F", "job-f-new")),
                            )
                        }
                    }
                    "/v1/materials/mat-e/snapshots", "/v1/materials/mat-f/snapshots" ->
                        okJson("""{"items":[]}""")

                    "/v1/jobs/job-e-old", "/v1/jobs/job-f-old" -> {
                        val jobId = request.url.encodedPath.substringAfterLast('/')
                        val count = oldJobGets.getValue(jobId).incrementAndGet()
                        if (jobId == "job-f-old" && count == 1) {
                            respond(
                                """{"error":{"code":"AI_UNAVAILABLE","message":"暂不可用","retryable":true,"details":[],"request_id":"rf"}}""",
                                HttpStatusCode.ServiceUnavailable,
                                jsonHeaders,
                            )
                        } else {
                            val targetId = "mat-" + jobId.removePrefix("job-").removeSuffix("-old")
                            okJson(json.encodeToString(supersededJob(jobId, targetId)))
                        }
                    }
                    "/v1/jobs/job-e-new" -> {
                        newJobGets.getValue("job-e-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-e-new", "mat-e")))
                    }
                    "/v1/jobs/job-f-new" -> {
                        newJobGets.getValue("job-f-new").incrementAndGet()
                        okJson(json.encodeToString(failedNewJob("job-f-new", "mat-f")))
                    }
                    "/v1/materials/mat-e/parse", "/v1/materials/mat-f/parse" -> {
                        parsePostCalls.incrementAndGet()
                        error("失效任务恢复只能发送 GET")
                    }
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // 1) 第一页的失效回读被阻塞后退出并重开：新页面必须能发起自己的回读
            vm.openReview("mat-e", "prep-1")
            drainAll { oldRereadEntered.isCompleted }
            assertEquals(1, vm.state.value.material?.revision)
            assertEquals(MaterialJobLookupStatus.LOADING, vm.state.value.materialJobLookup?.status)

            vm.back()
            vm.openReview("mat-e", "prep-1")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.material?.text == "新正文E" &&
                    vm.state.value.job?.id == "job-e-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals(4, materialGets.getValue("mat-e").get())
            assertEquals(2, oldJobGets.getValue("job-e-old").get())
            assertEquals(1, newJobGets.getValue("job-e-new").get())

            // 旧响应放行后只能被丢弃，不得覆盖新页面已同步的版本和任务
            oldRereadRelease.complete(Unit)
            drainAll()
            assertEquals(2, vm.state.value.material?.revision)
            assertEquals("新正文E", vm.state.value.material?.text)
            assertEquals("job-e-new", vm.state.value.job?.id)
            assertEquals("failed", vm.state.value.job?.state)
            assertEquals(MaterialJobLookupStatus.LOADED, vm.state.value.materialJobLookup?.status)
            assertEquals(4, materialGets.getValue("mat-e").get())
            assertEquals(2, oldJobGets.getValue("job-e-old").get())
            assertEquals(0, parsePostCalls.get())

            // 2) 同一页重复点击查询只发一次失效回读
            vm.back()
            vm.openReview("mat-f", "prep-1")
            drainAll { vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.FAILED }
            assertEquals("旧正文F", vm.state.value.material?.text)
            vm.queryLatestMaterialJob("mat-f")
            vm.queryLatestMaterialJob("mat-f")
            drainAll {
                vm.state.value.material?.revision == 2 &&
                    vm.state.value.material?.text == "新正文F" &&
                    vm.state.value.job?.id == "job-f-new" &&
                    vm.state.value.materialJobLookup?.status == MaterialJobLookupStatus.LOADED
            }
            assertEquals(2, materialGets.getValue("mat-f").get())
            assertEquals(2, oldJobGets.getValue("job-f-old").get())
            assertEquals(1, newJobGets.getValue("job-f-new").get())
            assertEquals(0, parsePostCalls.get())
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
