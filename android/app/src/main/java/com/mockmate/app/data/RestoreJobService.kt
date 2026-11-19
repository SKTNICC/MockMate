package com.mockmate.app.data

import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewReportDto
import java.util.concurrent.ConcurrentHashMap

/**
 * 发起恢复查询时捕获的账号／会话／导航／当前对象快照。
 * 网络返回后必须用它和当前页面状态重新比较，不能只比较账号，也不能在等待后用当前页面重建来源。
 */
data class RestoreScope(
    val accountId: String,
    val sessionGeneration: Int,
    val navigationEpoch: Int,
    val screen: String,
    val preparationId: String? = null,
    val materialId: String? = null,
    val interviewSessionId: String? = null,
    /** 发起恢复查询的任务 ID；只用于取任务，不参与当前页面匹配。 */
    val jobId: String = "",
    /** 面试间与报告页共享 job 匹配屏幕名，但写操作迟到时仍需区分具体页面。 */
    val screenKey: String? = null,
    /** 发起材料恢复时捕获的材料版本，避免晚到的错误回退新版本状态。 */
    val materialRevision: Int? = null,
    /** 发起恢复或查询时捕获的任务尝试次序，避免旧 attempt 的错误覆盖新状态。 */
    val jobAttempt: Int? = null,
)

interface RestorePorts {
    suspend fun job(id: String): ApiResult<JobDto>
    suspend fun preparation(id: String): ApiResult<PreparationDto>
    suspend fun plan(id: String): ApiResult<PublicPlanDto>
    suspend fun matchReport(id: String): ApiResult<MatchReportDto>
    suspend fun material(id: String): ApiResult<MaterialDraftDto>
    suspend fun snapshots(materialId: String): ApiResult<List<SnapshotDto>>
    suspend fun interview(sessionId: String): ApiResult<InterviewDetailDto> =
        ApiResult.Err("NOT_IMPLEMENTED", "面试恢复端口未实现", false)
    suspend fun interviewReport(sessionId: String): ApiResult<InterviewReportDto> =
        ApiResult.Err("NOT_IMPLEMENTED", "报告恢复端口未实现", false)
}

sealed interface RestoreOutcome {
    /** 只有原 scope 仍匹配当前页面时才返回；ViewModel 才能投影到 UI。 */
    data class Project(
        val job: JobDto,
        val preparation: PreparationDto? = null,
        val plan: PublicPlanDto? = null,
        val matchReport: MatchReportDto? = null,
        val material: MaterialDraftDto? = null,
        val snapshots: List<SnapshotDto>? = null,
        val interview: InterviewDetailDto? = null,
        val interviewReport: InterviewReportDto? = null,
        val message: String,
    ) : RestoreOutcome

    data class Inform(
        val job: JobDto,
        val message: String,
        val clearActiveJob: Boolean = false,
    ) : RestoreOutcome

    /** 账号／导航／当前对象已变化：结果只进入账号内对象记录，不碰当前页面。 */
    data class Stale(
        val job: JobDto? = null,
        val clearActiveJob: Boolean = false,
    ) : RestoreOutcome

    data class NotFound(val jobId: String) : RestoreOutcome

    data class Failed(val code: String?, val message: String) : RestoreOutcome
}

/** 归并结果保留发起查询时的原 scope；UI 投影必须使用它而不是当前页面。 */
data class RestoreResult(
    val scope: RestoreScope,
    val outcome: RestoreOutcome,
)

/**
 * 恢复结果的唯一归并边界：先保存“该账号该对象的真实状态”，再判断是否投影到当前页面。
 * 成功、运行中、失败、NOT_FOUND、下游 GET 错误、缺引用、revision 变化都走同一判断；
 * 任何返回值都不会把 P1 的来源替换成等待期间打开的 P2。
 */
class RestoreJobService(private val ports: RestorePorts) {

    private val recoveredJobs = ConcurrentHashMap<String, JobDto>()
    private val objectStates = ConcurrentHashMap<String, JobDto>()

    fun recoveredJob(accountId: String, jobId: String): JobDto? = recoveredJobs["$accountId/$jobId"]

    fun recoveredObjectState(accountId: String, kind: String, targetId: String): JobDto? =
        objectStates["$accountId/$kind/$targetId"]

    private fun cache(accountId: String, job: JobDto) {
        recoveredJobs["$accountId/${job.id}"] = job
        objectStates["$accountId/${job.kind}/${job.target_id}"] = job
    }

    private fun sameSession(entry: RestoreScope, current: RestoreScope?): Boolean =
        current != null &&
            current.accountId == entry.accountId &&
            current.sessionGeneration == entry.sessionGeneration &&
            current.navigationEpoch == entry.navigationEpoch

    private fun expectedPreparation(entry: RestoreScope, job: JobDto): String =
        entry.preparationId ?: job.target_id

    private fun targetMatches(entry: RestoreScope, job: JobDto, current: RestoreScope): Boolean =
        when (job.kind) {
            "plan" -> {
                val preparationId = expectedPreparation(entry, job)
                job.target_id == preparationId &&
                    current.screen == SCREEN_PLAN &&
                    current.preparationId == preparationId
            }
            "match" -> {
                val preparationId = expectedPreparation(entry, job)
                job.target_id == preparationId &&
                    current.screen == SCREEN_MATCH &&
                    current.preparationId == preparationId
            }
            "follow_up", "interview_report" -> current.screen == SCREEN_INTERVIEW &&
                current.interviewSessionId == job.target_id
            else -> current.screen == SCREEN_REVIEW &&
                current.materialId == job.target_id &&
                (entry.materialId == null || entry.materialId == job.target_id)
        }

    /** 已拿到 JobDto 时判断原来源是否仍是当前页面；kind/target 关系也要一致。 */
    fun isCurrentScope(entry: RestoreScope, job: JobDto, current: RestoreScope?): Boolean =
        sameSession(entry, current) && current != null && targetMatches(entry, job, current)

    /**
     * 任务本身读取失败时还没有 JobDto：只比较页面、账号与已捕获的对象 ID。
     * 页面还没加载对象（entry 为 null）时不把未知目标当匹配。
     */
    private fun pageMatches(entry: RestoreScope, current: RestoreScope?): Boolean {
        if (!sameSession(entry, current) || current == null) return false
        if (current.screen != entry.screen) return false
        if (entry.preparationId != null && current.preparationId != entry.preparationId) return false
        if (entry.materialId != null && current.materialId != entry.materialId) return false
        if (entry.interviewSessionId != null && current.interviewSessionId != entry.interviewSessionId) return false
        return true
    }

    private fun staleOrFailed(
        entry: RestoreScope,
        job: JobDto?,
        code: String,
        message: String,
        current: () -> RestoreScope?,
    ): RestoreResult {
        val now = current()
        return if (job != null && isCurrentScope(entry, job, now)) {
            RestoreResult(entry, RestoreOutcome.Failed(code, message))
        } else if (job == null && pageMatches(entry, now)) {
            RestoreResult(entry, RestoreOutcome.Failed(code, message))
        } else {
            RestoreResult(entry, RestoreOutcome.Stale(job))
        }
    }

    suspend fun restore(
        entry: RestoreScope,
        current: () -> RestoreScope?,
    ): RestoreResult {
        val job = when (val result = ports.job(entry.jobId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> {
                if (result.code == "NOT_FOUND") {
                    return RestoreResult(entry, RestoreOutcome.NotFound(entry.jobId))
                }
                return if (pageMatches(entry, current())) {
                    RestoreResult(entry, RestoreOutcome.Failed(result.code, result.message))
                } else {
                    RestoreResult(entry, RestoreOutcome.Stale())
                }
            }
        }
        cache(entry.accountId, job)
        return project(entry, job, current)
    }

    /** 已经拿到 JobDto 的恢复（例如 POST 回执或前台重查）不再重复 GET 任务。 */
    suspend fun project(
        entry: RestoreScope,
        job: JobDto,
        current: () -> RestoreScope?,
    ): RestoreResult {
        cache(entry.accountId, job)
        if (!isCurrentScope(entry, job, current())) {
            return RestoreResult(entry, RestoreOutcome.Stale(job))
        }
        return when (job.state) {
            "superseded" -> RestoreResult(
                entry,
                RestoreOutcome.Inform(
                    job = job,
                    message = "旧任务已失效，不会挂到新准备上",
                    clearActiveJob = true,
                ),
            )
            "failed", "interrupted" -> RestoreResult(
                entry,
                RestoreOutcome.Inform(
                    job = job,
                    message = job.error?.message ?: "任务未完成，可查询或重试",
                ),
            )
            "succeeded" -> when (job.kind) {
                "plan" -> projectPlan(entry, job, current)
                "match" -> projectMatch(entry, job, current)
                "follow_up", "interview_report" -> projectInterview(entry, job, current)
                else -> projectMaterial(entry, job, current)
            }
            else -> RestoreResult(
                entry,
                RestoreOutcome.Inform(
                    job = job,
                    message = "有任务仍在执行；本页只查询状态，不会重复提交",
                ),
            )
        }
    }

    private suspend fun projectPlan(
        entry: RestoreScope,
        job: JobDto,
        current: () -> RestoreScope?,
    ): RestoreResult {
        val preparationId = expectedPreparation(entry, job)
        val prepared = when (val result = ports.preparation(preparationId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (prepared.id != preparationId) {
            return staleOrFailed(entry, job, "PLAN_MISMATCH", "准备与当前页面不匹配", current)
        }
        if (prepared.revision != job.input_revision) {
            val now = current()
            return if (isCurrentScope(entry, job, now)) {
                RestoreResult(
                    entry,
                    RestoreOutcome.Inform(
                        job = job,
                        message = "旧生成任务对应的是准备 revision ${job.input_revision}，当前已变化；不会挂到新准备",
                        clearActiveJob = true,
                    ),
                )
            } else {
                // 记录清理可针对原账号与任务，但不得投影到当前页面。
                RestoreResult(entry, RestoreOutcome.Stale(job, clearActiveJob = true))
            }
        }
        val planId = job.result_ref?.id
            ?: return staleOrFailed(entry, job, "PLAN_MISSING", "生成任务成功但缺少计划引用", current)
        val plan = when (val result = ports.plan(planId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (plan.id != planId || plan.preparation_id != preparationId) {
            return staleOrFailed(entry, job, "PLAN_MISMATCH", "计划与准备或任务引用不一致", current)
        }
        if (!isCurrentScope(entry, job, current())) {
            return RestoreResult(entry, RestoreOutcome.Stale(job))
        }
        return RestoreResult(
            entry,
            RestoreOutcome.Project(
                job = job,
                preparation = prepared,
                plan = plan,
                message = "已从服务端恢复计划",
            ),
        )
    }

    private suspend fun projectMatch(
        entry: RestoreScope,
        job: JobDto,
        current: () -> RestoreScope?,
    ): RestoreResult {
        val preparationId = expectedPreparation(entry, job)
        val prepared = when (val result = ports.preparation(preparationId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (prepared.id != preparationId) {
            return staleOrFailed(entry, job, "MATCH_MISMATCH", "准备与当前页面不匹配", current)
        }
        if (prepared.revision != job.input_revision) {
            val now = current()
            return if (isCurrentScope(entry, job, now)) {
                RestoreResult(
                    entry,
                    RestoreOutcome.Inform(
                        job = job,
                        message = "旧匹配任务对应的是准备 revision ${job.input_revision}，当前已变化；不会挂到新准备",
                        clearActiveJob = true,
                    ),
                )
            } else {
                RestoreResult(entry, RestoreOutcome.Stale(job, clearActiveJob = true))
            }
        }
        val reportId = job.result_ref?.id
            ?: return staleOrFailed(entry, job, "MATCH_MISSING", "匹配任务成功但缺少报告引用", current)
        val report = when (val result = ports.matchReport(reportId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (report.id != reportId || report.preparation_id != preparationId) {
            return staleOrFailed(entry, job, "MATCH_MISMATCH", "匹配报告与准备或任务引用不一致", current)
        }
        if (!isCurrentScope(entry, job, current())) {
            return RestoreResult(entry, RestoreOutcome.Stale(job))
        }
        return RestoreResult(
            entry,
            RestoreOutcome.Project(
                job = job,
                preparation = prepared,
                matchReport = report,
                message = "已从服务端恢复匹配结果",
            ),
        )
    }

    private suspend fun projectInterview(
        entry: RestoreScope,
        job: JobDto,
        current: () -> RestoreScope?,
    ): RestoreResult {
        val sessionId = job.target_id
        val detail = when (val result = ports.interview(sessionId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (detail.session_id != sessionId) {
            return staleOrFailed(entry, job, "INTERVIEW_MISMATCH", "面试与任务引用不一致", current)
        }
        var report: InterviewReportDto? = null
        if (job.kind == "interview_report" && detail.report_state == "ready") {
            when (val result = ports.interviewReport(sessionId)) {
                is ApiResult.Ok -> report = result.value
                is ApiResult.Err -> {
                    val code = result.code.orEmpty()
                    if (code !in setOf(
                            "REPORT_PENDING",
                            "REPORT_FAILED",
                            "REPORT_NOT_AVAILABLE",
                        )
                    ) {
                        return staleOrFailed(
                            entry, job, result.code.orEmpty(), result.message, current
                        )
                    }
                }
            }
        }
        if (!isCurrentScope(entry, job, current())) {
            return RestoreResult(entry, RestoreOutcome.Stale(job))
        }
        return RestoreResult(
            entry,
            RestoreOutcome.Project(
                job = job,
                interview = detail,
                interviewReport = report,
                message = if (job.kind == "interview_report") "报告已生成" else "已恢复面试状态",
            ),
        )
    }

    private suspend fun projectMaterial(
        entry: RestoreScope,
        job: JobDto,
        current: () -> RestoreScope?,
    ): RestoreResult {        val materialId = job.target_id
        val material = when (val result = ports.material(materialId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (material.id != materialId) {
            return staleOrFailed(entry, job, "MATERIAL_MISMATCH", "材料与任务引用不一致", current)
        }
        val snapshots = when (val result = ports.snapshots(materialId)) {
            is ApiResult.Ok -> result.value
            is ApiResult.Err -> return staleOrFailed(entry, job, result.code.orEmpty(), result.message, current)
        }
        if (snapshots.any { it.material_id != materialId }) {
            return staleOrFailed(entry, job, "MATERIAL_MISMATCH", "快照与材料不一致", current)
        }
        if (!isCurrentScope(entry, job, current())) {
            return RestoreResult(entry, RestoreOutcome.Stale(job))
        }
        return RestoreResult(
            entry,
            RestoreOutcome.Project(
                job = job,
                material = material,
                snapshots = snapshots,
                message = "已从服务端恢复材料处理结果",
            ),
        )
    }

    companion object {
        const val SCREEN_PREPARATION_LIST = "preparation_list"
        const val SCREEN_SETUP = "setup"
        const val SCREEN_MATCH = "match"
        const val SCREEN_PLAN = "plan"
        const val SCREEN_REVIEW = "review"
        const val SCREEN_LIBRARY = "library"
        const val SCREEN_EDITOR = "editor"
        const val SCREEN_TEMPLATES = "templates"
        const val SCREEN_INTERVIEW = "interview"
    }
}
