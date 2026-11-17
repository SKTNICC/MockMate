package com.mockmate.app.data

import com.mockmate.app.core.operation.ConfirmMaterialPayload
import com.mockmate.app.core.operation.ConfirmPlanPayload
import com.mockmate.app.core.operation.CreatePreparationPayload
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.DeleteMaterialPayload
import com.mockmate.app.core.operation.DeletePreparationPayload
import com.mockmate.app.core.operation.EditMaterialPayload
import com.mockmate.app.core.operation.EditPreparationPayload
import com.mockmate.app.core.operation.ExtractMaterialPayload
import com.mockmate.app.core.operation.GeneratePlanPayload
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.InterviewBasicReportPayload
import com.mockmate.app.core.operation.InterviewContinuePayload
import com.mockmate.app.core.operation.InterviewFinishPayload
import com.mockmate.app.core.operation.InterviewPausePayload
import com.mockmate.app.core.operation.InterviewResumePayload
import com.mockmate.app.core.operation.InterviewSkipPayload
import com.mockmate.app.core.operation.MatchPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationPayload
import com.mockmate.app.core.operation.OperationReplayExecutor
import com.mockmate.app.core.operation.ParseMaterialPayload
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.operation.ReplayExecution
import com.mockmate.app.core.operation.RetryJobPayload
import com.mockmate.app.core.operation.StartInterviewPayload
import com.mockmate.app.core.operation.UploadFilePayload
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject

/**
 * 用保存的 PendingOperation 重放真实请求；key 直接取 operationKey，不重新计算。
 * 旧 v1 记录用原有 \u001f 正文解析一次，只作为兼容路径；无法可靠解析时保留并提示人工核对。
 */
class RepositoryOperationReplayExecutor(
    private val repository: M1Repository,
) : OperationReplayExecutor {

    private val profileJson = Json { ignoreUnknownKeys = true }

    override suspend fun execute(op: PendingOperation): ReplayExecution {
        val payload = op.payload
        if (payload != null) return executeTyped(op, payload)
        return executeLegacy(op)
    }

    private fun <T> classify(result: ApiResult<T>): ReplayExecution = when (result) {
        is ApiResult.Ok -> ReplayExecution.Ok(result.value)
        is ApiResult.Err -> if (
            OperationCoordinator.isUncertain(result.code, result.httpStatus, result.requestSent)
        ) {
            ReplayExecution.Uncertain(result)
        } else {
            ReplayExecution.DefiniteError(result)
        }
    }

    private fun needsUser(reason: String): ReplayExecution =
        ReplayExecution.NeedsUser("有未确认的恢复操作需要人工核对：$reason")

    private suspend fun executeTyped(op: PendingOperation, payload: OperationPayload): ReplayExecution {
        val target = op.targetId
        return when (payload) {
            is CreateTextMaterialPayload -> classify(
                repository.createTextMaterial(
                    payload.kind,
                    payload.title,
                    payload.text,
                    payload.templateId,
                    payload.templateVersion,
                    op.operationKey,
                )
            )
            is EditMaterialPayload -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(
                    repository.editMaterial(
                        target,
                        payload.revision,
                        payload.title,
                        payload.text,
                        op.operationKey,
                    )
                )
            }
            is CreatePreparationPayload -> classify(
                repository.createPreparation(
                    payload.roleId,
                    payload.level,
                    payload.count,
                    payload.resumeSnapshotId,
                    payload.jdSnapshotId,
                    op.operationKey,
                )
            )
            is EditPreparationPayload -> {
                if (target == null) return needsUser("缺少准备 ID")
                classify(
                    repository.editPreparation(
                        target,
                        payload.revision,
                        payload.roleId,
                        payload.level,
                        payload.count,
                        payload.resumeSnapshotId,
                        payload.jdSnapshotId,
                        op.operationKey,
                    )
                )
            }
            DeleteMaterialPayload -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(repository.deleteMaterial(target, op.operationKey))
            }
            DeletePreparationPayload -> {
                if (target == null) return needsUser("缺少准备 ID")
                classify(repository.deletePreparation(target, op.operationKey))
            }
            is ExtractMaterialPayload -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(repository.extractMaterial(target, payload.revision, op.operationKey))
            }
            is ParseMaterialPayload -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(
                    repository.parseMaterial(
                        target,
                        payload.revision,
                        payload.roleId,
                        payload.level,
                        op.operationKey,
                    )
                )
            }
            is ConfirmMaterialPayload -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(
                    repository.confirmMaterial(
                        target,
                        payload.revision,
                        payload.profile,
                        op.operationKey,
                    )
                )
            }
            is MatchPayload -> {
                if (target == null) return needsUser("缺少准备 ID")
                classify(repository.match(target, payload.revision, op.operationKey))
            }
            is GeneratePlanPayload -> {
                if (target == null) return needsUser("缺少准备 ID")
                classify(repository.generatePlan(target, payload.revision, op.operationKey))
            }
            is ConfirmPlanPayload -> {
                if (target == null) return needsUser("缺少计划 ID")
                classify(repository.confirmPlan(target, payload.revision, op.operationKey))
            }
            is RetryJobPayload -> {
                if (target == null) return needsUser("缺少任务 ID")
                classify(repository.retryJob(target, payload.expectedAttempt, op.operationKey))
            }
            is UploadFilePayload -> needsUser("文件上传需要重新选择同一文件（摘要 ${payload.fileSha256.take(10)}…）")
            is StartInterviewPayload -> {
                if (target == null) return needsUser("缺少计划 ID")
                classify(repository.startInterview(target, payload.planRevision, op.operationKey))
            }
            is InterviewAnswerPayload -> classify(
                repository.submitInterviewAnswer(
                    payload.sessionId,
                    payload.revision,
                    payload.turnId,
                    payload.text,
                    op.operationKey,
                )
            )
            is InterviewSkipPayload -> classify(
                repository.skipInterviewTurn(
                    payload.sessionId,
                    payload.revision,
                    payload.turnId,
                    op.operationKey,
                )
            )
            is InterviewPausePayload -> classify(
                repository.pauseInterview(
                    payload.sessionId, payload.revision, op.operationKey
                )
            )
            is InterviewResumePayload -> classify(
                repository.resumeInterview(
                    payload.sessionId, payload.revision, op.operationKey
                )
            )
            is InterviewContinuePayload -> classify(
                repository.continueWithoutFollowUp(
                    payload.sessionId,
                    payload.revision,
                    payload.jobId,
                    op.operationKey,
                )
            )
            is InterviewFinishPayload -> classify(
                repository.finishInterview(
                    payload.sessionId, payload.revision, op.operationKey
                )
            )
            is InterviewBasicReportPayload -> classify(
                repository.createBasicInterviewReport(
                    payload.sessionId,
                    payload.revision,
                    payload.jobId,
                    op.operationKey,
                )
            )
        }
    }

    private suspend fun executeLegacy(op: PendingOperation): ReplayExecution {
        val parts = op.bodyJson?.split("\u001f") ?: emptyList()
        val target = op.targetId
        return when (op.type) {
            "createTextMaterial" -> {
                if (parts.size < 3) return needsUser("旧文本材料记录不完整")
                classify(
                    repository.createTextMaterial(
                        parts[0],
                        parts[1],
                        parts[2],
                        parts.getOrNull(3)?.takeIf { it.isNotBlank() },
                        parts.getOrNull(4)?.toIntOrNull(),
                        op.operationKey,
                    )
                )
            }
            "editMaterial" -> {
                if (parts.size < 3 || target == null || op.revision == null) {
                    return needsUser("旧材料修改记录不完整")
                }
                classify(
                    repository.editMaterial(
                        target,
                        op.revision,
                        parts[1],
                        parts[2],
                        op.operationKey,
                    )
                )
            }
            "createPreparation" -> {
                if (parts.size < 3) return needsUser("旧准备创建记录不完整")
                classify(
                    repository.createPreparation(
                        parts[0],
                        parts[1],
                        parts[2].toIntOrNull() ?: 0,
                        parts.getOrNull(3)?.takeIf { it.isNotBlank() },
                        parts.getOrNull(4)?.takeIf { it.isNotBlank() },
                        op.operationKey,
                    )
                )
            }
            "editPreparation" -> {
                if (parts.size < 5 || target == null || op.revision == null) {
                    return needsUser("旧准备修改记录不完整")
                }
                classify(
                    repository.editPreparation(
                        target,
                        op.revision,
                        parts[0],
                        parts[1],
                        parts[2].toIntOrNull() ?: 0,
                        parts[3].takeIf { it.isNotBlank() },
                        parts[4].takeIf { it.isNotBlank() },
                        op.operationKey,
                    )
                )
            }
            "confirmMaterial" -> {
                if (parts.size < 2 || target == null || op.revision == null) {
                    return needsUser("旧材料确认记录不完整")
                }
                val profile = runCatching {
                    profileJson.parseToJsonElement(parts[1]).jsonObject
                }.getOrNull() ?: return needsUser("旧材料确认的结构无法解析")
                classify(
                    repository.confirmMaterial(target, op.revision, profile, op.operationKey)
                )
            }
            "deleteMaterial" -> {
                if (target == null) return needsUser("缺少材料 ID")
                classify(repository.deleteMaterial(target, op.operationKey))
            }
            "deletePreparation" -> {
                if (target == null) return needsUser("缺少准备 ID")
                classify(repository.deletePreparation(target, op.operationKey))
            }
            "extractMaterial" -> {
                if (target == null || op.revision == null) return needsUser("旧提取记录不完整")
                classify(repository.extractMaterial(target, op.revision, op.operationKey))
            }
            "parseMaterial" -> {
                if (parts.size < 3 || target == null || op.revision == null) {
                    return needsUser("旧解析记录不完整")
                }
                classify(
                    repository.parseMaterial(
                        target,
                        op.revision,
                        parts[1],
                        parts[2],
                        op.operationKey,
                    )
                )
            }
            "match" -> {
                if (target == null || op.revision == null) return needsUser("旧匹配记录不完整")
                classify(repository.match(target, op.revision, op.operationKey))
            }
            "generatePlan" -> {
                if (target == null || op.revision == null) return needsUser("旧生成记录不完整")
                classify(repository.generatePlan(target, op.revision, op.operationKey))
            }
            "confirmPlan" -> {
                if (target == null || op.revision == null) return needsUser("旧计划确认记录不完整")
                classify(repository.confirmPlan(target, op.revision, op.operationKey))
            }
            "retryJob" -> {
                if (target == null || op.revision == null) return needsUser("旧重试记录不完整")
                classify(repository.retryJob(target, op.revision, op.operationKey))
            }
            "uploadFile" -> needsUser("文件上传需要重新选择同一文件")
            else -> needsUser("未知的旧操作类型 ${op.type}")
        }
    }
}
