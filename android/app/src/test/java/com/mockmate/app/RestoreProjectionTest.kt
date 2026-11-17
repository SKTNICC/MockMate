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
import com.mockmate.app.data.RestoreCoordinator
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreResult
import com.mockmate.app.data.RestoreScope
import com.mockmate.app.data.RestoreUiEffect
import com.mockmate.app.data.RestoreUiGate
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * E01：最终 UI 归并边界。断言的是实际生产类 RestoreUiGate/RestoreCoordinator 的行为：
 * 迟到结果不投影、clearJob 挂起期间切页也不投影、清理只针对原账号与原任务。
 */
class RestoreProjectionTest {

    private fun job(
        state: String = "succeeded",
        kind: String = "plan",
        target: String = "prep-1",
        revision: Int = 1,
        resultId: String = "plan-1",
    ) = JobDto(
        id = "job-1",
        kind = kind,
        target_id = target,
        input_revision = revision,
        state = state,
        attempt = 1,
        retryable = false,
        result_ref = ResultRefDto("plan", resultId),
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

    private class Ports(
        private val jobValue: JobDto,
        private val preparationValue: PreparationDto = PreparationDto(
            id = "prep-1",
            revision = 1,
            role_id = "java-backend",
            level = "intern",
            main_question_count = 6,
            created_at = "2026-11-17T00:00:00Z",
            updated_at = "2026-11-17T00:00:00Z",
        ),
    ) : RestorePorts {
        override suspend fun job(id: String) = ApiResult.Ok(jobValue)
        override suspend fun preparation(id: String) = ApiResult.Ok(preparationValue)
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

    private fun projectResult(scope: RestoreScope): RestoreResult = RestoreResult(
        scope,
        RestoreOutcome.Project(job = job(), preparation = preparation(), plan = plan(), message = "恢复计划"),
    )

    @Test
    fun gateProjectsWhenOriginalScopeIsStillCurrent() = runBlocking {
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ scope() }) { account, jobId -> cleared += account to jobId }
        val effect = gate.gate(projectResult(scope()))
        assertTrue(effect is RestoreUiEffect.Project)
        assertEquals(listOf("account-a" to "job-1"), cleared)
    }

    @Test
    fun pageChangeDuringClearJobDoesNotProjectOldPlan() = runBlocking {
        val current = AtomicReference(scope())
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ current.get() }) { account, jobId ->
            cleared += account to jobId
            entered.complete(Unit)
            release.await()
        }
        val pending = async { gate.gate(projectResult(scope())) }
        entered.await()
        // clearActiveJob 挂起期间用户从 P1 切到 P2。
        current.set(scope(prep = "prep-2", navigation = 2))
        release.complete(Unit)
        val effect = pending.await()
        assertTrue("clearJob 挂起期间切页后不得投影旧计划", effect is RestoreUiEffect.Ignore)
        assertEquals(listOf("account-a" to "job-1"), cleared)
    }

    @Test
    fun staleInformFromP1DoesNotChangeCurrentPage() = runBlocking {
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ scope(prep = "prep-2", navigation = 2) }) { account, jobId ->
            cleared += account to jobId
        }
        val result = RestoreResult(
            scope(),
            RestoreOutcome.Inform(job(), "旧 revision 提示", clearActiveJob = true),
        )
        val effect = gate.gate(result)
        assertTrue(effect is RestoreUiEffect.Ignore)
        // 记录清理针对原账号与原任务，允许发生；UI 不投影。
        assertEquals(listOf("account-a" to "job-1"), cleared)
    }

    @Test
    fun staleFailureFromP1DoesNotShowError() = runBlocking {
        val gate = RestoreUiGate({ scope(prep = "prep-2", navigation = 2) }) { _, _ -> }
        val effect = gate.gate(
            RestoreResult(scope(), RestoreOutcome.Failed("NOT_FOUND", "旧计划不存在"))
        )
        assertTrue(effect is RestoreUiEffect.Ignore)
    }

    @Test
    fun notFoundClearsOnlyOriginalAccountAndJob() = runBlocking {
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ scope(prep = "prep-2", navigation = 2) }) { account, jobId ->
            cleared += account to jobId
        }
        val effect = gate.gate(RestoreResult(scope(), RestoreOutcome.NotFound("job-1")))
        assertTrue(effect is RestoreUiEffect.Ignore)
        assertEquals(listOf("account-a" to "job-1"), cleared)
    }

    @Test
    fun currentFailureBecomesOneErrorEffect() = runBlocking {
        val gate = RestoreUiGate({ scope() }) { _, _ -> }
        val effect = gate.gate(
            RestoreResult(scope(), RestoreOutcome.Failed("NETWORK_UNAVAILABLE", "离线"))
        )
        assertTrue(effect is RestoreUiEffect.Error)
        assertEquals("NETWORK_UNAVAILABLE", (effect as RestoreUiEffect.Error).code)
    }

    @Test
    fun staleSucceededWithoutCleanupFlagKeepsRecord() = runBlocking {
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ scope(prep = "prep-2", navigation = 2) }) { account, jobId ->
            cleared += account to jobId
        }
        val effect = gate.gate(RestoreResult(scope(), RestoreOutcome.Stale(job())))
        assertTrue(effect is RestoreUiEffect.Ignore)
        assertTrue(cleared.isEmpty())
    }

    @Test
    fun staleRevisionMismatchClearsTheOldJobRecord() = runBlocking {
        val cleared = mutableListOf<Pair<String, String>>()
        val gate = RestoreUiGate({ scope(prep = "prep-2", navigation = 2) }) { account, jobId ->
            cleared += account to jobId
        }
        val effect = gate.gate(
            RestoreResult(scope(), RestoreOutcome.Stale(job(), clearActiveJob = true))
        )
        assertTrue(effect is RestoreUiEffect.Ignore)
        assertEquals(listOf("account-a" to "job-1"), cleared)
    }

    @Test
    fun activeQueryCapturesSourceBeforeWaitingAndDoesNotRebindToCurrentPage() = runBlocking {
        val current = AtomicReference(scope())
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val coordinator = RestoreCoordinator(
            service = RestoreJobService(Ports(job())),
            gate = RestoreUiGate({ current.get() }) { _, _ -> },
            fetchJob = {
                entered.complete(Unit)
                release.await()
                ApiResult.Ok(job())
            },
            currentScope = { current.get() },
        )
        val pending = async { coordinator.query(scope()) }
        entered.await()
        current.set(scope(prep = "prep-2", navigation = 2))
        release.complete(Unit)
        val effect = pending.await()
        assertTrue("主动查询等待后切页必须仍按原 scope 判断", effect is RestoreUiEffect.Ignore)
    }

    @Test
    fun serviceAndGateProjectCurrentPlanEndToEnd() = runBlocking {
        val current = AtomicReference(scope())
        val coordinator = RestoreCoordinator(
            service = RestoreJobService(Ports(job())),
            gate = RestoreUiGate({ current.get() }) { _, _ -> },
            fetchJob = { ApiResult.Ok(job()) },
            currentScope = { current.get() },
        )
        val effect = coordinator.restore(scope())
        assertTrue(effect is RestoreUiEffect.Project)
        val projected = (effect as RestoreUiEffect.Project).result.outcome as RestoreOutcome.Project
        assertEquals("plan-1", projected.plan?.id)
        assertEquals("prep-1", projected.preparation?.id)
        assertFalse(effect.result.scope.jobId.isEmpty())
    }
}
