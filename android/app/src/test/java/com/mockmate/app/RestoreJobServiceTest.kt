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
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreResult
import com.mockmate.app.data.RestoreScope
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** D02/E01：恢复结果按账号／导航／当前对象隔离，迟到回包不得改写当前页。 */
class RestoreJobServiceTest {

    private fun job(
        id: String = "job-1",
        kind: String = "plan",
        state: String = "succeeded",
        target: String = "prep-1",
        revision: Int = 1,
        resultId: String? = "plan-1",
    ) = JobDto(
        id = id,
        kind = kind,
        target_id = target,
        input_revision = revision,
        state = state,
        attempt = 1,
        retryable = false,
        result_ref = resultId?.let { ResultRefDto("plan", it) },
        error = null,
        created_at = "2026-11-16T00:00:00Z",
        updated_at = "2026-11-16T00:00:00Z",
    )

    private fun preparation(id: String = "prep-1", revision: Int = 1) = PreparationDto(
        id = id,
        revision = revision,
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        match_report_id = "match-1",
        current_plan_id = "plan-1",
        created_at = "2026-11-16T00:00:00Z",
        updated_at = "2026-11-16T00:00:00Z",
    )

    private fun plan(id: String = "plan-1", preparationId: String = "prep-1") = PublicPlanDto(
        id = id,
        preparation_id = preparationId,
        input_revision = 1,
        revision = 1,
        state = "ready",
        role_id = "java-backend",
        level = "intern",
        main_question_count = 6,
        source_counts = SourceCountsDto(3, 3),
        resume_snapshot_id = "resume-1",
        jd_snapshot_id = "jd-1",
        created_at = "2026-11-16T00:00:00Z",
    )

    private fun material(id: String = "material-1") = MaterialDraftDto(
        id = id,
        revision = 1,
        kind = "resume",
        title = "synthetic",
        text = "synthetic text",
        state = "parsed",
        created_at = "2026-11-16T00:00:00Z",
        updated_at = "2026-11-16T00:00:00Z",
    )

    private class FakePorts(
        var jobValue: JobDto,
        var preparationValue: PreparationDto = PreparationDto(
            id = "prep-1",
            revision = 1,
            role_id = "java-backend",
            level = "intern",
            main_question_count = 6,
            created_at = "2026-11-16T00:00:00Z",
            updated_at = "2026-11-16T00:00:00Z",
        ),
        var planValue: PublicPlanDto? = null,
        var reportValue: MatchReportDto? = null,
        var materialValue: MaterialDraftDto? = null,
        var snapshotsValue: List<SnapshotDto> = emptyList(),
    ) : RestorePorts {
        var jobGate: CompletableDeferred<Unit>? = null
        var planGate: CompletableDeferred<Unit>? = null
        var jobResult: ApiResult<JobDto>? = null

        override suspend fun job(id: String): ApiResult<JobDto> {
            jobGate?.await()
            return jobResult ?: ApiResult.Ok(jobValue)
        }

        override suspend fun preparation(id: String): ApiResult<PreparationDto> =
            ApiResult.Ok(preparationValue)

        override suspend fun plan(id: String): ApiResult<PublicPlanDto> {
            planGate?.await()
            return planValue?.let { ApiResult.Ok(it) }
                ?: ApiResult.Err("NOT_FOUND", "missing", false)
        }

        override suspend fun matchReport(id: String): ApiResult<MatchReportDto> =
            reportValue?.let { ApiResult.Ok(it) }
                ?: ApiResult.Err("NOT_FOUND", "missing", false)

        override suspend fun material(id: String): ApiResult<MaterialDraftDto> =
            materialValue?.let { ApiResult.Ok(it) }
                ?: ApiResult.Err("NOT_FOUND", "missing", false)

        override suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>> =
            ApiResult.Ok(snapshotsValue)
    }

    private fun scope(
        account: String = "account-a",
        prep: String? = "prep-1",
        screen: String = RestoreJobService.SCREEN_PLAN,
        navigation: Int = 3,
        material: String? = null,
    ) = RestoreScope(
        accountId = account,
        sessionGeneration = 1,
        navigationEpoch = navigation,
        screen = screen,
        preparationId = prep,
        materialId = material,
        jobId = "job-1",
    )

    private fun outcome(result: RestoreResult): RestoreOutcome = result.outcome

    @Test
    fun delayedResultAfterAccountSwitchDoesNotTouchNewAccount() = runBlocking {
        val delayed = CompletableDeferred<Unit>()
        val ports = FakePorts(jobValue = job()).apply { jobGate = delayed }
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val result = async(Dispatchers.Default) {
            service.restore(scope()) { current.get() }
        }
        // 请求还在等待时切换到账号 B。
        current.set(scope(account = "account-b", prep = "prep-b"))
        delayed.complete(Unit)
        val value = result.await()
        assertTrue(outcome(value) is RestoreOutcome.Stale)
        assertEquals(scope().accountId, value.scope.accountId)
        // A 的账号内恢复记录仍在，没有被错误丢失。
        assertNotNull(service.recoveredJob("account-a", "job-1"))
        assertEquals("account-b", current.get().accountId)
    }

    @Test
    fun delayedPlanForP1DoesNotProjectIntoP2() = runBlocking {
        val delayed = CompletableDeferred<Unit>()
        val ports = FakePorts(jobValue = job(), planValue = plan()).apply { planGate = delayed }
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope(prep = "prep-1"))
        val result = async(Dispatchers.Default) {
            service.restore(scope(prep = "prep-1")) { current.get() }
        }
        // 同账号从 P1 切到 P2，P1 的计划回包迟到。
        current.set(scope(prep = "prep-2"))
        delayed.complete(Unit)
        val value = result.await()
        assertTrue(outcome(value) is RestoreOutcome.Stale)
        assertEquals("prep-1", value.scope.preparationId)
        assertNotNull(service.recoveredObjectState("account-a", "plan", "prep-1"))
        assertEquals("prep-2", current.get().preparationId)
    }

    @Test
    fun navigationChangeInvalidatesDelayedSuccess() = runBlocking {
        val delayed = CompletableDeferred<Unit>()
        val ports = FakePorts(jobValue = job(), planValue = plan()).apply { planGate = delayed }
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope(navigation = 3))
        val result = async(Dispatchers.Default) {
            service.restore(scope(navigation = 3)) { current.get() }
        }
        current.set(scope(navigation = 4))
        delayed.complete(Unit)
        assertTrue(outcome(result.await()) is RestoreOutcome.Stale)
    }

    @Test
    fun staleFailureDoesNotShowErrorOnAnotherPreparation() = runBlocking {
        val ports = FakePorts(jobValue = job(state = "failed"))
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope(prep = "prep-2"))
        val value = service.restore(scope(prep = "prep-1")) { current.get() }
        assertTrue(outcome(value) is RestoreOutcome.Stale)
        assertNotNull(service.recoveredJob("account-a", "job-1"))
    }

    @Test
    fun delayedJobNotFoundForP1StaysBoundToOriginalScope() = runBlocking {
        val delayed = CompletableDeferred<Unit>()
        val ports = FakePorts(jobValue = job()).apply {
            jobGate = delayed
            jobResult = ApiResult.Err("NOT_FOUND", "missing", false, httpStatus = 404)
        }
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope())
        val pending = async { service.restore(scope()) { current.get() } }
        current.set(scope(prep = "prep-2", navigation = 4))
        delayed.complete(Unit)
        val value = pending.await()
        assertTrue(outcome(value) is RestoreOutcome.NotFound)
        assertEquals("prep-1", value.scope.preparationId)
    }

    @Test
    fun currentJobReadErrorShowsOnlyWithinTheSameCapturedPage() = runBlocking {
        val ports = FakePorts(jobValue = job()).apply {
            jobResult = ApiResult.Err("NETWORK_UNAVAILABLE", "offline", true)
        }
        val service = RestoreJobService(ports)
        val value = service.restore(scope()) { scope() }
        assertTrue(outcome(value) is RestoreOutcome.Failed)
        assertEquals("prep-1", value.scope.preparationId)
    }

    @Test
    fun currentPlanIsProjectedAndRecovered() = runBlocking {
        val ports = FakePorts(jobValue = job(), planValue = plan())
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope(prep = "prep-1"))
        val value = service.restore(scope(prep = "prep-1")) { current.get() }
        assertTrue(outcome(value) is RestoreOutcome.Project)
        val project = outcome(value) as RestoreOutcome.Project
        assertEquals("plan-1", project.plan?.id)
        assertEquals("prep-1", project.preparation?.id)
        assertNotNull(service.recoveredJob("account-a", "job-1"))
    }

    @Test
    fun planReferencingAnotherPreparationIsRejectedNotProjected() = runBlocking {
        val ports = FakePorts(jobValue = job(), planValue = plan(preparationId = "prep-other"))
        val service = RestoreJobService(ports)
        val value = service.restore(scope(prep = "prep-1")) { scope(prep = "prep-1") }
        assertTrue(outcome(value) is RestoreOutcome.Failed)
    }

    @Test
    fun runningJobOnlyInformsWhenCurrentTargetMatches() = runBlocking {
        val ports = FakePorts(jobValue = job(state = "running", resultId = null))
        val service = RestoreJobService(ports)
        val current = AtomicReference(scope(prep = "prep-1"))
        val value = service.restore(scope(prep = "prep-1")) { current.get() }
        assertTrue(outcome(value) is RestoreOutcome.Inform)
        assertTrue((outcome(value) as RestoreOutcome.Inform).message.contains("不会重复提交"))
    }

    @Test
    fun notFoundClearsOnlyThatJob() = runBlocking {
        val ports = FakePorts(jobValue = job()).apply {
            jobResult = ApiResult.Err("NOT_FOUND", "missing", false)
        }
        val service = RestoreJobService(ports)
        val value = service.restore(scope()) { scope() }
        assertTrue(outcome(value) is RestoreOutcome.NotFound)
    }

    @Test
    fun materialProjectionUsesScreenMaterialIdNotStaleGlobalState() = runBlocking {
        val ports = FakePorts(
            jobValue = job(kind = "parse", target = "material-1", resultId = "material-1"),
            materialValue = material("material-1"),
        )
        val service = RestoreJobService(ports)
        val current = AtomicReference(
            scope(prep = "prep-1", screen = RestoreJobService.SCREEN_REVIEW, material = "material-1")
        )
        val value = service.restore(
            scope(prep = "prep-1", screen = RestoreJobService.SCREEN_REVIEW, material = "material-1")
        ) { current.get() }
        assertTrue(outcome(value) is RestoreOutcome.Project)
        assertEquals("material-1", (outcome(value) as RestoreOutcome.Project).material?.id)
    }

    @Test
    fun materialProjectionFailsWhenPageHasAnotherMaterial() = runBlocking {
        val ports = FakePorts(
            jobValue = job(kind = "parse", target = "material-1", resultId = "material-1"),
            materialValue = material("material-1"),
        )
        val service = RestoreJobService(ports)
        val current = AtomicReference(
            scope(prep = "prep-1", screen = RestoreJobService.SCREEN_REVIEW, material = "material-2")
        )
        val value = service.restore(
            scope(prep = "prep-1", screen = RestoreJobService.SCREEN_REVIEW, material = "material-1")
        ) { current.get() }
        assertTrue(outcome(value) is RestoreOutcome.Stale)
    }
}
