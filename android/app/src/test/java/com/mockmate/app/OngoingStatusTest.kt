package com.mockmate.app

import com.mockmate.app.core.network.ErrorEnvelope
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.OngoingStatus
import com.mockmate.app.ui.Screen
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.IOException
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class OngoingStatusTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }
    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
    private lateinit var mainDispatcher: TestDispatcher
    private val createdVms = mutableListOf<AppViewModel>()

    @Before
    fun setUpMain() {
        mainDispatcher = StandardTestDispatcher()
        Dispatchers.setMain(mainDispatcher)
    }

    @After
    fun tearDownMain() {
        createdVms.forEach { it.closeForTest() }
        createdVms.clear()
        Dispatchers.resetMain()
    }

    private fun MockRequestHandleScope.okJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.OK, jsonHeaders)

    private fun MockRequestHandleScope.acceptedJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.Accepted, jsonHeaders)

    private fun TestScope.drainUntil(until: () -> Boolean = { true }) {
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
        assertTrue("Timed out waiting for condition", until())
    }

    private fun TestScope.advancePollWindow(pollCounter: AtomicInteger, expectedPolls: Int) {
        testScheduler.advanceTimeBy(900)
        drainUntil { pollCounter.get() >= expectedPolls }
        // 确保 MockEngine 返回后 mainDispatcher 上的 applyEffect 也完成执行
        Thread.sleep(15)
        testScheduler.runCurrent()
    }

    private fun createVm(
        store: InMemoryOperationStore = InMemoryOperationStore(),
        handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData,
    ): AppViewModel {
        var keySeq = 0
        return AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = store,
            httpEngine = MockEngine(handler),
            ioDispatcher = mainDispatcher,
            keyFactory = { "op-key-${++keySeq}" },
            bootstrapOnInit = false,
        ).also { createdVms += it }
    }

    private fun commonRoutes(
        request: HttpRequestData,
        scope: MockRequestHandleScope,
        preparations: List<PreparationDto> = listOf(prepDto()),
    ): HttpResponseData? = when (request.url.encodedPath) {
        "/v1/auth/login" -> scope.okJson(
            json.encodeToString(
                TokenResponse(
                    access_token = "tok-a",
                    token_type = "bearer",
                    expires_at = "2026-12-01T00:00:00Z",
                    user = UserDto("acc-a", "userA"),
                ),
            ),
        )
        "/v1/roles" -> scope.okJson("""{"items":[]}""")
        "/v1/preparations" -> scope.okJson(json.encodeToString(mapOf("items" to preparations)))
        "/v1/materials" -> scope.okJson("""{"items":[]}""")
        else -> null
    }

    private fun prepDto(
        id: String = "prep-1",
        revision: Int = 4,
        currentPlanId: String? = null,
    ) = PreparationDto(
        id = id,
        revision = revision,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        match_report_id = "report-1",
        current_plan_id = currentPlanId,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    private fun planJobDto(
        id: String = "job-plan-1",
        state: String = "running",
        resultRef: ResultRefDto? = null,
        error: ErrorInfo? = null,
    ) = JobDto(
        id = id,
        kind = "plan",
        target_id = "prep-1",
        input_revision = 4,
        state = state,
        attempt = 1,
        retryable = state == "failed",
        result_ref = resultRef,
        error = error,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:01Z",
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
        focus_areas = listOf("Java 集合"),
        limitations = emptyList(),
        created_at = "2026-11-18T00:00:00Z",
    )

    private fun interviewDetailDto(
        sessionId: String = "session-a",
        revision: Int = 2,
        phase: String = "deciding_follow_up",
        followUpJob: InterviewJobSummaryDto? = InterviewJobSummaryDto(
            id = "job-fu-1",
            state = "running",
            attempt = 1,
            retryable = false,
        ),
        currentTurn: InterviewTurnDto? = null,
    ) = InterviewDetailDto(
        session_id = sessionId,
        plan_id = "plan-1",
        preparation_id = "prep-1",
        role_id = "java-backend",
        level = "intern",
        mode = "text",
        status = "active",
        phase = phase,
        revision = revision,
        started_at = "2026-11-18T00:00:00Z",
        plan_summary = InterviewPlanSummaryDto(
            plan_id = "plan-1",
            main_question_count = 6,
            source_counts = SourceCountsDto(3, 3),
        ),
        opening_text = "开场",
        current_main_position = 1,
        current_turn = currentTurn,
        allowed_actions = emptyList(),
        transcript = emptyList(),
        follow_up_job = followUpJob,
    )

    @Test
    fun planJobOngoingStatusPersistsAcrossPollsWithoutToastAndClearsOnSucceeded() = runTest(mainDispatcher) {
        var jobState = "queued"
        val jobPolls = AtomicInteger(0)

        val vm = createVm { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/preparations/prep-1" -> okJson(json.encodeToString(prepDto()))
                "/v1/snapshots/resume-1", "/v1/snapshots/jd-1" -> okJson(
                    """{"id":"${req.url.encodedPath.substringAfterLast('/')}","material_id":"mat-1","kind":"resume","revision":1,"profile":{},"created_at":"2026-11-18T00:00:00Z"}""",
                )
                "/v1/preparations/prep-1/generate-plan" -> acceptedJson(
                    json.encodeToString(planJobDto(state = "queued")),
                )
                "/v1/jobs/job-plan-1" -> {
                    jobPolls.incrementAndGet()
                    okJson(
                        json.encodeToString(
                            planJobDto(
                                state = jobState,
                                resultRef = if (jobState == "succeeded") ResultRefDto("plan", "plan-1") else null,
                            ),
                        ),
                    )
                }
                "/v1/plans/plan-1" -> okJson(json.encodeToString(publicPlanDto()))
                "/v1/interviews" -> okJson("""{"items":[]}""")
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }

        vm.authenticate(false, "userA", "pw")
        drainUntil { vm.state.value.account != null }
        vm.openPreparation("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.Setup && !vm.state.value.loading }
        vm.openPlan("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.PlanHub }

        vm.generatePlan("prep-1")
        drainUntil { vm.state.value.ongoingStatus != null }

        val expectedOngoing = OngoingStatus(
            jobId = "job-plan-1",
            jobKind = "plan",
            text = "正在生成计划；本页只查询状态，不会重复提交",
        )
        assertEquals(expectedOngoing, vm.state.value.ongoingStatus)
        assertNull("进行中不得重复弹出普通 Toast", vm.state.value.message)

        // 切换到 running，连续轮询超过 5 秒（7 * 900ms = 6.3s），持续状态条保持不变且不产生 Toast
        jobState = "running"
        for (poll in 1..7) {
            advancePollWindow(jobPolls, poll)
            assertEquals(expectedOngoing, vm.state.value.ongoingStatus)
            assertNull("轮询期间不得再次写入 Toast", vm.state.value.message)
        }
        assertTrue(jobPolls.get() >= 7)

        // 服务端完成：清除持续状态条并展示一次完成提示
        jobState = "succeeded"
        advancePollWindow(jobPolls, 8)
        drainUntil { vm.state.value.plan?.id == "plan-1" }

        assertNull(vm.state.value.ongoingStatus)
        assertEquals("计划已生成，请确认保存", vm.state.value.message?.text)
        assertEquals(false, vm.state.value.message?.isError)
    }

    @Test
    fun runningFollowUpDoesNotClobberTransientToastAndClearsOnFailed() = runTest(mainDispatcher) {
        var jobState = "running"
        val jobPolls = AtomicInteger(0)

        val vm = createVm { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        interviewDetailDto(
                            followUpJob = InterviewJobSummaryDto(
                                id = "job-fu-1",
                                state = jobState,
                                attempt = 1,
                                retryable = jobState == "failed",
                            ),
                        ),
                    ),
                )
                "/v1/jobs/job-fu-1" -> {
                    jobPolls.incrementAndGet()
                    okJson(
                        json.encodeToString(
                            JobDto(
                                id = "job-fu-1",
                                kind = "follow_up",
                                target_id = "session-a",
                                input_revision = 2,
                                state = jobState,
                                attempt = 1,
                                retryable = jobState == "failed",
                                error = if (jobState == "failed") {
                                    ErrorInfo(
                                        code = "AI_TIMEOUT",
                                        message = "AI 服务超时，结果未知",
                                        retryable = true,
                                    )
                                } else {
                                    null
                                },
                                created_at = "2026-11-18T00:00:00Z",
                                updated_at = "2026-11-18T00:00:01Z",
                            ),
                        ),
                    )
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }

        vm.authenticate(false, "userA", "pw")
        drainUntil { vm.state.value.account != null }
        vm.openInterview("session-a")
        drainUntil { vm.state.value.ongoingStatus != null && !vm.state.value.loading }

        assertEquals("job-fu-1", vm.state.value.ongoingStatus?.jobId)
        assertEquals("follow_up", vm.state.value.ongoingStatus?.jobKind)
        assertNull(vm.state.value.message)

        // 进行中弹出一次性提示：轮询不得覆盖它；手动/超时关闭后 ongoingStatus 仍保持可见
        vm.notify("临时提示", isError = false)
        assertEquals("临时提示", vm.state.value.message?.text)

        val initialPolls = jobPolls.get()
        advancePollWindow(jobPolls, initialPolls + 1)
        assertEquals("临时提示", vm.state.value.message?.text)
        assertEquals("job-fu-1", vm.state.value.ongoingStatus?.jobId)

        val transientMsg = vm.state.value.message!!
        vm.dismissMessage(transientMsg)
        assertNull(vm.state.value.message)
        assertEquals("job-fu-1", vm.state.value.ongoingStatus?.jobId)

        // 任务失败：清除持续状态条并展示错误 Toast
        jobState = "failed"
        advancePollWindow(jobPolls, initialPolls + 2)
        drainUntil { vm.state.value.message?.isError == true }

        assertNull(vm.state.value.ongoingStatus)
        assertEquals(true, vm.state.value.message?.isError)
    }

    @Test
    fun ongoingStatusClearsOnNotFoundAndNetworkErrorDuringPolling() = runTest(mainDispatcher) {
        var mode = "running"
        val jobPolls = AtomicInteger(0)

        val vm = createVm { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/preparations/prep-1" -> okJson(json.encodeToString(prepDto()))
                "/v1/snapshots/resume-1", "/v1/snapshots/jd-1" -> okJson(
                    """{"id":"${req.url.encodedPath.substringAfterLast('/')}","material_id":"mat-1","kind":"resume","revision":1,"profile":{},"created_at":"2026-11-18T00:00:00Z"}""",
                )
                "/v1/preparations/prep-1/generate-plan" -> acceptedJson(
                    json.encodeToString(planJobDto(state = "running")),
                )
                "/v1/jobs/job-plan-1" -> {
                    jobPolls.incrementAndGet()
                    when (mode) {
                        "not_found" -> respond(
                            json.encodeToString(ErrorEnvelope(ErrorInfo("NOT_FOUND", "job missing", false))),
                            HttpStatusCode.NotFound,
                            jsonHeaders,
                        )
                        "network_error" -> throw IOException("connection lost")
                        else -> okJson(json.encodeToString(planJobDto(state = "running")))
                    }
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }

        vm.authenticate(false, "userA", "pw")
        drainUntil { vm.state.value.account != null }
        vm.openPreparation("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.Setup && !vm.state.value.loading }
        vm.openPlan("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.PlanHub }

        // 1. NOT_FOUND (Ignore) 停止轮询并清除状态条
        vm.generatePlan("prep-1")
        drainUntil { vm.state.value.ongoingStatus != null }
        mode = "not_found"
        advancePollWindow(jobPolls, 1)
        assertNull("NOT_FOUND 停止轮询后必须清除持续状态条", vm.state.value.ongoingStatus)

        // 2. 轮询网络 Error 停止轮询并清除状态条
        mode = "running"
        vm.generatePlan("prep-1")
        drainUntil { vm.state.value.ongoingStatus != null }
        val pollsBeforeErr = jobPolls.get()
        mode = "network_error"
        advancePollWindow(jobPolls, pollsBeforeErr + 1)
        drainUntil { vm.state.value.message?.code == "NETWORK_UNAVAILABLE" }
        assertNull("轮询网络错误停止后必须清除持续状态条", vm.state.value.ongoingStatus)
    }

    @Test
    fun ongoingStatusClearsOnBackgroundAndBackAndRestoresOnForegroundIfStillRunning() = runTest(mainDispatcher) {
        val jobPolls = AtomicInteger(0)

        val vm = createVm { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/preparations/prep-1" -> okJson(json.encodeToString(prepDto()))
                "/v1/snapshots/resume-1", "/v1/snapshots/jd-1" -> okJson(
                    """{"id":"${req.url.encodedPath.substringAfterLast('/')}","material_id":"mat-1","kind":"resume","revision":1,"profile":{},"created_at":"2026-11-18T00:00:00Z"}""",
                )
                "/v1/preparations/prep-1/generate-plan" -> acceptedJson(
                    json.encodeToString(planJobDto(state = "running")),
                )
                "/v1/jobs/job-plan-1" -> {
                    jobPolls.incrementAndGet()
                    okJson(json.encodeToString(planJobDto(state = "running")))
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }

        vm.authenticate(false, "userA", "pw")
        drainUntil { vm.state.value.account != null }
        vm.openPreparation("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.Setup && !vm.state.value.loading }
        vm.openPlan("prep-1")
        drainUntil { vm.state.value.stack.lastOrNull() is Screen.PlanHub }

        vm.generatePlan("prep-1")
        drainUntil { vm.state.value.ongoingStatus != null }

        // 切到后台：取消轮询并清除状态条
        vm.setAppVisible(false)
        assertNull(vm.state.value.ongoingStatus)
        val pollsAtBackground = jobPolls.get()
        testScheduler.advanceTimeBy(3_000)
        testScheduler.runCurrent()
        assertEquals("后台不应继续轮询", pollsAtBackground, jobPolls.get())

        // 回到前台：若任务仍在运行，自动恢复持续状态条
        vm.setAppVisible(true)
        drainUntil { vm.state.value.ongoingStatus != null }
        assertEquals("job-plan-1", vm.state.value.ongoingStatus?.jobId)

        // 返回上一页：立即清除持续状态条
        vm.back()
        assertNull(vm.state.value.ongoingStatus)
        assertTrue(vm.state.value.stack.lastOrNull() is Screen.Setup)
    }

    @Test
    fun manualLoadInterviewClearsOngoingStatusWhenDetailShowsTerminalOrNonDecidingState() = runTest(mainDispatcher) {
        var detailRevision = 2
        var detailPhase = "deciding_follow_up"
        var followUpJob: InterviewJobSummaryDto? = InterviewJobSummaryDto(
            id = "job-fu-1",
            state = "running",
            attempt = 1,
            retryable = false,
        )
        var currentTurn: InterviewTurnDto? = null

        val vm = createVm { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        interviewDetailDto(
                            revision = detailRevision,
                            phase = detailPhase,
                            followUpJob = followUpJob,
                            currentTurn = currentTurn,
                        ),
                    ),
                )
                "/v1/jobs/job-fu-1" -> okJson(
                    json.encodeToString(
                        JobDto(
                            id = "job-fu-1",
                            kind = "follow_up",
                            target_id = "session-a",
                            input_revision = 2,
                            state = "running",
                            attempt = 1,
                            retryable = false,
                            created_at = "2026-11-18T00:00:00Z",
                            updated_at = "2026-11-18T00:00:01Z",
                        ),
                    ),
                )
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }

        vm.authenticate(false, "userA", "pw")
        drainUntil { vm.state.value.account != null }
        vm.openInterview("session-a")
        drainUntil { vm.state.value.ongoingStatus != null && !vm.state.value.loading }
        assertNotNull(vm.state.value.ongoingStatus)

        // 场景 A：详情显示 follow_up_job 已进入终态 succeeded，手动刷新应立即清除持续状态条
        detailRevision = 3
        detailPhase = "awaiting_answer"
        followUpJob = InterviewJobSummaryDto(
            id = "job-fu-1",
            state = "succeeded",
            attempt = 1,
            retryable = false,
        )
        currentTurn = InterviewTurnDto(
            turn_id = "turn-2",
            question_id = "q-2",
            position = 2,
            kind = "main",
            prompt = "下一题",
            status = "pending",
            answer = null,
        )

        vm.loadInterview("session-a")
        drainUntil { vm.state.value.interviewDetail?.revision == 3 && !vm.state.value.loading }
        assertNull("手动刷新拿到终态 follow_up_job 时应立即清除持续状态条", vm.state.value.ongoingStatus)

        // 场景 B：重新模拟处于 deciding 中后，详情直接切回 awaiting_answer 且 follow_up_job 为 null
        detailRevision = 4
        detailPhase = "deciding_follow_up"
        followUpJob = InterviewJobSummaryDto(
            id = "job-fu-1",
            state = "running",
            attempt = 1,
            retryable = false,
        )
        currentTurn = null
        vm.loadInterview("session-a")
        drainUntil { vm.state.value.ongoingStatus != null && !vm.state.value.loading }

        detailRevision = 5
        detailPhase = "awaiting_answer"
        followUpJob = null
        currentTurn = InterviewTurnDto(
            turn_id = "turn-3",
            question_id = "q-3",
            position = 3,
            kind = "main",
            prompt = "再下一题",
            status = "pending",
            answer = null,
        )
        vm.loadInterview("session-a")
        drainUntil { vm.state.value.interviewDetail?.revision == 5 && !vm.state.value.loading }
        assertNull("手动刷新拿到非 deciding_follow_up 且无 follow_up_job 时应立即清除持续状态条", vm.state.value.ongoingStatus)
    }
}
