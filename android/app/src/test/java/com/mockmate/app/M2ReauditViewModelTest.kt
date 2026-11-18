package com.mockmate.app

import com.mockmate.app.core.network.InterviewAnswerDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewCoverageDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.OperationStore
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class M2ReauditViewModelTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }
    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
    private val createdVms = mutableListOf<AppViewModel>()

    private suspend fun TestScope.awaitSignal(signal: CompletableDeferred<Unit>) {
        val deadline = System.currentTimeMillis() + 5_000
        while (!signal.isCompleted && System.currentTimeMillis() < deadline) {
            testScheduler.runCurrent()
            if (!signal.isCompleted) Thread.sleep(5)
        }
        assertTrue("Timed out waiting for signal", signal.isCompleted)
        signal.await()
    }

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

    private class BlockingOperationStore(
        val delegate: InMemoryOperationStore = InMemoryOperationStore(),
    ) : OperationStore {
        private val loadGate = ArrayDeque<CompletableDeferred<Unit>>()
        private val entered = ArrayDeque<CompletableDeferred<Unit>>()

        fun armNextLoadGate(
            onEnter: CompletableDeferred<Unit> = CompletableDeferred(),
            release: CompletableDeferred<Unit> = CompletableDeferred(),
        ): Pair<CompletableDeferred<Unit>, CompletableDeferred<Unit>> {
            entered.addLast(onEnter)
            loadGate.addLast(release)
            return onEnter to release
        }

        override suspend fun load(accountId: String): OperationLoad {
            val onEnter = entered.removeFirstOrNull()
            val release = loadGate.removeFirstOrNull()
            if (onEnter != null && release != null) {
                onEnter.complete(Unit)
                release.await()
            }
            return delegate.load(accountId)
        }

        override suspend fun save(accountId: String, operations: AccountOperations): OperationSave =
            delegate.save(accountId, operations)
    }

    private fun detailDto(
        sessionId: String,
        revision: Int = 2,
        turnId: String = "turn-$sessionId",
        followUpJob: InterviewJobSummaryDto? = null,
        reportJob: InterviewJobSummaryDto? = null,
        transcript: List<InterviewTurnDto> = listOf(
            InterviewTurnDto(
                turn_id = turnId,
                question_id = "q-$sessionId",
                position = 1,
                kind = "main",
                prompt = "题目 $sessionId",
                status = "pending",
                answer = null,
            )
        ),
        currentTurn: InterviewTurnDto? = transcript.firstOrNull { it.status == "pending" },
        allowedActions: List<String> = listOf("answer", "skip", "pause", "finish"),
    ) = InterviewDetailDto(
        session_id = sessionId,
        plan_id = "plan-$sessionId",
        preparation_id = "prep-$sessionId",
        role_id = "java-backend",
        level = "intern",
        mode = "text",
        status = "active",
        phase = "awaiting_answer",
        revision = revision,
        started_at = "2026-11-18T00:00:00Z",
        plan_summary = InterviewPlanSummaryDto(
            plan_id = "plan-$sessionId",
            main_question_count = 6,
            source_counts = SourceCountsDto(3, 3),
        ),
        opening_text = "开场 $sessionId",
        current_main_position = 1,
        current_turn = currentTurn,
        allowed_actions = allowedActions,
        transcript = transcript,
        follow_up_job = followUpJob,
        report_job = reportJob,
    )

    private fun reportDto(sessionId: String) = InterviewReportDto(
        report_id = "report-$sessionId",
        session_id = sessionId,
        role_id = "java-backend",
        level = "intern",
        quality = "full",
        rubric_policy_version = "m2-rubric-policy-v1",
        created_at = "2026-11-18T00:10:00Z",
        end_reason = "completed",
        coverage = InterviewCoverageDto(
            planned = 2, presented = 2, answered = 2, skipped = 0, unanswered = 0,
            not_reached = 0, scored = 2, bank_planned = 1, bank_answered = 1,
            generated_planned = 1, generated_answered = 1,
            follow_up_presented = 0, follow_up_answered = 0,
        ),
    )

    private fun jobDto(
        id: String,
        kind: String,
        targetId: String,
        state: String = "failed",
        attempt: Int = 1,
        retryable: Boolean = true,
    ) = JobDto(
        id = id,
        kind = kind,
        target_id = targetId,
        input_revision = 2,
        state = state,
        attempt = attempt,
        retryable = retryable,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:01Z",
    )

    private fun MockRequestHandleScope.okJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.OK, jsonHeaders)

    private fun loginUser(
        vm: AppViewModel,
        accountId: String = "acc-a",
        username: String = "userA",
    ) {
        vm.authenticate(register = false, username = username, password = "pw")
    }

    private fun defaultCommonRoutes(
        request: HttpRequestData,
        scope: MockRequestHandleScope,
        accountId: String = "acc-a",
        username: String = "userA",
    ): HttpResponseData? = when (request.url.encodedPath) {
        "/v1/auth/login" -> scope.okJson(
            json.encodeToString(
                TokenResponse(
                    access_token = "tok-$accountId",
                    token_type = "bearer",
                    expires_at = "2026-12-01T00:00:00Z",
                    user = UserDto(accountId, username),
                )
            )
        )
        "/v1/auth/logout" -> scope.respond("", HttpStatusCode.NoContent)
        "/v1/roles" -> scope.okJson("""{"items":[]}""")
        "/v1/preparations" -> scope.okJson("""{"items":[]}""")
        "/v1/materials" -> scope.okJson("""{"items":[]}""")
        else -> null
    }

    private fun createVm(
        store: OperationStore,
        drafts: InMemoryInterviewDraftStorage = InMemoryInterviewDraftStorage(),
        dispatcher: kotlinx.coroutines.CoroutineDispatcher,
        handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData,
    ): AppViewModel {
        val engine = MockEngine(handler)
        return AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = drafts,
            operationStore = store,
            httpEngine = engine,
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        ).also { createdVms += it }
    }

    // ------------------------------------------------------------ 问题 2：切场丢弃迟到详情更新

    @Test
    fun switchingFromAToBDuringPostGetPendingForDropsStaleDetailAndPolling() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val drafts = InMemoryInterviewDraftStorage()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            // 给 A 放一条待确认回答（但不影响这里，我们直接用 pending 列表模拟 A 的 pendingFor 结果），
            // 但要避免 recoverInterviewSession 重放时触发网络请求，用 B 和 A 各自的状态。
            // 为避免 replay 发出 POST，测试把 A 的 pending op 标记为只用于 pendingFor，或者在 replay 后再写入；
            // 实际上只要 POST /answers 返回 NETWORK_UNAVAILABLE，pending 就会保留！
            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-a-pending", "A 草稿"),
            )
            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-b",
                revision = 1,
                payload = InterviewAnswerPayload("session-b", 1, "turn-b-pending", "B 草稿"),
            )
            // B 的孤儿草稿：已回答 turn-b-answered，本地有不同草稿
            drafts.put("acc-a", "session-b", "turn-b-answered", "B 本地未提交草稿")
            drafts.put("acc-a", "session-a", "turn-a-answered", "A 本地未提交草稿")

            val requestedPaths = CopyOnWriteArrayList<String>()
            val postGetEntered = CompletableDeferred<Unit>()
            val postGetRelease = CompletableDeferred<Unit>()
            var armedPostGetGate = false

            val vm = createVm(store, drafts, dispatcher) { req ->
                requestedPaths += req.url.encodedPath
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-b/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-a" -> {
                        if (!armedPostGetGate) {
                            armedPostGetGate = true
                            store.armNextLoadGate(postGetEntered, postGetRelease)
                        }
                        okJson(
                            json.encodeToString(
                                detailDto(
                                    sessionId = "session-a",
                                    revision = 5,
                                    followUpJob = InterviewJobSummaryDto("job-a", "running", 1),
                                    transcript = listOf(
                                        InterviewTurnDto(
                                            "turn-a-answered", "q-a", 1, "main", "A 题", "answered",
                                            InterviewAnswerDto("ans-a", "A 服务端回答", "2026-11-18T00:00:00Z"),
                                        ),
                                        InterviewTurnDto("turn-a", "q-a2", 2, "main", "A 下一题", "pending"),
                                    ),
                                )
                            )
                        )
                    }
                    "/v1/interviews/session-b" -> okJson(
                        json.encodeToString(
                            detailDto(
                                sessionId = "session-b",
                                revision = 2,
                                transcript = listOf(
                                    InterviewTurnDto(
                                        "turn-b-answered", "q-b", 1, "main", "B 题", "answered",
                                        InterviewAnswerDto("ans-b", "B 服务端回答", "2026-11-18T00:00:00Z"),
                                    ),
                                    InterviewTurnDto("turn-b", "q-b2", 2, "main", "B 下一题", "pending"),
                                ),
                            )
                        )
                    )
                    "/v1/jobs/job-a" -> okJson(json.encodeToString(jobDto("job-a", "follow_up", "session-a", "running")))
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            // 打开 A：GET 返回后卡在 fetchInterviewAndOptionalReport 里的 pendingFor 读取
            vm.openInterview("session-a")
            awaitSignal(postGetEntered)

            // 切到 B 并等待 B 完全加载
            vm.openInterview("session-b")
            drainAll { vm.state.value.interviewDetail?.session_id == "session-b" && !vm.state.value.loading }
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertEquals("turn-b-pending", vm.state.value.pendingInterviewTurnId)
            assertEquals(listOf("turn-b-answered"), vm.state.value.interviewOrphanDrafts.map { it.turnId })
            assertFalse(vm.state.value.loading)

            // 放行 A 的迟到 pendingFor 读取
            postGetRelease.complete(Unit)
            drainAll()

            // 必须仍完全属于 B，A 的迟到结果不能覆盖且不能触发 job-a 轮询
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertEquals("turn-b-pending", vm.state.value.pendingInterviewTurnId)
            assertEquals(listOf("turn-b-answered"), vm.state.value.interviewOrphanDrafts.map { it.turnId })
            assertFalse(vm.state.value.loading)
            assertNull(vm.state.value.busyLabel)
            assertFalse("A 不应触发 job-a 轮询", requestedPaths.contains("/v1/jobs/job-a"))
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun switchingFromAToBDuringInitialPendingForDropsStaleTurnAndLoadingClear() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-a-pending", "A 草稿"),
            )
            val requestedPaths = CopyOnWriteArrayList<String>()
            val bGetGate = CompletableDeferred<Unit>()
            val bGetEntered = CompletableDeferred<Unit>()

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                requestedPaths += req.url.encodedPath
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-a" -> okJson(json.encodeToString(detailDto("session-a")))
                    "/v1/interviews/session-b" -> {
                        bGetEntered.complete(Unit)
                        bGetGate.await()
                        okJson(json.encodeToString(detailDto("session-b")))
                    }
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            // 跳过 replay 内部第一次 load，卡住 refreshInterviewSession 初始 pendingFor
            store.armNextLoadGate().let { (_, rel) -> rel.complete(Unit) }
            val (onInitialPendingEnter, releaseInitialPending) = store.armNextLoadGate()

            vm.openInterview("session-a")
            awaitSignal(onInitialPendingEnter)

            // 切到 B，此时 B 正在加载（loading = true）
            vm.openInterview("session-b")
            awaitSignal(bGetEntered)
            assertTrue(vm.state.value.loading)
            assertNull(vm.state.value.pendingInterviewTurnId)

            // 放行 A 的初始 pendingFor：A 不得写入 turn-a-pending，也不得继续发起 GET /session-a 或把 B 的 loading 清掉
            releaseInitialPending.complete(Unit)
            drainAll()
            assertNull(vm.state.value.pendingInterviewTurnId)
            assertTrue(vm.state.value.loading)
            assertFalse(requestedPaths.contains("/v1/interviews/session-a"))

            bGetGate.complete(Unit)
            drainAll { vm.state.value.interviewDetail?.session_id == "session-b" && !vm.state.value.loading }
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertNull(vm.state.value.pendingInterviewTurnId)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun backgroundRefreshBlockedOnPendingForDropsStaleDetailAfterSwitchingToB() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-a-pending", "A 草稿"),
            )
            var aDetailFetches = 0
            val bgPendingEntered = CompletableDeferred<Unit>()
            val bgPendingRelease = CompletableDeferred<Unit>()

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-a" -> {
                        aDetailFetches += 1
                        if (aDetailFetches == 2) {
                            // 第二次是 refreshInterviewInBackground 触发的 GET，返回后卡住它的 pendingFor
                            store.armNextLoadGate(bgPendingEntered, bgPendingRelease)
                        }
                        okJson(
                            json.encodeToString(
                                detailDto(
                                    sessionId = "session-a",
                                    revision = aDetailFetches + 1,
                                    followUpJob = InterviewJobSummaryDto("job-fu-a", "running", 1),
                                )
                            )
                        )
                    }
                    "/v1/jobs/job-fu-a" -> okJson(
                        json.encodeToString(
                            jobDto("job-fu-a", "follow_up", "session-a", state = "failed", attempt = 1)
                        )
                    )
                    "/v1/interviews/session-b" -> okJson(json.encodeToString(detailDto("session-b", revision = 3)))
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            // 打开 A -> 触发 watch job-fu-a -> job 返回 failed -> 触发 refreshInterviewInBackground("session-a")
            vm.openInterview("session-a")
            awaitSignal(bgPendingEntered)

            // 切到 B 并等待 B 加载完成
            vm.openInterview("session-b")
            drainAll { vm.state.value.interviewDetail?.session_id == "session-b" && !vm.state.value.loading }
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertNull(vm.state.value.pendingInterviewTurnId)

            // 放行 A 的后台刷新 pendingFor
            bgPendingRelease.complete(Unit)
            drainAll()

            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertNull(vm.state.value.pendingInterviewTurnId)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun leavingAndReopeningSameSessionDropsStaleRecoveryMessageAndLoadingClear() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            val firstPostEntered = CompletableDeferred<Unit>()
            val firstPostRelease = CompletableDeferred<Unit>()
            val secondPostEntered = CompletableDeferred<Unit>()
            val secondPostRelease = CompletableDeferred<Unit>()
            var postCalls = 0

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a/answers" -> {
                        postCalls += 1
                        if (postCalls == 1) {
                            firstPostEntered.complete(Unit)
                            firstPostRelease.await()
                            throw java.io.IOException("offline")
                        }
                        secondPostEntered.complete(Unit)
                        secondPostRelease.await()
                        okJson("""{"session_id":"session-a","revision":2,"answer_id":"ans-2","job_id":null}""")
                    }
                    "/v1/interviews/session-a" -> okJson(json.encodeToString(detailDto("session-a", revision = 2)))
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-a", "原回答"),
            )

            // 第 1 次进入 A：卡在 recoverInterviewSession 的重放 POST
            vm.openInterview("session-a")
            awaitSignal(firstPostEntered)

            // 离开 A 再重新打开 A（新的 navigationEpoch），第 2 次正在加载中
            vm.back()
            vm.openInterview("session-a")
            drainAll()
            assertTrue(vm.state.value.loading)
            assertNull(vm.state.value.message)

            // 放行第 1 次进场失败的重放；第 2 次进入重放 POST 时，同场新页面不得被第 1 次的提示和 loading=false 改写
            firstPostRelease.complete(Unit)
            awaitSignal(secondPostEntered)
            assertTrue("新打开的同场页面不应被旧协程清掉 loading", vm.state.value.loading)
            assertNull("新打开的同场页面不应被旧协程写入恢复提示", vm.state.value.message)

            secondPostRelease.complete(Unit)
            drainAll { !vm.state.value.loading && vm.state.value.interviewDetail?.session_id == "session-a" }
            assertFalse(vm.state.value.loading)
            assertEquals("session-a", vm.state.value.interviewDetail?.session_id)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun switchingFromRoomAToReportADropsStaleRoomPendingUpdate() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            var detailCalls = 0
            val roomEntered = CompletableDeferred<Unit>()
            val roomRelease = CompletableDeferred<Unit>()
            val reportEntered = CompletableDeferred<Unit>()
            val reportRelease = CompletableDeferred<Unit>()

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-a" -> {
                        detailCalls += 1
                        if (detailCalls == 1) {
                            store.armNextLoadGate(roomEntered, roomRelease)
                            okJson(json.encodeToString(detailDto("session-a", revision = 2)))
                        } else {
                            // 报告页看到最新服务端状态且本地 pending 已清除
                            store.delegate.save("acc-a", AccountOperations())
                            okJson(json.encodeToString(detailDto("session-a", revision = 2)))
                        }
                    }
                    "/v1/interviews/session-a/report" -> {
                        reportEntered.complete(Unit)
                        reportRelease.await()
                        okJson(json.encodeToString(reportDto("session-a")))
                    }
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-room-stale", "草稿"),
            )

            vm.openInterview("session-a")
            awaitSignal(roomEntered)

            // 从面试间切到同场报告页，报告 GET 仍在加载中
            vm.openInterviewReport("session-a")
            awaitSignal(reportEntered)
            assertTrue(vm.state.value.stack.last() is Screen.InterviewReport)
            assertTrue(vm.state.value.loading)

            // 放行旧面试间协程的 pendingFor：不得把报告页 loading 清掉，也不得写入旧 pendingTurn
            roomRelease.complete(Unit)
            drainAll()
            assertTrue("报告页加载中不应被旧面试间协程清掉 loading", vm.state.value.loading)
            assertNull("报告页不应写入旧面试间读到的 pendingTurn", vm.state.value.pendingInterviewTurnId)

            reportRelease.complete(Unit)
            drainAll { vm.state.value.interviewReport?.report_id == "report-session-a" && !vm.state.value.loading }
            assertEquals("report-session-a", vm.state.value.interviewReport?.report_id)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun loggingOutAndSwitchingAccountDropsStaleDetailUpdate() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val coordinator = OperationCoordinator(store.delegate, ioDispatcher = dispatcher)
            coordinator.prepare(
                accountId = "acc-a",
                type = "interviewAnswer",
                targetId = "session-a",
                revision = 1,
                payload = InterviewAnswerPayload("session-a", 1, "turn-a", "A 草稿"),
            )
            val gateEntered = CompletableDeferred<Unit>()
            val gateRelease = CompletableDeferred<Unit>()
            var armed = false

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                when (req.url.encodedPath) {
                    "/v1/auth/login" -> {
                        val isB = req.body.let {
                            (it as? io.ktor.http.content.TextContent)?.text?.contains("userB") == true
                        }
                        val id = if (isB) "acc-b" else "acc-a"
                        val name = if (isB) "userB" else "userA"
                        okJson(
                            json.encodeToString(
                                TokenResponse("tok-$id", "bearer", "2026-12-01T00:00:00Z", UserDto(id, name))
                            )
                        )
                    }
                    "/v1/interviews/session-a/answers" -> throw java.io.IOException("offline")
                    "/v1/interviews/session-a" -> {
                        if (!armed) {
                            armed = true
                            store.armNextLoadGate(gateEntered, gateRelease)
                        }
                        okJson(json.encodeToString(detailDto("session-a", revision = 7)))
                    }
                    "/v1/interviews/session-b" -> okJson(json.encodeToString(detailDto("session-b", revision = 1)))
                    else -> defaultCommonRoutes(req, this) ?: respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm, "acc-a", "userA")
            drainAll { vm.state.value.account?.id == "acc-a" }

            vm.openInterview("session-a")
            awaitSignal(gateEntered)

            // 退出并切换到账号 B，打开 B 的面试
            vm.logout()
            loginUser(vm, "acc-b", "userB")
            drainAll { vm.state.value.account?.id == "acc-b" }
            vm.openInterview("session-b")
            drainAll { vm.state.value.interviewDetail?.session_id == "session-b" && !vm.state.value.loading }
            assertEquals("acc-b", vm.state.value.account?.id)
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertNull(vm.state.value.pendingInterviewTurnId)

            // 放行账号 A 的迟到读写
            gateRelease.complete(Unit)
            drainAll()

            assertEquals("acc-b", vm.state.value.account?.id)
            assertEquals("session-b", vm.state.value.interviewDetail?.session_id)
            assertNull(vm.state.value.pendingInterviewTurnId)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun samePageOutOfOrderRevisionsKeepHigherRevision() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        try {
            val store = BlockingOperationStore()
            val firstGetEntered = CompletableDeferred<Unit>()
            val firstGetRelease = CompletableDeferred<Unit>()
            var calls = 0

            val vm = createVm(store, dispatcher = dispatcher) { req ->
                defaultCommonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/interviews/session-a" -> {
                        calls += 1
                        if (calls == 1) {
                            firstGetEntered.complete(Unit)
                            firstGetRelease.await()
                            okJson(json.encodeToString(detailDto("session-a", revision = 4)))
                        } else {
                            okJson(json.encodeToString(detailDto("session-a", revision = 9)))
                        }
                    }
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }

            loginUser(vm)
            drainAll { vm.state.value.account != null }

            vm.openInterview("session-a")
            awaitSignal(firstGetEntered)
            // 同页再次手动刷新，不触发 bumpNavigation（同一页面快照），第 2 次先返回 revision=9
            vm.loadInterview("session-a")
            drainAll { vm.state.value.interviewDetail?.revision == 9 }
            assertEquals(9, vm.state.value.interviewDetail?.revision)

            // 放行迟到的 revision=4
            firstGetRelease.complete(Unit)
            drainAll()
            assertEquals(9, vm.state.value.interviewDetail?.revision)
        } finally {
            createdVms.forEach { it.closeForTest() }
            createdVms.clear()
            Dispatchers.resetMain()
        }
    }
}
