package com.mockmate.app.ui

import com.mockmate.app.core.network.JobDto

data class FriendlyErrorInfo(
    val userTitle: String,
    val userExplanation: String,
    val suggestion: String? = null,
    val code: String? = null,
    val rawServerMessage: String = "",
    val details: List<String> = emptyList(),
    val httpStatus: Int? = null,
    val retryable: Boolean = false,
    val diagnosticContext: String? = null,
) {
    fun formatDiagnosticText(): String = buildString {
        appendLine("=== MockMate 诊断信息 ===")
        appendLine("错误码 (Code): ${code ?: "UNKNOWN"}")
        if (httpStatus != null) appendLine("HTTP 状态: HTTP $httpStatus")
        appendLine("可重试 (Retryable): $retryable")
        if (!diagnosticContext.isNullOrBlank()) {
            appendLine("上下文 (Context): $diagnosticContext")
        }
        appendLine("原始消息 (Server/Raw Message): $rawServerMessage")
        if (details.isNotEmpty()) {
            appendLine("诊断明细 (Details):")
            details.forEachIndexed { idx, detail ->
                appendLine("  [${idx + 1}] $detail")
            }
        }
    }.trimEnd()
}

object ErrorDiagnosticsMapper {

    fun fromMessage(message: UiMessage): FriendlyErrorInfo = resolve(
        code = message.code,
        rawMessage = message.text,
        details = message.details,
        httpStatus = message.httpStatus,
        retryable = message.retryable,
        diagnosticContext = message.diagnosticContext,
    )

    fun fromJob(job: JobDto, extraContext: String? = null): FriendlyErrorInfo {
        val err = job.error
        val ctx = buildString {
            append("jobId=${job.id} · kind=${job.kind} · state=${job.state} · attempt=${job.attempt} · target=${job.target_id} (rev=${job.input_revision})")
            if (!extraContext.isNullOrBlank()) {
                append(" · ")
                append(extraContext)
            }
        }
        return resolve(
            code = err?.code ?: if (job.state == "superseded") "SUPERSEDED" else null,
            rawMessage = err?.message ?: "任务状态：${job.state}",
            details = err?.details ?: emptyList(),
            httpStatus = null,
            retryable = job.retryable,
            diagnosticContext = ctx,
        )
    }

    fun resolve(
        code: String?,
        rawMessage: String,
        details: List<String> = emptyList(),
        httpStatus: Int? = null,
        retryable: Boolean = false,
        diagnosticContext: String? = null,
    ): FriendlyErrorInfo {
        val kvDetails = parseKeyValueDetails(details)
        val slot = kvDetails["slot"]
        val itemId = kvDetails["item_id"]
        val field = kvDetails["field"]
        val section = kvDetails["section"]
        val lastCode = kvDetails["last_code"]

        return when (code) {
            "AI_INVALID_OUTPUT", "EVALUATION_INVALID", "GROUNDING_INVALID", "DUPLICATE_QUESTION", "DEDUP_UNCERTAIN" -> {
                val slotHint = if (slot != null) "（第 $slot 个出题槽位）" else ""
                val subReason = when {
                    rawMessage.contains("无法从该简历文本结构化") ->
                        "AI 未能从当前简历文本中识别出有效的教育、项目或技能段落。"
                    rawMessage.contains("未找到职责或要求") || rawMessage.contains("没有可分析的显式要求") ->
                        "AI 未能从当前 JD 文本中识别出明确的岗位职责或任职要求。"
                    rawMessage.contains("权重之和") || lastCode == "EVALUATION_INVALID" ->
                        "AI 生成的评分维度或要点权重格式未通过服务端校验$slotHint。"
                    rawMessage.contains("重复") || code == "DUPLICATE_QUESTION" || code == "DEDUP_UNCERTAIN" ->
                        "AI 本次生成的题目与已有题库或已曝光题目过于相似$slotHint，已被服务端拦截。"
                    rawMessage.contains("依据卡") || rawMessage.contains("锚点") || code == "GROUNDING_INVALID" ->
                        "AI 生成的题目未能严格关联到您的材料原文或知识依据卡$slotHint。"
                    else -> "外部 AI 模型本次返回的数据结构未通过严格契约校验$slotHint。"
                }
                val suggestion = when {
                    rawMessage.contains("手动填写") -> "建议检查原文是否过于简短，或直接在下方手动添加结构化条目。"
                    retryable -> "您的材料和准备进度完好无损，直接点击「重试」重新调用模型即可。"
                    else -> "建议检查并丰富简历/JD 的结构化内容后再试，或点击重试。"
                }
                FriendlyErrorInfo(
                    userTitle = "AI 生成结果未通过质量校验",
                    userExplanation = subReason,
                    suggestion = suggestion,
                    code = code,
                    rawServerMessage = rawMessage,
                    details = details,
                    httpStatus = httpStatus,
                    retryable = retryable,
                    diagnosticContext = diagnosticContext,
                )
            }

            "AI_TIMEOUT", "JOB_POLL_TIMEOUT" -> FriendlyErrorInfo(
                userTitle = "AI 服务响应超时",
                userExplanation = "外部模型处理耗时较长，当前尚未拿到完整结果。",
                suggestion = "您可以先点击「刷新/查询最新状态」确认服务端是否已完成，或点击「重试」重新发起。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
            )

            "AI_UNAVAILABLE", "AI_RATE_LIMITED", "RATE_LIMITED" -> FriendlyErrorInfo(
                userTitle = "AI 服务繁忙或暂时不可用",
                userExplanation = "当前外部模型接口触发限流或网络波动，请求未能完成。",
                suggestion = "请稍等片刻后点击「重试」，不会产生重复记录。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
            )

            "AI_NOT_CONFIGURED", "AI_AUTH_FAILED", "AI_REQUEST_REJECTED" -> FriendlyErrorInfo(
                userTitle = "后端 AI 模型服务配置异常",
                userExplanation = "服务端未正确配置模型密钥（API Key）或模型拒绝了本次调用。",
                suggestion = "开发者请检查后端 .env 中的模型配置与日志；用户可先使用手动填写完成核对。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = retryable,
                diagnosticContext = diagnosticContext,
            )

            "EVIDENCE_INVALID", "PROFILE_INVALID" -> {
                val targetLabel = formatItemTarget(section, itemId, field)
                val explanation = when {
                    rawMessage.contains("quote") || field?.contains("quote") == true ->
                        "${targetLabel}中的「引用原文」未在上方材料正文中逐字找到。"
                    rawMessage.contains("技能") || field == "skill_codes" || field == "code" ->
                        "${targetLabel}包含了已失效或未启用的技能标签。"
                    else -> "${targetLabel}填写的字段内容不符合结构化核对要求：$rawMessage"
                }
                val suggestion = when {
                    rawMessage.contains("quote") || field?.contains("quote") == true ->
                        "请从上方原文直接复制完全一致的句子填入「引用原文」，或将该条目标记为「我手动确认」。"
                    rawMessage.contains("技能") || field == "skill_codes" || field == "code" ->
                        "请定位到标红条目，移除或替换为当前可用的技能标签后重新保存。"
                    else -> "请检查对应条目的必填项（如名称、描述、引用原文）是否完整。"
                }
                FriendlyErrorInfo(
                    userTitle = "材料核对内容需要调整",
                    userExplanation = explanation,
                    suggestion = suggestion,
                    code = code,
                    rawServerMessage = rawMessage,
                    details = details,
                    httpStatus = httpStatus,
                    retryable = false,
                    diagnosticContext = diagnosticContext,
                )
            }

            "MATERIALS_REQUIRED" -> FriendlyErrorInfo(
                userTitle = "尚未完成简历与 JD 确认",
                userExplanation = "匹配分析与生成计划需要同时绑定「已核对确认的简历」和「已核对确认的岗位 JD」。",
                suggestion = "请先在准备设置页上传或新建简历与 JD，并在核对页点击「确认并保存」后再继续。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "MATCH_REQUIRED" -> FriendlyErrorInfo(
                userTitle = "请先完成匹配度分析",
                userExplanation = "生成面试计划前，需要先对当前版本的简历与 JD 执行一次匹配分析。",
                suggestion = "请先前往「第 2 步：匹配分析」完成分析，再生成 3+3 面试计划。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "BANK_INSUFFICIENT" -> FriendlyErrorInfo(
                userTitle = "关联的题库题目数量不足",
                userExplanation = rawMessage,
                suggestion = "您可以返回核对页为项目/技能关联更多技能标签，或返回设置页将主问题数量调小（如改为 2 题）。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "REVISION_CONFLICT", "JOB_INPUT_CONFLICT", "PLAN_STALE", "SUPERSEDED" -> FriendlyErrorInfo(
                userTitle = "页面数据版本已更新",
                userExplanation = "您修改了岗位设置或材料版本，之前的旧任务或旧快照已自动作废。",
                suggestion = "请刷新当前页面，基于最新保存的设置重新发起操作。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "RECEIPT_UNREADABLE", "INTERVIEW_RECEIPT_UNKNOWN", "PENDING_OPERATION" -> FriendlyErrorInfo(
                userTitle = "正在确认上次提交状态",
                userExplanation = "请求已发出但未收到完整回执。系统已安全保留原操作记录，不会重复创建数据。",
                suggestion = "请点击页面上的「刷新 / 确认提交结果」同步最新状态。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
            )

            "NETWORK_UNAVAILABLE" -> FriendlyErrorInfo(
                userTitle = "网络连接不可用",
                userExplanation = "无法连接到 MockMate 服务端，请检查网络或确认本地后端服务已启动。",
                suggestion = "恢复连接后点击「刷新」或重新尝试操作即可。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
            )

            "UNAUTHENTICATED" -> FriendlyErrorInfo(
                userTitle = "登录认证失败或已过期",
                userExplanation = rawMessage,
                suggestion = "请检查用户名与密码是否正确，或重新登录。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "USERNAME_TAKEN" -> FriendlyErrorInfo(
                userTitle = "用户名已被注册",
                userExplanation = "该用户名已存在，无法重复注册。",
                suggestion = "请更换其他用户名，或直接切换到「去登录」使用已有账号。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            "FILE_TEXT_UNAVAILABLE", "UNSUPPORTED_FILE_TYPE", "FILE_TOO_LARGE", "TEXT_INVALID" -> FriendlyErrorInfo(
                userTitle = "文件内容无法读取或格式不支持",
                userExplanation = rawMessage,
                suggestion = "请上传可复制文字的 PDF/DOCX/TXT 文件（不超过 10MB），或使用「粘贴新建」直接粘贴文本。",
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
            )

            else -> FriendlyErrorInfo(
                userTitle = if (code != null) "操作未能完成" else "提示",
                userExplanation = rawMessage,
                suggestion = if (retryable) "您可以稍后重试，或展开下方开发者诊断查看详细原因。" else null,
                code = code,
                rawServerMessage = rawMessage,
                details = details,
                httpStatus = httpStatus,
                retryable = retryable,
                diagnosticContext = diagnosticContext,
            )
        }
    }

    private fun parseKeyValueDetails(details: List<String>): Map<String, String> {
        val map = mutableMapOf<String, String>()
        for (entry in details) {
            val idx = entry.indexOf('=')
            if (idx > 0 && idx < entry.length - 1) {
                map[entry.substring(0, idx).trim()] = entry.substring(idx + 1).trim()
            }
        }
        return map
    }

    private fun formatItemTarget(section: String?, itemId: String?, field: String?): String {
        val secName = when (section) {
            "education" -> "教育经历"
            "work_experience" -> "工作经历"
            "projects" -> "项目经历"
            "skills" -> "技能条目"
            "responsibilities" -> "岗位职责"
            "requirements" -> "显式要求"
            else -> null
        }
        val idPart = itemId?.let { "（$it）" } ?: ""
        return when {
            secName != null -> "「$secName$idPart」"
            itemId != null -> "条目「$itemId」"
            field != null -> "字段「$field」"
            else -> "当前核对条目"
        }
    }
}
