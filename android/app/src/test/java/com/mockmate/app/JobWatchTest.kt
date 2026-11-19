package com.mockmate.app

import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.JobWatch
import com.mockmate.app.data.RestoreCoordinator
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreScope
import com.mockmate.app.data.RestoreUiEffect
import com.mockmate.app.data.RestoreUiGate
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.runCurrent
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * E03：前后台与冷启的唯一持续轮询。驱动的是 ViewModel 实际使用的 JobWatch 组件，
 * 断言 GET 数量、停止/恢复窗口、唯一订阅与终态自动投影；不依赖用户再点查询。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class JobWatchTest {

    private val interval = 1_000L

    private fun job(state: String, resultId: String? = "plan-1") = JobDto(
        id = "job-1",
        kind = "plan",
        target_id = "prep-1",
        input_revision = 1,
        state = state,
        attempt = 1,
        retryable = false,
        result_ref = resultId?.let { ResultRefDto("plan", it) },
        error = null,
        created_at = "2026-11-17T00:00:00Z",
        updated_at = "2026-11-17T00:00:00Z",
    )

    private fun scope(prep: String = "prep-1", navigation: Int = 1) = RestoreScope(
        accountId = "account-a",
        sessionGeneration = 1,
        navigationEpoch = navigation,
        screen = RestoreJobService.SCREEN_PLAN,
        preparationId = prep,
        jobId = "job-1",
    )

    private fun preparation() = PreparationDto(
        id = "prep-1",
        revision = 1,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        created_at = "2026-11-17T00:00:00Z",
        updated_at = "2026-11-17T00:00:00Z",
    )

    private fun plan() = PublicPlanDto(
        id = "plan-1",
        preparation_id = "prep-1",
        input_revision = 1,
        revision = 1,
        state = "ready",
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        source_counts = SourceCountsDto(3, 3),
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        created_at = "2026-11-17T00:00:00Z",
    )

    private class Ports : RestorePorts {
        override suspend fun job(id: String): ApiResult<JobDto> =
            throw UnsupportedOperationException("JobWatch 只通过 fetchJob 读取任务")

        override suspend fun preparation(id: String) = ApiResult.Ok(
            PreparationDto(
                id = "prep-1",
                revision = 1,
                role_id = "java-backend",
                level = "intern",
                main_question_count = 6,
                created_at = "2026-11-17T00:00:00Z",
                updated_at = "2026-11-17T00:00:00Z",
            )
        )

        override suspend fun plan(id: String) = ApiResult.Ok(
            PublicPlanDto(
                id = id,
                preparation_id = "prep-1",
                input_revision = 1,
                revision = 1,
                state = "ready",
                role_id = "java-backend",
                level = "intern",
                main_question_count = 6,
                source_counts = SourceCountsDto(3, 3),
                resume_snapshot_id = "resume-1",
                jd_snapshot_id = "jd-1",
                created_at = "2026-11-17T00:00:00Z",
            )
        )

        override suspend fun matchReport(id: String): ApiResult<MatchReportDto> =
            ApiResult.Err("NOT_FOUND", "missing", false)

        override suspend fun material(id: String): ApiResult<MaterialDraftDto> =
            ApiResult.Err("NOT_FOUND", "missing", false)

        override suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>> =
            ApiResult.Ok(emptyList())
    }

    private inner class Harness(
        val service: RestoreJobService = RestoreJobService(Ports()),
        val gets: AtomicInteger = AtomicInteger(),
        val posts: AtomicInteger = AtomicInteger(),
        val current: AtomicReference<RestoreScope?> = AtomicReference(scope()),
        val effects: MutableList<RestoreUiEffect> = mutableListOf(),
    ) {
        var serverState = "running"

        private fun currentScope() = current.get()

        val coordinator = RestoreCoordinator(
            service = service,
            gate = RestoreUiGate({ currentScope() }) { _, _ -> },
            fetchJob = {
                gets.incrementAndGet()
                ApiResult.Ok(job(serverState))
            },
            currentScope = { currentScope() },
        )
    }

    private fun Harness.ensure(watch: JobWatch): Boolean =
        watch.ensure(scope(), job(serverState, resultId = if (serverState == "succeeded") "plan-1" else null))

    @Test
    fun restoredRunningJobContinuesToTerminalWithoutUserClick() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)
        assertTrue(h.ensure(watch))

        advanceTimeBy(interval); runCurrent()
        assertEquals(1, h.gets.get())
        assertTrue(h.effects.last() is RestoreUiEffect.Inform)

        h.serverState = "succeeded"
        advanceTimeBy(interval); runCurrent()
        assertEquals(2, h.gets.get())
        assertTrue(h.effects.last() is RestoreUiEffect.Project)

        advanceTimeBy(interval * 5); runCurrent()
        assertEquals("终态后必须停止轮询", 2, h.gets.get())
        assertEquals("只读轮询不得产生 POST", 0, h.posts.get())
        watch.cancelAll()
    }

    @Test
    fun backgroundStopsPollingAndForegroundResumesSameJobUntilTerminal() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)
        h.ensure(watch)

        advanceTimeBy(interval); runCurrent()
        assertEquals(1, h.gets.get())

        // 进入后台：当前窗口后 GET 必须停止。
        visible = false
        advanceTimeBy(interval * 3); runCurrent()
        val afterBackground = h.gets.get()
        assertEquals(1, afterBackground)

        // 前台恢复同一 job：重新建立唯一订阅，无需用户点击。
        visible = true
        h.ensure(watch)
        advanceTimeBy(interval); runCurrent()
        assertEquals(afterBackground + 1, h.gets.get())

        h.serverState = "succeeded"
        advanceTimeBy(interval); runCurrent()
        assertTrue(h.effects.last() is RestoreUiEffect.Project)
        val afterTerminal = h.gets.get()
        advanceTimeBy(interval * 5); runCurrent()
        assertEquals(afterTerminal, h.gets.get())
        assertEquals(0, h.posts.get())
        watch.cancelAll()
    }

    @Test
    fun repeatedForegroundAndManualEnsureDoNotCreateParallelPolling() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)

        assertTrue(h.ensure(watch))
        assertTrue(!h.ensure(watch))
        assertTrue(!h.ensure(watch))

        advanceTimeBy(interval * 3); runCurrent()
        // 3 个时间窗口只应查询 3 次；重复订阅会翻倍。每个窗口一次 running 通知。
        assertEquals(3, h.gets.get())
        assertEquals(3, h.effects.count { it is RestoreUiEffect.Inform })
        assertEquals(0, h.posts.get())
        watch.cancelAll()
    }

    @Test
    fun navigationToAnotherPreparationCancelsTheOldSubscription() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)
        h.ensure(watch)

        advanceTimeBy(interval); runCurrent()
        assertEquals(1, h.gets.get())

        h.current.set(scope(prep = "prep-2", navigation = 2))
        advanceTimeBy(interval * 4); runCurrent()
        assertEquals("离开原页面后不得继续查询旧任务", 1, h.gets.get())
        assertTrue(h.effects.none { it is RestoreUiEffect.Project })
        watch.cancelAll()
    }

    @Test
    fun cancelAllStopsPollingAndDoesNotResurrectStaleJob() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)
        h.ensure(watch)
        advanceTimeBy(interval); runCurrent()
        assertEquals(1, h.gets.get())

        watch.cancelAll()
        advanceTimeBy(interval * 4); runCurrent()
        assertEquals(1, h.gets.get())
        assertTrue(!watch.isWatching("account-a", "job-1"))
    }

    @Test
    fun coldStartRunningJobIsWatchedAndProjectedInTheSamePage() = runTest {
        val h = Harness()
        var visible = true
        val watch = JobWatch(this, h.coordinator, { visible }, { h.effects += it }, interval)
        // 冷启后用户打开计划页，恢复读取仍是 running，建立订阅。
        assertTrue(h.ensure(watch))
        advanceTimeBy(interval); runCurrent()
        assertTrue((h.effects.last() as RestoreUiEffect.Inform).result.outcome is RestoreOutcome.Inform)

        h.serverState = "succeeded"
        advanceTimeBy(interval); runCurrent()
        val project = h.effects.last() as RestoreUiEffect.Project
        assertEquals("plan-1", (project.result.outcome as RestoreOutcome.Project).plan?.id)
        assertEquals(0, h.posts.get())
        watch.cancelAll()
    }

    @Test
    fun onStoppedFiresOnceAcrossProjectIgnoreErrorCancelAndCancelAll() = runTest {
        // 1. Terminal Project
        val h1 = Harness()
        val stopped1 = mutableListOf<Pair<String, String>>()
        val w1 = JobWatch(this, h1.coordinator, { true }, { h1.effects += it }, interval) { acc, id ->
            stopped1 += acc to id
        }
        h1.ensure(w1)
        h1.serverState = "succeeded"
        advanceTimeBy(interval); runCurrent()
        assertEquals(listOf("account-a" to "job-1"), stopped1)
        w1.cancelAll()
        assertEquals(listOf("account-a" to "job-1"), stopped1)

        // 2. Ignore (navigation away)
        val h2 = Harness()
        val stopped2 = mutableListOf<Pair<String, String>>()
        val w2 = JobWatch(this, h2.coordinator, { true }, { h2.effects += it }, interval) { acc, id ->
            stopped2 += acc to id
        }
        h2.ensure(w2)
        h2.current.set(scope(prep = "prep-2", navigation = 2))
        advanceTimeBy(interval); runCurrent()
        assertEquals(listOf("account-a" to "job-1"), stopped2)

        // 3. Ignore (NOT_FOUND)
        val stopped3 = mutableListOf<Pair<String, String>>()
        val coordNotFound = RestoreCoordinator(
            service = RestoreJobService(Ports()),
            gate = RestoreUiGate({ scope() }) { _, _ -> },
            fetchJob = { ApiResult.Err("NOT_FOUND", "missing", false) },
            currentScope = { scope() },
        )
        val w3 = JobWatch(this, coordNotFound, { true }, {}, interval) { acc, id ->
            stopped3 += acc to id
        }
        w3.ensure(scope(), job("running", resultId = null))
        advanceTimeBy(interval); runCurrent()
        assertEquals(listOf("account-a" to "job-1"), stopped3)

        // 4. Error (network failure)
        val stopped4 = mutableListOf<Pair<String, String>>()
        val errorEffects = mutableListOf<RestoreUiEffect>()
        val coordError = RestoreCoordinator(
            service = RestoreJobService(Ports()),
            gate = RestoreUiGate({ scope() }) { _, _ -> },
            fetchJob = { ApiResult.Err("NETWORK_UNAVAILABLE", "offline", true) },
            currentScope = { scope() },
        )
        val w4 = JobWatch(this, coordError, { true }, { errorEffects += it }, interval) { acc, id ->
            stopped4 += acc to id
        }
        w4.ensure(scope(), job("running", resultId = null))
        advanceTimeBy(interval); runCurrent()
        assertTrue(errorEffects.lastOrNull() is RestoreUiEffect.Error)
        assertEquals(listOf("account-a" to "job-1"), stopped4)

        // 5. cancel & cancelAll
        val h5 = Harness()
        val stopped5 = mutableListOf<Pair<String, String>>()
        val w5 = JobWatch(this, h5.coordinator, { true }, { h5.effects += it }, interval) { acc, id ->
            stopped5 += acc to id
        }
        h5.ensure(w5)
        w5.cancel("account-a", "job-1")
        advanceTimeBy(interval * 2); runCurrent()
        assertEquals(listOf("account-a" to "job-1"), stopped5)

        h5.ensure(w5)
        w5.cancelAll()
        advanceTimeBy(interval * 2); runCurrent()
        assertEquals(listOf("account-a" to "job-1", "account-a" to "job-1"), stopped5)
    }

    @Test
    fun cancelThenImmediateReEnsureDoesNotLetOldFinallyStopNewWatch() = runTest {
        val h = Harness()
        val stopped = mutableListOf<Pair<String, String>>()
        val watch = JobWatch(this, h.coordinator, { true }, { h.effects += it }, interval) { acc, id ->
            stopped += acc to id
        }

        assertTrue(h.ensure(watch))
        advanceTimeBy(interval); runCurrent()
        assertTrue(stopped.isEmpty())

        watch.cancel("account-a", "job-1")
        assertEquals(1, stopped.size)

        // 立即重新订阅同一个 jobId：旧协程的 finally 不得误触发新订阅的 onStopped
        assertTrue(h.ensure(watch))
        advanceTimeBy(interval); runCurrent()
        assertEquals(1, stopped.size)
        assertTrue(watch.isWatching("account-a", "job-1"))

        h.serverState = "succeeded"
        advanceTimeBy(interval); runCurrent()
        assertEquals(2, stopped.size)
        watch.cancelAll()
    }
}
