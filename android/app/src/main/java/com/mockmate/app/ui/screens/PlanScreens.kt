package com.mockmate.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AssistChip
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.MatchRowDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.JobRecoveryActions
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.MessageBanner
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip

private fun matchStatusLabel(status: String): String = when (status) {
    "supported" -> "有原文支持"
    "to_verify" -> "待验证"
    "not_evidenced" -> "未体现"
    else -> status
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MatchScreen(vm: AppViewModel, state: AppState, preparationId: String) {
    var showConsent by rememberSaveable { mutableStateOf(false) }
    val prep = state.preparation
    val report = state.matchReport
    val ready = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("匹配分析") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Spacer(Modifier.height(4.dp))
                InfoCard(
                    title = "要求 → 支持原文 → 待验证/未体现",
                    subtitle = "结论只基于已确认材料；不预测录用概率，不使用百分比",
                ) {
                    Text(
                        if (ready) "匹配只读取当前已确认的简历与 JD 版本。" else "请先绑定并确认简历与 JD。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    PrimaryAction(
                        text = if (report == null) "开始匹配分析" else "重新分析",
                        enabled = ready && !state.loading,
                        onClick = { showConsent = true },
                    )
                    if (report != null) {
                        Spacer(Modifier.height(8.dp))
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
                    job = state.job,
                    onQuery = { vm.queryCurrentJob() },
                    onRetry = { vm.retryCurrentJob() },
                )
            }
            if (report == null) {
                item { EmptyHint("还没有匹配结果。") }
            } else {
                items(report.rows, key = { it.requirement_id }) { row -> MatchRowCard(row) }
            }
            item { Spacer(Modifier.height(12.dp)) }
            item {
                if (report != null) {
                    SecondaryAction(text = "查看/生成计划", onClick = { vm.openPlan(preparationId) })
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
        "supported" -> MaterialTheme.colorScheme.primary
        "to_verify" -> MaterialTheme.colorScheme.tertiary
        else -> MaterialTheme.colorScheme.error
    }
    InfoCard(title = "要求：${row.requirement_id}") {
        StatusChip(matchStatusLabel(row.status), color)
        Spacer(Modifier.height(6.dp))
        Text(row.explanation, style = MaterialTheme.typography.bodySmall)
        if (row.resume_evidence.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            row.resume_evidence.forEach { anchor ->
                Text(
                    "原文：${anchor.quote}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        Spacer(Modifier.height(6.dp))
        Text("准备建议：${row.suggested_focus}", style = MaterialTheme.typography.bodySmall)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PlanHubScreen(vm: AppViewModel, state: AppState, preparationId: String) {
    var showConsent by rememberSaveable { mutableStateOf(false) }
    val prep = state.preparation
    val plan = state.plan
    val ready = prep != null && prep.resume_snapshot_id != null && prep.jd_snapshot_id != null && prep.match_report_id != null

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("计划") },
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
            if (plan == null) {
                InfoCard(
                    title = "生成个性化计划",
                    subtitle = "题库与 AI 主问题严格各半（${(prep?.main_question_count ?: 6) / 2}+${(prep?.main_question_count ?: 6) / 2}）",
                ) {
                    if (!ready) {
                        Text(
                            "生成前提：已确认简历与 JD，并完成一次当前版本的匹配分析。",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    } else {
                        Text(
                            "材料版本：简历 ${prep?.resume_snapshot_id?.take(8)}… / JD ${prep?.jd_snapshot_id?.take(8)}…",
                            style = MaterialTheme.typography.bodySmall,
                        )
                        Spacer(Modifier.height(8.dp))
                        PrimaryAction(
                            text = "生成计划",
                            enabled = !state.loading,
                            onClick = { showConsent = true },
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    JobRecoveryActions(
                        job = state.job,
                        onQuery = { vm.queryCurrentJob() },
                        onRetry = { vm.retryCurrentJob() },
                    )
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
                    InfoCard(title = "已保存", subtitle = "计划已准备好，面试功能后续阶段开放") {
                        Text(
                            "该计划已确认保存。退出、杀进程或重新登录后，可从准备列表重新打开同一计划。",
                            style = MaterialTheme.typography.bodySmall,
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    SecondaryAction(text = "基于当前材料重新制定", onClick = { vm.redoPreparation() })
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
}

@Composable
private fun PlanSummaryCard(plan: PublicPlanDto) {
    InfoCard(
        title = "计划摘要",
        subtitle = "计划 ${plan.id.take(8)}… · revision ${plan.revision}",
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(if (plan.state == "confirmed") "已确认" else "待确认", MaterialTheme.colorScheme.primary)
            StatusChip("题库 ${plan.source_counts.bank} + 生成 ${plan.source_counts.generated}", MaterialTheme.colorScheme.secondary)
        }
        Spacer(Modifier.height(8.dp))
        KeyValueRow("岗位", plan.role_id)
        KeyValueRow("级别", levelLabel(plan.level))
        KeyValueRow("题量", "${plan.main_question_count} 题")
        KeyValueRow("准备版本", "revision ${plan.input_revision}")
        KeyValueRow("创建时间", plan.created_at)
        KeyValueRow("确认时间", plan.confirmed_at ?: "尚未确认")
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
