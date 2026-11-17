package com.mockmate.app.core.operation

import android.content.Context
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.session.AesGcmPrefs
import com.mockmate.app.core.session.SecureRead
import com.mockmate.app.core.session.SecureWrite
import com.mockmate.app.data.ApiResult
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject

/**
 * 写操作的类型化正文。恢复时直接复用保存的 payload，不靠分隔符拼接/拆解用户正文。
 * 字段只包含重放所需的最小内容；文件字节不落盘，只保存摘要与元数据。
 */
@Serializable
sealed interface OperationPayload

@Serializable
@SerialName("createTextMaterial")
data class CreateTextMaterialPayload(
    val kind: String,
    val title: String,
    val text: String,
    val templateId: String? = null,
    val templateVersion: Int? = null,
) : OperationPayload

@Serializable
@SerialName("editMaterial")
data class EditMaterialPayload(
    val revision: Int,
    val title: String,
    val text: String,
) : OperationPayload

@Serializable
@SerialName("createPreparation")
data class CreatePreparationPayload(
    val roleId: String,
    val level: String,
    val count: Int,
    val resumeSnapshotId: String? = null,
    val jdSnapshotId: String? = null,
) : OperationPayload

@Serializable
@SerialName("editPreparation")
data class EditPreparationPayload(
    val revision: Int,
    val roleId: String,
    val level: String,
    val count: Int,
    val resumeSnapshotId: String? = null,
    val jdSnapshotId: String? = null,
) : OperationPayload

@Serializable
@SerialName("deleteMaterial")
data object DeleteMaterialPayload : OperationPayload

@Serializable
@SerialName("deletePreparation")
data object DeletePreparationPayload : OperationPayload

@Serializable
@SerialName("extractMaterial")
data class ExtractMaterialPayload(val revision: Int) : OperationPayload

@Serializable
@SerialName("parseMaterial")
data class ParseMaterialPayload(
    val revision: Int,
    val roleId: String,
    val level: String,
) : OperationPayload

@Serializable
@SerialName("confirmMaterial")
data class ConfirmMaterialPayload(
    val revision: Int,
    val profile: JsonObject,
) : OperationPayload

@Serializable
@SerialName("match")
data class MatchPayload(val revision: Int) : OperationPayload

@Serializable
@SerialName("generatePlan")
data class GeneratePlanPayload(val revision: Int) : OperationPayload

@Serializable
@SerialName("confirmPlan")
data class ConfirmPlanPayload(val revision: Int) : OperationPayload

@Serializable
@SerialName("retryJob")
data class RetryJobPayload(val expectedAttempt: Int) : OperationPayload

@Serializable
@SerialName("uploadFile")
data class UploadFilePayload(
    val kind: String,
    val title: String,
    val fileName: String,
    val mimeType: String,
    val fileSha256: String,
) : OperationPayload

@Serializable
data class PendingOperation(
    val operationKey: String,
    val type: String,
    val targetId: String? = null,
    val revision: Int? = null,
    val bodyHash: String,
    /** v1 兼容字段：旧记录的 \u001f 拼接正文；新记录为 null。 */
    val bodyJson: String? = null,
    /** v2：类型化 payload；旧记录为 null。 */
    val payload: OperationPayload? = null,
    val preparationId: String? = null,
    val fileName: String? = null,
    val fileSha256: String? = null,
    val fileMime: String? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

@Serializable
data class ActiveJob(
    val jobId: String,
    val kind: String,
    val targetId: String,
    val preparationId: String? = null,
    val revision: Int? = null,
    val createdAt: Long = System.currentTimeMillis(),
)

@Serializable
data class AccountOperations(
    val pending: List<PendingOperation> = emptyList(),
    val jobs: List<ActiveJob> = emptyList(),
)

sealed interface OperationLoad {
    data object Empty : OperationLoad
    data class Loaded(val operations: AccountOperations) : OperationLoad
    data class Corrupt(val message: String) : OperationLoad
    data class Failed(val message: String) : OperationLoad
}

sealed interface OperationSave {
    data object Ok : OperationSave
    data class Failed(val message: String) : OperationSave
}

interface OperationStore {
    suspend fun load(accountId: String): OperationLoad
    suspend fun save(accountId: String, operations: AccountOperations): OperationSave
}

class InMemoryOperationStore : OperationStore {
    private val values = mutableMapOf<String, AccountOperations>()
    var failWrites: Boolean = false
    var failReads: Boolean = false
    var corrupt: Boolean = false

    override suspend fun load(accountId: String): OperationLoad {
        if (failReads) return OperationLoad.Failed("injected read failure")
        if (corrupt) return OperationLoad.Corrupt("injected corrupt record")
        return values[accountId]?.let { OperationLoad.Loaded(it) } ?: OperationLoad.Empty
    }

    override suspend fun save(accountId: String, operations: AccountOperations): OperationSave {
        if (failWrites) return OperationSave.Failed("injected write failure")
        values[accountId] = operations
        return OperationSave.Ok
    }

    fun snapshot(accountId: String): AccountOperations = values[accountId] ?: AccountOperations()
}

/**
 * 按账号加密持久化待确认操作与活跃 job；不保存密码、token 或 AI Key。
 * 保存失败必须可见：调用方在发请求前检查返回值。
 */
class SecureOperationStore(context: Context) : OperationStore {
    private val prefs = AesGcmPrefs(
        context,
        preferencesName = "mockmate_operations",
        keyAlias = "mockmate_operations_key",
    )
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    private fun key(accountId: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest(accountId.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
            .take(24)
        return "account_$digest"
    }

    override suspend fun load(accountId: String): OperationLoad = withContext(Dispatchers.IO) {
        when (val read = prefs.read(key(accountId))) {
            SecureRead.Absent -> OperationLoad.Empty
            is SecureRead.Corrupt -> OperationLoad.Corrupt(read.message)
            is SecureRead.Failed -> OperationLoad.Failed(read.message)
            is SecureRead.Present -> {
                try {
                    OperationLoad.Loaded(json.decodeFromString<AccountOperations>(read.value))
                } catch (e: Exception) {
                    // 反序列化失败不等于首次无记录；保留原始密文，禁止静默清空。
                    OperationLoad.Corrupt("本地操作记录无法解析（${e.javaClass.simpleName}），已保留原始数据")
                }
            }
        }
    }

    override suspend fun save(accountId: String, operations: AccountOperations): OperationSave =
        withContext(Dispatchers.IO) {
            val encoded = try {
                json.encodeToString(AccountOperations.serializer(), operations)
            } catch (e: Exception) {
                return@withContext OperationSave.Failed("本地操作记录序列化失败：${e.javaClass.simpleName}")
            }
            when (val write = prefs.write(key(accountId), encoded)) {
                SecureWrite.Ok -> OperationSave.Ok
                is SecureWrite.Failed -> OperationSave.Failed(write.message)
            }
        }
}

sealed interface PrepareResult {
    data class Prepared(val operation: PendingOperation) : PrepareResult
    data class Rejected(val code: String, val message: String) : PrepareResult
}

/**
 * 请求记录必须在发请求前落盘；同一逻辑操作复用原 key 与原 payload。
 * 恢复接口直接使用 PendingOperation.operationKey，不重新计算 hash 猜旧记录。
 *
 * 同一账号的全部读改写经过共享的原子更新边界（进程内 per-account Mutex），
 * 覆盖完整“load → 校验 → 修改 → save”，而不是只锁单次 load 或 save。
 * 锁只覆盖本地存储事务，不包含 HTTP 调用、轮询或用户交互。
 */
class OperationCoordinator(
    private val store: OperationStore,
    private val keyFactory: () -> String = { UUID.randomUUID().toString() },
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
) {
    private val payloadJson = Json { encodeDefaults = true }

    fun bodyHash(body: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(body.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private suspend fun <T> atomic(accountId: String, block: suspend () -> T): T =
        accountLocks.computeIfAbsent(accountId) { Mutex() }.withLock { block() }

    suspend fun prepare(
        accountId: String,
        type: String,
        targetId: String?,
        revision: Int?,
        payload: OperationPayload? = null,
        legacyBody: String? = null,
        preparationId: String? = null,
        fileName: String? = null,
        fileSha256: String? = null,
        fileMime: String? = null,
    ): PrepareResult = withContext(ioDispatcher) {
        atomic(accountId) {
            prepareLocked(
                accountId = accountId,
                type = type,
                targetId = targetId,
                revision = revision,
                payload = payload,
                legacyBody = legacyBody,
                preparationId = preparationId,
                fileName = fileName,
                fileSha256 = fileSha256,
                fileMime = fileMime,
            )
        }
    }

    private suspend fun prepareLocked(
        accountId: String,
        type: String,
        targetId: String?,
        revision: Int?,
        payload: OperationPayload?,
        legacyBody: String?,
        preparationId: String?,
        fileName: String?,
        fileSha256: String?,
        fileMime: String?,
    ): PrepareResult {
        val encodedPayload = payload?.let { payloadJson.encodeToString(OperationPayload.serializer(), it) }
        val hash = if (encodedPayload != null) {
            bodyHash(encodedPayload)
        } else {
            bodyHash(
                listOfNotNull(legacyBody, fileName, fileSha256, fileMime, preparationId)
                    .joinToString("\u001f")
            )
        }
        val operations = when (val loaded = store.load(accountId)) {
            is OperationLoad.Corrupt -> return PrepareResult.Rejected(
                "OPERATION_LOG_CORRUPT",
                loaded.message,
            )
            is OperationLoad.Failed -> return PrepareResult.Rejected(
                "STORAGE_UNAVAILABLE",
                loaded.message,
            )
            OperationLoad.Empty -> AccountOperations()
            is OperationLoad.Loaded -> loaded.operations
        }
        val existing = operations.pending.firstOrNull {
            it.type == type &&
                it.targetId == targetId &&
                it.revision == revision &&
                it.bodyHash == hash &&
                it.fileName == fileName &&
                it.fileSha256 == fileSha256 &&
                it.fileMime == fileMime
        }
        if (existing != null) return PrepareResult.Prepared(existing)

        val pending = PendingOperation(
            operationKey = keyFactory(),
            type = type,
            targetId = targetId,
            revision = revision,
            bodyHash = hash,
            bodyJson = if (payload == null) legacyBody else null,
            payload = payload,
            preparationId = preparationId,
            fileName = fileName,
            fileSha256 = fileSha256,
            fileMime = fileMime,
        )
        return when (val saved = store.save(accountId, operations.copy(pending = operations.pending + pending))) {
            is OperationSave.Failed -> PrepareResult.Rejected("STORAGE_UNAVAILABLE", saved.message)
            OperationSave.Ok -> PrepareResult.Prepared(pending)
        }
    }

    suspend fun pending(accountId: String): OperationLoad = withContext(ioDispatcher) {
        store.load(accountId)
    }

    suspend fun pendingFor(
        accountId: String,
        type: String,
        targetId: String? = null,
    ): PendingOperation? = when (val loaded = pending(accountId)) {
        is OperationLoad.Loaded -> loaded.operations.pending.firstOrNull {
            it.type == type && (targetId == null || it.targetId == targetId)
        }
        else -> null
    }

    suspend fun complete(accountId: String, operationKey: String): OperationSave =
        withContext(ioDispatcher) {
            atomic(accountId) {
                when (val loaded = store.load(accountId)) {
                    is OperationLoad.Corrupt -> OperationSave.Failed(loaded.message)
                    is OperationLoad.Failed -> OperationSave.Failed(loaded.message)
                    OperationLoad.Empty -> OperationSave.Ok
                    is OperationLoad.Loaded -> store.save(
                        accountId,
                        loaded.operations.copy(
                            pending = loaded.operations.pending.filterNot {
                                it.operationKey == operationKey
                            },
                        ),
                    )
                }
            }
        }

    suspend fun recordActiveJob(accountId: String, job: ActiveJob): OperationSave =
        withContext(ioDispatcher) {
            atomic(accountId) {
                when (val loaded = store.load(accountId)) {
                    is OperationLoad.Corrupt -> OperationSave.Failed(loaded.message)
                    is OperationLoad.Failed -> OperationSave.Failed(loaded.message)
                    OperationLoad.Empty -> store.save(accountId, AccountOperations(jobs = listOf(job)))
                    is OperationLoad.Loaded -> store.save(
                        accountId,
                        loaded.operations.copy(
                            jobs = loaded.operations.jobs.filterNot { it.jobId == job.jobId } + job,
                        ),
                    )
                }
            }
        }

    suspend fun activeJobs(accountId: String): OperationLoad = withContext(ioDispatcher) {
        store.load(accountId)
    }

    suspend fun activeJob(
        accountId: String,
        kind: String,
        targetId: String? = null,
    ): ActiveJob? = when (val loaded = activeJobs(accountId)) {
        is OperationLoad.Loaded -> loaded.operations.jobs.lastOrNull {
            it.kind == kind && (targetId == null || it.targetId == targetId)
        }
        else -> null
    }

    suspend fun completeActiveJob(accountId: String, jobId: String): OperationSave =
        withContext(ioDispatcher) {
            atomic(accountId) {
                when (val loaded = store.load(accountId)) {
                    is OperationLoad.Corrupt -> OperationSave.Failed(loaded.message)
                    is OperationLoad.Failed -> OperationSave.Failed(loaded.message)
                    OperationLoad.Empty -> OperationSave.Ok
                    is OperationLoad.Loaded -> store.save(
                        accountId,
                        loaded.operations.copy(
                            jobs = loaded.operations.jobs.filterNot { it.jobId == jobId },
                        ),
                    )
                }
            }
        }

    companion object {
        /**
         * 进程内按账号共享的原子更新边界。多个 Coordinator/Store 实例访问同一账号时
         * 也必须经过同一把锁；它只保护本地日志事务，不覆盖网络等待。
         */
        private val accountLocks = ConcurrentHashMap<String, Mutex>()

        /** 请求可能已在服务端成功但客户端没拿到确定回执。 */
        fun isUncertain(code: String?, httpStatus: Int?, requestSent: Boolean = true): Boolean {
            if (!requestSent) return false
            if (code == "NETWORK_UNAVAILABLE") return true
            if (code == "RECEIPT_UNREADABLE") return true
            if (code == "JOB_POLL_TIMEOUT") return true
            if (code == "JOB_POLL_PAUSED") return false
            val status = httpStatus ?: return false
            return status == 408 || status == 429 || status >= 500
        }

        fun isDefiniteAuthFailure(code: String?): Boolean = code == "UNAUTHENTICATED"
    }
}

sealed interface ReplayExecution {
    data class Ok(val value: Any?) : ReplayExecution
    data class Uncertain(val error: ApiResult.Err) : ReplayExecution
    data class DefiniteError(val error: ApiResult.Err) : ReplayExecution
    data class NeedsUser(val message: String) : ReplayExecution
}

interface OperationReplayExecutor {
    suspend fun execute(op: PendingOperation): ReplayExecution
}

data class ReplayReport(
    val attempted: Int = 0,
    val completed: Int = 0,
    val retained: Int = 0,
    val stopped: Boolean = false,
    val storageError: String? = null,
    val userMessage: String? = null,
)

/**
 * 冷启恢复：逐条用原 PendingOperation 的 key 与 payload 重放。
 * 未知回执保留，确定业务拒绝才清理；存储损坏时停止，不当作首次无记录。
 */
class PendingOperationReplayer(
    private val coordinator: OperationCoordinator,
    private val executor: OperationReplayExecutor,
) {
    suspend fun replay(
        accountId: String,
        isCurrent: () -> Boolean,
    ): ReplayReport {
        val loaded = coordinator.pending(accountId)
        when (loaded) {
            is OperationLoad.Corrupt -> return ReplayReport(storageError = loaded.message, stopped = true)
            is OperationLoad.Failed -> return ReplayReport(storageError = loaded.message, stopped = true)
            OperationLoad.Empty -> return ReplayReport()
            is OperationLoad.Loaded -> Unit
        }
        val operations = (loaded as OperationLoad.Loaded).operations.pending
        var completed = 0
        var retained = 0
        var attempted = 0
        var userMessage: String? = null
        for (op in operations) {
            if (!isCurrent()) {
                return ReplayReport(
                    attempted = attempted,
                    completed = completed,
                    retained = retained + (operations.size - attempted),
                    stopped = true,
                    userMessage = userMessage,
                )
            }
            attempted += 1
            val execution = try {
                executor.execute(op)
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // 执行器自身异常不能把未确定操作当成成功清除。
                ReplayExecution.Uncertain(
                    ApiResult.Err("CLIENT_ERROR", e.message ?: "恢复执行失败", false)
                )
            }
            when (execution) {
                is ReplayExecution.Ok -> {
                    val value = execution.value
                    if (value is JobDto) {
                        val saved = coordinator.recordActiveJob(
                            accountId,
                            ActiveJob(
                                jobId = value.id,
                                kind = value.kind,
                                targetId = value.target_id,
                                preparationId = op.preparationId,
                                revision = value.input_revision,
                            ),
                        )
                        if (saved is OperationSave.Failed) {
                            retained += 1
                            userMessage = "恢复结果已返回，但本地任务记录保存失败：${saved.message}"
                            continue
                        }
                    }
                    when (val cleared = coordinator.complete(accountId, op.operationKey)) {
                        is OperationSave.Failed -> {
                            userMessage = "恢复成功但清理本地记录失败：${cleared.message}"
                            retained += 1
                        }
                        OperationSave.Ok -> completed += 1
                    }
                }
                is ReplayExecution.Uncertain -> retained += 1
                is ReplayExecution.DefiniteError -> {
                    when (val cleared = coordinator.complete(accountId, op.operationKey)) {
                        is OperationSave.Failed -> {
                            userMessage = cleared.message
                            retained += 1
                        }
                        OperationSave.Ok -> completed += 1
                    }
                }
                is ReplayExecution.NeedsUser -> {
                    retained += 1
                    userMessage = execution.message
                }
            }
        }
        return ReplayReport(
            attempted = attempted,
            completed = completed,
            retained = retained,
            stopped = false,
            userMessage = userMessage,
        )
    }
}
