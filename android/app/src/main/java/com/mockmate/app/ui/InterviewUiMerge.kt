package com.mockmate.app.ui

import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.JobDto

/**
 * 迟到回包的合并规则：同一对象只能向前推进。
 * ViewModel 与 JVM 回归测试共用这一实现，避免只测恢复服务而漏掉写入回调。
 */
object InterviewUiMerge {
    private val terminalStates = setOf("succeeded", "failed", "interrupted", "superseded")

    enum class BasicReportPolicy { OPEN_REPORT, REFRESH_REPORT, LEAVE }

    /** R2：报告创建回包到达时决定是否跳转；旧页面不得把用户拉回已离开的场次。 */
    fun basicReportPolicy(current: Screen?, sessionId: String): BasicReportPolicy = when {
        current is Screen.InterviewReport && current.sessionId == sessionId ->
            BasicReportPolicy.REFRESH_REPORT
        current is Screen.InterviewRoom && current.sessionId == sessionId ->
            BasicReportPolicy.OPEN_REPORT
        else -> BasicReportPolicy.LEAVE
    }

    private fun olderJobSummary(
        current: InterviewJobSummaryDto?,
        incoming: InterviewJobSummaryDto?,
    ): Boolean {
        if (current == null || incoming == null || current.id != incoming.id) return false
        if (incoming.attempt < current.attempt) return true
        if (
            current.state == "succeeded" &&
            incoming.state != "succeeded" &&
            incoming.attempt <= current.attempt
        ) {
            return true
        }
        if (
            current.state in terminalStates &&
            incoming.state !in terminalStates &&
            incoming.attempt <= current.attempt
        ) {
            return true
        }
        return false
    }

    fun shouldApplyDetail(
        current: InterviewDetailDto?,
        incoming: InterviewDetailDto,
    ): Boolean {
        if (current == null || current.session_id != incoming.session_id) return true
        if (incoming.revision < current.revision) return false
        if (incoming.revision == current.revision) {
            // 报告发布不递增 session revision：已就绪不能被同版本的旧响应改回失败/生成中。
            if (current.report_state == "ready" && incoming.report_state != "ready") {
                return false
            }
            if (
                olderJobSummary(current.follow_up_job, incoming.follow_up_job) ||
                olderJobSummary(current.report_job, incoming.report_job)
            ) {
                return false
            }
        }
        // 相等版本允许刷新 report_state 等不改变 revision 的字段。
        return true
    }

    fun shouldApplyJob(current: JobDto?, incoming: JobDto): Boolean {
        if (current == null) return true
        if (current.id != incoming.id) {
            if (current.target_id == incoming.target_id &&
                current.kind in setOf("extract", "parse") &&
                incoming.kind in setOf("extract", "parse")
            ) {
                if (incoming.input_revision < current.input_revision) return false
                if (incoming.input_revision == current.input_revision &&
                    current.kind == "parse" &&
                    incoming.kind == "extract"
                ) {
                    return false
                }
            }
            if (current.kind == incoming.kind &&
                current.target_id == incoming.target_id &&
                incoming.input_revision < current.input_revision
            ) {
                return false
            }
            return true
        }
        if (incoming.attempt < current.attempt) return false
        if (current.state in terminalStates && incoming.state !in terminalStates) {
            // 只有显式重试（attempt 递增）才能把终态任务重新置为 queued/running。
            return incoming.attempt > current.attempt
        }
        if (
            current.state == "succeeded" &&
            incoming.state != "succeeded" &&
            incoming.attempt <= current.attempt
        ) {
            return false
        }
        return true
    }

    fun interviewJobUpdateAllowed(detail: InterviewDetailDto?, job: JobDto): Boolean {
        if (detail == null) return true
        val summary = when (job.kind) {
            "follow_up" -> detail.follow_up_job
            "interview_report" -> detail.report_job
            else -> null
        } ?: return true
        if (summary.id != job.id) return true
        if (job.attempt < summary.attempt) return false
        if (summary.state == "succeeded" && job.state != "succeeded" && job.attempt <= summary.attempt) {
            return false
        }
        if (summary.state in terminalStates && job.state !in terminalStates && job.attempt <= summary.attempt) {
            return false
        }
        return true
    }
}
