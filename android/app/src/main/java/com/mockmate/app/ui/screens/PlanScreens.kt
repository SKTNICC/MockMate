package com.mockmate.app.ui.screens

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MatchRowDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.ErrorDiagnosticsMapper
import com.mockmate.app.ui.PlanErrorState
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.DeveloperDiagnosticPanel
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.JobRecoveryActions
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.PreparationStepHeader
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip

private fun matchStatusLabel(status: String): String = when (status) {
    "supported" -> "有原文支持"
    "to_verify" -> "待验证"
    "not_evidenced" -> "未体现"
    else -> status
}

data class MatchUiPolicyState(
    val recoveryJob: JobDto?,
    val hasCurrentFailedMatch: Boolean,
    val hasCurrentRunningMatch: Boolean,
    val showStartMatchAction: Boolean,
    val startMatchEnabled: Boolean,
)

object MatchScreenPolicy {
    fun evaluate(
        preparationId: String,
        prep: PreparationDto?,
        job: JobDto?,
        loading: Boolean,
    ): MatchUiPolicyState {
        val ready = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null
        val recoveryJob = job?.takeIf { candidate ->
            candidate.kind == "match" &&
                candidate.target_id == preparationId &&
                (prep == null || candidate.input_revision == prep.revision || candidate.state == "superseded")
        }
        val currentRevisionJob = recoveryJob?.takeIf { candidate ->
            prep != null && candidate.input_revision == prep.revision
        }
        val hasCurrentFailedMatch = currentRevisionJob != null &&
            (currentRevisionJob.state == "failed" || currentRevisionJob.state == "interrupted")
        val hasCurrentRunningMatch = currentRevisionJob != null &&
            (currentRevisionJob.state == "queued" || currentRevisionJob.state == "running")
        val showStartMatchAction = !hasCurrentFailedMatch
        val startMatchEnabled = showStartMatchAction && ready && !loading && !hasCurrentRunningMatch
        return MatchUiPolicyState(
            recoveryJob = recoveryJob,
            hasCurrentFailedMatch = hasCurrentFailedMatch,
            hasCurrentRunningMatch = hasCurrentRunningMatch,
            showStartMatchAction = showStartMatchAction,
            startMatchEnabled = startMatchEnabled,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class, ExperimentalLayoutApi::class)
@Composable
fun MatchScreen(vm: AppViewModel, state: AppState, preparationId: String) {
    var showConsent by rememberSaveable { mutableStateOf(false) }
    val prep = state.preparation
    val report = state.matchReport
    val ready = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null
    val policy = remember(preparationId, prep, state.job, state.loading) {
        MatchScreenPolicy.evaluate(preparationId, prep, state.job, state.loading)
    }
    val matchJob = policy.recoveryJob
    val hasCurrentFailedMatch = policy.hasCurrentFailedMatch
    val hasCurrentRunningMatch = policy.hasCurrentRunningMatch

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("匹配分析", fontWeight = FontWeight.SemiBold) },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Spacer(Modifier.height(4.dp))
                PreparationStepHeader(
                    currentStep = 2,
                    materialsReady = ready,
                    matchReady = report != null || prep?.match_report_id != null,
                    planConfirmed = prep?.current_plan_id != null,
                    onSelectStep = { step ->
                        when (step) {
                            1 -> vm.returnToSetup(preparationId)
                            3 -> if (ready) vm.openPlan(preparationId)
                        }
                    },
                )
            }
            item {
                InfoCard(
                    title = "要求 → 支持原文 → 待验证/未体现",
                    subtitle = "结论只基于已确认材料；不预测录用概率，不使用百分比",
                    accentColor = MaterialTheme.colorScheme.primary,
                ) {
                    Text(
                        if (ready) "匹配只读取当前已确认的简历与 JD 版本。" else "请先绑定并确认简历与 JD。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    if (hasCurrentFailedMatch) {
                        Text(
                            text = if (matchJob?.retryable == true) {
                                "当前版本已有失败的匹配任务，请使用下方「重试匹配分析」按钮。"
                            } else {
                                "当前版本的匹配任务已失败且不可继续重试，请查看下方原因；如需重新分析，请修改材料或准备设置以生成新版本。"
                            },
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    } else {
                        PrimaryAction(
                            text = if (report == null) "开始匹配分析" else "重新分析",
                            enabled = policy.startMatchEnabled,
                            onClick = { showConsent = true },
                        )
                    }
                    if (report != null) {
                        Spacer(Modifier.height(8.dp))
                        val supportedCount = report.rows.count { it.status == "supported" }
                        val verifyCount = report.rows.count { it.status == "to_verify" }
                        val missingCount = report.rows.count { it.status == "not_evidenced" }
                        FlowRow(
                            horizontalArrangement = Arrangement.spacedBy(8.dp),
                            verticalArrangement = Arrangement.spacedBy(4.dp),
                        ) {
                            StatusChip("有原文支持 $supportedCount", MaterialTheme.colorScheme.secondary)
                            StatusChip("待验证 $verifyCount", MaterialTheme.colorScheme.tertiary)
                            StatusChip("未体现 $missingCount", MaterialTheme.colorScheme.error)
                        }
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "本报告对应准备版本 revision ${report.input_revision}；" +
                                "如果材料或设置变化，需要重新分析。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
            item {
                JobRecoveryActions(
                    job = matchJob,
                    onQuery = { vm.queryCurrentJob() },
                    onRetry = { vm.retryCurrentJob() },
                )
            }
            if (report == null) {
                if (!hasCurrentFailedMatch && !hasCurrentRunningMatch) {
                    item { EmptyHint("还没有匹配结果。点击上方「开始匹配分析」逐项比对 JD 要求与简历证据。") }
                }
            } else {
                items(report.rows, key = { it.requirement_id }) { row -> MatchRowCard(row) }
            }
            item { Spacer(Modifier.height(8.dp)) }
            item {
                if (report != null) {
                    PrimaryAction(text = "查看/生成计划", onClick = { vm.openPlan(preparationId) })
                }
                Spacer(Modifier.height(24.dp))
            }
        }
    }

    if (showConsent) {
        ConsentDialog(
            title = "确认发送材料用于匹配？",
            body = "匹配会把已确认的简历与 JD 文本发送给服务端配置的外部模型。结果只用于准备建议，不代表录用结论。",
            onConfirm = {
                showConsent = false
                vm.runMatch(preparationId)
            },
            onDismiss = { showConsent = false },
        )
    }
}

@Composable
private fun MatchRowCard(row: MatchRowDto) {
    val color = when (row.status) {
        "supported" -> MaterialTheme.colorScheme.secondary
        "to_verify" -> MaterialTheme.colorScheme.tertiary
        else -> MaterialTheme.colorScheme.error
    }
    InfoCard(
        title = "要求：${row.requirement_id}",
        accentColor = color,
        headerTrailing = { StatusChip(matchStatusLabel(row.status), color) },
    ) {
        Text(row.explanation, style = MaterialTheme.typography.bodySmall)
        if (row.resume_evidence.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            Surface(
                shape = RoundedCornerShape(8.dp),
                color = MaterialTheme.colorScheme.background,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(8.dp), verticalArrangement = Arrangement.spacedBy(3.dp)) {
                    row.resume_evidence.forEach { anchor ->
                        Text(
                            "原文：${anchor.quote}",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
            }
        }
        Spacer(Modifier.height(6.dp))
        Text(
            "💡 准备建议：${row.suggested_focus}",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.primary,
            fontWeight = FontWeight.Medium,
        )
    }
}

data class PlanUiPolicyState(
    val recoveryJob: JobDto?,
    val hasCurrentFailedPlan: Boolean,
    val hasCurrentRunningPlan: Boolean,
    val showStartPlanAction: Boolean,
    val startPlanEnabled: Boolean,
    val showPlanErrorCard: Boolean,
)

object PlanScreenPolicy {
    fun evaluate(
        preparationId: String,
        prep: PreparationDto?,
        job: JobDto?,
        planError: PlanErrorState?,
        loading: Boolean,
    ): PlanUiPolicyState {
        val materialsReady = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null
        val matchReady = prep != null && prep.match_report_id != null
        val ready = materialsReady && matchReady
        val recoveryJob = job?.takeIf { candidate ->
            candidate.kind == "plan" &&
                candidate.target_id == preparationId &&
                (prep == null || candidate.input_revision == prep.revision || candidate.state == "superseded")
        }
        val currentRevisionJob = recoveryJob?.takeIf { candidate ->
            prep != null && candidate.input_revision == prep.revision
        }
        val hasCurrentFailedPlan = currentRevisionJob != null &&
            (currentRevisionJob.state == "failed" || currentRevisionJob.state == "interrupted")
        val hasCurrentRunningPlan = currentRevisionJob != null &&
            (currentRevisionJob.state == "queued" || currentRevisionJob.state == "running")
        val showStartPlanAction = ready && !hasCurrentFailedPlan && !hasCurrentRunningPlan
        val startPlanEnabled = showStartPlanAction && !loading

        val planErrorForCurrent = planError != null &&
            (planError.preparationId == null || planError.preparationId == preparationId) &&
            (planError.inputRevision == null || prep == null || planError.inputRevision == prep.revision)

        val duplicatesRecoveryJob = recoveryJob != null && planError != null && (
            planError.jobId == recoveryJob.id ||
                (
                    planError.jobId == null &&
                        planError.code != "BANK_INSUFFICIENT" &&
                        planError.code == recoveryJob.error?.code &&
                        (recoveryJob.state == "failed" || recoveryJob.state == "interrupted")
                    )
            )
        val showPlanErrorCard = planErrorForCurrent && !duplicatesRecoveryJob

        return PlanUiPolicyState(
            recoveryJob = recoveryJob,
            hasCurrentFailedPlan = hasCurrentFailedPlan,
            hasCurrentRunningPlan = hasCurrentRunningPlan,
            showStartPlanAction = showStartPlanAction,
            startPlanEnabled = startPlanEnabled,
            showPlanErrorCard = showPlanErrorCard,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlanHubScreen(vm: AppViewModel, state: AppState, preparationId: String) {
    var showConsent by rememberSaveable { mutableStateOf(false) }
    var showInterviewConsent by rememberSaveable { mutableStateOf(false) }
    val prep = state.preparation
    val plan = state.plan
    val materialsReady = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null
    val matchReady = prep != null && prep.match_report_id != null
    val ready = materialsReady && matchReady
    val policy = remember(preparationId, prep, state.job, state.planError, state.loading) {
        PlanScreenPolicy.evaluate(preparationId, prep, state.job, state.planError, state.loading)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("计划", fontWeight = FontWeight.SemiBold) },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp),
        ) {
            Spacer(Modifier.height(4.dp))
            PreparationStepHeader(
                currentStep = 3,
                materialsReady = materialsReady,
                matchReady = matchReady,
                planConfirmed = plan?.state == "confirmed",
                onSelectStep = { step ->
                    when (step) {
                        1 -> vm.returnToSetup(preparationId)
                        2 -> if (materialsReady) vm.openMatch(preparationId)
                    }
                },
            )
            Spacer(Modifier.height(10.dp))
            if (plan == null) {
                InfoCard(
                    title = "生成个性化计划",
                    subtitle = "题库与 AI 主问题严格各半（${(prep?.main_question_count ?: 6) / 2}+${(prep?.main_question_count ?: 6) / 2}）",
                    accentColor = MaterialTheme.colorScheme.primary,
                ) {
                    Surface(
                        shape = RoundedCornerShape(10.dp),
                        color = MaterialTheme.colorScheme.background,
                        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                        modifier = Modifier.fillMaxWidth(),
                    ) {
                        Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                            Text(
                                text = if (materialsReady) {
                                    "✓ 1. 简历与 JD 已确认（简历 ${prep?.resume_snapshot_id?.take(8)}… / JD ${prep?.jd_snapshot_id?.take(8)}…）"
                                } else {
                                    "○ 1. 待确认简历与岗位 JD"
                                },
                                style = MaterialTheme.typography.bodySmall,
                                color = if (materialsReady) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.error,
                            )
                            Text(
                                text = if (matchReady) "✓ 2. 已完成当前版本的匹配分析" else "○ 2. 待完成当前版本的匹配分析",
                                style = MaterialTheme.typography.bodySmall,
                                color = if (matchReady) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.error,
                            )
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    if (!ready) {
                        Text(
                            "生成前提：已确认简历与 JD，并完成一次当前版本的匹配分析。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        if (materialsReady && !matchReady) {
                            Spacer(Modifier.height(8.dp))
                            SecondaryAction(
                                text = "前往匹配分析",
                                onClick = { vm.openMatch(preparationId) },
                            )
                        }
                    } else if (policy.hasCurrentFailedPlan) {
                        val failedJob = policy.recoveryJob
                        Text(
                            text = when {
                                failedJob?.retryable == true ->
                                    "当前版本已有失败的计划生成任务，请使用下方「重试生成计划」按钮。"
                                (failedJob?.attempt ?: 0) >= 2 ->
                                    "当前版本的计划生成任务已用完 2 次生成尝试且仍失败，可沿用当前材料新建准备，或修改材料／设置以产生新版本。"
                                else ->
                                    "当前版本的计划生成任务已失败且不可直接重试，可沿用当前材料新建准备，或修改材料／设置以生成新版本。"
                            },
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                        if (failedJob?.retryable != true && materialsReady) {
                            Spacer(Modifier.height(8.dp))
                            SecondaryAction(
                                text = "沿用材料新建准备",
                                enabled = !state.loading,
                                onClick = { vm.redoPreparation(preparationId) },
                            )
                        }
                    } else if (policy.hasCurrentRunningPlan) {
                        Text(
                            text = "当前版本已有计划生成任务正在进行，请查看下方任务进度。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.primary,
                        )
                    } else {
                        PrimaryAction(
                            text = "生成计划",
                            enabled = policy.startPlanEnabled,
                            onClick = { showConsent = true },
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    JobRecoveryActions(
                        job = policy.recoveryJob,
                        onQuery = { vm.queryCurrentJob() },
                        onRetry = { vm.retryCurrentJob() },
                    )
                }
                if (policy.showPlanErrorCard) {
                    state.planError?.let { planError ->
                    Spacer(Modifier.height(10.dp))
                    val insufficient = if (planError.code == "BANK_INSUFFICIENT") {
                        PlanErrorFormatter.parseBankInsufficient(planError.details)
                    } else {
                        null
                    }
                    val friendly = remember(planError) {
                        ErrorDiagnosticsMapper.resolve(
                            code = planError.code,
                            rawMessage = planError.rawServerMessage ?: planError.message,
                            details = planError.details,
                            httpStatus = planError.httpStatus,
                            retryable = planError.retryable,
                            diagnosticContext = planError.diagnosticContext,
                        )
                    }
                    InfoCard(
                        title = if (insufficient != null) "题库覆盖不足" else "无法生成计划：${friendly.userTitle}",
                        subtitle = if (insufficient != null) "题库与 AI 主问题需严格各半" else (planError.code ?: "PLAN_ERROR"),
                        accentColor = MaterialTheme.colorScheme.error,
                    ) {
                        if (insufficient != null) {
                            Text(
                                insufficient.summary,
                                style = MaterialTheme.typography.bodyMedium,
                                fontWeight = FontWeight.SemiBold,
                                color = MaterialTheme.colorScheme.error,
                            )
                            Spacer(Modifier.height(4.dp))
                            Text(insufficient.guidance, style = MaterialTheme.typography.bodySmall)
                            Spacer(Modifier.height(10.dp))
                            SecondaryAction(
                                text = "回到准备检查材料关联",
                                onClick = { vm.returnToSetup(preparationId) },
                            )
                            if (insufficient.feasibleCounts.isNotEmpty()) {
                                Spacer(Modifier.height(6.dp))
                                PrimaryAction(
                                    text = "回到设置选择较少主问题（可选 ${insufficient.feasibleCounts.joinToString("/")} 题）",
                                    onClick = { vm.returnToSetup(preparationId) },
                                )
                            }
                        } else {
                            if (friendly.userExplanation != planError.message) {
                                Text(
                                    friendly.userExplanation,
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurface,
                                )
                                Spacer(Modifier.height(4.dp))
                            }
                            Text(
                                planError.message,
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
                            )
                            friendly.suggestion?.let { hint ->
                                Spacer(Modifier.height(4.dp))
                                Text(
                                    "💡 $hint",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.primary,
                                )
                            }
                            Spacer(Modifier.height(8.dp))
                            if (materialsReady) {
                                SecondaryAction(
                                    text = "沿用材料新建准备",
                                    enabled = !state.loading,
                                    onClick = { vm.redoPreparation(preparationId) },
                                )
                                Spacer(Modifier.height(6.dp))
                            }
                            SecondaryAction(
                                text = "回到准备检查材料或设置",
                                onClick = { vm.returnToSetup(preparationId) },
                            )
                        }
                        Spacer(Modifier.height(6.dp))
                        DeveloperDiagnosticPanel(info = friendly)
                    }
                    }
                }
            } else {
                PlanSummaryCard(plan)
                Spacer(Modifier.height(10.dp))
                if (plan.state == "ready") {
                    PrimaryAction(
                        text = "确认并保存计划",
                        enabled = !state.loading,
                        onClick = { vm.confirmPlan(plan.id) },
                    )
                } else {
                    InfoCard(
                        title = "已保存",
                        subtitle = "该计划已确认，可开始一场正式文字面试",
                        accentColor = MaterialTheme.colorScheme.secondary,
                        headerTrailing = { StatusChip("第 4 步已解锁", MaterialTheme.colorScheme.secondary) },
                    ) {
                        val existing = state.interviewForPlan
                        Text(
                            if (existing == null) {
                                "本场会把你的回答及必要上下文发送给配置的 AI，用于追问判定与面后评分；需要在本场单独确认。"
                            } else {
                                "本计划已有一场面试：${existing.status} · 已答 ${existing.answered_main_count}/${existing.main_question_count}。"
                            },
                            style = MaterialTheme.typography.bodySmall,
                        )
                        if (state.plan?.limitations?.any { it.startsWith("测试数据流程") } == true) {
                            Spacer(Modifier.height(6.dp))
                            Text(
                                "合成环境：本场使用开发样板岗位与题库，仅用于联调；是否使用真实模型以服务端任务记录为准。",
                                style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.error,
                            )
                        }
                        Spacer(Modifier.height(8.dp))
                        when {
                            existing == null -> PrimaryAction(
                                text = "开始文字面试",
                                enabled = !state.loading,
                                onClick = { showInterviewConsent = true },
                            )
                            existing.status == "ended" -> PrimaryAction(
                                text = "查看本场报告与问答",
                                onClick = { vm.openInterviewReport(existing.session_id) },
                            )
                            else -> PrimaryAction(
                                text = "继续文字面试",
                                onClick = { vm.openInterview(existing.session_id) },
                            )
                        }
                        Spacer(Modifier.height(6.dp))
                        SecondaryAction(
                            text = "查看面试记录",
                            onClick = { vm.openInterviewHistory() },
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    SecondaryAction(text = "基于当前材料重新制定", onClick = { vm.redoPreparation(preparationId) })
                }
                Spacer(Modifier.height(8.dp))
                SecondaryAction(text = "刷新摘要（只读，不重新生成）", onClick = {
                    vm.refreshPlan(preparationId)
                })
            }
            Spacer(Modifier.height(24.dp))
        }
    }

    if (showConsent) {
        ConsentDialog(
            title = "确认生成计划？",
            body = "服务端会先校验题库审核资格与去重，再把材料文本发送给配置的外部模型生成一半主问题。" +
                "生成不会展示题干与答案。",
            onConfirm = {
                showConsent = false
                vm.generatePlan(preparationId)
            },
            onDismiss = { showConsent = false },
        )
    }

    if (showInterviewConsent && plan != null) {
        ConsentDialog(
            title = "确认开始本场文字面试？",
            body = "你的回答及必要上下文会由配置的 AI 处理，用于追问判定与面后评分。" +
                "本场独立确认；开始后可暂停、跳过或提前结束，结束后可查看带原话证据的报告。",
            confirmLabel = "同意并开始",
            onConfirm = {
                showInterviewConsent = false
                vm.startInterviewForPlan(preparationId, plan)
            },
            onDismiss = { showInterviewConsent = false },
        )
    }
}

@Composable
private fun PlanSummaryCard(plan: PublicPlanDto) {
    InfoCard(
        title = "计划摘要",
        subtitle = "计划 ${plan.id.take(8)}… · revision ${plan.revision}",
        accentColor = if (plan.state == "confirmed") MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.primary,
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(if (plan.state == "confirmed") "已确认" else "待确认", MaterialTheme.colorScheme.primary)
            StatusChip("题库 ${plan.source_counts.bank} + 生成 ${plan.source_counts.generated}", MaterialTheme.colorScheme.secondary)
        }
        Spacer(Modifier.height(8.dp))
        Surface(
            shape = RoundedCornerShape(10.dp),
            color = MaterialTheme.colorScheme.background,
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Column(Modifier.padding(10.dp)) {
                KeyValueRow("岗位", "${roleDisplayName(plan.role_id)} (${plan.role_id})")
                KeyValueRow("级别", levelLabel(plan.level))
                KeyValueRow("题量", "${plan.main_question_count} 题")
                KeyValueRow("准备版本", "revision ${plan.input_revision}")
                KeyValueRow("创建时间", plan.created_at)
                KeyValueRow("确认时间", plan.confirmed_at ?: "尚未确认")
            }
        }
        if (plan.focus_areas.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("能力重点", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
            plan.focus_areas.forEach { Text("• $it", style = MaterialTheme.typography.bodySmall) }
        }
        if (plan.limitations.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            Text("说明与限制", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
            plan.limitations.forEach {
                Text(
                    "• $it",
                    style = MaterialTheme.typography.bodySmall,
                    color = if (it.startsWith("测试数据流程")) MaterialTheme.colorScheme.error
                    else MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Spacer(Modifier.height(8.dp))
        Text(
            "本页不展示题干、参考答案与评分细则；这些内容只在后续面试阶段由服务端内部读取。",
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.outline,
        )
    }
}
