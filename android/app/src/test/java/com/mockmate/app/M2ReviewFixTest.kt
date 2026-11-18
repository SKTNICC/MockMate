package com.mockmate.app

import com.mockmate.app.core.network.InterviewAnswerDto
import com.mockmate.app.core.network.InterviewCoverageDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewReceiptDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewReportQuestionDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationReplayExecutor
import com.mockmate.app.core.operation.PendingOperationReplayer
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.core.operation.ReplayExecution
import com.mockmate.app.data.ApiResult
import com.mockmate.app.ui.InterviewUiMerge
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.screens.buildReportLayoutIndex
import com.mockmate.app.ui.screens.quoteRange
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

class M2ReviewFixTest {

    // ------------------------------------------------------------ R3 筛选恢复

    private suspend fun prepare(
        coordinator: OperationCoordinator,
        sessionId: String,
        turnId: String,
        text: String = "原回答",
    ) = coordinator.prepare(
        accountId = "account-a",
        type = "interviewAnswer",
        targetId = sessionId,
        revision = 1,
        payload = InterviewAnswerPayload(sessionId, 1, turnId, text),
    ) as PrepareResult.Prepared

    @Test
    fun replayFiltersBySessionAndRunsReceiptCallback() = runBlocking {
        val store = InMemoryOperationStore()
        var sequence = 0
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${++sequence}" })
        val currentSession = prepare(coordinator, "session-1", "turn-1")
        val otherSession = prepare(coordinator, "session-2", "turn-2")

        val executed = mutableListOf<String>()
        val callbackValues = mutableListOf<Any?>()
        val executor = object : OperationReplayExecutor {
            override suspend fun execute(op: com.mockmate.app.core.operation.PendingOperation): ReplayExecution {
                executed += op.operationKey
                return ReplayExecution.Ok(
                    InterviewReceiptDto("session-1", 2, "answer-1", "job-1")
                )
            }
        }
        val report = PendingOperationReplayer(coordinator, executor).replay(
            accountId = "account-a",
            isCurrent = { true },
            filter = { op ->
                (op.payload as? InterviewAnswerPayload)?.sessionId == "session-1"
            },
            onCompleted = { _, value -> callbackValues += value },
        )

        assertEquals(1, report.attempted)
        assertEquals(1, report.completed)
        assertEquals(listOf(currentSession.operation.operationKey), executed)
        assertTrue(callbackValues.single() is InterviewReceiptDto)
        val pending = (coordinator.pending("account-a") as OperationLoad.Loaded).operations.pending
        assertEquals(listOf(otherSession.operation.operationKey), pending.map { it.operationKey })
    }

    @Test
    fun uncertainReplayKeepsPendingAndSkipsReceiptCallback() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "key-1" })
        prepare(coordinator, "session-1", "turn-1")
        var callbacks = 0
        val executor = object : OperationReplayExecutor {
            override suspend fun execute(op: com.mockmate.app.core.operation.PendingOperation): ReplayExecution =
                ReplayExecution.Uncertain(ApiResult.Err("NETWORK_UNAVAILABLE", "offline", true))
        }
        val report = PendingOperationReplayer(coordinator, executor).replay(
            accountId = "account-a",
            isCurrent = { true },
            filter = { true },
            onCompleted = { _, _ -> callbacks += 1 },
        )
        assertEquals(0, report.completed)
        assertEquals(1, report.retained)
        assertEquals(0, callbacks)
        val pending = (coordinator.pending("account-a") as OperationLoad.Loaded).operations.pending
        assertEquals(1, pending.size)
    }

    @Test
    fun definiteReplayErrorIsReportedAndClearsPending() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "key-1" })
        prepare(coordinator, "session-1", "turn-1")
        var callbacks = 0
        val executor = object : OperationReplayExecutor {
            override suspend fun execute(op: com.mockmate.app.core.operation.PendingOperation): ReplayExecution =
                ReplayExecution.DefiniteError(
                    ApiResult.Err("TURN_NOT_CURRENT", "该回合已被其他设备提交", false)
                )
        }
        val report = PendingOperationReplayer(coordinator, executor).replay(
            accountId = "account-a",
            isCurrent = { true },
            filter = { true },
            onCompleted = { _, _ -> callbacks += 1 },
        )
        assertEquals(1, report.completed)
        assertEquals("TURN_NOT_CURRENT", report.rejected.single().code)
        assertEquals(0, callbacks)
        val pending = (coordinator.pending("account-a") as OperationLoad.Loaded).operations.pending
        assertTrue(pending.isEmpty())
    }

    // ------------------------------------------------------------ R4 迟到回包合并

    private fun detail(
        revision: Int = 3,
        reportState: String = "pending",
        followUpAttempt: Int = 1,
        followUpState: String = "running",
    ) = InterviewDetailDto(
        session_id = "session-1",
        plan_id = "plan-1",
        preparation_id = "prep-1",
        role_id = "java-backend",
        level = "intern",
        mode = "text",
        status = "active",
        phase = "deciding_follow_up",
        revision = revision,
        started_at = "2026-11-17T00:00:00Z",
        plan_summary = InterviewPlanSummaryDto(
            plan_id = "plan-1",
            main_question_count = 6,
            source_counts = SourceCountsDto(3, 3),
        ),
        opening_text = "开场",
        follow_up_job = InterviewJobSummaryDto(
            id = "job-1",
            state = followUpState,
            attempt = followUpAttempt,
        ),
        report_state = reportState,
    )

    private fun job(attempt: Int = 1, state: String = "running") = JobDto(
        id = "job-1",
        kind = "follow_up",
        target_id = "session-1",
        input_revision = 2,
        state = state,
        attempt = attempt,
        retryable = false,
        created_at = "2026-11-17T00:00:00Z",
        updated_at = "2026-11-17T00:00:01Z",
    )

    @Test
    fun lowerSessionRevisionCannotOverwriteNewerDetail() {
        assertFalse(InterviewUiMerge.shouldApplyDetail(detail(revision = 9), detail(revision = 4)))
        assertTrue(InterviewUiMerge.shouldApplyDetail(detail(revision = 4), detail(revision = 4)))
        assertTrue(InterviewUiMerge.shouldApplyDetail(detail(revision = 4), detail(revision = 5)))
        assertFalse(
            InterviewUiMerge.shouldApplyDetail(
                detail(revision = 5, reportState = "ready"),
                detail(revision = 5, reportState = "pending"),
            )
        )
        assertTrue(
            InterviewUiMerge.shouldApplyDetail(
                detail(revision = 5, reportState = "pending"),
                detail(revision = 5, reportState = "ready"),
            )
        )
        assertFalse(
            InterviewUiMerge.shouldApplyDetail(
                detail(revision = 5, followUpAttempt = 2, followUpState = "succeeded"),
                detail(revision = 5, followUpAttempt = 1, followUpState = "running"),
            )
        )
    }

    @Test
    fun oldAttemptOrRunningPollCannotOverrideSucceededJob() {
        assertFalse(InterviewUiMerge.shouldApplyJob(job(2, "succeeded"), job(1, "running")))
        assertFalse(InterviewUiMerge.shouldApplyJob(job(1, "succeeded"), job(1, "running")))
        assertTrue(InterviewUiMerge.shouldApplyJob(job(1, "failed"), job(2, "queued")))
        assertTrue(InterviewUiMerge.shouldApplyJob(null, job(1, "running")))
    }

    @Test
    fun oldInterviewJobAttemptCannotOverrideDetailPointer() {
        val current = detail(followUpAttempt = 2, followUpState = "succeeded")
        assertFalse(InterviewUiMerge.interviewJobUpdateAllowed(current, job(1, "running")))
        assertTrue(InterviewUiMerge.interviewJobUpdateAllowed(current, job(3, "queued")))
    }

    // ------------------------------------------------------------ R2 基础报告跳转

    @Test
    fun basicReportPolicyKeepsUserOnTheCurrentSessionPage() {
        assertEquals(
            InterviewUiMerge.BasicReportPolicy.OPEN_REPORT,
            InterviewUiMerge.basicReportPolicy(Screen.InterviewRoom("s1"), "s1"),
        )
        assertEquals(
            InterviewUiMerge.BasicReportPolicy.REFRESH_REPORT,
            InterviewUiMerge.basicReportPolicy(Screen.InterviewReport("s1"), "s1"),
        )
        assertEquals(
            InterviewUiMerge.BasicReportPolicy.LEAVE,
            InterviewUiMerge.basicReportPolicy(Screen.InterviewRoom("s2"), "s1"),
        )
        assertEquals(
            InterviewUiMerge.BasicReportPolicy.LEAVE,
            InterviewUiMerge.basicReportPolicy(Screen.InterviewHistory, "s1"),
        )
    }

    // ------------------------------------------------------------ R8 报告定位

    private fun report(
        strengths: Int = 1,
        improvements: Int = 1,
    ): InterviewReportDto = InterviewReportDto(
        report_id = "report-1",
        session_id = "session-1",
        role_id = "java-backend",
        level = "intern",
        quality = "full",
        rubric_policy_version = "m2-rubric-policy-v1",
        created_at = "2026-11-17T00:10:00Z",
        end_reason = "completed",
        coverage = InterviewCoverageDto(
            planned = 2, presented = 2, answered = 2, skipped = 0, unanswered = 0,
            not_reached = 0, scored = 2, bank_planned = 1, bank_answered = 1,
            generated_planned = 1, generated_answered = 1,
            follow_up_presented = 1, follow_up_answered = 1,
        ),
        strengths = List(strengths) {
            com.mockmate.app.core.network.InterviewFindingDto(
                question_id = "q-$it",
                position = 1,
                dimension_id = "dim",
                text = "优势 $it",
            )
        },
        improvements = List(improvements) {
            com.mockmate.app.core.network.InterviewFindingDto(
                question_id = "q-$it",
                position = 1,
                dimension_id = "dim",
                text = "改进 $it",
            )
        },
        questions = listOf(
            InterviewReportQuestionDto(
                question_id = "q-1",
                position = 1,
                source = "bank",
                outcome = "answered",
                prompt = "主问题 1",
                assessment_trust = "verified",
                rubric_version = "r@1",
                turns = listOf(
                    InterviewTurnDto(
                        turn_id = "turn-1",
                        question_id = "q-1",
                        position = 1,
                        kind = "main",
                        prompt = "主问题 1",
                        status = "answered",
                        answer = InterviewAnswerDto("answer-1", "第一题主答", "2026-11-17T00:00:00Z"),
                    )
                ),
            ),
            InterviewReportQuestionDto(
                question_id = "q-2",
                position = 2,
                source = "generated",
                outcome = "answered",
                prompt = "主问题 2",
                assessment_trust = "provisional",
                rubric_version = "r@1",
                turns = listOf(
                    InterviewTurnDto(
                        turn_id = "turn-2",
                        question_id = "q-2",
                        position = 2,
                        kind = "main",
                        prompt = "主问题 2",
                        status = "answered",
                        answer = InterviewAnswerDto("answer-2", "第二题主答", "2026-11-17T00:00:01Z"),
                    )
                ),
            ),
        ),
    )

    @Test
    fun reportLayoutAccountsForOptionalOverviewCards() {
        val both = buildReportLayoutIndex(report(strengths = 1, improvements = 1))
        assertEquals(4, both.questionItemIndex["q-1"])
        assertEquals(5, both.questionItemIndex["q-2"])
        assertEquals(5, both.answerQuestionItemIndex["answer-2"])
        assertEquals("第二题主答", both.answerText["answer-2"])

        val onlyStrengths = buildReportLayoutIndex(report(strengths = 1, improvements = 0))
        assertEquals(3, onlyStrengths.questionItemIndex["q-1"])
        assertEquals(4, onlyStrengths.answerQuestionItemIndex["answer-2"])
    }

    @Test
    fun quoteRangeFindsFirstExactMatchWithChineseNewlineAndEmoji() {
        val text = "第一行\n用了「集合，哈希」验证 😀 表情；再次提到「集合，哈希」。"
        val range = quoteRange(text, "「集合，哈希」")
        assertNotNull(range)
        assertEquals("「集合，哈希」", text.substring(range!!))
        assertTrue(range.first < text.indexOf("😀"))
        assertEquals(text.indexOf("「集合，哈希」"), range.first)
        assertEquals(range, quoteRange(text, "「集合，哈希」"))
        assertEquals(null, quoteRange(text, "不存在"))
    }
}
