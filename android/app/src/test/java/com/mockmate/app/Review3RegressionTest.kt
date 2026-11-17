package com.mockmate.app

import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.ActiveJob
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.OperationStore
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreScope
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.delay
import kotlinx.coroutines.runBlocking
import kotlinx.coroutines.withTimeoutOrNull
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 第三轮独立复核 T01/T02 的正式回归（E00）。
 * 断言的是“正确行为”：迟到结果不得投影到另一准备；并发写不得丢未知回执与活跃任务。
 * 修复前这些用例必须失败，修复后必须通过；诊断“缺陷存在”的断言不计入验收。
 */
private fun samplePlan(): PublicPlanDto = PublicPlanDto(
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

class Review3RegressionTest {

    // ---------------------------------------------------------------- T01 恢复隔离

    private fun scope(
        prep: String = "prep-1",
        navigation: Int = 1,
        screen: String = RestoreJobService.SCREEN_PLAN,
    ) = RestoreScope(
        accountId = "account-a",
        sessionGeneration = 1,
        navigationEpoch = navigation,
        screen = screen,
        preparationId = prep,
        jobId = "job-1",
    )

    private fun job(
        state: String = "succeeded",
        kind: String = "plan",
        target: String = "prep-1",
        revision: Int = 1,
        resultId: String? = "plan-1",
    ) = JobDto(
        id = "job-1",
        kind = kind,
        target_id = target,
        input_revision = revision,
        state = state,
        attempt = 1,
        retryable = false,
        result_ref = resultId?.let { ResultRefDto("plan", it) },
        error = null,
        created_at = "2026-11-17T00:00:00Z",
        updated_at = "2026-11-17T00:00:00Z",
    )

    private fun preparation(revision: Int = 1) = PreparationDto(
        id = "prep-1",
        revision = revision,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        created_at = "2026-11-17T00:00:00Z",
        updated_at = "2026-11-17T00:00:00Z",
    )

    /** 可在指定下游调用处暂停，用来制造 P1 回包迟到。 */
    private class DelayedPorts(
        private val jobValue: JobDto,
        private val preparationValue: PreparationDto,
        private val delayAt: String,
    ) : RestorePorts {
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        var jobError: ApiResult.Err? = null

        override suspend fun job(id: String): ApiResult<JobDto> {
            if (delayAt == "job") {
                entered.complete(Unit)
                release.await()
            }
            jobError?.let { return it }
            return ApiResult.Ok(jobValue)
        }

        override suspend fun preparation(id: String): ApiResult<PreparationDto> {
            if (delayAt == "preparation") {
                entered.complete(Unit)
                release.await()
            }
            return ApiResult.Ok(preparationValue)
        }

        override suspend fun plan(id: String): ApiResult<PublicPlanDto> {
            if (delayAt == "plan") {
                entered.complete(Unit)
                release.await()
            }
            return ApiResult.Ok(samplePlan())
        }

        override suspend fun matchReport(id: String): ApiResult<MatchReportDto> =
            ApiResult.Err("NOT_FOUND", "missing", false, httpStatus = 404)

        override suspend fun material(id: String): ApiResult<MaterialDraftDto> =
            ApiResult.Err("NOT_FOUND", "missing", false, httpStatus = 404)

        override suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>> =
            ApiResult.Ok(emptyList())
    }

    @Test
    fun delayedJobNotFoundForP1StaysBoundToOriginalScope() = runBlocking {
        val ports = DelayedPorts(job(), preparation(), "job")
        ports.jobError = ApiResult.Err("NOT_FOUND", "old job missing", false, httpStatus = 404)
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        ports.entered.await()
        current.set(scope("prep-2", 2))
        ports.release.complete(Unit)

        val result = pending.await()
        assertTrue(
            "P1 任务读取 NOT_FOUND 只能是原 scope 的内部结果，实际为 ${result.outcome}",
            result.outcome is RestoreOutcome.NotFound,
        )
        assertEquals("prep-1", result.scope.preparationId)
        assertEquals("job-1", (result.outcome as RestoreOutcome.NotFound).jobId)
    }

    @Test
    fun delayedPlanReadErrorForP1IsStaleAfterNavigationToP2() = runBlocking {
        val ports = object : RestorePorts {
            override suspend fun job(id: String) = ApiResult.Ok(job())
            override suspend fun preparation(id: String) = ApiResult.Ok(preparation())
            val entered = CompletableDeferred<Unit>()
            val release = CompletableDeferred<Unit>()
            override suspend fun plan(id: String): ApiResult<PublicPlanDto> {
                entered.complete(Unit)
                release.await()
                return ApiResult.Err("NOT_FOUND", "old plan missing", false, httpStatus = 404)
            }

            override suspend fun matchReport(id: String) =
                ApiResult.Err("NOT_FOUND", "missing", false, httpStatus = 404)

            override suspend fun material(id: String) =
                ApiResult.Err("NOT_FOUND", "missing", false, httpStatus = 404)

            override suspend fun snapshots(materialId: String) = ApiResult.Ok(emptyList<SnapshotDto>())
        }
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        ports.entered.await()
        current.set(scope("prep-2", 2))
        ports.release.complete(Unit)

        val outcome = pending.await().outcome
        assertTrue("P1 计划读取失败在切到 P2 后必须按 Stale 处理，实际为 $outcome", outcome is RestoreOutcome.Stale)
    }

    @Test
    fun delayedRevisionMismatchForP1IsStaleAfterNavigationToP2() = runBlocking {
        val ports = DelayedPorts(job(), preparation(revision = 2), "preparation")
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        ports.entered.await()
        current.set(scope("prep-2", 2))
        ports.release.complete(Unit)

        val outcome = pending.await().outcome
        assertTrue(
            "P1 的旧 revision 提示在切到 P2 后必须按 Stale 处理，实际为 $outcome",
            outcome is RestoreOutcome.Stale,
        )
    }

    @Test
    fun delayedMissingResultRefForP1IsStaleAfterNavigationToP2() = runBlocking {
        val ports = DelayedPorts(job(resultId = null), preparation(), "job")
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        ports.entered.await()
        current.set(scope("prep-2", 2))
        ports.release.complete(Unit)

        val outcome = pending.await().outcome
        assertTrue(
            "P1 缺少结果引用在切到 P2 后必须按 Stale 处理，实际为 $outcome",
            outcome is RestoreOutcome.Stale,
        )
    }

    @Test
    fun delayedSuccessForP1DoesNotProjectIntoP2() = runBlocking {
        val ports = DelayedPorts(job(), preparation(), "plan")
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        ports.entered.await()
        current.set(scope("prep-2", 2))
        ports.release.complete(Unit)

        assertTrue(pending.await().outcome is RestoreOutcome.Stale)
        assertNotNull(service.recoveredObjectState("account-a", "plan", "prep-1"))
    }

    // ---------------------------------------------------------------- T02 操作日志原子性

    /**
     * 第一次 save 暂停；用来让第二个并发事务在第一个事务提交前完成读改写。
     * 修复后共享原子边界会阻塞第二个事务，因此 secondSave 不会发生。
     */
    private class GatedStore(initial: AccountOperations = AccountOperations()) : OperationStore {
        @Volatile
        var value: AccountOperations = initial
        val firstSaveEntered = CompletableDeferred<Unit>()
        val releaseFirstSave = CompletableDeferred<Unit>()
        val secondSaveEntered = CompletableDeferred<Unit>()
        private val saves = AtomicInteger()

        override suspend fun load(accountId: String): OperationLoad = OperationLoad.Loaded(value)

        override suspend fun save(accountId: String, operations: AccountOperations): OperationSave {
            val index = saves.incrementAndGet()
            if (index == 1) {
                firstSaveEntered.complete(Unit)
                releaseFirstSave.await()
            } else if (index == 2) {
                secondSaveEntered.complete(Unit)
            }
            value = operations
            return OperationSave.Ok
        }
    }

    private fun payload(text: String) = CreateTextMaterialPayload("resume", "title-$text", "body-$text")

    private suspend fun pendingCount(coordinator: OperationCoordinator, account: String = "account-a"): Int =
        ((coordinator.pending(account) as OperationLoad.Loaded).operations).pending.size

    /** 等第二个事务确实抢在第一个提交前完成；返回是否观察到并发写。 */
    private suspend fun observeSecondSave(store: GatedStore): Boolean =
        withTimeoutOrNull(1_000) { store.secondSaveEntered.await() } != null

    @Test
    fun concurrentDistinctUnknownReceiptWritesKeepBothKeys() = runBlocking {
        val store = GatedStore()
        val sequence = AtomicInteger()
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${sequence.incrementAndGet()}" })

        val first = async {
            coordinator.prepare("account-a", "createTextMaterial", null, null, payload("first"))
        }
        store.firstSaveEntered.await()
        val second = async {
            coordinator.prepare("account-a", "createTextMaterial", null, null, payload("second"))
        }
        observeSecondSave(store)
        store.releaseFirstSave.complete(Unit)

        val a = first.await() as PrepareResult.Prepared
        val b = second.await() as PrepareResult.Prepared
        assertEquals("两个不同操作必须各保留一条 pending", 2, pendingCount(coordinator))
        assertTrue(a.operation.operationKey != b.operation.operationKey)
    }

    @Test
    fun concurrentSameLogicalOperationGetsOneKeyAndOneRecord() = runBlocking {
        val store = GatedStore()
        val sequence = AtomicInteger()
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${sequence.incrementAndGet()}" })

        val first = async {
            coordinator.prepare("account-a", "createTextMaterial", null, null, payload("same"))
        }
        store.firstSaveEntered.await()
        val second = async {
            coordinator.prepare("account-a", "createTextMaterial", null, null, payload("same"))
        }
        observeSecondSave(store)
        store.releaseFirstSave.complete(Unit)

        val a = first.await() as PrepareResult.Prepared
        val b = second.await() as PrepareResult.Prepared
        assertEquals("同一逻辑操作并发只能分配一个 key", a.operation.operationKey, b.operation.operationKey)
        assertEquals("同一逻辑操作并发只能落一条 pending", 1, pendingCount(coordinator))
    }

    @Test
    fun prepareAndRecordActiveJobDoNotOverwriteEachOther() = runBlocking {
        val store = GatedStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "prepare-key" })

        val prepared = async {
            coordinator.prepare("account-a", "createTextMaterial", null, null, payload("first"))
        }
        store.firstSaveEntered.await()
        val job = async {
            coordinator.recordActiveJob(
                "account-a",
                ActiveJob(jobId = "job-1", kind = "plan", targetId = "prep-1", preparationId = "prep-1"),
            )
        }
        observeSecondSave(store)
        store.releaseFirstSave.complete(Unit)
        prepared.await()
        job.await()

        val operations = (coordinator.pending("account-a") as OperationLoad.Loaded).operations
        assertEquals(1, operations.pending.size)
        assertEquals("活跃任务记录不能被 pending 的旧副本覆盖", 1, operations.jobs.size)
    }

    @Test
    fun clearActiveJobDoesNotDropJobAddedConcurrently() = runBlocking {
        val store = GatedStore(
            AccountOperations(
                jobs = listOf(
                    ActiveJob(jobId = "job-old", kind = "plan", targetId = "prep-1", preparationId = "prep-1"),
                )
            )
        )
        val coordinator = OperationCoordinator(store, keyFactory = { "unused" })

        val clear = async { coordinator.completeActiveJob("account-a", "job-old") }
        store.firstSaveEntered.await()
        val add = async {
            coordinator.recordActiveJob(
                "account-a",
                ActiveJob(jobId = "job-new", kind = "match", targetId = "prep-1", preparationId = "prep-1"),
            )
        }
        observeSecondSave(store)
        store.releaseFirstSave.complete(Unit)
        clear.await()
        add.await()

        val jobs = (coordinator.pending("account-a") as OperationLoad.Loaded).operations.jobs
        assertEquals("清理旧任务不能抹掉同时新增的任务", listOf("job-new"), jobs.map { it.jobId })
    }
}
