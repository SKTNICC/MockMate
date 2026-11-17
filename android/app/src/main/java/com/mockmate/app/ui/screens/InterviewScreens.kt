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
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewDimensionResultDto
import com.mockmate.app.core.network.InterviewHistoryItemDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewReportQuestionDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private fun interviewStatusLabel(status: String): String = when (status) {
    "active" -> "进行中"
    "paused" -> "已暂停"
    "ended" -> "已结束"
    else -> status
}

private fun reportStateLabel(state: String): String = when (state) {
    "not_requested" -> "未生成"
    "pending" -> "生成中"
    "failed" -> "生成失败"
    "ready" -> "已就绪"
    else -> state
}

private fun qualityLabel(quality: String): String = when (quality) {
    "full" -> "完整评分"
    "partial" -> "部分受限"
    "no_answers" -> "无可评分回答"
    "unscored" -> "评分未完成"
    else -> quality
}

private fun outcomeLabel(outcome: String): String = when (outcome) {
    "answered" -> "已答"
    "skipped" -> "已跳过"
    "unanswered" -> "未答"
    "not_reached" -> "未触达"
    else -> outcome
}

private fun verdictLabel(verdict: String): String = when (verdict) {
    "scored" -> "已评分"
    "insufficient_evidence" -> "证据不足"
    "uncertain" -> "口径受限"
    else -> verdict
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InterviewHistoryScreen(vm: AppViewModel, state: AppState) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("面试记录") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
                actions = { TextButton(onClick = { vm.loadInterviewHistory(false) }) { Text("刷新") } },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Spacer(Modifier.height(4.dp))
                Text(
                    "按开场时间倒序。原准备或计划被删除后，旧场仍可按 sessionId 查阅。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
            }
            if (state.interviewHistory.isEmpty() && !state.interviewHistoryLoading) {
                item { EmptyHint("还没有面试记录。确认计划后可从计划页开始文字面试。") }
            }
            items(state.interviewHistory, key = { it.session_id }) { item ->
                InterviewHistoryCard(
                    item = item,
                    onOpen = {
                        if (item.status == "ended") {
                            vm.openInterviewReport(item.session_id)
                        } else {
                            vm.openInterview(item.session_id)
                        }
                    },
                )
            }
            item {
                if (state.interviewHistoryCursor != null) {
                    SecondaryAction(
                        text = if (state.interviewHistoryLoading) "加载中…" else "加载更多",
                        enabled = !state.interviewHistoryLoading,
                        onClick = { vm.loadInterviewHistory(true) },
                    )
                }
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

@Composable
private fun InterviewHistoryCard(item: InterviewHistoryItemDto, onOpen: () -> Unit) {
    InfoCard(
        title = "${item.role_id} · ${levelLabel(item.level)} · ${item.main_question_count} 题",
        subtitle = "开场 ${item.started_at}",
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(interviewStatusLabel(item.status), MaterialTheme.colorScheme.primary)
            StatusChip("已答 ${item.answered_main_count}/${item.main_question_count}")
            StatusChip("报告：${reportStateLabel(item.report_state)}", MaterialTheme.colorScheme.secondary)
        }
        if (item.end_reason != null) {
            Spacer(Modifier.height(6.dp))
            Text(
                if (item.end_reason == "early_finish") "提前结束" else "正常收尾",
                style = MaterialTheme.typography.bodySmall,
            )
        }
        Spacer(Modifier.height(8.dp))
        PrimaryAction(
            text = if (item.status == "ended") "查看报告与问答" else "回到面试",
            onClick = onOpen,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InterviewRoomScreen(vm: AppViewModel, state: AppState, sessionId: String) {
    val detail = state.interviewDetail?.takeIf { it.session_id == sessionId }
    val listState = rememberLazyListState()
    val scope = rememberCoroutineScope()
    var text by rememberSaveable(sessionId) { mutableStateOf("") }
    var loadedTurnId by rememberSaveable(sessionId) { mutableStateOf<String?>(null) }
    var showSkip by rememberSaveable { mutableStateOf(false) }
    var showFinish by rememberSaveable { mutableStateOf(false) }

    val turn = detail?.current_turn
    LaunchedEffect(turn?.turn_id) {
        if (turn != null && loadedTurnId != turn.turn_id) {
            loadedTurnId = turn.turn_id
            text = vm.interviewDraft(sessionId, turn.turn_id) ?: ""
        }
    }
    LaunchedEffect(text, turn?.turn_id) {
        val current = turn ?: return@LaunchedEffect
        if (text.isNotEmpty()) {
            delay(600)
            vm.saveInterviewDraft(sessionId, current.turn_id, text)
        }
    }
    val latestText by rememberUpdatedState(text)
    DisposableEffect(turn?.turn_id) {
        onDispose {
            turn?.let { vm.saveInterviewDraft(sessionId, it.turn_id, latestText) }
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("文字模拟面试") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
                actions = { TextButton(onClick = { vm.loadInterview(sessionId) }) { Text("刷新") } },
            )
        },
    ) { padding ->
        if (detail == null) {
            Column(Modifier.fillMaxSize().padding(padding)) {
                EmptyHint(if (state.loading) "正在加载面试…" else "面试尚未加载，请刷新")
                SecondaryAction("重新加载", onClick = { vm.loadInterview(sessionId) })
            }
            return@Scaffold
        }
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Spacer(Modifier.height(4.dp))
                InfoCard(
                    title = "本场说明",
                    subtitle = "来源 ${detail.plan_summary.source_counts.bank} 题库 + ${detail.plan_summary.source_counts.generated} 生成",
                ) {
                    Text(detail.opening_text, style = MaterialTheme.typography.bodySmall)
                    if (detail.plan_summary.sample_only) {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "测试样板：本场使用开发样板岗位与测试 provider，不代表真实 AI 或人工审核结果。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        StatusChip(interviewStatusLabel(detail.status), MaterialTheme.colorScheme.primary)
                        StatusChip("已答 ${detail.answered_main_count}/${detail.plan_summary.main_question_count}")
                        if (detail.skipped_main_count > 0) {
                            StatusChip("跳过 ${detail.skipped_main_count}", MaterialTheme.colorScheme.tertiary)
                        }
                        StatusChip("报告：${reportStateLabel(detail.report_state)}", MaterialTheme.colorScheme.secondary)
                    }
                }
            }
            if (detail.transcript.isEmpty()) {
                item { EmptyHint("还没有已曝光的回合。") }
            }
            items(detail.transcript, key = { it.turn_id }) { item ->
                TurnCard(item)
            }
            item {
                when {
                    detail.status == "ended" -> EndedCard(vm, detail, sessionId)
                    detail.phase == "deciding_follow_up" -> DecidingCard(vm, detail, sessionId)
                    turn != null -> AnswerCard(
                        detail = detail,
                        turn = turn,
                        text = text,
                        pending = state.pendingInterviewTurnId == turn.turn_id,
                        onTextChange = { next ->
                            if (next.codePointCount(0, next.length) <= 4000) text = next
                        },
                        onSubmit = { vm.submitInterviewAnswer(sessionId, text) },
                        onReview = {
                            // 无写请求：滚动到当前题；当前题卡片同时保留完整题干。
                            scope.launch {
                                listState.animateScrollToItem(1 + detail.transcript.size)
                            }
                        },
                    )
                    else -> EmptyHint("当前没有可作答的回合，请刷新。")
                }
            }
            item {
                Spacer(Modifier.height(4.dp))
                InterviewControls(
                    vm = vm,
                    detail = detail,
                    onSkip = { showSkip = true },
                    onFinish = { showFinish = true },
                )
                Spacer(Modifier.height(24.dp))
            }
        }
    }

    if (showSkip) {
        ConsentDialog(
            title = "跳过当前题？",
            body = if (turn?.kind == "follow_up") {
                "跳过追问会保留已提交的主回答，并结清该主问题。"
            } else {
                "跳过后该主问题记作 skipped，会降低报告覆盖度。"
            },
            confirmLabel = "确认跳过",
            onConfirm = {
                showSkip = false
                vm.skipInterviewTurn(sessionId)
            },
            onDismiss = { showSkip = false },
        )
    }
    if (showFinish) {
        ConsentDialog(
            title = "提前结束本场面试？",
            body = "结束后问答锁定，未回答的回合记作未答，未来主问题记作未触达；报告会按真实覆盖度生成。",
            confirmLabel = "结束并生成报告",
            onConfirm = {
                showFinish = false
                vm.finishInterview(sessionId)
            },
            onDismiss = { showFinish = false },
        )
    }
}

@Composable
private fun TurnCard(turn: InterviewTurnDto) {
    InfoCard(
        title = if (turn.kind == "follow_up") {
            "追问 · 第 ${turn.position} 题"
        } else {
            "第 ${turn.position} 题"
        },
        subtitle = when (turn.status) {
            "pending" -> "当前待作答"
            "answered" -> "已提交"
            "skipped" -> "已跳过"
            else -> "未答"
        },
    ) {
        Text(turn.prompt, style = MaterialTheme.typography.bodyMedium)
        turn.answer?.let { answer ->
            Spacer(Modifier.height(8.dp))
            Text(
                "我的回答",
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.SemiBold,
            )
            Text(answer.text, style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun AnswerCard(
    detail: InterviewDetailDto,
    turn: InterviewTurnDto,
    text: String,
    pending: Boolean,
    onTextChange: (String) -> Unit,
    onSubmit: () -> Unit,
    onReview: () -> Unit,
) {
    val active = detail.status == "active"
    InfoCard(
        title = if (turn.kind == "follow_up") "追问" else "第 ${turn.position}/${detail.plan_summary.main_question_count} 题",
        subtitle = when {
            pending -> "正在确认提交结果；可离页，恢复后会按原 key 重放"
            active -> "提交后先保存回答，再准备下一步"
            else -> "面试已暂停，请先继续"
        },
    ) {
        Text(turn.prompt, style = MaterialTheme.typography.bodyMedium)
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = text,
            onValueChange = onTextChange,
            modifier = Modifier.fillMaxWidth(),
            enabled = active && !pending,
            minLines = 5,
            maxLines = 12,
            keyboardOptions = KeyboardOptions(
                keyboardType = KeyboardType.Text,
                imeAction = ImeAction.Default,
            ),
            placeholder = { Text("用中文写下你的回答；换行和表情都会保留。") },
        )
        Spacer(Modifier.height(4.dp))
        Text(
            "${text.codePointCount(0, text.length)}/4000 字符",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        PrimaryAction(
            text = if (pending) "正在确认提交结果…" else "提交回答",
            enabled = active && text.isNotBlank() && !pending,
            onClick = onSubmit,
        )
        Spacer(Modifier.height(6.dp))
        SecondaryAction(text = "重看本题（不写入）", onClick = onReview)
    }
}

@Composable
private fun DecidingCard(vm: AppViewModel, detail: InterviewDetailDto, sessionId: String) {
    val job = detail.follow_up_job
    InfoCard(title = "回答已保存，正在准备下一步", subtitle = "追问判定中；不会重复提交回答") {
        if (job != null) {
            KeyValueRow("任务", job.id.take(8) + "…")
            KeyValueRow("状态", job.state)
            job.error_code?.let { KeyValueRow("错误", it) }
        }
        if (detail.status == "paused") {
            Spacer(Modifier.height(6.dp))
            Text("面试已暂停；可继续、重试失败的判定或保留主回答继续。", style = MaterialTheme.typography.bodySmall)
        }
        Spacer(Modifier.height(8.dp))
        if (detail.allowed_actions.contains("retry_follow_up") && job != null) {
            SecondaryAction(
                text = "重试追问判定（可能产生新的模型调用）",
                onClick = { vm.retryInterviewJob(sessionId, job.id, job.attempt) },
            )
            Spacer(Modifier.height(6.dp))
        }
        if (detail.allowed_actions.contains("continue_without_follow_up")) {
            SecondaryAction(
                text = "保留主回答，跳过追问继续",
                onClick = { vm.continueWithoutFollowUp(sessionId) },
            )
        }
        if (!detail.allowed_actions.contains("retry_follow_up") &&
            !detail.allowed_actions.contains("continue_without_follow_up")
        ) {
            Text("任务仍在处理中；离开页面不会丢失，回场后会继续查询。", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun EndedCard(vm: AppViewModel, detail: InterviewDetailDto, sessionId: String) {
    InfoCard(
        title = if (detail.end_reason == "early_finish") "已提前结束" else "面试已收尾",
        subtitle = "报告状态：${reportStateLabel(detail.report_state)}",
    ) {
        detail.closing_text?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        Spacer(Modifier.height(8.dp))
        when (detail.report_state) {
            "ready" -> PrimaryAction(
                text = "查看报告与逐题证据",
                onClick = { vm.openInterviewReport(sessionId) },
            )
            "pending" -> {
                Text("报告生成中；已有回答与任务已持久化。", style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                SecondaryAction("刷新报告状态", onClick = { vm.loadInterview(sessionId) })
            }
            "failed" -> {
                Text("评分未完成。可重试评分任务，或生成保留原文与覆盖度的基础报告。", style = MaterialTheme.typography.bodySmall)
                detail.report_job?.let { job ->
                    if (detail.allowed_actions.contains("retry_report")) {
                        Spacer(Modifier.height(6.dp))
                        SecondaryAction(
                            text = "重试评分任务",
                            onClick = { vm.retryInterviewJob(sessionId, job.id, job.attempt) },
                        )
                    }
                    if (detail.allowed_actions.contains("create_basic_report")) {
                        Spacer(Modifier.height(6.dp))
                        SecondaryAction(
                            text = "生成基础报告（评分未完成）",
                            onClick = { vm.createBasicInterviewReport(sessionId) },
                        )
                    }
                }
            }
            else -> Text("本场没有可用的报告。", style = MaterialTheme.typography.bodySmall)
        }
    }
}

@Composable
private fun InterviewControls(
    vm: AppViewModel,
    detail: InterviewDetailDto,
    onSkip: () -> Unit,
    onFinish: () -> Unit,
) {
    Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.fillMaxWidth()) {
        if (detail.allowed_actions.contains("pause")) {
            TextButton(onClick = { vm.pauseInterview(detail.session_id) }) { Text("暂停") }
        }
        if (detail.allowed_actions.contains("resume")) {
            TextButton(onClick = { vm.resumeInterview(detail.session_id) }) { Text("继续") }
        }
        if (detail.allowed_actions.contains("skip")) {
            TextButton(onClick = onSkip) { Text("跳过本题") }
        }
        if (detail.allowed_actions.contains("finish")) {
            TextButton(onClick = onFinish) { Text("提前结束") }
        }
        if (detail.status == "ended") {
            TextButton(onClick = { vm.openInterviewReport(detail.session_id) }) { Text("报告") }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InterviewReportScreen(vm: AppViewModel, state: AppState, sessionId: String) {
    val report = state.interviewReport?.takeIf { it.session_id == sessionId }
    var selectedAnswer by rememberSaveable(sessionId) { mutableStateOf<String?>(null) }
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("面试报告") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
                actions = {
                    TextButton(onClick = { vm.loadInterviewReport(sessionId) }) { Text("刷新") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            if (report == null) {
                item {
                    EmptyHint(state.interviewReportError ?: "报告尚未加载。")
                    SecondaryAction("重新加载", onClick = { vm.loadInterviewReport(sessionId) })
                }
                val detail = state.interviewDetail?.takeIf { it.session_id == sessionId }
                if (detail != null) {
                    item { UnavailableReportCard(vm, detail, sessionId) }
                    items(detail.transcript, key = { it.turn_id }) { turn -> TurnCard(turn) }
                }
                return@LazyColumn
            }
            item {
                Spacer(Modifier.height(4.dp))
                InfoCard(
                    title = "报告概览",
                    subtitle = "报告 ${report.report_id.take(8)}… · ${report.created_at}",
                ) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        StatusChip(qualityLabel(report.quality), MaterialTheme.colorScheme.primary)
                        StatusChip(
                            if (report.end_reason == "early_finish") "提前结束" else "正常收尾",
                            MaterialTheme.colorScheme.secondary,
                        )
                        StatusChip("M2 无整场总分", MaterialTheme.colorScheme.tertiary)
                    }
                    Spacer(Modifier.height(8.dp))
                    KeyValueRow("岗位", "${report.role_id} · ${levelLabel(report.level)}")
                    report.material_versions.forEach {
                        KeyValueRow("材料", "${it.kind} · ${it.title} · v${it.source_revision}")
                    }
                }
            }
            item {
                InfoCard(title = "覆盖度", subtitle = "计数与来源统计由后端产出") {
                    KeyValueRow("计划主问题", "${report.coverage.planned}")
                    KeyValueRow("已曝光", "${report.coverage.presented}")
                    KeyValueRow("已答", "${report.coverage.answered}")
                    KeyValueRow("跳过", "${report.coverage.skipped}")
                    KeyValueRow("未答", "${report.coverage.unanswered}")
                    KeyValueRow("未触达", "${report.coverage.not_reached}")
                    KeyValueRow("已评分", "${report.coverage.scored}")
                    KeyValueRow(
                        "来源",
                        "题库 ${report.coverage.bank_answered}/${report.coverage.bank_planned} · " +
                            "生成 ${report.coverage.generated_answered}/${report.coverage.generated_planned}",
                    )
                    KeyValueRow(
                        "追问",
                        "已曝光 ${report.coverage.follow_up_presented} · 已答 ${report.coverage.follow_up_answered}",
                    )
                }
            }
            if (report.strengths.isNotEmpty()) {
                item {
                    InfoCard(title = "优势（引用题目与原话）") {
                        report.strengths.forEach { finding ->
                            Text("• ${finding.text}", style = MaterialTheme.typography.bodySmall)
                            finding.evidence.forEach { evidence ->
                                TextButton(onClick = { selectedAnswer = evidence.answer_id }) {
                                    Text("原话：${evidence.quote}")
                                }
                            }
                        }
                    }
                }
            }
            if (report.improvements.isNotEmpty()) {
                item {
                    InfoCard(title = "待改进与练习方向") {
                        report.improvements.forEach { finding ->
                            Text("• ${finding.text}", style = MaterialTheme.typography.bodySmall)
                            finding.evidence.forEach { evidence ->
                                TextButton(onClick = { selectedAnswer = evidence.answer_id }) {
                                    Text("原话：${evidence.quote}")
                                }
                            }
                        }
                    }
                }
            }
            items(report.questions, key = { it.question_id }) { question ->
                ReportQuestionCard(question, selectedAnswer) { selectedAnswer = it }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

@Composable
private fun UnavailableReportCard(
    vm: AppViewModel,
    detail: InterviewDetailDto,
    sessionId: String,
) {
    InfoCard(
        title = "报告状态：${reportStateLabel(detail.report_state)}",
        subtitle = "评分未完成时仍可查看本场原答卷与允许的恢复操作",
    ) {
        detail.closing_text?.let { Text(it, style = MaterialTheme.typography.bodySmall) }
        Spacer(Modifier.height(6.dp))
        Text(
            "未完成的评分片段不会展示；问答与覆盖度来自已持久化的本场记录。",
            style = MaterialTheme.typography.bodySmall,
        )
        val job = detail.report_job
        if (job != null && detail.report_state == "failed") {
            if (detail.allowed_actions.contains("retry_report")) {
                Spacer(Modifier.height(6.dp))
                SecondaryAction(
                    text = "重试评分任务",
                    onClick = { vm.retryInterviewJob(sessionId, job.id, job.attempt) },
                )
            }
            if (detail.allowed_actions.contains("create_basic_report")) {
                Spacer(Modifier.height(6.dp))
                SecondaryAction(
                    text = "生成基础报告（评分未完成）",
                    onClick = { vm.createBasicInterviewReport(sessionId) },
                )
            }
        }
    }
}

@Composable
private fun ReportQuestionCard(
    question: InterviewReportQuestionDto,
    selectedAnswer: String?,
    onSelectAnswer: (String) -> Unit,
) {
    InfoCard(
        title = "第 ${question.position} 题 · ${if (question.source == "bank") "题库" else "生成"} · ${outcomeLabel(question.outcome)}",
        subtitle = "可信度 ${question.assessment_trust} · 量表 ${question.rubric_version}",
    ) {
        question.prompt?.let { Text(it, style = MaterialTheme.typography.bodyMedium) }
        question.question_score?.let {
            Spacer(Modifier.height(4.dp))
            Text("单题分：$it / 100", style = MaterialTheme.typography.bodySmall, fontWeight = FontWeight.SemiBold)
        }
        if (question.turns.isNotEmpty()) {
            Spacer(Modifier.height(8.dp))
            question.turns.forEach { turn ->
                Text(
                    if (turn.kind == "follow_up") "追问：${turn.prompt}" else "主问题：${turn.prompt}",
                    style = MaterialTheme.typography.bodySmall,
                )
                turn.answer?.let { answer ->
                    val highlighted = answer.answer_id == selectedAnswer
                    Text(
                        answer.text,
                        style = MaterialTheme.typography.bodySmall,
                        color = if (highlighted) MaterialTheme.colorScheme.primary
                        else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                Spacer(Modifier.height(4.dp))
            }
        }
        if (question.dimensions.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            Text("维度结果", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
            question.dimensions.forEach { dim -> DimensionRow(dim, onSelectAnswer) }
        }
        question.follow_up_outcome?.let {
            Spacer(Modifier.height(4.dp))
            Text("追问结果：$it", style = MaterialTheme.typography.labelSmall)
        }
    }
}

@Composable
private fun DimensionRow(
    dim: InterviewDimensionResultDto,
    onSelectAnswer: (String) -> Unit,
) {
    Column(Modifier.fillMaxWidth().padding(vertical = 4.dp)) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(
                dim.name,
                if (dim.verdict == "scored") MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.tertiary,
            )
            StatusChip(verdictLabel(dim.verdict))
            dim.score?.let { StatusChip("$it/4") }
        }
        if (dim.explanation.isNotBlank()) {
            Text(dim.explanation, style = MaterialTheme.typography.bodySmall)
        }
        if (dim.improvement.isNotBlank()) {
            Text(
                "改进：${dim.improvement}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        dim.evidence.forEach { evidence ->
            TextButton(onClick = { onSelectAnswer(evidence.answer_id) }) {
                Text("证据：${evidence.quote}")
            }
        }
    }
}
