package com.mockmate.app.data

import com.mockmate.app.core.network.JobDto

/** UI 归并后的唯一效果；只有 Project/Inform/Error 可以触碰当前页面。 */
sealed interface RestoreUiEffect {
    data class Project(val result: RestoreResult) : RestoreUiEffect
    data class Inform(val result: RestoreResult) : RestoreUiEffect
    data class Error(val code: String?, val message: String, val scope: RestoreScope) : RestoreUiEffect
    data object Ignore : RestoreUiEffect
}

/**
 * 最终 UI 闸门：在原 scope 仍匹配当前页面时才放行；清理只能针对原账号与原任务。
 * 先清理记录（可能挂起），再做“检查→更新 UI”的最后一次无挂起判断。
 */
class RestoreUiGate(
    private val currentScope: () -> RestoreScope?,
    private val clearActiveJob: suspend (accountId: String, jobId: String) -> Unit,
) {
    fun isCurrent(scope: RestoreScope): Boolean {
        val current = currentScope() ?: return false
        return current.accountId == scope.accountId &&
            current.sessionGeneration == scope.sessionGeneration &&
            current.navigationEpoch == scope.navigationEpoch &&
            current.screen == scope.screen &&
            current.preparationId == scope.preparationId &&
            current.materialId == scope.materialId
    }

    suspend fun gate(result: RestoreResult): RestoreUiEffect {
        val entry = result.scope
        return when (val outcome = result.outcome) {
            is RestoreOutcome.Project -> {
                // 已成功读回真实结果：可清理原账号的原任务记录，即使页面已切走。
                clearActiveJob(entry.accountId, outcome.job.id)
                if (isCurrent(entry)) RestoreUiEffect.Project(result) else RestoreUiEffect.Ignore
            }
            is RestoreOutcome.Inform -> {
                if (outcome.clearActiveJob) {
                    clearActiveJob(entry.accountId, outcome.job.id)
                }
                if (isCurrent(entry)) RestoreUiEffect.Inform(result) else RestoreUiEffect.Ignore
            }
            is RestoreOutcome.Stale -> {
                if (outcome.clearActiveJob) {
                    outcome.job?.let { clearActiveJob(entry.accountId, it.id) }
                }
                RestoreUiEffect.Ignore
            }
            is RestoreOutcome.NotFound -> {
                clearActiveJob(entry.accountId, outcome.jobId)
                RestoreUiEffect.Ignore
            }
            is RestoreOutcome.Failed -> {
                if (isCurrent(entry)) {
                    RestoreUiEffect.Error(outcome.code, outcome.message, entry)
                } else {
                    RestoreUiEffect.Ignore
                }
            }
        }
    }
}

/**
 * 恢复调用链的窄控制器：ViewModel 的自动恢复、主动查询和终态投影都经过它。
 * 每次查询都显式携带发起时的 scope，等待结束后不会从当前页面重新构造来源。
 */
class RestoreCoordinator(
    private val service: RestoreJobService,
    private val gate: RestoreUiGate,
    private val fetchJob: suspend (String) -> ApiResult<JobDto>,
    private val currentScope: () -> RestoreScope?,
) {
    fun isCurrent(scope: RestoreScope): Boolean = gate.isCurrent(scope)

    suspend fun restore(entry: RestoreScope): RestoreUiEffect =
        gate.gate(service.restore(entry, currentScope))

    suspend fun project(entry: RestoreScope, job: JobDto): RestoreUiEffect =
        gate.gate(service.project(entry, job, currentScope))

    /** 主动查询也先捕获 scope，再 GET；等待期间切页不会换绑到新准备。 */
    suspend fun query(entry: RestoreScope): RestoreUiEffect =
        when (val result = fetchJob(entry.jobId)) {
            is ApiResult.Ok -> project(entry, result.value)
            is ApiResult.Err -> {
                val outcome = if (result.code == "NOT_FOUND") {
                    RestoreOutcome.NotFound(entry.jobId)
                } else {
                    RestoreOutcome.Failed(result.code, result.message)
                }
                gate.gate(RestoreResult(entry, outcome))
            }
        }
}
