package com.mockmate.app

import com.mockmate.app.core.network.AnchorDto
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.MatchRowDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.RetryRequest
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.ActiveJob
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.ErrorDiagnosticsMapper
import com.mockmate.app.ui.InterviewUiMerge
import com.mockmate.app.ui.components.JobRecoveryPolicy
import com.mockmate.app.ui.screens.MatchScreenPolicy
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
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
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MatchEvidenceRecoveryTest {

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
                Thread.sleep(5)
            }
        }
    }

    private fun prepDto(
        id: String = "prep-1",
        revision: Int = 4,
        matchReportId: String? = null,
    ) = PreparationDto(
        id = id,
        revision = revision,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 4,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        match_report_id = matchReportId,
        current_plan_id = null,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    private fun matchJobDto(
        id: String = "job-match-1",
        revision: Int = 4,
        state: String = "failed",
        attempt: Int = 1,
        retryable: Boolean = false,
        error: ErrorInfo? = null,
        resultRef: ResultRefDto? = null,
    ) = JobDto(
        id = id,
        kind = "match",
        target_id = "prep-1",
        input_revision = revision,
        state = state,
        attempt = attempt,
        retryable = retryable,
        result_ref = resultRef,
        error = error,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    private fun reportDto(
        id: String = "report-1",
        inputRevision: Int = 4,
    ) = MatchReportDto(
        id = id,
        preparation_id = "prep-1",
        input_revision = inputRevision,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        rows = listOf(
            MatchRowDto(
                requirement_id = "req-1",
                status = "supported",
                resume_evidence = listOf(
                    AnchorDto(
                        snapshot_id = "resume-1",
                        item_id = "skill-1",
                        quote = "熟悉 Java 集合基础。",
                    ),
                ),
                explanation = "技能条目直接支持。",
                suggested_focus = "说明集合边界。",
            ),
        ),
        created_at = "2026-11-18T00:00:00Z",
    )

    @Test
    fun errorDiagnosticsCoversLegacyAndStructuredMatchErrorsForRetryableAndNonRetryable() {
        val legacyRetryable = ErrorDiagnosticsMapper.fromJob(
            matchJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "引用原文不属于所引用的材料条目",
                    retryable = true,
                    details = listOf("skill-1", "坏引文"),
                    request_id = "req-legacy",
                ),
            ),
        )
        assertEquals(
            "本次分析未能把引用关联到正确的简历条目。本次匹配报告未生成，已确认的材料已保留。",
            legacyRetryable.userExplanation,
        )
        assertTrue(legacyRetryable.suggestion.orEmpty().contains("重试匹配分析"))
        assertTrue(legacyRetryable.suggestion.orEmpty().contains("再次调用模型"))
        assertFalse(legacyRetryable.suggestion.orEmpty().contains("丰富简历"))
        assertTrue(legacyRetryable.formatDiagnosticText().contains("kind=match"))

        val legacyNotRetryable = ErrorDiagnosticsMapper.fromJob(
            matchJobDto(
                attempt = 2,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "引用原文不属于所引用的材料条目",
                    retryable = false,
                    details = listOf("skill-1"),
                    request_id = "req-legacy-2",
                ),
            ),
        )
        assertEquals(
            "本次分析未能把引用关联到正确的简历条目。本次匹配报告未生成，已确认的材料已保留。",
            legacyNotRetryable.userExplanation,
        )
        assertFalse(legacyNotRetryable.suggestion.orEmpty().contains("重试"))
        assertFalse(legacyNotRetryable.suggestion.orEmpty().contains("丰富简历"))

        val structuredRetryable = ErrorDiagnosticsMapper.fromJob(
            matchJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "匹配结果引用了不存在或不可引用的简历条目 ID",
                    retryable = true,
                    details = listOf(
                        "stage=match",
                        "reason=invalid_evidence_selection",
                        "requirement_id=req-1",
                        "item_id=skill-99",
                    ),
                    request_id = "req-new-1",
                ),
            ),
        )
        assertEquals(
            "本次分析未能把引用关联到正确的简历条目。本次匹配报告未生成，已确认的材料已保留。",
            structuredRetryable.userExplanation,
        )
        assertTrue(structuredRetryable.suggestion.orEmpty().contains("重试匹配分析"))
        assertFalse(structuredRetryable.suggestion.orEmpty().contains("丰富简历"))
        assertTrue(structuredRetryable.formatDiagnosticText().contains("invalid_evidence_selection"))

        val structuredNotRetryable = ErrorDiagnosticsMapper.fromJob(
            matchJobDto(
                attempt = 2,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "匹配结果引用了不存在或不可引用的简历条目 ID",
                    retryable = false,
                    details = listOf("stage=match", "reason=invalid_evidence_selection"),
                    request_id = "req-new-2",
                ),
            ),
        )
        assertFalse(structuredNotRetryable.suggestion.orEmpty().contains("重试"))
        assertFalse(structuredNotRetryable.suggestion.orEmpty().contains("丰富简历"))

        // 服务端明确给出材料缺失或待确认原因时，仍引导修改材料。
        val missingMaterial = ErrorDiagnosticsMapper.resolve(
            code = "MATERIALS_REQUIRED",
            rawMessage = "请先确认简历和 JD",
            retryable = false,
            jobKind = "match",
        )
        assertTrue(missingMaterial.suggestion.orEmpty().contains("确认"))
    }

    @Test
    fun matchScreenPolicyAndRecoveryButtonFollowServerRetryabilityAndRevision() {
        val currentRetryableJob = matchJobDto(revision = 4, state = "failed", retryable = true)
        val currentRetryablePolicy = MatchScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = currentRetryableJob,
            loading = false,
        )
        assertTrue(currentRetryablePolicy.hasCurrentFailedMatch)
        assertFalse(currentRetryablePolicy.showStartMatchAction)
        assertFalse(currentRetryablePolicy.startMatchEnabled)
        assertEquals("重试匹配分析（会再次调用模型）", JobRecoveryPolicy.retryButtonText(currentRetryableJob))

        val currentNonRetryableJob = matchJobDto(revision = 4, state = "failed", attempt = 2, retryable = false)
        val currentNonRetryablePolicy = MatchScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = currentNonRetryableJob,
            loading = false,
        )
        assertTrue(currentNonRetryablePolicy.hasCurrentFailedMatch)
        assertFalse(currentNonRetryablePolicy.showStartMatchAction)
        assertFalse(currentNonRetryablePolicy.startMatchEnabled)

        // 旧版本（rev=3）的失败结果不应挡住新版本（rev=4）的「开始匹配分析」。
        val oldRevisionFailedJob = matchJobDto(revision = 3, state = "failed", attempt = 1, retryable = false)
        val newRevisionPolicy = MatchScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = oldRevisionFailedJob,
            loading = false,
        )
        assertFalse(newRevisionPolicy.hasCurrentFailedMatch)
        assertTrue(newRevisionPolicy.showStartMatchAction)
        assertTrue(newRevisionPolicy.startMatchEnabled)
    }

    @Test
    fun queryRefreshesRetryabilityAndRetryCallsExistingEndpointToPublishReport() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val operationStore = InMemoryOperationStore()
        operationStore.save(
            "acc-a",
            AccountOperations(
                jobs = listOf(
                    ActiveJob(
                        jobId = "job-match-1",
                        kind = "match",
                        targetId = "prep-1",
                        preparationId = "prep-1",
                        revision = 4,
                    ),
                ),
            ),
        )
        val getJobCount = AtomicInteger(0)
        val retryCalls = mutableListOf<RetryRequest>()
        val matchStartCalls = AtomicInteger(0)

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = operationStore,
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
                    "/v1/preparations" -> okJson(json.encodeToString(mapOf("items" to listOf(prepDto(revision = 4)))))
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/preparations/prep-1/match" -> {
                        matchStartCalls.incrementAndGet()
                        error("不应自动或通过开始接口重试")
                    }
                    "/v1/jobs/job-match-1" -> {
                        val n = getJobCount.incrementAndGet()
                        val dto = when (n) {
                            1 -> matchJobDto(
                                revision = 4,
                                state = "failed",
                                attempt = 1,
                                retryable = false,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "引用原文不属于所引用的材料条目",
                                    retryable = false,
                                    details = listOf("skill-1"),
                                    request_id = "legacy",
                                ),
                            )
                            2 -> matchJobDto(
                                revision = 4,
                                state = "failed",
                                attempt = 1,
                                retryable = true,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "引用原文不属于所引用的材料条目",
                                    retryable = true,
                                    details = listOf("skill-1"),
                                    request_id = "legacy",
                                ),
                            )
                            else -> matchJobDto(
                                revision = 4,
                                state = "succeeded",
                                attempt = 2,
                                retryable = false,
                                resultRef = ResultRefDto("match_report", "report-1"),
                            )
                        }
                        okJson(json.encodeToString(dto))
                    }
                    "/v1/jobs/job-match-1/retry" -> {
                        assertEquals(HttpMethod.Post, request.method)
                        val bodyText = (request.body as TextContent).text
                        retryCalls += json.decodeFromString<RetryRequest>(bodyText)
                        acceptedJson(
                            json.encodeToString(
                                matchJobDto(revision = 4, state = "queued", attempt = 2, retryable = false),
                            ),
                        )
                    }
                    "/v1/preparations/prep-1" -> okJson(
                        json.encodeToString(prepDto(revision = 4, matchReportId = "report-1")),
                    )
                    "/v1/match-reports/report-1" -> okJson(
                        json.encodeToString(reportDto(id = "report-1", inputRevision = 4)),
                    )
                    else -> error("Unexpected path: ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )

        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null && vm.state.value.preparations.isNotEmpty() }
            vm.openPreparation("prep-1")
            drainAll { vm.state.value.preparation?.id == "prep-1" }

            vm.openMatch("prep-1")
            drainAll { vm.state.value.job?.id == "job-match-1" && getJobCount.get() >= 1 }
            assertFalse(vm.state.value.job!!.retryable)
            assertEquals(0, matchStartCalls.get())

            // 只读查询刷新服务端重试资格，不触发重跑。
            vm.queryCurrentJob()
            drainAll { vm.state.value.job?.retryable == true }
            assertEquals(0, matchStartCalls.get())
            assertTrue(retryCalls.isEmpty())

            // 点击重试走已有 /v1/jobs/{id}/retry 接口并最终恢复匹配报告。
            vm.retryCurrentJob()
            drainAll { vm.state.value.matchReport?.id == "report-1" }
            assertEquals(listOf(RetryRequest(expected_attempt = 1)), retryCalls)
            assertEquals("report-1", vm.state.value.matchReport?.id)
            assertEquals("report-1", vm.state.value.preparation?.match_report_id)
            assertEquals(0, matchStartCalls.get())
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun lateMatchResultFromOldPreparationRevisionDoesNotOverwriteCurrentPageOrReport() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val oldJobEntered = CompletableDeferred<Unit>()
        val oldJobRelease = CompletableDeferred<Unit>()
        val operationStore = InMemoryOperationStore()
        operationStore.save(
            "acc-a",
            AccountOperations(
                jobs = listOf(
                    ActiveJob(
                        jobId = "job-old",
                        kind = "match",
                        targetId = "prep-1",
                        preparationId = "prep-1",
                        revision = 1,
                    ),
                ),
            ),
        )

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = operationStore,
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
                    "/v1/preparations" -> okJson(
                        json.encodeToString(
                            mapOf("items" to listOf(prepDto(revision = 2, matchReportId = "report-rev2"))),
                        ),
                    )
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/preparations/prep-1" -> okJson(
                        json.encodeToString(prepDto(revision = 2, matchReportId = "report-rev2")),
                    )
                    "/v1/match-reports/report-rev2" -> okJson(
                        json.encodeToString(reportDto(id = "report-rev2", inputRevision = 2)),
                    )
                    "/v1/match-reports/report-rev1" -> okJson(
                        json.encodeToString(reportDto(id = "report-rev1", inputRevision = 1)),
                    )
                    "/v1/jobs/job-old" -> {
                        oldJobEntered.complete(Unit)
                        oldJobRelease.await()
                        okJson(
                            json.encodeToString(
                                matchJobDto(
                                    id = "job-old",
                                    revision = 1,
                                    state = "succeeded",
                                    attempt = 1,
                                    retryable = false,
                                    resultRef = ResultRefDto("match_report", "report-rev1"),
                                ),
                            ),
                        )
                    }
                    "/v1/preparations/prep-1/match" -> acceptedJson(
                        json.encodeToString(
                            matchJobDto(
                                id = "job-new",
                                revision = 2,
                                state = "succeeded",
                                attempt = 1,
                                retryable = false,
                                resultRef = ResultRefDto("match_report", "report-rev2"),
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
            drainAll { vm.state.value.account != null && vm.state.value.preparations.isNotEmpty() }
            vm.openPreparation("prep-1")
            drainAll { vm.state.value.preparation?.revision == 2 }

            // 打开 rev=2 匹配页面：恢复旧任务 job-old 被卡在网络层。
            vm.openMatch("prep-1")
            drainAll { oldJobEntered.isCompleted && vm.state.value.matchReport?.id == "report-rev2" }

            // 同时新版本任务 job-new 完成并写入当前页面。
            vm.runMatch("prep-1")
            drainAll { vm.state.value.job?.id == "job-new" && vm.state.value.matchReport?.id == "report-rev2" }

            // 放行迟到的旧版本 job-old 响应：不得覆盖当前页面、报告或新版本 job。
            oldJobRelease.complete(Unit)
            drainAll()

            assertEquals(2, vm.state.value.preparation?.revision)
            assertEquals("report-rev2", vm.state.value.matchReport?.id)
            assertEquals("job-new", vm.state.value.job?.id)
            assertFalse(
                InterviewUiMerge.shouldApplyJob(
                    matchJobDto(id = "job-new", revision = 2, state = "succeeded"),
                    matchJobDto(id = "job-old", revision = 1, state = "succeeded"),
                ),
            )
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
