package com.mockmate.app

import com.mockmate.app.core.network.InterviewCoverageDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewMaterialVersionDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.ResultRefDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreScope
import java.util.concurrent.atomic.AtomicReference
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class M2RestoreTest {
    private val sessionId = "session-1"

    private fun detail(reportState: String = "ready") = InterviewDetailDto(
        session_id = sessionId,
        plan_id = "plan-1",
        preparation_id = "prep-1",
        role_id = "java-backend",
        level = "intern",
        mode = "text",
        status = "ended",
        phase = "closed",
        revision = 12,
        started_at = "2026-11-17T00:00:00Z",
        ended_at = "2026-11-17T00:10:00Z",
        end_reason = "completed",
        plan_summary = InterviewPlanSummaryDto(
            plan_id = "plan-1",
            main_question_count = 6,
            source_counts = SourceCountsDto(3, 3),
            material_versions = listOf(
                InterviewMaterialVersionDto("resume", "r1", "m1", "简历", 1),
                InterviewMaterialVersionDto("jd", "j1", "m2", "JD", 1),
            ),
            sample_only = true,
        ),
        opening_text = "开场",
        closing_text = "收尾",
        answered_main_count = 6,
        allowed_actions = listOf("retry_report"),
        report_state = reportState,
    )

    private fun report() = InterviewReportDto(
        report_id = "report-1",
        session_id = sessionId,
        role_id = "java-backend",
        level = "intern",
        material_versions = listOf(
            InterviewMaterialVersionDto("resume", "r1", "m1", "简历", 1),
            InterviewMaterialVersionDto("jd", "j1", "m2", "JD", 1),
        ),
        quality = "full",
        rubric_policy_version = "m2-rubric-policy-v1",
        created_at = "2026-11-17T00:10:01Z",
        end_reason = "completed",
        coverage = InterviewCoverageDto(
            planned = 6, presented = 6, answered = 6, skipped = 0, unanswered = 0,
            not_reached = 0, scored = 6, bank_planned = 3, bank_answered = 3,
            generated_planned = 3, generated_answered = 3,
            follow_up_presented = 2, follow_up_answered = 2,
        ),
    )

    private fun job(kind: String) = JobDto(
        id = "job-1",
        kind = kind,
        target_id = sessionId,
        input_revision = 12,
        state = "succeeded",
        attempt = 1,
        retryable = false,
        result_ref = ResultRefDto("interview_report", "report-1"),
        created_at = "2026-11-17T00:10:00Z",
        updated_at = "2026-11-17T00:10:01Z",
    )

    private inner class Ports(
        private val jobValue: JobDto,
        private val detailValue: InterviewDetailDto,
        private val reportValue: InterviewReportDto?,
    ) : RestorePorts {
        override suspend fun job(id: String) = ApiResult.Ok(jobValue)
        override suspend fun preparation(id: String) = ApiResult.Err("NOT_FOUND", "missing", false)
        override suspend fun plan(id: String) = ApiResult.Err("NOT_FOUND", "missing", false)
        override suspend fun matchReport(id: String) = ApiResult.Err("NOT_FOUND", "missing", false)
        override suspend fun material(id: String) = ApiResult.Err("NOT_FOUND", "missing", false)
        override suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>> =
            ApiResult.Ok(emptyList())
        override suspend fun interview(sessionId: String) = ApiResult.Ok(detailValue)
        override suspend fun interviewReport(sessionId: String): ApiResult<InterviewReportDto> =
            reportValue?.let { ApiResult.Ok(it) }
                ?: ApiResult.Err("REPORT_PENDING", "pending", false)
    }

    private fun scope(session: String = sessionId) = RestoreScope(
        accountId = "account-a",
        sessionGeneration = 1,
        navigationEpoch = 3,
        screen = RestoreJobService.SCREEN_INTERVIEW,
        interviewSessionId = session,
    )

    @Test
    fun followUpJobProjectsInterviewDetail() = runBlocking {
        val service = RestoreJobService(Ports(job("follow_up"), detail("not_requested"), null))
        val current = AtomicReference(scope())
        val result = service.project(scope(), job("follow_up")) { current.get() }
        val outcome = result.outcome as RestoreOutcome.Project
        assertEquals(sessionId, outcome.interview?.session_id)
        assertNull(outcome.interviewReport)
    }

    @Test
    fun reportJobProjectsDetailAndPublishedReport() = runBlocking {
        val service = RestoreJobService(Ports(job("interview_report"), detail(), report()))
        val result = service.project(scope(), job("interview_report")) { scope() }
        val outcome = result.outcome as RestoreOutcome.Project
        assertEquals("report-1", outcome.interviewReport?.report_id)
        assertEquals("full", outcome.interviewReport?.quality)
    }

    @Test
    fun anotherInterviewSessionCannotReceiveLateResult() = runBlocking {
        val service = RestoreJobService(Ports(job("follow_up"), detail(), null))
        val current = AtomicReference(scope("session-2"))
        val result = service.project(scope(), job("follow_up")) { current.get() }
        assertTrue(result.outcome is RestoreOutcome.Stale)
    }
}
