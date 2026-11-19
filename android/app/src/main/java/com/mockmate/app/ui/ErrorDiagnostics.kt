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
    val jobKind: String? = null,
) {
    fun formatDiagnosticText(): String = buildString {
        val safeCode = ErrorDiagnosticsMapper.sanitizeErrorCode(code) ?: "UNKNOWN"
        val safeDetails = ErrorDiagnosticsMapper.sanitizeDetailsForDiagnostics(details, jobKind, safeCode)
        val safeRaw = ErrorDiagnosticsMapper.sanitizeRawServerMessage(rawServerMessage, safeCode)
        appendLine("=== MockMate 诊断信息 ===")
        appendLine("错误码 (Code): $safeCode")
        if (httpStatus != null) appendLine("HTTP 状态: HTTP $httpStatus")
        appendLine("可重试 (Retryable): $retryable")
        if (!diagnosticContext.isNullOrBlank()) {
            appendLine("上下文 (Context): $diagnosticContext")
        }
        appendLine("原始消息 (Server/Raw Message): $safeRaw")
        if (safeDetails.isNotEmpty()) {
            appendLine("诊断明细 (Details):")
            safeDetails.forEachIndexed { idx, detail ->
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
        jobKind = message.jobKind,
    )

    fun fromJob(
        job: JobDto,
        extraContext: String? = null,
        inputUpdated: Boolean = false,
    ): FriendlyErrorInfo {
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
            jobKind = job.kind,
            attempt = job.attempt,
            inputUpdated = inputUpdated || job.state == "superseded",
        )
    }

    fun resolve(
        code: String?,
        rawMessage: String,
        details: List<String> = emptyList(),
        httpStatus: Int? = null,
        retryable: Boolean = false,
        diagnosticContext: String? = null,
        jobKind: String? = null,
        attempt: Int? = null,
        inputUpdated: Boolean = false,
    ): FriendlyErrorInfo {
        val safeCode = sanitizeErrorCode(code)
        val safeDetails = sanitizeDetailsForDiagnostics(details, jobKind, safeCode)
        val safeRawMessage = sanitizeRawServerMessage(rawMessage, safeCode)
        val kvDetails = parseKeyValueDetails(safeDetails)
        val rawSlot = kvDetails["slot"] ?: safeDetails.firstOrNull { it.startsWith("generated-") }
        val slotNumber = rawSlot?.removePrefix("generated-")?.takeIf { it.isNotBlank() }
        val itemId = kvDetails["item_id"]
        val field = kvDetails["field"]
        val section = kvDetails["section"]
        val lastCode = kvDetails["last_code"]
        val stage = kvDetails["stage"]
        val reason = kvDetails["reason"]
        val finishReason = kvDetails["finish_reason"]
        val conflictSource = kvDetails["conflict_source"]
        return when (safeCode) {
            "AI_INVALID_OUTPUT", "EVALUATION_INVALID", "GROUNDING_INVALID", "DUPLICATE_QUESTION", "DEDUP_UNCERTAIN" -> {
                val slotHint = if (slotNumber != null) "（第 $slotNumber 个出题槽位）" else ""
                val isParseStage = jobKind == "parse" || stage == "parse"
                val isPlanReviewStage = stage == "plan_review"
                val isMatchEvidenceError =
                    reason == "invalid_evidence_selection" ||
                        (stage == "match" && safeRawMessage.contains("引用原文不属于所引用的材料条目")) ||
                        (jobKind == "match" && (
                            safeRawMessage.contains("引用原文不属于所引用的材料条目") ||
                                safeRawMessage.contains("引用的条目不在材料结构中") ||
                                safeRawMessage.contains("引用原文在当前材料中不存在")
                            ))
                val isMatchStage = jobKind == "match" || stage == "match"
                val isPlanStage = jobKind == "plan" || stage == "plan" || isPlanReviewStage || rawSlot?.startsWith("generated-") == true
                val isLengthTruncated = finishReason == "length"
                val isPlanUnreadableOutput = isPlanStage && !isLengthTruncated && (
                    reason in setOf("invalid_json", "empty_content", "non_object_json", "duplicate_json_key", "invalid_json_number", "invalid_json_text") ||
                        safeRawMessage.contains("不是合法 JSON") ||
                        safeRawMessage.contains("不是非空字符串") ||
                        safeRawMessage.contains("必须是对象") ||
                        safeDetails.any {
                            it.contains("不是合法 JSON") ||
                                it.contains("不是非空字符串") ||
                                it.contains("必须是对象")
                        }
                    )
                val isPlanSchemaError = isPlanStage && reason == "invalid_question_schema"
                val isPlanDuplicateStem = isPlanStage && (
                    reason == "duplicate_stem" ||
                        safeRawMessage.contains("禁止题干重复") ||
                        safeDetails.any {
                            it.startsWith("重复题干：") ||
                                it.startsWith("forbidden=") ||
                                it.contains("禁止题干重复")
                        }
                    )
                val isPlanSemanticDuplicate = isPlanStage && reason == "semantic_duplicate"
                val isPlanSemanticUncertain = isPlanStage && reason == "semantic_uncertain"
                val isPlanGroundingIssue = isPlanStage && reason == "grounding_issue"
                val isMaterialMissingOrUnconfirmed =
                    safeRawMessage.contains("无法从该简历文本结构化") ||
                        safeRawMessage.contains("未找到职责或要求") ||
                        safeRawMessage.contains("没有可分析的显式要求") ||
                        safeRawMessage.contains("材料版本已失效") ||
                        safeRawMessage.contains("手动填写")
                val subReason = when {
                    isParseStage -> when {
                        safeRawMessage.contains("无法从该简历文本结构化") ->
                            "AI 未能从当前简历文本中识别出有效的教育、项目或技能段落。原文已保留，可以手动核对。"
                        safeRawMessage.contains("未找到职责或要求") || safeRawMessage.contains("没有可分析的显式要求") ->
                            "AI 未能从当前 JD 文本中识别出明确的岗位职责或任职要求。原文已保留，可以手动核对。"
                        else ->
                            "AI 解析返回的结构或字段未通过服务端校验，本次未覆盖您的材料。原文已保留，可以手动核对。"
                    }
                    isMatchEvidenceError ->
                        "本次分析未能把引用关联到正确的简历条目。本次匹配报告未生成，已确认的材料已保留。"
                    isPlanSemanticDuplicate -> {
                        val target = if (slotNumber != null) "第 $slotNumber 道 AI 题" else "AI 题目"
                        if (conflictSource == "bank") {
                            "${target}在语义复核中被判定与题库已有题目重复，本次计划未发布。已确认的材料仍可复用。"
                        } else {
                            "${target}在语义复核中被判定与本场已有题目重复，本次计划未发布。已确认的材料仍可复用。"
                        }
                    }
                    isPlanSemanticUncertain ->
                        "计划语义去重结果仍存在不确定性，本次计划未发布。已确认的材料仍可复用。"
                    isPlanGroundingIssue -> {
                        val target = if (slotNumber != null) "第 $slotNumber 道 AI 题" else "AI 题目"
                        "${target}在语义复核中被发现包含材料或依据卡无法支持的陈述，本次计划未发布。已确认的材料仍可复用。"
                    }
                    isPlanReviewStage ->
                        "计划语义复核未能返回完整合规的结果。本次计划未保存，已确认的材料与已生成的题目槽位已保留。"
                    isPlanStage && isLengthTruncated -> {
                        val target = if (slotNumber != null) "第 $slotNumber 道题" else "题目"
                        "AI 生成的${target}模型输出中途结束（达到长度上限），未能完整返回 JSON。本次计划未保存，已确认的材料和匹配报告已保留。"
                    }
                    isPlanUnreadableOutput ->
                        if (slotNumber != null) {
                            "AI 生成的第 $slotNumber 道题返回了无法读取的结果。本次计划未保存，已确认的材料和匹配报告已保留。"
                        } else {
                            "AI 生成题目时返回了无法读取的结果。本次计划未保存，已确认的材料和匹配报告已保留。"
                        }
                    isPlanDuplicateStem -> {
                        val target = if (slotNumber != null) "第 $slotNumber 道 AI 题" else "AI 题目"
                        if (conflictSource == "bank") {
                            "${target}与题库已有题目过于相似，本次计划未发布。已确认的材料仍可复用。"
                        } else {
                            "${target}与本场已有题目过于相似，本次计划未发布。已确认的材料仍可复用。"
                        }
                    }
                    isPlanSchemaError ->
                        if (slotNumber != null) {
                            "AI 生成的第 $slotNumber 道题格式未通过服务端校验。本次计划未保存，已确认的材料和匹配报告已保留。"
                        } else {
                            "AI 生成的题目格式未通过服务端校验。本次计划未保存，已确认的材料和匹配报告已保留。"
                        }
                    safeRawMessage.contains("无法从该简历文本结构化") ->
                        "AI 未能从当前简历文本中识别出有效的教育、项目或技能段落。"
                    safeRawMessage.contains("未找到职责或要求") || safeRawMessage.contains("没有可分析的显式要求") ->
                        "AI 未能从当前 JD 文本中识别出明确的岗位职责或任职要求。"
                    safeRawMessage.contains("权重之和") || lastCode == "EVALUATION_INVALID" ->
                        "AI 生成的评分维度或要点权重格式未通过服务端校验$slotHint。"
                    safeRawMessage.contains("重复") || code == "DUPLICATE_QUESTION" || code == "DEDUP_UNCERTAIN" ->
                        "AI 本次生成的题目与已有题库或已曝光题目过于相似$slotHint，已被服务端拦截。"
                    safeRawMessage.contains("依据卡") || safeRawMessage.contains("锚点") || code == "GROUNDING_INVALID" ->
                        "AI 生成的题目未能严格关联到您的材料原文或知识依据卡$slotHint。"
                    isMatchStage ->
                        "本次匹配分析的模型输出未通过服务端校验。本次匹配报告未生成，已确认的材料已保留。"
                    else -> "外部 AI 模型本次返回的数据结构未通过严格契约校验$slotHint。"
                }
                val suggestion = when {
                    isParseStage -> when {
                        retryable ->
                            "原文已保留，可点击「重试 AI 解析」再次尝试，或直接在下方「结构化核对」手动填写。"
                        inputUpdated ->
                            "当前材料版本已更新，旧解析任务已失效，请基于当前版本继续操作。"
                        else ->
                            "原文已保留，可以手动核对，请直接在下方「结构化核对」手动添加并确认。"
                    }
                    safeRawMessage.contains("手动填写") ->
                        "建议检查原文是否过于简短，或直接在下方手动添加结构化条目。"
                    isMatchEvidenceError || (isMatchStage && !isMaterialMissingOrUnconfirmed) ->
                        if (retryable) {
                            "已确认的材料已保留，请点击「重试匹配分析」（会再次调用模型）。"
                        } else {
                            "本次匹配报告未生成，已确认的材料已保留。可展开下方诊断详情查看具体原因。"
                        }
                    isPlanStage -> when {
                        retryable ->
                            "已确认的材料和匹配报告已保留，请点击「重试生成计划」（会再次调用模型）。"
                        inputUpdated ->
                            "当前准备设置或材料已更新，旧任务不可重试，请基于当前版本重新生成计划。"
                        (attempt ?: 0) >= 2 ->
                            "已用完 2 次生成尝试，当前任务不能继续重试。可点击「沿用材料新建准备」复用已确认材料重新生成，或展开下方诊断详情查看原因。"
                        isLengthTruncated || isPlanUnreadableOutput ->
                            "准备输入可能已更新或当前状态暂不支持重试，请先点击「只读查询最新状态」确认，或在更新设置后重新生成计划。"
                        isPlanDuplicateStem || isPlanSemanticDuplicate || isPlanSemanticUncertain || isPlanGroundingIssue ->
                            "可点击「沿用材料新建准备」复用已确认材料重新生成，或展开下方诊断详情查看原因。"
                        else ->
                            "当前失败原因不支持直接重试。本次计划未保存，已确认的材料和匹配报告已保留。可展开下方诊断详情查看具体原因。"
                    }
                    retryable -> "您的材料和准备进度完好无损，直接点击「重试」重新调用模型即可。"
                    isMaterialMissingOrUnconfirmed -> "建议检查并丰富简历/JD 的结构化内容后再试。"
                    else -> "可展开下方诊断详情查看原因，或调整设置后重新发起。"
                }
                FriendlyErrorInfo(
                    userTitle = "AI 生成结果未通过质量校验",
                    userExplanation = subReason,
                    suggestion = suggestion,
                    code = code,
                    rawServerMessage = safeRawMessage,
                    details = safeDetails,
                    httpStatus = httpStatus,
                    retryable = retryable,
                    diagnosticContext = diagnosticContext,
                    jobKind = jobKind,
                )
            }

            "AI_TIMEOUT", "JOB_POLL_TIMEOUT" -> FriendlyErrorInfo(
                userTitle = "AI 服务响应超时",
                userExplanation = "外部模型处理耗时较长，当前尚未拿到完整结果。",
                suggestion = "您可以先点击「刷新/查询最新状态」确认服务端是否已完成，或点击「重试」重新发起。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "AI_UNAVAILABLE", "AI_RATE_LIMITED", "RATE_LIMITED" -> FriendlyErrorInfo(
                userTitle = "AI 服务繁忙或暂时不可用",
                userExplanation = "当前外部模型接口触发限流或网络波动，请求未能完成。",
                suggestion = "请稍等片刻后点击「重试」，不会产生重复记录。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "AI_NOT_CONFIGURED", "AI_AUTH_FAILED", "AI_REQUEST_REJECTED" -> FriendlyErrorInfo(
                userTitle = "后端 AI 模型服务配置异常",
                userExplanation = "服务端未正确配置模型密钥（API Key）或模型拒绝了本次调用。",
                suggestion = "开发者请检查后端 .env 中的模型配置与日志；用户可先使用手动填写完成核对。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = retryable,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "EVIDENCE_INVALID", "PROFILE_INVALID" -> {
                val isParseStage = jobKind == "parse" || stage == "parse"
                val targetLabel = formatItemTarget(section, itemId, field)
                val explanation = when {
                    isParseStage ->
                        "AI 解析结果中的${targetLabel}未通过原文或字段校验，本次未覆盖您的材料。原文已保留，可以手动核对。"
                    safeRawMessage.contains("quote") || field?.contains("quote") == true ->
                        "${targetLabel}中的「引用原文」未在上方材料正文中逐字找到。"
                    safeRawMessage.contains("技能") || field == "skill_codes" || field == "code" ->
                        "${targetLabel}包含了已失效或未启用的技能标签。"
                    else -> "${targetLabel}填写的字段内容不符合结构化核对要求：$safeRawMessage"
                }
                val suggestion = when {
                    isParseStage -> if (retryable) {
                        "原文已保留，可点击「重试 AI 解析」再次尝试，或直接在下方「结构化核对」手动填写。"
                    } else {
                        "原文已保留，可以手动核对，请直接在下方「结构化核对」手动添加并确认。"
                    }
                    safeRawMessage.contains("quote") || field?.contains("quote") == true ->
                        "请从上方原文直接复制完全一致的句子填入「引用原文」，或将该条目标记为「我手动确认」。"
                    safeRawMessage.contains("技能") || field == "skill_codes" || field == "code" ->
                        "请定位到标红条目，移除或替换为当前可用的技能标签后重新保存。"
                    else -> "请检查对应条目的必填项（如名称、描述、引用原文）是否完整。"
                }
                FriendlyErrorInfo(
                    userTitle = if (isParseStage) "AI 解析结果未通过原文校验" else "材料核对内容需要调整",
                    userExplanation = explanation,
                    suggestion = suggestion,
                    code = code,
                    rawServerMessage = safeRawMessage,
                    details = safeDetails,
                    httpStatus = httpStatus,
                    retryable = retryable,
                    diagnosticContext = diagnosticContext,
                    jobKind = jobKind,
                )
            }

            "MATERIALS_REQUIRED" -> FriendlyErrorInfo(
                userTitle = "尚未完成简历与 JD 确认",
                userExplanation = "匹配分析与生成计划需要同时绑定「已核对确认的简历」和「已核对确认的岗位 JD」。",
                suggestion = "请先在准备设置页上传或新建简历与 JD，并在核对页点击「确认并保存」后再继续。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "MATCH_REQUIRED" -> FriendlyErrorInfo(
                userTitle = "请先完成匹配度分析",
                userExplanation = "生成面试计划前，需要先对当前版本的简历与 JD 执行一次匹配分析。",
                suggestion = "请先前往「第 2 步：匹配分析」完成分析，再生成 3+3 面试计划。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "BANK_INSUFFICIENT" -> FriendlyErrorInfo(
                userTitle = "关联的题库题目数量不足",
                userExplanation = safeRawMessage,
                suggestion = "您可以返回核对页为项目/技能关联更多技能标签，或返回设置页将主问题数量调小（如改为 2 题）。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "REVISION_CONFLICT", "JOB_INPUT_CONFLICT", "PLAN_STALE", "SUPERSEDED" -> FriendlyErrorInfo(
                userTitle = "页面数据版本已更新",
                userExplanation = "您修改了岗位设置或材料版本，之前的旧任务或旧快照已自动作废。",
                suggestion = "请刷新当前页面，基于最新保存的设置重新发起操作。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "RECEIPT_UNREADABLE", "INTERVIEW_RECEIPT_UNKNOWN", "PENDING_OPERATION" -> FriendlyErrorInfo(
                userTitle = "正在确认上次提交状态",
                userExplanation = "请求已发出但未收到完整回执。系统已安全保留原操作记录，不会重复创建数据。",
                suggestion = "请点击页面上的「刷新 / 确认提交结果」同步最新状态。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "NETWORK_UNAVAILABLE" -> FriendlyErrorInfo(
                userTitle = "网络连接不可用",
                userExplanation = "无法连接到 MockMate 服务端，请检查网络或确认本地后端服务已启动。",
                suggestion = "恢复连接后点击「刷新」或重新尝试操作即可。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = true,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "UNAUTHENTICATED" -> FriendlyErrorInfo(
                userTitle = "登录认证失败或已过期",
                userExplanation = safeRawMessage,
                suggestion = "请检查用户名与密码是否正确，或重新登录。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "USERNAME_TAKEN" -> FriendlyErrorInfo(
                userTitle = "用户名已被注册",
                userExplanation = "该用户名已存在，无法重复注册。",
                suggestion = "请更换其他用户名，或直接切换到「去登录」使用已有账号。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            "FILE_TEXT_UNAVAILABLE", "UNSUPPORTED_FILE_TYPE", "FILE_TOO_LARGE", "TEXT_INVALID" -> FriendlyErrorInfo(
                userTitle = "文件内容无法读取或格式不支持",
                userExplanation = safeRawMessage,
                suggestion = "请上传可复制文字的 PDF/DOCX/TXT 文件（不超过 10MB），或使用「粘贴新建」直接粘贴文本。",
                code = code,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = false,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )

            else -> FriendlyErrorInfo(
                userTitle = if (safeCode != null) "操作未能完成" else "提示",
                userExplanation = safeRawMessage,
                suggestion = if (retryable) "您可以稍后重试，或展开下方开发者诊断查看详细原因。" else null,
                code = safeCode,
                rawServerMessage = safeRawMessage,
                details = safeDetails,
                httpStatus = httpStatus,
                retryable = retryable,
                diagnosticContext = diagnosticContext,
                jobKind = jobKind,
            )
        }
    }

    private val SAFE_DIAGNOSTIC_KEYS = setOf(
        "stage",
        "reason",
        "slot",
        "field",
        "section",
        "item_id",
        "requirement_id",
        "finish_reason",
        "source",
        "last_code",
        "duplicate_kind",
        "conflict_source",
        "conflict_slot",
        "conflict_question_id",
        "similarity",
        "threshold",
        "required",
        "available",
        "returned",
        "current_attempt",
        "current_revision",
        "expected",
        "actual",
        "length",
        "uncertainties_count",
        "unknown_count",
        "duplicate_count",
        "http_requests",
        "thinking_fallback",
        "question_id",
        "dimension_id",
        "status",
    )
    private val SAFE_STAGES = setOf(
        "extract",
        "parse",
        "match",
        "plan",
        "plan_review",
        "follow_up",
        "interview_score",
        "interview_report",
    )

    private val SAFE_REASONS = setOf(
        "output_truncated",
        "output_aborted",
        "insufficient_system_resource",
        "unexpected_tool_calls",
        "unknown_finish_reason",
        "empty_content",
        "invalid_json",
        "duplicate_json_key",
        "invalid_json_number",
        "invalid_json_text",
        "non_object_json",
        "invalid_response_shape",
        "content_filter",
        "invalid_profile_schema",
        "invalid_jd_catalog_role",
        "invalid_work_experience",
        "missing_work_experience_fields",
        "blank_quote",
        "quote_not_in_text",
        "invalid_evidence_origin",
        "ungrounded_work_experience",
        "blank_field",
        "duplicate_item_id",
        "empty_profile",
        "kind_mismatch",
        "invalid_evidence_selection",
        "quote_mismatch_item",
        "invalid_question_schema",
        "invalid_question_count",
        "question_type_mismatch",
        "duplicate_stem",
        "duplicate_normalized_stem",
        "candidate_stem_reused",
        "invalid_anchor_selection",
        "unknown_skill_code",
        "invalid_review_schema",
        "invalid_review_reference",
        "semantic_duplicate",
        "semantic_uncertain",
        "grounding_issue",
        "slot_validation_failed",
        "invalid_follow_up_schema",
        "invalid_follow_up_advance",
        "invalid_follow_up_prompt",
        "invalid_follow_up_quote",
        "invalid_follow_up_target",
        "duplicate_follow_up",
        "invalid_score_schema",
        "unknown_dimension_id",
        "invalid_cached_score_slot",
    )

    private val SAFE_SOURCES = setOf(
        "provider_output",
        "user_confirmed",
        "extracted",
        "bank",
        "prior_generated",
    )

    private val SAFE_STATUSES = setOf(
        "supported",
        "to_verify",
        "not_evidenced",
        "queued",
        "running",
        "succeeded",
        "failed",
        "interrupted",
        "superseded",
    )

    private val SAFE_SECTIONS = setOf(
        "education",
        "work_experience",
        "projects",
        "skills",
        "responsibilities",
        "requirements",
    )

    private val SAFE_FINISH_REASONS = setOf(
        "stop",
        "length",
        "content_filter",
        "aborted",
        "insufficient_system_resource",
        "tool_calls",
        "unknown",
        "other",
    )

    private val SAFE_DUPLICATE_KINDS = setOf("exact", "near", "duplicate")
    private val SAFE_CONFLICT_SOURCES = setOf("prior_generated", "bank")
    private val SAFE_LAST_CODES = setOf(
        "AI_INVALID_OUTPUT",
        "AI_TIMEOUT",
        "AI_UNAVAILABLE",
        "AI_RATE_LIMITED",
        "AI_AUTH_FAILED",
        "AI_REQUEST_REJECTED",
        "EVIDENCE_INVALID",
        "EVALUATION_INVALID",
        "DUPLICATE_QUESTION",
        "DEDUP_UNCERTAIN",
        "GROUNDING_INVALID",
        "PROFILE_INVALID",
        "BANK_INSUFFICIENT",
        "INTERNAL_ERROR",
    )
    private val SAFE_EXPECTED_ACTUAL_VALUES = setOf(
        "resume",
        "jd",
        "experience",
        "knowledge",
        "invalid_value",
        "text/plain",
        "application/pdf",
        "application/x-pdf",
        "application/octet-stream",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )
    private val SAFE_DIMENSION_IDS = setOf(
        "core",
        "accuracy",
        "depth",
        "clarity",
        "expression",
        "completeness",
        "communication",
        "logic",
        "structure",
    )
    private val NONNEG_INT_REGEX = Regex("^\\d{1,10}$")
    private val DECIMAL_REGEX = Regex("^\\d{1,4}(?:\\.\\d{1,4})?$")
    private val UUID_REGEX = Regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
    private val SLOT_VALUE_REGEX = Regex("^(?:generated-[1-3]|semantic|[1-3])$")
    private val SAFE_ITEM_ID_REGEX = Regex("^(?:edu|work|project|skill|resp|responsibility|req|education|work_experience|projects|skills|responsibilities|requirements)-[0-9]+$")
    private val SAFE_REQ_ID_REGEX = Regex("^(?:req|requirement|requirements)-[0-9]+$")
    private val SAFE_BANK_QUESTION_ID_REGEX = Regex("^M[0-9]+-[A-Z0-9-]+$")

    private val SAFE_BARE_ID_REGEX = Regex("^(generated-[1-3]|skill-[0-9]+|req-[0-9]+|project-[0-9]+|work-[0-9]+|edu-[0-9]+|resp-[0-9]+|responsibility-[0-9]+|role_id|level|text|title|cursor|Idempotency-Key|education|work_experience|projects|skills|responsibilities|requirements|M[0-9]+-[A-Z0-9-]+|ValidationError)$")

    private val KNOWN_LEGACY_SAFE_DETAILS = setOf(
        "AI 返回内容不是合法 JSON",
        "生成题与本次禁止题干重复",
        "生成题类型与本次槽位要求不一致",
        "AI 输出不符合契约结构",
    )

    private val KNOWN_SAFE_SERVER_MESSAGES = setOf(
        "AI 服务超时，结果未知",
        "AI 服务暂时不可用",
        "AI 服务限流，结果未知",
        "AI 服务认证失败，请检查服务端配置",
        "AI 服务拒绝了请求",
        "AI 输出不符合契约",
        "AI 输出不符合契约结构",
        "AI 输出触发上游内容过滤",
        "AI 输出因达到长度上限而未完整结束",
        "AI 输出被服务端中止",
        "AI 服务计算资源不足导致输出中断",
        "AI 返回了未请求的工具调用",
        "AI 返回了未知的结束原因",
        "AI 响应缺少合法的 choices/message/content",
        "AI 返回内容不是非空字符串",
        "AI 返回的 JSON 包含重复键",
        "AI 返回的 JSON 包含非有限数值",
        "AI 返回内容不是合法 JSON",
        "AI 返回的 JSON 必须是对象",
        "AI 返回的 JSON 包含不支持的控制字符或无效编码",
        "AI 服务未配置",
        "AI 服务未配置，无法执行模型任务",
        "测试 provider 只能在隔离测试环境启用",
        "任务租约已失效",
        "任务租约已失效，不能写入槽位结果",
        "题库可用题不足",
        "题库与生成题数量不满足各半要求",
        "JD 缺少显式技能要求",
        "JD 缺少显式技能要求，无法建立题库主问题的依据",
        "文件无法提取文本",
        "任务执行中断，外部结果未知",
        "任务执行中断，外部结果未知；不会自动重发外部调用",
        "任务未完成，可查询或重试",
        "题目或匹配引用无依据",
        "评价依据不完整",
        "本场存在重复主问题",
        "存在重复主问题",
        "语义去重复核发现重复题目",
        "语义去重结果不确定，不能发布 ready 计划",
        "语义去重不确定",
        "语义复核发现无依据陈述",
        "存在无依据陈述",
        "未确认材料外发",
        "JD 原文与当前岗位/级别设置冲突",
        "JD 原文明确要求资深或多年经验，不能自动改写为实习/初级；请修改 JD 或手动核对结构",
        "JD 解析出的岗位或级别不在服务端目录中",
        "任务执行失败",
        "工作经历缺少明确的组织或职务，请核对原文",
        "测试 provider 无法从该简历文本结构化出任何条目，请手动填写或更换文本",
        "测试 provider 未找到职责或要求行，请手动填写 JD 结构",
        "JD 没有可分析的显式要求",
        "没有可用于出题的本场材料锚点",
        "knowledge 槽位没有可用依据卡",
        "AI 未返回生成题",
        "模型每个槽位必须返回且只返回 1 道题",
        "生成槽位在有限纠正后仍不合法",
        "生成题类型与本次槽位要求不一致",
        "生成题与本次禁止题干重复",
        "生成题锚点不在本次允许锚点列表中",
        "生成题包含未启用的技能编码",
        "生成题不能自称已核验",
        "生成题包含未解决的不确定性，不能进入 ready 计划",
        "生成题缺少合理替代解",
        "评分要点权重之和不为 1",
        "评分量表维度权重之和不为 1",
        "生成题评分量表与本次提供的不一致",
        "生成题评价依据引用了不存在的材料条目",
        "生成题评价依据引用了不在本次允许锚点列表中的材料条目",
        "生成题引用了本次未提供的依据卡",
        "技术知识生成题必须引用本次提供的已审依据卡",
        "经历／推理生成题必须引用本场材料",
        "语义复核输出结构不符合契约",
        "语义复核包含未知或非法题目引用",
        "语义复核包含重复关系",
        "语义复核引用了未知题目",
        "语义复核包含重复无依据声明",
        "材料版本已变化",
        "材料没有可解析文本",
        "准备已删除",
        "材料版本已失效",
        "请先确认简历和 JD",
        "匹配结果引用了不存在的 JD 要求",
        "匹配结果 JD 要求重复",
        "匹配结果引用了重复的简历条目 ID",
        "匹配结果引用了不存在或不可引用的简历条目 ID",
        "supported 匹配必须选择至少一条有效简历条目",
        "supported 匹配必须有至少一条有效简历证据",
        "not_evidenced 匹配不能附带支持条目",
        "not_evidenced 匹配不能附带支持证据",
        "匹配结果未覆盖全部 JD 要求",
        "匹配说明与准备建议不能为空白",
        "题目或证据引用了不属于本场的材料",
        "引用原文不能为空白",
        "引用的条目不在材料结构中",
        "引用原文不属于所引用的材料条目",
        "引用原文在当前材料中不存在",
        "evidence.quote 必须逐字出现在原文中",
        "profile 必须是对象",
        "profile 类型必须与材料类型一致",
        "缺少合法的 profile.kind",
        "每个条目必须有 item_id",
        "item_id 不能重复",
        "条目必填字段不能为空白",
        "每个条目必须有非空白原文引用",
        "引用内容必须能在当前原文中找到",
        "证据来源标记非法",
        "模型解析结果只能标记为 extracted；user_confirmed 仅用于用户核对提交",
        "工作经历的单位必须在同条引文中有依据",
        "工作经历的职务必须在同条引文中有依据",
        "工作经历引文不能跨越多个分隔条目",
        "工作经历表格行无法按表头可靠对齐列，请核对原文",
        "技能编码不在已启用词表中",
        "简历至少需要一条教育、工作、项目或技能信息",
        "JD 至少需要一条职责或要求",
        "追问判定输出结构不符合契约",
        "无需追问时不能携带问题或引文",
        "追问长度必须在 8–220 码点之间",
        "追问引文必须逐字出现在本题主回答中",
        "追问缺少考察点",
        "追问包含参考答案、评分或点评内容",
        "追问与主问题重复",
        "追问与主问题过于相似",
        "追问与已曝光题目重复",
        "追问与已曝光题目过于相似",
        "追问任务已变化，请刷新",
        "追问任务已不是当前任务",
        "追问任务输入已过期",
        "同一主问题不能生成第二个追问回合",
        "评分输出结构不符合契约",
        "评分输出引用了其他题目",
        "评分输出包含未知维度",
        "评分输出包含重复维度",
        "scored 维度必须给出 0–4 分",
        "scored 维度必须引用原话",
        "非 scored 维度不能携带分数",
        "非 scored 维度不能携带引文",
        "评分输出缺少维度",
        "评分结果不完整，不能发布报告",
        "生成期间题库内容或审核准入已变化，不能发布旧计划",
        "生成期间依据卡审核状态已变化，不能发布旧计划",
        "服务器缺少 PDF 解析组件",
        "服务器缺少 DOCX 解析组件",
        "材料没有原文件",
        "文件路径非法",
        "原文件不存在，请重新上传",
        "无法以 UTF-8 读取该 TXT 文件，请另存为 UTF-8 后重试或粘贴文本",
        "PDF 已加密，无法提取文本；请粘贴文本后继续",
        "PDF 文件无法解析，可能已损坏或不是 PDF；也可粘贴文本",
        "DOCX 文件无法解析，可能已损坏或不是 DOCX；也可粘贴文本",
        "文件没有可提取的文字（可能是扫描件）。请粘贴文本或换一份可复制文本的文件",
        "仅支持 PDF、DOCX、TXT",
        "仅支持 PDF、DOCX、TXT 文件",
        "缺少文件名",
        "不支持 .doc，请另存为 .docx 或 PDF",
        "文件声明类型与扩展名不一致",
        "文件为空",
        "面试不存在",
        "面试已结束",
        "面试已不在追问判定阶段",
        "面试尚未结束",
        "报告任务已不是当前任务",
        "有任务仍在执行；本页只查询状态，不会重复提交",
        "旧任务已失效，不会挂到新准备上",
        "暂不可用",
        "用户名或密码不正确",
        "登录会话已过期",
        "登录会话无效",
        "登录会话无效或已退出",
        "需要有效的登录会话",
        "材料尚未加载完成",
        "材料尚未加载完成，请稍后再试",
        "准备尚未加载完成，请返回准备页重试",
        "该任务不可重试或输入已过期",
        "任务状态未明",
    )

    private val SAFE_SERVER_MESSAGE_TEMPLATES = listOf(
        Regex("^任务状态：(queued|running|failed|interrupted|superseded)$"),
        Regex("^请求失败（HTTP \\d{3}）$"),
        Regex("^材料文本超过 \\d+ 字符上限$"),
        Regex("^文件超过 \\d+ MiB 上限$"),
        Regex("^HTTP \\d{3}$"),
    )

    private val STANDARD_CODE_MESSAGES = mapOf(
        "AI_TIMEOUT" to "AI 服务超时，结果未知",
        "AI_UNAVAILABLE" to "AI 服务暂时不可用",
        "AI_RATE_LIMITED" to "AI 服务限流，结果未知",
        "AI_AUTH_FAILED" to "AI 服务认证失败，请检查服务端配置",
        "AI_REQUEST_REJECTED" to "AI 服务拒绝了请求",
        "AI_INVALID_OUTPUT" to "AI 输出不符合契约",
        "JOB_LEASE_LOST" to "任务租约已失效",
        "BANK_INSUFFICIENT" to "题库可用题不足",
        "AI_NOT_CONFIGURED" to "AI 服务未配置",
        "FILE_TEXT_UNAVAILABLE" to "文件无法提取文本",
        "JOB_INTERRUPTED" to "任务执行中断，外部结果未知",
        "EVIDENCE_INVALID" to "题目或匹配引用无依据",
        "EVALUATION_INVALID" to "评价依据不完整",
        "DUPLICATE_QUESTION" to "本场存在重复主问题",
        "DEDUP_UNCERTAIN" to "语义去重不确定",
        "GROUNDING_INVALID" to "存在无依据陈述",
        "CONSENT_REQUIRED" to "未确认材料外发",
        "JD_LEVEL_CONFLICT" to "JD 原文与当前岗位/级别设置冲突",
        "PROFILE_INVALID" to "材料结构校验未通过",
        "UNAUTHENTICATED" to "登录会话无效或已过期",
        "MATERIALS_REQUIRED" to "尚未完成简历与 JD 确认",
        "MATCH_REQUIRED" to "请先完成一次匹配分析再生成计划",
        "SUPERSEDED" to "旧任务已失效，不会挂到新准备上",
        "INTERNAL_ERROR" to "任务执行失败",
    )

    private const val DEFAULT_SAFE_SERVER_MESSAGE = "任务执行失败"

    private val KNOWN_SCHEMA_FIELD_NAMES = setOf(
        "kind",
        "education",
        "work_experience",
        "projects",
        "skills",
        "role_id",
        "level",
        "title",
        "responsibilities",
        "requirements",
        "item_id",
        "degree",
        "major",
        "evidence",
        "organization",
        "description",
        "name",
        "skill_codes",
        "code",
        "text",
        "importance",
        "quote",
        "origin",
        "snapshot_id",
        "rows",
        "requirement_id",
        "status",
        "resume_item_ids",
        "resume_evidence",
        "explanation",
        "suggested_focus",
        "questions",
        "question_type",
        "canonical_id",
        "stem",
        "focus",
        "topic_key",
        "anchors",
        "evaluation",
        "goal",
        "reference_approach",
        "key_points",
        "point",
        "weight",
        "acceptable_alternatives",
        "common_mistakes",
        "rubric",
        "id",
        "version",
        "dimensions",
        "low_anchor",
        "high_anchor",
        "basis",
        "ref_id",
        "note",
        "answer_status",
        "trust",
        "uncertainties",
        "duplicate_pairs",
        "left_id",
        "right_id",
        "reason",
        "grounding_issues",
        "question_id",
        "claim",
        "uncertain",
        "decision",
        "prompt",
        "answer_quote",
        "target",
        "dimension_id",
        "verdict",
        "score",
        "improvement",
        "answer_id",
        "content_hash",
        "root",
        "extra_field",
        "unknown_field",
    )

    private val SAFE_VALIDATION_MESSAGES = setOf(
        "Field required",
        "Extra inputs are not permitted",
        "value_error",
        "invalid_value",
        "Input should be a valid string",
        "String is too short",
        "String is too long",
        "String does not match pattern",
        "Input should be a valid integer",
        "Input should be a valid number",
        "Input should be a valid boolean",
        "Input should be a valid list",
        "List is too short",
        "List is too long",
        "Input should be a valid object",
        "Input does not match allowed values",
        "Input should be a valid UUID",
        "Number out of range",
        "Invalid JSON",
        "Invalid union tag",
        "Missing union tag",
    )

    private const val GENERIC_UNKNOWN_DETAIL = "未知诊断明细（已隐藏原始内容）"

    private fun isSafeSchemaPath(path: String): Boolean {
        val trimmed = path.trim()
        if (trimmed.isEmpty() || trimmed.length > 80) return false
        return trimmed.split('.').all { part ->
            part.isNotEmpty() && (part.all { it.isDigit() } || part in KNOWN_SCHEMA_FIELD_NAMES)
        }
    }

    private val KNOWN_ERROR_CODES = STANDARD_CODE_MESSAGES.keys + setOf(
        "JOB_POLL_TIMEOUT",
        "JOB_POLL_PAUSED",
        "JOB_STATE_CONFLICT",
        "RATE_LIMITED",
        "REVISION_CONFLICT",
        "JOB_INPUT_CONFLICT",
        "PLAN_STALE",
        "PLAN_STATE_INVALID",
        "INTERVIEW_STATE_INVALID",
        "TURN_NOT_CURRENT",
        "REPORT_PENDING",
        "REPORT_FAILED",
        "REPORT_NOT_AVAILABLE",
        "RECEIPT_UNREADABLE",
        "INTERVIEW_RECEIPT_UNKNOWN",
        "PENDING_OPERATION",
        "NETWORK_UNAVAILABLE",
        "USERNAME_TAKEN",
        "UNSUPPORTED_FILE_TYPE",
        "FILE_TOO_LARGE",
        "TEXT_INVALID",
        "STORAGE_UNAVAILABLE",
        "OPERATION_LOG_CORRUPT",
        "OPERATION_REPLAY_INCOMPLETE",
        "ROLE_LEVEL_MISMATCH",
        "ROLE_NOT_AVAILABLE",
        "TEMPLATE_MISMATCH",
        "MATERIAL_IN_USE",
        "IDEMPOTENCY_CONFLICT",
        "IDEMPOTENCY_KEY_REQUIRED",
        "VALIDATION_ERROR",
        "NOT_FOUND",
        "INVALID_INPUT",
        "RETRY_NOT_ALLOWED",
        "BANK_INELIGIBLE",
    )

    fun sanitizeErrorCode(rawCode: String?): String? {
        if (rawCode == null) return null
        val trimmed = rawCode.trim()
        return if (trimmed in KNOWN_ERROR_CODES) trimmed else "INTERNAL_ERROR"
    }

    fun sanitizeRawServerMessage(rawMessage: String, code: String? = null): String {
        val cleaned = rawMessage.replace('\r', ' ').replace('\n', ' ').trim()
        if (cleaned in KNOWN_SAFE_SERVER_MESSAGES) return cleaned
        if (SAFE_SERVER_MESSAGE_TEMPLATES.any { it.matches(cleaned) }) return cleaned
        val safeCode = sanitizeErrorCode(code)
        return STANDARD_CODE_MESSAGES[safeCode] ?: DEFAULT_SAFE_SERVER_MESSAGE
    }

    private fun isSafeDiagnosticKeyValue(
        key: String,
        value: String,
        jobKind: String?,
    ): Boolean = when (key) {
        "stage" -> value in SAFE_STAGES
        "reason" -> value in SAFE_REASONS
        "source" -> value in SAFE_SOURCES
        "status" -> value in SAFE_STATUSES
        "section" -> value in SAFE_SECTIONS
        "finish_reason" -> value in SAFE_FINISH_REASONS
        "duplicate_kind" -> value in SAFE_DUPLICATE_KINDS
        "conflict_source" -> value in SAFE_CONFLICT_SOURCES
        "last_code" -> value in SAFE_LAST_CODES
        "thinking_fallback" -> value == "true" || value == "false"
        "slot", "conflict_slot" -> SLOT_VALUE_REGEX.matches(value) || UUID_REGEX.matches(value)
        "required",
        "available",
        "returned",
        "current_attempt",
        "current_revision",
        "length",
        "uncertainties_count",
        "unknown_count",
        "duplicate_count",
        "http_requests", -> NONNEG_INT_REGEX.matches(value)
        "similarity", "threshold" -> DECIMAL_REGEX.matches(value)
        "expected", "actual" -> value in SAFE_EXPECTED_ACTUAL_VALUES || NONNEG_INT_REGEX.matches(value)
        "item_id" -> jobKind != "parse" && (SAFE_ITEM_ID_REGEX.matches(value) || UUID_REGEX.matches(value))
        "requirement_id" -> SAFE_REQ_ID_REGEX.matches(value) || UUID_REGEX.matches(value)
        "dimension_id" -> value in SAFE_DIMENSION_IDS || UUID_REGEX.matches(value)
        "conflict_question_id", "question_id" -> SAFE_BANK_QUESTION_ID_REGEX.matches(value) || UUID_REGEX.matches(value)
        else -> false
    }

    fun sanitizeDetailsForDiagnostics(
        details: List<String>,
        jobKind: String? = null,
        code: String? = null,
    ): List<String> =
        details.mapNotNull { entry ->
            val trimmed = entry.trim()
            if (trimmed.isEmpty()) return@mapNotNull null
            val eqIdx = trimmed.indexOf('=')
            if (eqIdx > 0) {
                val key = trimmed.substring(0, eqIdx).trim()
                val value = trimmed.substring(eqIdx + 1).trim()
                if (key in SAFE_DIAGNOSTIC_KEYS) {
                    return@mapNotNull when {
                        key == "field" -> if (isSafeSchemaPath(value)) "field=$value" else GENERIC_UNKNOWN_DETAIL
                        isSafeDiagnosticKeyValue(key, value, jobKind) -> "$key=$value"
                        else -> GENERIC_UNKNOWN_DETAIL
                    }
                }
                return@mapNotNull GENERIC_UNKNOWN_DETAIL
            }
            val colonIdx = trimmed.indexOf(':')
            if (colonIdx > 0) {
                val path = trimmed.substring(0, colonIdx).trim()
                val msg = trimmed.substring(colonIdx + 1).trim()
                if (isSafeSchemaPath(path) && msg in SAFE_VALIDATION_MESSAGES) {
                    return@mapNotNull "$path: $msg"
                }
                return@mapNotNull GENERIC_UNKNOWN_DETAIL
            }
            if (SAFE_BARE_ID_REGEX.matches(trimmed) || trimmed in KNOWN_LEGACY_SAFE_DETAILS) {
                trimmed
            } else {
                GENERIC_UNKNOWN_DETAIL
            }
        }.distinct()

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
