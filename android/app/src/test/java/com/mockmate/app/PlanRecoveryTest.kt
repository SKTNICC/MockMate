package com.mockmate.app

import com.mockmate.app.core.network.CreatePreparationRequest
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.RetryRequest
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SourceCountsDto
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
import com.mockmate.app.ui.PlanErrorState
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.components.JobRecoveryPolicy
import com.mockmate.app.ui.screens.PlanErrorFormatter
import com.mockmate.app.ui.screens.PlanScreenPolicy
import com.mockmate.app.ui.screens.PlanUiPolicyState
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
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class PlanRecoveryTest {

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

    private fun prepDto(
        id: String = "prep-1",
        revision: Int = 4,
        matchReportId: String? = "report-1",
        currentPlanId: String? = null,
    ) = PreparationDto(
        id = id,
        revision = revision,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        match_report_id = matchReportId,
        current_plan_id = currentPlanId,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    private fun planJobDto(
        id: String = "job-plan-1",
        targetId: String = "prep-1",
        revision: Int = 4,
        state: String = "failed",
        attempt: Int = 1,
        retryable: Boolean = false,
        error: ErrorInfo? = null,
        resultRef: ResultRefDto? = null,
    ) = JobDto(
        id = id,
        kind = "plan",
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

    private fun publicPlanDto(
        id: String = "plan-1",
        preparationId: String = "prep-1",
        inputRevision: Int = 4,
    ) = PublicPlanDto(
        id = id,
        preparation_id = preparationId,
        input_revision = inputRevision,
        revision = 1,
        state = "ready",
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        source_counts = SourceCountsDto(bank = 3, generated = 3),
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        focus_areas = listOf("Java 集合与边界测试"),
        limitations = emptyList(),
        created_at = "2026-11-18T00:00:00Z",
    )

    private fun failureCardCount(policy: PlanUiPolicyState): Int {
        val jobCard = if (policy.recoveryJob != null &&
            (policy.recoveryJob.state == "failed" || policy.recoveryJob.state == "interrupted")
        ) 1 else 0
        val errorCard = if (policy.showPlanErrorCard) 1 else 0
        return jobCard + errorCard
    }

    @Test
    fun errorDiagnosticsMapsLegacyPositionalAndStructuredPlanFailures() {
        val legacyRetryable = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = true,
                    details = listOf("generated-1", "AI 返回内容不是合法 JSON"),
                    request_id = "req-legacy",
                ),
            ),
        )
        assertEquals(
            "AI 生成的第 1 道题返回了无法读取的结果。本次计划未保存，已确认的材料和匹配报告已保留。",
            legacyRetryable.userExplanation,
        )
        assertFalse(legacyRetryable.userExplanation.contains("模型输出中途结束"))
        assertTrue(legacyRetryable.suggestion.orEmpty().contains("重试生成计划"))

        val structuredStop = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = true,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-1",
                        "reason=invalid_json",
                        "finish_reason=stop",
                    ),
                    request_id = "req-stop",
                ),
            ),
        )
        assertEquals(
            "AI 生成的第 1 道题返回了无法读取的结果。本次计划未保存，已确认的材料和匹配报告已保留。",
            structuredStop.userExplanation,
        )
        assertFalse(structuredStop.userExplanation.contains("模型输出中途结束"))

        val truncated = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                retryable = true,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = true,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-2",
                        "reason=output_truncated",
                        "finish_reason=length",
                    ),
                    request_id = "req-len",
                ),
            ),
        )
        assertTrue(truncated.userExplanation.contains("第 2 道题"))
        assertTrue(truncated.userExplanation.contains("模型输出中途结束"))
        assertTrue(truncated.suggestion.orEmpty().contains("重试生成计划"))

        val exhausted = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                attempt = 2,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-1",
                        "reason=output_truncated",
                        "finish_reason=length",
                    ),
                    request_id = "req-exhausted",
                ),
            ),
        )
        assertTrue(exhausted.suggestion.orEmpty().contains("已用完 2 次生成尝试"))
        assertFalse(exhausted.suggestion.orEmpty().contains("最大重试次数"))
        assertFalse(exhausted.suggestion.orEmpty().contains("点击「重试生成计划」"))

        val duplicatePrior = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                attempt = 2,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-3",
                        "reason=duplicate_stem",
                        "duplicate_kind=exact",
                        "conflict_source=prior_generated",
                        "conflict_slot=generated-1",
                    ),
                    request_id = "req-dup-prior",
                ),
            ),
        )
        assertEquals(
            "第 3 道 AI 题与本场已有题目过于相似，本次计划未发布。已确认的材料仍可复用。",
            duplicatePrior.userExplanation,
        )
        assertTrue(duplicatePrior.suggestion.orEmpty().contains("已用完 2 次生成尝试"))
        assertFalse(duplicatePrior.suggestion.orEmpty().contains("最大重试次数"))

        val duplicateBank = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                attempt = 1,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-3",
                        "reason=duplicate_stem",
                        "duplicate_kind=near",
                        "conflict_source=bank",
                        "conflict_question_id=M1-JAVA-0001",
                    ),
                    request_id = "req-dup-bank",
                ),
            ),
        )
        assertEquals(
            "第 3 道 AI 题与题库已有题目过于相似，本次计划未发布。已确认的材料仍可复用。",
            duplicateBank.userExplanation,
        )
        assertTrue(duplicateBank.suggestion.orEmpty().contains("沿用材料新建准备"))

        val duplicateLegacy = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                attempt = 2,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf(
                        "stage=plan",
                        "slot=generated-3",
                        "生成题与本次禁止题干重复",
                        "重复题干：旧前缀",
                        "forbidden=旧前缀",
                    ),
                    request_id = "req-dup-legacy",
                ),
            ),
        )
        assertEquals(
            "第 3 道 AI 题与本场已有题目过于相似，本次计划未发布。已确认的材料仍可复用。",
            duplicateLegacy.userExplanation,
        )
        assertTrue(duplicateLegacy.suggestion.orEmpty().contains("已用完 2 次生成尝试"))

        val superseded = ErrorDiagnosticsMapper.fromJob(
            planJobDto(
                state = "superseded",
                attempt = 1,
                retryable = false,
                error = ErrorInfo(
                    code = "AI_INVALID_OUTPUT",
                    message = "生成槽位在有限纠正后仍不合法",
                    retryable = false,
                    details = listOf("stage=plan", "slot=generated-1", "reason=invalid_json"),
                ),
            ),
        )
        assertTrue(superseded.suggestion.orEmpty().contains("已更新"))
    }

    @Test
    fun planScreenPolicyShowsSingleFailureCardAndPreservesBankInsufficientCard() {
        val failedJob = planJobDto(
            id = "job-plan-1",
            revision = 4,
            state = "failed",
            attempt = 1,
            retryable = true,
            error = ErrorInfo(
                code = "AI_INVALID_OUTPUT",
                message = "生成槽位在有限纠正后仍不合法",
                retryable = true,
                details = listOf("stage=plan", "slot=generated-1", "reason=invalid_json", "finish_reason=stop"),
            ),
        )
        val duplicatedPlanError = PlanErrorState(
            code = "AI_INVALID_OUTPUT",
            message = "生成槽位在有限纠正后仍不合法",
            details = listOf("stage=plan", "slot=generated-1", "reason=invalid_json", "finish_reason=stop"),
            rawServerMessage = "生成槽位在有限纠正后仍不合法",
            retryable = true,
            jobId = "job-plan-1",
            preparationId = "prep-1",
            inputRevision = 4,
        )
        val failedPolicy = PlanScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = failedJob,
            planError = duplicatedPlanError,
            loading = false,
        )
        assertEquals(1, failureCardCount(failedPolicy))
        assertFalse(failedPolicy.showPlanErrorCard)
        assertTrue(failedPolicy.hasCurrentFailedPlan)
        assertFalse(failedPolicy.showStartPlanAction)
        assertEquals("重试生成计划（会再次调用模型）", JobRecoveryPolicy.retryButtonText(failedJob))

        // 同步 BANK_INSUFFICIENT（无后台任务）继续单独展示题库不足卡片与数量解析。
        val bankInsufficientError = PlanErrorState(
            code = "BANK_INSUFFICIENT",
            message = "本场需要 3 道相关题库题，目前匹配到 1 道。",
            details = listOf("required=3", "available=1"),
            preparationId = "prep-1",
            inputRevision = 4,
        )
        val bankPolicy = PlanScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = null,
            planError = bankInsufficientError,
            loading = false,
        )
        assertEquals(1, failureCardCount(bankPolicy))
        assertTrue(bankPolicy.showPlanErrorCard)
        assertTrue(bankPolicy.showStartPlanAction)
        val parsed = PlanErrorFormatter.parseBankInsufficient(bankInsufficientError.details)!!
        assertEquals(3, parsed.required)
        assertEquals(1, parsed.available)
        assertEquals(listOf(2), parsed.feasibleCounts)

        // 排队/运行中时阻塞重复点「生成计划」。
        val runningPolicy = PlanScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 4),
            job = planJobDto(revision = 4, state = "running"),
            planError = null,
            loading = false,
        )
        assertTrue(runningPolicy.hasCurrentRunningPlan)
        assertFalse(runningPolicy.showStartPlanAction)
        assertEquals(0, failureCardCount(runningPolicy))

        // 准备版本更新到 rev=5 后，允许重新开始生成新计划，且旧失败卡不再占位。
        val newRevisionPolicy = PlanScreenPolicy.evaluate(
            preparationId = "prep-1",
            prep = prepDto(revision = 5),
            job = failedJob,
            planError = duplicatedPlanError,
            loading = false,
        )
        assertFalse(newRevisionPolicy.hasCurrentFailedPlan)
        assertTrue(newRevisionPolicy.showStartPlanAction)
        assertTrue(newRevisionPolicy.startPlanEnabled)
        assertEquals(0, failureCardCount(newRevisionPolicy))
    }

    @Test
    fun planQueryAndRetryCallsExistingRetryEndpointAndIgnoresStaleResponses() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val operationStore = InMemoryOperationStore()
        operationStore.save(
            "acc-a",
            AccountOperations(
                jobs = listOf(
                    ActiveJob(
                        jobId = "job-plan-1",
                        kind = "plan",
                        targetId = "prep-1",
                        preparationId = "prep-1",
                        revision = 4,
                    ),
                ),
            ),
        )
        val getJobCount = AtomicInteger(0)
        val generatePlanCalls = AtomicInteger(0)
        val retryCalls = mutableListOf<RetryRequest>()
        val staleJobEntered = CompletableDeferred<Unit>()
        val staleJobRelease = CompletableDeferred<Unit>()

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
                    "/v1/preparations/prep-1" -> okJson(
                        json.encodeToString(
                            prepDto(
                                revision = 4,
                                currentPlanId = if (retryCalls.isEmpty()) null else "plan-1",
                            ),
                        ),
                    )
                    "/v1/preparations/prep-1/generate-plan" -> {
                        generatePlanCalls.incrementAndGet()
                        error("只读查询与显式重试不应调用 generate-plan")
                    }
                    "/v1/jobs/job-plan-1" -> {
                        val n = getJobCount.incrementAndGet()
                        val dto = when (n) {
                            1 -> planJobDto(
                                revision = 4,
                                state = "failed",
                                attempt = 1,
                                retryable = false,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "生成槽位在有限纠正后仍不合法",
                                    retryable = false,
                                    details = listOf("generated-1", "AI 返回内容不是合法 JSON"),
                                    request_id = "legacy",
                                ),
                            )
                            2 -> planJobDto(
                                revision = 4,
                                state = "failed",
                                attempt = 1,
                                retryable = true,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "生成槽位在有限纠正后仍不合法",
                                    retryable = true,
                                    details = listOf("generated-1", "AI 返回内容不是合法 JSON"),
                                    request_id = "legacy",
                                ),
                            )
                            else -> planJobDto(
                                revision = 4,
                                state = "succeeded",
                                attempt = 2,
                                retryable = false,
                                resultRef = ResultRefDto("plan", "plan-1"),
                            )
                        }
                        okJson(json.encodeToString(dto))
                    }
                    "/v1/jobs/job-plan-1/retry" -> {
                        assertEquals(HttpMethod.Post, request.method)
                        val bodyText = (request.body as TextContent).text
                        retryCalls += json.decodeFromString<RetryRequest>(bodyText)
                        acceptedJson(
                            json.encodeToString(
                                planJobDto(revision = 4, state = "queued", attempt = 2, retryable = false),
                            ),
                        )
                    }
                    "/v1/plans/plan-1" -> okJson(json.encodeToString(publicPlanDto(id = "plan-1", inputRevision = 4)))
                    "/v1/interviews" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-stale" -> {
                        staleJobEntered.complete(Unit)
                        staleJobRelease.await()
                        okJson(
                            json.encodeToString(
                                planJobDto(
                                    id = "job-stale",
                                    targetId = "prep-1",
                                    revision = 3,
                                    state = "failed",
                                    attempt = 1,
                                    retryable = false,
                                    error = ErrorInfo(
                                        code = "AI_INVALID_OUTPUT",
                                        message = "旧版本失败不应覆盖当前页面",
                                        retryable = false,
                                    ),
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
            drainAll { vm.state.value.account != null && vm.state.value.preparations.isNotEmpty() }
            vm.openPreparation("prep-1")
            drainAll { vm.state.value.preparation?.id == "prep-1" }

            vm.openPlan("prep-1")
            drainAll { vm.state.value.job?.id == "job-plan-1" && getJobCount.get() >= 1 }
            assertEquals(
                1,
                failureCardCount(
                    PlanScreenPolicy.evaluate(
                        "prep-1",
                        vm.state.value.preparation,
                        vm.state.value.job,
                        vm.state.value.planError,
                        vm.state.value.loading,
                    ),
                ),
            )
            assertFalse(vm.state.value.job!!.retryable)
            assertEquals(0, generatePlanCalls.get())

            // 只读刷新出 retryable=true，且不产生重复失败卡
            vm.queryCurrentJob()
            drainAll { vm.state.value.job?.retryable == true }
            val retryablePolicy = PlanScreenPolicy.evaluate(
                "prep-1",
                vm.state.value.preparation,
                vm.state.value.job,
                vm.state.value.planError,
                vm.state.value.loading,
            )
            assertEquals(1, failureCardCount(retryablePolicy))
            assertFalse(retryablePolicy.showPlanErrorCard)
            assertTrue(retryCalls.isEmpty())

            // 点击重试走 /v1/jobs/{id}/retry，成功后清掉 planError 并加载新计划
            vm.retryCurrentJob()
            drainAll { vm.state.value.plan?.id == "plan-1" }
            assertEquals(listOf(RetryRequest(expected_attempt = 1)), retryCalls)
            assertEquals("plan-1", vm.state.value.plan?.id)
            assertNull(vm.state.value.planError)
            assertEquals(0, generatePlanCalls.get())
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun stalePlanResultFromOldPreparationOrOtherPrepCannotOverwriteCurrentPage() = runTest {
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
                        jobId = "job-old-plan",
                        kind = "plan",
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
                            mapOf("items" to listOf(prepDto(id = "prep-1", revision = 2, currentPlanId = "plan-rev2"))),
                        ),
                    )
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/preparations/prep-1" -> okJson(
                        json.encodeToString(prepDto(id = "prep-1", revision = 2, currentPlanId = "plan-rev2")),
                    )
                    "/v1/plans/plan-rev2" -> okJson(
                        json.encodeToString(publicPlanDto(id = "plan-rev2", preparationId = "prep-1", inputRevision = 2)),
                    )
                    "/v1/interviews" -> okJson("""{"items":[]}""")
                    "/v1/jobs/job-old-plan" -> {
                        oldJobEntered.complete(Unit)
                        oldJobRelease.await()
                        okJson(
                            json.encodeToString(
                                planJobDto(
                                    id = "job-old-plan",
                                    targetId = "prep-1",
                                    revision = 1,
                                    state = "failed",
                                    attempt = 1,
                                    retryable = false,
                                    error = ErrorInfo(
                                        code = "AI_INVALID_OUTPUT",
                                        message = "生成槽位在有限纠正后仍不合法",
                                        retryable = false,
                                        details = listOf("stage=plan", "slot=generated-1", "reason=invalid_json"),
                                    ),
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
            drainAll { vm.state.value.account != null && vm.state.value.preparations.isNotEmpty() }
            vm.openPreparation("prep-1")
            drainAll { vm.state.value.preparation?.revision == 2 }

            vm.openPlan("prep-1")
            drainAll { oldJobEntered.isCompleted && vm.state.value.plan?.id == "plan-rev2" }

            // 放行旧版本 rev=1 的迟到失败任务：不得覆盖 rev=2 的计划或写入 planError。
            oldJobRelease.complete(Unit)
            drainAll()

            assertEquals("plan-rev2", vm.state.value.plan?.id)
            assertNull(vm.state.value.planError)
            assertNull(vm.state.value.job)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun redoPreparationFromExhaustedPlanUsesBoundSnapshotsWithoutAiCallsAndChecksPageScope() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val operationStore = InMemoryOperationStore()
        operationStore.save(
            "acc-a",
            AccountOperations(
                jobs = listOf(
                    ActiveJob(
                        jobId = "job-exhausted",
                        kind = "plan",
                        targetId = "prep-1",
                        preparationId = "prep-1",
                        revision = 4,
                    ),
                ),
            ),
        )
        val createRequests = mutableListOf<Pair<CreatePreparationRequest, String?>>()
        val aiCalls = AtomicInteger(0)

        fun snapshotDto(id: String, kind: String, title: String) = SnapshotDto(
            id = id,
            material_id = "mat-$id",
            source_revision = 1,
            kind = kind,
            title = title,
            text = "材料原文",
            profile = buildJsonObject { put("kind", kind) },
            confirmed_at = "2026-11-18T00:00:00Z",
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
                    "/v1/preparations" -> {
                        if (request.method == HttpMethod.Post) {
                            val bodyText = (request.body as TextContent).text
                            val req = json.decodeFromString<CreatePreparationRequest>(bodyText)
                            val idemKey = request.headers["Idempotency-Key"]
                            createRequests += req to idemKey
                            respond(
                                json.encodeToString(
                                    prepDto(id = "prep-2", revision = 1, matchReportId = null),
                                ),
                                HttpStatusCode.Created,
                                jsonHeaders,
                            )
                        } else {
                            okJson(json.encodeToString(mapOf("items" to listOf(prepDto(revision = 4)))))
                        }
                    }
                    "/v1/materials" -> okJson("""{"items":[]}""")
                    "/v1/preparations/prep-1" -> okJson(json.encodeToString(prepDto(revision = 4)))
                    "/v1/snapshots/resume-1" -> okJson(json.encodeToString(snapshotDto("resume-1", "resume", "原简历")))
                    "/v1/snapshots/jd-1" -> okJson(json.encodeToString(snapshotDto("jd-1", "jd", "原 JD")))
                    "/v1/jobs/job-exhausted" -> okJson(
                        json.encodeToString(
                            planJobDto(
                                id = "job-exhausted",
                                revision = 4,
                                state = "failed",
                                attempt = 2,
                                retryable = false,
                                error = ErrorInfo(
                                    code = "AI_INVALID_OUTPUT",
                                    message = "生成槽位在有限纠正后仍不合法",
                                    retryable = false,
                                    details = listOf(
                                        "stage=plan",
                                        "slot=generated-3",
                                        "reason=duplicate_stem",
                                        "duplicate_kind=exact",
                                        "conflict_source=prior_generated",
                                        "conflict_slot=generated-1",
                                    ),
                                ),
                            ),
                        ),
                    )
                    "/v1/preparations/prep-1/match",
                    "/v1/preparations/prep-1/generate-plan",
                    "/v1/preparations/prep-2/match",
                    "/v1/preparations/prep-2/generate-plan" -> {
                        aiCalls.incrementAndGet()
                        error("新建准备过程不应触发 AI 调用")
                    }
                    "/v1/interviews" -> okJson("""{"items":[]}""")
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
            vm.openPlan("prep-1")
            drainAll { vm.state.value.job?.id == "job-exhausted" }

            // 单张失败卡且不可重试
            val exhaustedPolicy = PlanScreenPolicy.evaluate(
                preparationId = "prep-1",
                prep = vm.state.value.preparation,
                job = vm.state.value.job,
                planError = vm.state.value.planError,
                loading = vm.state.value.loading,
            )
            assertEquals(1, failureCardCount(exhaustedPolicy))
            assertFalse(exhaustedPolicy.showPlanErrorCard)
            assertFalse(vm.state.value.job!!.retryable)

            // 模拟其他页面残留的错误快照：redoPreparation 必须忽略它们，只使用 prep-1 绑定的快照
            vm.state.value = vm.state.value.copy(
                resumeSnapshot = snapshotDto("stale-resume-99", "resume", "残留简历"),
                jdSnapshot = snapshotDto("stale-jd-99", "jd", "残留 JD"),
            )

            vm.redoPreparation("prep-1")
            drainAll { vm.state.value.preparation?.id == "prep-2" && vm.state.value.resumeSnapshot?.id == "resume-1" }

            assertEquals(1, createRequests.size)
            val (req, idemKey) = createRequests.single()
            assertEquals("java-backend", req.role_id)
            assertEquals("intern", req.level)
            assertEquals(6, req.main_question_count)
            assertEquals("resume-1", req.resume_snapshot_id)
            assertEquals("jd-1", req.jd_snapshot_id)
            assertFalse(idemKey.isNullOrBlank())
            assertEquals(0, aiCalls.get())
            assertEquals(listOf(Screen.PreparationList, Screen.Setup("prep-2")), vm.state.value.stack)
            assertNull(vm.state.value.job)
            assertNull(vm.state.value.planError)
            assertNull(vm.state.value.plan)
            assertNull(vm.state.value.matchReport)
            assertEquals("resume-1", vm.state.value.resumeSnapshot?.id)
            assertEquals("jd-1", vm.state.value.jdSnapshot?.id)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
