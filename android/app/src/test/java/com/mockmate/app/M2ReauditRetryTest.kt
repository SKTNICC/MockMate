package com.mockmate.app

import com.mockmate.app.core.network.ErrorEnvelope
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.core.operation.RetryJobPayload
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.core.session.StoredSession
import com.mockmate.app.ui.AppViewModel
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import java.util.concurrent.CopyOnWriteArrayList
import kotlinx.coroutines.CompletableDeferred
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
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class M2ReauditRetryTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }
    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")
    private lateinit var mainDispatcher: TestDispatcher

    @Before
    fun setUpMain() {
        mainDispatcher = StandardTestDispatcher()
        Dispatchers.setMain(mainDispatcher)
    }

    @After
    fun tearDownMain() {
        Dispatchers.resetMain()
    }

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
    }

    private suspend fun TestScope.awaitSignal(signal: CompletableDeferred<Unit>) {
        val deadline = System.currentTimeMillis() + 5_000
        while (!signal.isCompleted && System.currentTimeMillis() < deadline) {
            testScheduler.runCurrent()
            if (!signal.isCompleted) Thread.sleep(5)
        }
        assertTrue("Timed out waiting for signal", signal.isCompleted)
        signal.await()
    }

    private fun detailDto(
        sessionId: String,
        revision: Int = 4,
        followUpJob: InterviewJobSummaryDto? = null,
        reportJob: InterviewJobSummaryDto? = null,
        allowedActions: List<String> = emptyList(),
    ) = InterviewDetailDto(
        session_id = sessionId,
        plan_id = "plan-$sessionId",
        preparation_id = "prep-$sessionId",
        role_id = "java-backend",
        level = "intern",
        mode = "text",
        status = if (reportJob != null) "ended" else "active",
        phase = if (reportJob != null) "closed" else "deciding_follow_up",
        revision = revision,
        started_at = "2026-11-18T00:00:00Z",
        plan_summary = InterviewPlanSummaryDto(
            plan_id = "plan-$sessionId",
            main_question_count = 6,
            source_counts = SourceCountsDto(3, 3),
        ),
        opening_text = "开场",
        allowed_actions = allowedActions,
        transcript = listOf(
            InterviewTurnDto("t-1", "q-1", 1, "main", "题目", "answered")
        ),
        follow_up_job = followUpJob,
        report_job = reportJob,
        report_state = if (reportJob != null) followUpReportState(reportJob.state) else "not_requested",
    )

    private fun followUpReportState(state: String) = when (state) {
        "succeeded" -> "ready"
        "failed", "interrupted" -> "failed"
        else -> "pending"
    }

    private fun jobDto(
        id: String,
        kind: String,
        targetId: String,
        state: String,
        attempt: Int,
        retryable: Boolean = state == "failed",
    ) = JobDto(
        id = id,
        kind = kind,
        target_id = targetId,
        input_revision = 4,
        state = state,
        attempt = attempt,
        retryable = retryable,
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:01Z",
    )

    private fun MockRequestHandleScope.okJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.OK, jsonHeaders)

    private fun commonRoutes(req: HttpRequestData, scope: MockRequestHandleScope): HttpResponseData? =
        when (req.url.encodedPath) {
            "/v1/auth/login" -> scope.okJson(
                json.encodeToString(
                    TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                )
            )
            "/v1/auth/me", "/v1/me" -> scope.okJson(json.encodeToString(UserDto("acc-a", "userA")))
            "/v1/roles" -> scope.okJson("""{"items":[]}""")
            "/v1/preparations" -> scope.okJson("""{"items":[]}""")
            "/v1/materials" -> scope.okJson("""{"items":[]}""")
            else -> null
        }

    private fun createVm(
        store: InMemoryOperationStore,
        sessionStore: InMemorySessionStore = InMemorySessionStore(),
        bootstrapOnInit: Boolean = false,
        handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData,
    ): AppViewModel {
        var n = 0
        return AppViewModel(
            sessionStore = sessionStore,
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = store,
            httpEngine = MockEngine(handler),
            ioDispatcher = mainDispatcher,
            keyFactory = { "op-key-${++n}" },
            bootstrapOnInit = bootstrapOnInit,
        )
    }

    @Test
    fun followUpRetryNeverReachedServerReplaysOnceWithSameKeyOnForegroundAndReenter() = runTest(mainDispatcher) {
        val store = InMemoryOperationStore()
        var serverAttempt = 1
        var jobState = "failed"
        val retryRequests = CopyOnWriteArrayList<Pair<String?, String>>()
        val jobPolls = CopyOnWriteArrayList<String>()
        var failRetryNetwork = true

        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-a",
                            followUpJob = InterviewJobSummaryDto(
                                id = "job-fu-a",
                                state = jobState,
                                attempt = serverAttempt,
                                retryable = jobState == "failed",
                            ),
                            allowedActions = if (jobState == "failed") listOf("retry_follow_up") else emptyList(),
                        )
                    )
                )
                "/v1/jobs/job-fu-a/retry" -> {
                    val key = req.headers["Idempotency-Key"]
                    val body = (req.body as? TextContent)?.text ?: ""
                    retryRequests += key to body
                    if (failRetryNetwork) throw java.io.IOException("network dropped before server")
                    serverAttempt += 1
                    jobState = "queued"
                    okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", serverAttempt)))
                }
                "/v1/jobs/job-fu-a" -> {
                    jobPolls += req.url.encodedPath
                    okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", jobState, serverAttempt)))
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }

            vm.openInterview("session-a")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-a" && !vm.state.value.loading }

            // 点「重试」但断网，请求未到达服务端
            vm.retryInterviewJob("session-a", "job-fu-a", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "NETWORK_UNAVAILABLE" }
            assertEquals(1, retryRequests.size)
            val originalKey = retryRequests.single().first
            assertEquals(1, serverAttempt)
            assertEquals("session-a", store.snapshot("acc-a").pending.single().interviewSessionId)

            // 恢复网络并回到前台：按原 key 重放一次，attempt 只加 1，刷新详情并恢复轮询
            failRetryNetwork = false
            vm.setAppVisible(true)
            drainUntil { store.snapshot("acc-a").pending.isEmpty() && jobPolls.isNotEmpty() }

            assertEquals(2, retryRequests.size)
            assertEquals(originalKey, retryRequests[1].first)
            assertEquals("""{"expected_attempt":1}""", retryRequests[1].second)
            assertEquals(2, serverAttempt)
            assertEquals("queued", vm.state.value.interviewDetail?.follow_up_job?.state)
            assertEquals(2, vm.state.value.interviewDetail?.follow_up_job?.attempt)
            assertTrue(jobPolls.isNotEmpty())
        } finally {
            vm.closeForTest()
        }
    }

    @Test
    fun reportRetryDroppedReceiptReplaysCachedReceiptWithoutIncrementingAttempt() = runTest(mainDispatcher) {
        val store = InMemoryOperationStore()
        var serverAttempt = 1
        var jobState = "failed"
        val retryKeys = CopyOnWriteArrayList<String?>()
        val cachedReceipts = mutableMapOf<String, JobDto>()
        val jobPolls = CopyOnWriteArrayList<String>()
        var dropNext2xx = true

        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-a",
                            reportJob = InterviewJobSummaryDto(
                                id = "job-rep-a",
                                state = jobState,
                                attempt = serverAttempt,
                                retryable = jobState == "failed",
                            ),
                            allowedActions = if (jobState == "failed") listOf("retry_report") else emptyList(),
                        )
                    )
                )
                "/v1/interviews/session-a/report" -> respond(
                    json.encodeToString(ErrorEnvelope(ErrorInfo("REPORT_PENDING", "pending"))),
                    HttpStatusCode.Conflict,
                    jsonHeaders,
                )
                "/v1/jobs/job-rep-a/retry" -> {
                    val key = req.headers["Idempotency-Key"]!!
                    retryKeys += key
                    val receipt = cachedReceipts.getOrPut(key) {
                        serverAttempt += 1
                        jobState = "queued"
                        jobDto("job-rep-a", "interview_report", "session-a", "queued", serverAttempt)
                    }
                    if (dropNext2xx) {
                        dropNext2xx = false
                        respond("", HttpStatusCode.OK, jsonHeaders)
                    } else {
                        okJson(json.encodeToString(receipt))
                    }
                }
                "/v1/jobs/job-rep-a" -> {
                    jobPolls += req.url.encodedPath
                    okJson(json.encodeToString(jobDto("job-rep-a", "interview_report", "session-a", jobState, serverAttempt)))
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }
            vm.openInterview("session-a")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-a" && !vm.state.value.loading }

            vm.retryInterviewJob("session-a", "job-rep-a", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message != null }
            assertEquals(1, retryKeys.size)
            assertEquals(2, serverAttempt)
            assertEquals(1, store.snapshot("acc-a").pending.size)

            // 重进原场：重放原 key，服务端返回原回执，attempt 不再增加
            vm.back()
            vm.openInterview("session-a")
            drainUntil { store.snapshot("acc-a").pending.isEmpty() && jobPolls.isNotEmpty() }

            assertEquals(2, retryKeys.size)
            assertEquals(retryKeys[0], retryKeys[1])
            assertEquals(2, serverAttempt)
            assertEquals(2, vm.state.value.interviewDetail?.report_job?.attempt)
            assertEquals("queued", vm.state.value.interviewDetail?.report_job?.state)
            assertTrue(jobPolls.isNotEmpty())
        } finally {
            vm.closeForTest()
        }
    }

    @Test
    fun retryRecoveryIsolatesSessionsAndM1AndPreservesKeyAcrossUncertainAndHandlesRejection() = runTest(mainDispatcher) {
        val store = InMemoryOperationStore()
        var serverAttempt = 1
        var jobState = "failed"
        var serverFailuresLeft = 2
        val retriedTargets = CopyOnWriteArrayList<Pair<String, String?>>()

        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-a",
                            followUpJob = InterviewJobSummaryDto("job-fu-a", jobState, serverAttempt, retryable = jobState == "failed"),
                            allowedActions = if (jobState == "failed") listOf("retry_follow_up") else emptyList(),
                        )
                    )
                )
                "/v1/interviews/session-b" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-b",
                            followUpJob = InterviewJobSummaryDto("job-fu-b", "failed", 1, retryable = true),
                            allowedActions = listOf("retry_follow_up"),
                        )
                    )
                )
                "/v1/jobs/job-fu-a/retry" -> {
                    retriedTargets += "job-fu-a" to req.headers["Idempotency-Key"]
                    if (serverFailuresLeft > 0) {
                        serverFailuresLeft -= 1
                        respond(
                            json.encodeToString(ErrorEnvelope(ErrorInfo("INTERNAL_ERROR", "临时故障", retryable = true))),
                            HttpStatusCode.InternalServerError,
                            jsonHeaders,
                        )
                    } else {
                        serverAttempt = 2
                        jobState = "queued"
                        okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                    }
                }
                "/v1/jobs/job-fu-b/retry" -> {
                    retriedTargets += "job-fu-b" to req.headers["Idempotency-Key"]
                    throw java.io.IOException("offline")
                }
                "/v1/jobs/job-fu-a" -> okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }

            val m1Prep = OperationCoordinator(store, keyFactory = { "m1-key" }, ioDispatcher = mainDispatcher)
                .prepare("acc-a", "retryJob", "job-m1", 1, RetryJobPayload(1)) as PrepareResult.Prepared

            vm.openInterview("session-b")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-b" && !vm.state.value.loading }
            vm.retryInterviewJob("session-b", "job-fu-b", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "NETWORK_UNAVAILABLE" }

            vm.openInterview("session-a")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-a" && !vm.state.value.loading }
            vm.retryInterviewJob("session-a", "job-fu-a", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "INTERNAL_ERROR" }

            // 连续刷新 A：第 2 次仍不确定（500），保留原 key 与 pending
            vm.loadInterview("session-a")
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "INTERVIEW_RECEIPT_UNKNOWN" }
            assertEquals(3, store.snapshot("acc-a").pending.size)

            // 第 3 次刷新 A：成功 200，只发送 A 且使用原 key，随后移除 A 的 pending，保留 B 与 M1
            vm.loadInterview("session-a")
            drainUntil { store.snapshot("acc-a").pending.size == 2 && !vm.state.value.loading }

            val aCalls = retriedTargets.filter { it.first == "job-fu-a" }
            assertEquals(3, aCalls.size)
            assertEquals(1, aCalls.map { it.second }.distinct().size)
            assertEquals(1, retriedTargets.count { it.first == "job-fu-b" })
            assertEquals(0, retriedTargets.count { it.first == "job-m1" })
            assertEquals(setOf("job-fu-b", "job-m1"), store.snapshot("acc-a").pending.map { it.targetId }.toSet())
            assertEquals("m1-key", m1Prep.operation.operationKey)
        } finally {
            vm.closeForTest()
        }
    }

    @Test
    fun definiteRejectionDuringRetryReplayClearsPendingAndShowsLatestAllowedActions() = runTest(mainDispatcher) {
        val store = InMemoryOperationStore()
        var firstFailure = true
        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-a",
                            revision = if (firstFailure) 4 else 5,
                            followUpJob = InterviewJobSummaryDto(
                                "job-fu-a",
                                if (firstFailure) "failed" else "superseded",
                                1,
                                retryable = false,
                            ),
                            allowedActions = if (firstFailure) listOf("retry_follow_up") else listOf("answer"),
                        )
                    )
                )
                "/v1/jobs/job-fu-a/retry" -> {
                    if (firstFailure) {
                        firstFailure = false
                        throw java.io.IOException("offline")
                    }
                    respond(
                        json.encodeToString(ErrorEnvelope(ErrorInfo("JOB_STATE_CONFLICT", "任务已被替代"))),
                        HttpStatusCode.Conflict,
                        jsonHeaders,
                    )
                }
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }
            vm.openInterview("session-a")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-a" && !vm.state.value.loading }

            vm.retryInterviewJob("session-a", "job-fu-a", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "NETWORK_UNAVAILABLE" }
            assertEquals(1, store.snapshot("acc-a").pending.size)

            vm.loadInterview("session-a")
            drainUntil { store.snapshot("acc-a").pending.isEmpty() && vm.state.value.interviewDetail?.revision == 5 }

            assertEquals("JOB_STATE_CONFLICT", vm.state.value.message?.code)
            assertEquals(listOf("answer"), vm.state.value.interviewDetail?.allowed_actions)
        } finally {
            vm.closeForTest()
        }
    }

    @Test
    fun legacyRetryJsonWithoutSessionIdMatchesConfirmedJobPreservesM1AndBackfillsOnManualRetry() = runTest(mainDispatcher) {
        val legacyJson = """
            {
              "pending": [
                {
                  "operationKey": "legacy-key-a",
                  "type": "retryJob",
                  "targetId": "job-fu-a",
                  "revision": 1,
                  "bodyHash": "h1",
                  "payload": {"type": "retryJob", "expectedAttempt": 1}
                },
                {
                  "operationKey": "legacy-key-m1",
                  "type": "retryJob",
                  "targetId": "job-m1",
                  "revision": 1,
                  "bodyHash": "h2",
                  "payload": {"type": "retryJob", "expectedAttempt": 1}
                }
              ],
              "jobs": []
            }
        """.trimIndent()
        val decoded = json.decodeFromString(AccountOperations.serializer(), legacyJson)
        val store = InMemoryOperationStore()
        var jobAState = "failed"
        var jobAAttempt = 1
        var detailOffline = false
        val retried = CopyOnWriteArrayList<Pair<String, String?>>()

        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> {
                    if (detailOffline) throw java.io.IOException("offline")
                    okJson(
                        json.encodeToString(
                            detailDto(
                                sessionId = "session-a",
                                followUpJob = InterviewJobSummaryDto("job-fu-a", jobAState, jobAAttempt, retryable = jobAState == "failed"),
                                allowedActions = if (jobAState == "failed") listOf("retry_follow_up") else emptyList(),
                            )
                        )
                    )
                }
                "/v1/jobs/job-fu-a/retry" -> {
                    retried += "job-fu-a" to req.headers["Idempotency-Key"]
                    jobAAttempt = 2
                    jobAState = "queued"
                    okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                }
                "/v1/jobs/job-m1/retry" -> {
                    retried += "job-m1" to req.headers["Idempotency-Key"]
                    okJson(json.encodeToString(jobDto("job-m1", "plan", "prep-1", "queued", 2)))
                }
                "/v1/jobs/job-fu-a" -> okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }

            // 详情不可达无法确认归属时，保留旧记录不乱发
            store.save("acc-a", decoded)
            detailOffline = true
            vm.openInterview("session-a")
            drainUntil { !vm.state.value.loading && vm.state.value.message != null }
            assertEquals(0, retried.size)
            assertEquals(2, store.snapshot("acc-a").pending.size)

            // 网络恢复后刷新 A：按服务端详情 follow_up_job.id 认出 legacy-key-a 并重放，不动 legacy-key-m1
            detailOffline = false
            vm.loadInterview("session-a")
            drainUntil { store.snapshot("acc-a").pending.size == 1 && !vm.state.value.loading }
            assertEquals(listOf("job-fu-a" to "legacy-key-a"), retried.toList())
            assertEquals(listOf("legacy-key-m1"), store.snapshot("acc-a").pending.map { it.operationKey })

            // 手动再次 prepare 同一旧记录时，补齐 interviewSessionId 且保留旧键；写入失败走 STORAGE_UNAVAILABLE
            store.save("acc-a", decoded.copy(pending = listOf(decoded.pending.first())))
            val coordinator = OperationCoordinator(store, ioDispatcher = mainDispatcher)
                val hash = coordinator.bodyHash(
                json.encodeToString(
                    com.mockmate.app.core.operation.OperationPayload.serializer(),
                    RetryJobPayload(1),
                )
            )
            store.save(
                "acc-a",
                decoded.copy(pending = listOf(decoded.pending.first().copy(bodyHash = hash))),
            )
            store.failWrites = true
            val rejected = coordinator.prepare(
                accountId = "acc-a",
                type = "retryJob",
                targetId = "job-fu-a",
                revision = 1,
                payload = RetryJobPayload(1),
                interviewSessionId = "session-a",
            )
            assertTrue(rejected is PrepareResult.Rejected)
            assertEquals("STORAGE_UNAVAILABLE", (rejected as PrepareResult.Rejected).code)

            store.failWrites = false
            val backfilled = coordinator.prepare(
                accountId = "acc-a",
                type = "retryJob",
                targetId = "job-fu-a",
                revision = 1,
                payload = RetryJobPayload(1),
                interviewSessionId = "session-a",
            ) as PrepareResult.Prepared
            assertEquals("legacy-key-a", backfilled.operation.operationKey)
            assertEquals("session-a", backfilled.operation.interviewSessionId)
            assertEquals("session-a", store.snapshot("acc-a").pending.single().interviewSessionId)

            // 冷启全量恢复仍可读取并重放旧 M1 记录
            val coldStore = InMemoryOperationStore()
            coldStore.save("acc-a", decoded.copy(pending = listOf(decoded.pending[1])))
            val coldRetried = CopyOnWriteArrayList<Pair<String, String?>>()
            val coldVm = createVm(
                store = coldStore,
                sessionStore = InMemorySessionStore(StoredSession("acc-a", "userA", "tok-a")),
                bootstrapOnInit = true,
            ) { req ->
                commonRoutes(req, this)?.let { return@createVm it }
                when (req.url.encodedPath) {
                    "/v1/jobs/job-m1/retry" -> {
                        coldRetried += "job-m1" to req.headers["Idempotency-Key"]
                        okJson(json.encodeToString(jobDto("job-m1", "plan", "prep-1", "queued", 2)))
                    }
                    else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
                }
            }
            try {
                drainUntil { coldStore.snapshot("acc-a").pending.isEmpty() }
                assertEquals(listOf("job-m1" to "legacy-key-m1"), coldRetried.toList())
                assertFalse(coldVm.state.value.booting)
            } finally {
                coldVm.closeForTest()
            }
        } finally {
            vm.closeForTest()
        }
    }

    @Test
    fun concurrentRefreshesOnSameSessionDoNotDoubleSendRetry() = runTest(mainDispatcher) {
        val store = InMemoryOperationStore()
        val retryEntered = CompletableDeferred<Unit>()
        val retryRelease = CompletableDeferred<Unit>()
        val retryCalls = CopyOnWriteArrayList<String?>()
        var failFirst = true

        val vm = createVm(store) { req ->
            commonRoutes(req, this)?.let { return@createVm it }
            when (req.url.encodedPath) {
                "/v1/interviews/session-a" -> okJson(
                    json.encodeToString(
                        detailDto(
                            sessionId = "session-a",
                            followUpJob = InterviewJobSummaryDto(
                                "job-fu-a",
                                if (failFirst) "failed" else "queued",
                                if (failFirst) 1 else 2,
                                retryable = failFirst,
                            ),
                            allowedActions = if (failFirst) listOf("retry_follow_up") else emptyList(),
                        )
                    )
                )
                "/v1/jobs/job-fu-a/retry" -> {
                    retryCalls += req.headers["Idempotency-Key"]
                    if (failFirst) {
                        failFirst = false
                        throw java.io.IOException("offline")
                    }
                    retryEntered.complete(Unit)
                    retryRelease.await()
                    okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                }
                "/v1/jobs/job-fu-a" -> okJson(json.encodeToString(jobDto("job-fu-a", "follow_up", "session-a", "queued", 2)))
                else -> respond("{}", HttpStatusCode.NotFound, jsonHeaders)
            }
        }
        try {
            vm.authenticate(false, "userA", "pw")
            drainUntil { vm.state.value.account != null }
            vm.openInterview("session-a")
            drainUntil { vm.state.value.interviewDetail?.session_id == "session-a" && !vm.state.value.loading }

            vm.retryInterviewJob("session-a", "job-fu-a", 1)
            drainUntil { !vm.state.value.loading && vm.state.value.message?.code == "NETWORK_UNAVAILABLE" }
            assertEquals(1, retryCalls.size)

            vm.loadInterview("session-a")
            vm.setAppVisible(true)
            awaitSignal(retryEntered)
            retryRelease.complete(Unit)
            drainUntil { store.snapshot("acc-a").pending.isEmpty() && !vm.state.value.loading }

            assertEquals(2, retryCalls.size)
            assertEquals(retryCalls[0], retryCalls[1])
        } finally {
            vm.closeForTest()
        }
    }
}
