package com.mockmate.app.core.operation

import com.mockmate.app.core.network.JobDto
import com.mockmate.app.data.ApiResult
import kotlinx.coroutines.CancellationException

/**
 * 业务写请求的统一入口：先持久化类型化操作，再发请求；确定回执才清理。
 * ViewModel 的 performWrite 直接委托到这里，测试覆盖的就是生产调用路径。
 */
class WritePipeline(private val coordinator: OperationCoordinator) {

    suspend fun <T> perform(
        accountId: String,
        type: String,
        payload: OperationPayload,
        targetId: String? = null,
        revision: Int? = null,
        preparationId: String? = null,
        fileName: String? = null,
        fileSha256: String? = null,
        fileMime: String? = null,
        isContextCurrent: () -> Boolean = { true },
        onJob: suspend (JobDto) -> OperationSave = { OperationSave.Ok },
        call: suspend (String) -> ApiResult<T>,
    ): ApiResult<T> {
        if (!isContextCurrent()) {
            return ApiResult.Err("STALE_CONTEXT", "账号或会话已切换，已丢弃旧操作", false)
        }
        val prepared = when (
            val result = coordinator.prepare(
                accountId = accountId,
                type = type,
                targetId = targetId,
                revision = revision,
                payload = payload,
                preparationId = preparationId,
                fileName = fileName,
                fileSha256 = fileSha256,
                fileMime = fileMime,
            )
        ) {
            is PrepareResult.Prepared -> result.operation
            is PrepareResult.Rejected -> {
                return ApiResult.Err(result.code, result.message, false)
            }
        }
        val result = try {
            call(prepared.operationKey)
        } catch (e: CancellationException) {
            // 取消不清理 pending：请求可能已到达服务端，下次用同一 key 恢复。
            throw e
        }
        if (!isContextCurrent()) {
            return ApiResult.Err("STALE_CONTEXT", "账号或会话已切换，已丢弃旧操作", false)
        }
        when (result) {
            is ApiResult.Ok -> {
                val value = result.value
                if (value is JobDto) {
                    // 先持久化活跃 job，再清除 POST 的 pending，缩小进程被杀时的回执窗口。
                    val saved = onJob(value)
                    if (saved is OperationSave.Failed) {
                        // 保留 pending；下次恢复仍可凭原 key 读回同一 job。
                        return result
                    }
                }
                coordinator.complete(accountId, prepared.operationKey)
            }
            is ApiResult.Err -> {
                if (!OperationCoordinator.isUncertain(result.code, result.httpStatus, result.requestSent)) {
                    coordinator.complete(accountId, prepared.operationKey)
                }
            }
        }
        return result
    }
}
