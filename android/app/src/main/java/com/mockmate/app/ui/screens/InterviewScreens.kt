package com.mockmate.app.ui.screens

import android.Manifest
import android.content.pm.PackageManager
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.SpanStyle
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.text.TextStyle
import androidx.compose.ui.text.buildAnnotatedString
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.withStyle
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.audio.VoicePhase
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewDimensionResultDto
import com.mockmate.app.core.network.InterviewHistoryItemDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewReportQuestionDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.ErrorDiagnosticsMapper
import com.mockmate.app.ui.LocalInterviewDraft
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.DeveloperDiagnosticPanel
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.first
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
                item { EmptyHint("还没有面试记录。确认计划后可从计划页开始语音或文字面试。") }
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
            StatusChip(if (item.mode == "voice") "语音场" else "文字场", MaterialTheme.colorScheme.secondary)
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
    val clipboard = LocalClipboardManager.current
    val context = LocalContext.current
    val micPermission = rememberLauncherForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted ->
        if (granted) vm.startVoiceRecording(sessionId) else vm.onVoicePermissionDenied()
    }
    val requestMicrophone: () -> Unit = {
        if (context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        ) {
            vm.startVoiceRecording(sessionId)
        } else {
            micPermission.launch(Manifest.permission.RECORD_AUDIO)
        }
    }
    var text by rememberSaveable(sessionId) { mutableStateOf("") }
    var loadedTurnId by rememberSaveable(sessionId) { mutableStateOf<String?>(null) }
    var showSkip by rememberSaveable { mutableStateOf(false) }
    var showFinish by rememberSaveable { mutableStateOf(false) }

    val turn = detail?.current_turn
    val draftReady = turn != null && loadedTurnId == turn.turn_id
    LaunchedEffect(turn?.turn_id) {
        if (turn != null && loadedTurnId != turn.turn_id) {
            loadedTurnId = turn.turn_id
            text = vm.interviewDraft(sessionId, turn.turn_id) ?: ""
        }
    }
    LaunchedEffect(text, turn?.turn_id, draftReady) {
        val current = turn ?: return@LaunchedEffect
        if (draftReady && text.isNotEmpty()) {
            delay(600)
            vm.saveInterviewDraft(sessionId, current.turn_id, text)
        }
    }
    val latestText by rememberUpdatedState(text)
    DisposableEffect(turn?.turn_id, draftReady) {
        onDispose {
            // ViewModel 会拒绝把已确认提交的回合重新写回草稿。
            if (draftReady) {
                turn?.let { vm.saveInterviewDraft(sessionId, it.turn_id, latestText) }
            }
        }
    }

    LaunchedEffect(detail?.mode, detail?.status, turn?.turn_id, state.voicePhase) {
        if (detail?.mode == "voice" && detail.status == "active" && turn != null &&
            detail.phase == "awaiting_answer"
        ) {
            vm.ensureVoicePrompt(sessionId, turn.turn_id)
        }
    }
    DisposableEffect(Unit) {
        onDispose { vm.stopVoicePlayback() }
    }

    val pendingTurnId = state.pendingInterviewTurnId
    val pendingNotCurrent = pendingTurnId != null && pendingTurnId != turn?.turn_id
    val orphanDrafts = state.interviewOrphanDrafts
    val showNotice = pendingNotCurrent || orphanDrafts.isNotEmpty()
    val transcript = detail?.transcript.orEmpty()
    val currentAnswerIndex = 1 +
        (if (showNotice) 1 else 0) +
        (if (transcript.isEmpty()) 1 else 0) +
        transcript.size

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (detail?.mode == "voice") "语音模拟面试" else "文字模拟面试") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
                actions = { TextButton(onClick = { vm.loadInterview(sessionId) }) { Text("刷新") } },
            )
        },
    ) { padding ->
        if (detail == null) {
            Column(Modifier.fillMaxSize().padding(padding)) {
                EmptyHint(if (state.loading) "正在加载面试…" else "面试尚未加载，请刷新")
                SecondaryAction("重新加载", onClick = { vm.loadInterview(sessionId) })
                if (pendingNotCurrent) {
                    Spacer(Modifier.height(8.dp))
                    Text(
                        "有上一回合的提交结果未确认；重新加载会先用原 key 重放，不会新建重复操作。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(6.dp))
                    SecondaryAction(
                        "确认提交结果",
                        onClick = { vm.confirmPendingInterviewAnswer(sessionId) },
                    )
                }
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
                    if (detail.mode == "voice") {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "录音只用于整段转写；报告按你确认提交的文字评分，不评价口音、音色或情绪。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                    if (detail.plan_summary.sample_only) {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "测试样板：本场使用开发样板岗位与题库，仅用于联调；是否使用真实模型以服务端任务记录为准。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.error,
                        )
                    }
                    Spacer(Modifier.height(8.dp))
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        StatusChip(if (detail.mode == "voice") "语音场" else "文字场", MaterialTheme.colorScheme.secondary)
                        StatusChip(interviewStatusLabel(detail.status), MaterialTheme.colorScheme.primary)
                        StatusChip("已答 ${detail.answered_main_count}/${detail.plan_summary.main_question_count}")
                        if (detail.skipped_main_count > 0) {
                            StatusChip("跳过 ${detail.skipped_main_count}", MaterialTheme.colorScheme.tertiary)
                        }
                        StatusChip("报告：${reportStateLabel(detail.report_state)}", MaterialTheme.colorScheme.secondary)
                    }
                }
            }
            if (showNotice) {
                item {
                    InterviewRecoveryNotice(
                        pendingNotCurrent = pendingNotCurrent,
                        orphanDrafts = orphanDrafts,
                        onConfirmPending = { vm.confirmPendingInterviewAnswer(sessionId) },
                        onCopy = { clipboard.setText(AnnotatedString(it)) },
                    )
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
                    turn != null && detail.mode == "voice" && !state.voiceTextFallback ->
                        VoiceAnswerCard(
                            state = state,
                            detail = detail,
                            turn = turn,
                            onRequestMicrophone = requestMicrophone,
                            onPlay = { vm.playVoiceSegment(sessionId, "turn", turn.turn_id) },
                            onStopPlayback = { vm.stopVoicePlayback() },
                            onStopRecording = { vm.stopVoiceRecording(sessionId) },
                            onUpload = { vm.uploadVoiceTake(sessionId) },
                            onTranscribeExisting = { vm.transcribeExisting(sessionId) },
                            onRetryTranscription = { vm.retryVoiceTranscription(sessionId) },
                            onReviewChange = { vm.updateVoiceReviewText(it) },
                            onRetryReviewSave = { vm.retryVoiceReviewPersistence() },
                            onSubmit = { vm.submitVoiceReview(sessionId) },
                            onRecordAgain = requestMicrophone,
                            onDiscard = { vm.discardVoiceTake(sessionId) },
                            onUseServerVoice = { vm.useServerVoiceDraft(sessionId) },
                            onTextFallback = { vm.useVoiceTextFallback() },
                        )
                    turn != null -> AnswerCard(
                        detail = detail,
                        turn = turn,
                        text = if (draftReady) text else "",
                        draftReady = draftReady,
                        pending = state.pendingInterviewTurnId == turn.turn_id,
                        voiceFallback = detail.mode == "voice" && state.voiceTextFallback,
                        onLeaveVoiceFallback = { vm.leaveVoiceTextFallback() },
                        onTextChange = { next ->
                            if (next.codePointCount(0, next.length) <= 4000) text = next
                        },
                        onSubmit = {
                            if (draftReady) vm.submitInterviewAnswer(sessionId, text)
                        },
                        onConfirmPending = { vm.confirmPendingInterviewAnswer(sessionId) },
                        onReview = {
                            // 无写请求：滚动到当前题；当前题卡片同时保留完整题干。
                            scope.launch {
                                listState.animateScrollToItem(currentAnswerIndex)
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
    val statusText = when (turn.status) {
        "pending" -> "当前待作答"
        "answered" -> "已提交"
        "skipped" -> "已跳过"
        else -> "未答"
    }
    val statusColor = when (turn.status) {
        "answered" -> MaterialTheme.colorScheme.secondary
        "pending" -> MaterialTheme.colorScheme.primary
        else -> MaterialTheme.colorScheme.tertiary
    }
    InfoCard(
        title = if (turn.kind == "follow_up") {
            "追问 · 第 ${turn.position} 题"
        } else {
            "第 ${turn.position} 题"
        },
        subtitle = statusText,
        headerTrailing = { StatusChip(statusText, statusColor) },
    ) {
        Text(
            text = turn.prompt,
            style = MaterialTheme.typography.bodyMedium,
            fontWeight = FontWeight.Medium,
        )
        turn.answer?.let { answer ->
            Spacer(Modifier.height(8.dp))
            Surface(
                shape = RoundedCornerShape(10.dp),
                color = MaterialTheme.colorScheme.background,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(10.dp)) {
                    Text(
                        "我的回答",
                        style = MaterialTheme.typography.labelLarge,
                        fontWeight = FontWeight.SemiBold,
                        color = MaterialTheme.colorScheme.primary,
                    )
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        StatusChip(
                            when (answer.input_source) {
                                "asr" -> "来源：语音转写（原样提交）"
                                "asr_corrected" -> "来源：语音转写（人工核对）"
                                else -> "来源：文字输入"
                            },
                            MaterialTheme.colorScheme.secondary,
                        )
                    }
                    Spacer(Modifier.height(4.dp))
                    Text(answer.text, style = MaterialTheme.typography.bodySmall)
                }
            }
        }
    }
}

@Composable
private fun InterviewRecoveryNotice(
    pendingNotCurrent: Boolean,
    orphanDrafts: List<LocalInterviewDraft>,
    onConfirmPending: () -> Unit,
    onCopy: (String) -> Unit,
) {
    InfoCard(
        title = "待确认与未提交稿",
        subtitle = "服务端状态以最新详情为准；本机原稿只在本场保留",
        accentColor = MaterialTheme.colorScheme.tertiary,
    ) {
        if (pendingNotCurrent) {
            Text(
                "上一回合的提交结果仍未确认；可用原请求手动重试，不会新建重复回答。",
                style = MaterialTheme.typography.bodySmall,
            )
            Spacer(Modifier.height(6.dp))
            SecondaryAction("确认提交结果", onClick = onConfirmPending)
        }
        orphanDrafts.forEach { draft ->
            Spacer(Modifier.height(8.dp))
            Text(
                draft.prompt?.let { "原回合：$it" } ?: "原回合（已不在当前题）",
                style = MaterialTheme.typography.labelLarge,
                fontWeight = FontWeight.SemiBold,
            )
            Text(draft.text, style = MaterialTheme.typography.bodySmall)
            TextButton(onClick = { onCopy(draft.text) }) { Text("复制本机原稿") }
        }
    }
}

@Composable
private fun AnswerCard(
    detail: InterviewDetailDto,
    turn: InterviewTurnDto,
    text: String,
    draftReady: Boolean,
    pending: Boolean,
    voiceFallback: Boolean = false,
    onLeaveVoiceFallback: () -> Unit = {},
    onTextChange: (String) -> Unit,
    onSubmit: () -> Unit,
    onConfirmPending: () -> Unit,
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
        accentColor = MaterialTheme.colorScheme.primary,
        headerTrailing = {
            StatusChip(
                text = if (turn.kind == "follow_up") "面试官追问" else "主问题作答",
                color = MaterialTheme.colorScheme.primary,
            )
        },
    ) {
        if (voiceFallback) {
            Text(
                "已显式改用文字回答；本回合来源会记为 text，不冒充语音转写。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            TextButton(onClick = onLeaveVoiceFallback) { Text("返回语音作答") }
            Spacer(Modifier.height(4.dp))
        }
        Surface(
            shape = RoundedCornerShape(10.dp),
            color = MaterialTheme.colorScheme.background,
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(
                text = turn.prompt,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                modifier = Modifier.padding(10.dp),
            )
        }
        Spacer(Modifier.height(8.dp))
        OutlinedTextField(
            value = text,
            onValueChange = onTextChange,
            modifier = Modifier.fillMaxWidth(),
            enabled = active && draftReady && !pending,
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
            enabled = active && draftReady && text.isNotBlank() && !pending,
            onClick = onSubmit,
        )
        if (pending) {
            Spacer(Modifier.height(6.dp))
            SecondaryAction(text = "确认提交结果", onClick = onConfirmPending)
        }
        Spacer(Modifier.height(6.dp))
        SecondaryAction(text = "重看本题（不写入）", onClick = onReview)
    }
}

@Composable
private fun VoiceAnswerCard(
    state: AppState,
    detail: InterviewDetailDto,
    turn: InterviewTurnDto,
    onRequestMicrophone: () -> Unit,
    onPlay: () -> Unit,
    onStopPlayback: () -> Unit,
    onStopRecording: () -> Unit,
    onUpload: () -> Unit,
    onTranscribeExisting: () -> Unit,
    onRetryTranscription: () -> Unit,
    onReviewChange: (String) -> Unit,
    onRetryReviewSave: () -> Unit,
    onSubmit: () -> Unit,
    onRecordAgain: () -> Unit,
    onDiscard: () -> Unit,
    onUseServerVoice: () -> Unit,
    onTextFallback: () -> Unit,
) {
    val phase = state.voicePhase
    val active = detail.status == "active"
    val transcript = state.voiceDraft?.transcript
    val title = if (turn.kind == "follow_up") "语音追问" else
        "语音作答 · 第 ${turn.position}/${detail.plan_summary.main_question_count} 题"
    InfoCard(
        title = title,
        subtitle = when (phase) {
            VoicePhase.RECORDING -> "正在录音；到时自动停止，不会自动提交"
            VoicePhase.TRANSCRIBING -> "正在转写；等待时间不计入作答时限"
            VoicePhase.REVIEW -> "请核对原始转写，确认后冻结并提交"
            VoicePhase.LOCAL_READY -> "录音已保存到本机，可上传转写"
            VoicePhase.INTERRUPTED -> "录音已中断并保存；回前台后可继续或重录"
            else -> "先听题干，再开始录音；播放与录音互斥"
        },
        accentColor = when (phase) {
            VoicePhase.ERROR -> MaterialTheme.colorScheme.error
            VoicePhase.RECORDING -> MaterialTheme.colorScheme.tertiary
            else -> MaterialTheme.colorScheme.primary
        },
        headerTrailing = {
            StatusChip(
                if (turn.kind == "follow_up") "语音追问" else "语音主问题",
                MaterialTheme.colorScheme.primary,
            )
        },
    ) {
        Surface(
            shape = RoundedCornerShape(10.dp),
            color = MaterialTheme.colorScheme.background,
            border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(
                text = turn.prompt,
                style = MaterialTheme.typography.bodyMedium,
                fontWeight = FontWeight.Medium,
                modifier = Modifier.padding(10.dp),
            )
        }
        Spacer(Modifier.height(8.dp))
        Text(
            "录音格式 16kHz / 单声道 / PCM16 · 每次最多 120 秒；仅用于转写。",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        if (state.voiceSpeechSynthetic) {
            Text(
                "当前播放为测试合成语音，不是真实语音服务结果。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        if (state.voiceFixtureActive) {
            Text(
                "当前录音来自调试 fixture，不是实时麦克风采集；仅用于模拟器链路验证。",
                style = MaterialTheme.typography.labelSmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        Spacer(Modifier.height(8.dp))
        when (phase) {
            VoicePhase.LOADING -> Text("正在读取语音草稿…", style = MaterialTheme.typography.bodySmall)
            VoicePhase.TTS_LOADING -> Text("正在生成题目语音…", style = MaterialTheme.typography.bodySmall)
            VoicePhase.PLAYING -> {
                Text("正在播放题目；播放期间不能录音。", style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                SecondaryAction("停止播放", onClick = onStopPlayback)
            }
            VoicePhase.RECORDING -> {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    StatusChip("录音中", MaterialTheme.colorScheme.error)
                    StatusChip("剩余 ${state.voiceRemainingSeconds}s", MaterialTheme.colorScheme.primary)
                    StatusChip("已录 ${state.voiceRecordedSeconds}s")
                }
                Spacer(Modifier.height(8.dp))
                PrimaryAction(
                    text = "停止录音（不自动提交）",
                    enabled = true,
                    onClick = onStopRecording,
                )
                Spacer(Modifier.height(4.dp))
                Text(
                    "达到 120 秒会停止采集并保留音频，随后进入转写核对；不会替你提交答案。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            VoicePhase.LOCAL_READY, VoicePhase.INTERRUPTED -> {
                val take = state.voiceTake
                if (take != null) {
                    Text(
                        "本地录音已保存：${take.durationMs / 1000} 秒 · ${take.byteLength} 字节" +
                            if (take.interrupted) " · 已中断" else "",
                        style = MaterialTheme.typography.bodySmall,
                    )
                } else if (state.voiceDraft?.active_audio != null) {
                    Text(
                        "服务端已保存本回合录音，可继续转写。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                } else {
                    Text("本地录音暂不可用。", style = MaterialTheme.typography.bodySmall)
                }
                Spacer(Modifier.height(8.dp))
                if (take != null) {
                    PrimaryAction("上传并转写", enabled = active, onClick = onUpload)
                    Spacer(Modifier.height(6.dp))
                } else if (state.voiceDraft?.active_audio != null &&
                    transcript == null
                ) {
                    PrimaryAction("继续转写", enabled = active, onClick = onTranscribeExisting)
                    Spacer(Modifier.height(6.dp))
                }
                SecondaryAction("重新录音", onClick = onRecordAgain)
                if (take != null) {
                    Spacer(Modifier.height(4.dp))
                    TextButton(onClick = onDiscard) { Text("清除本地未提交录音") }
                }
            }
            VoicePhase.UPLOADING -> Text("正在上传录音…", style = MaterialTheme.typography.bodySmall)
            VoicePhase.TRANSCRIBING -> {
                Text("正在转写；不会自动提交答案。", style = MaterialTheme.typography.bodySmall)
                Spacer(Modifier.height(6.dp))
                SecondaryAction("刷新转写状态", onClick = onTranscribeExisting)
            }
            VoicePhase.REVIEW -> {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    TextButton(onClick = onPlay) { Text("重播题目") }
                    TextButton(onClick = onStopPlayback) { Text("停止播放") }
                }
                Text(
                    "原始转写（只读，保留识别结果）",
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.SemiBold,
                    color = MaterialTheme.colorScheme.primary,
                )
                Surface(
                    shape = RoundedCornerShape(10.dp),
                    color = MaterialTheme.colorScheme.background,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Text(
                        transcript?.text.orEmpty(),
                        style = MaterialTheme.typography.bodySmall,
                        modifier = Modifier.padding(10.dp),
                    )
                }
                Spacer(Modifier.height(8.dp))
                Text(
                    "核对后提交文本（可修正识别错误；不得静默截断）",
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.SemiBold,
                )
                OutlinedTextField(
                    value = state.voiceReviewText,
                    onValueChange = onReviewChange,
                    modifier = Modifier.fillMaxWidth(),
                    enabled = active,
                    minLines = 4,
                    maxLines = 10,
                    placeholder = { Text("核对转写；可修正错字后再提交。") },
                )
                Text(
                    "${state.voiceReviewText.codePointCount(0, state.voiceReviewText.length)}/4000 字符",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (state.voiceReviewText.codePointCount(0, state.voiceReviewText.length) > 4000) {
                    Text(
                        "提交文本超过 4000 字符；请精简，不会静默截断。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                }
                state.voiceReviewStorageIssue?.let { issue ->
                    Spacer(Modifier.height(6.dp))
                    Text(
                        issue,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.error,
                    )
                    Spacer(Modifier.height(4.dp))
                    SecondaryAction("重试保存核对修改", onClick = onRetryReviewSave)
                }
                Spacer(Modifier.height(8.dp))
                PrimaryAction(
                    text = if (state.voiceReviewText == transcript?.text) {
                        "确认提交（原样转写）"
                    } else {
                        "确认提交（人工核对文本）"
                    },
                    enabled = active && state.voiceReviewText.isNotBlank() &&
                        state.voiceReviewText.codePointCount(0, state.voiceReviewText.length) <= 4000,
                    onClick = onSubmit,
                )
                Spacer(Modifier.height(6.dp))
                SecondaryAction("重录本题", onClick = onRecordAgain)
                Spacer(Modifier.height(4.dp))
                TextButton(onClick = onTextFallback) { Text("改用文字回答") }
            }
            VoicePhase.SUBMITTING -> Text("正在提交已确认文本…", style = MaterialTheme.typography.bodySmall)
            VoicePhase.ERROR -> {
                Text(
                    state.voiceError ?: state.voiceTakeIssue ?: "语音处理未完成",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
                transcript?.error_code?.let {
                    Text("错误码：$it", style = MaterialTheme.typography.labelSmall)
                }
                Spacer(Modifier.height(8.dp))
                if (state.voiceConflict) {
                    SecondaryAction("使用服务端当前录音", onClick = onUseServerVoice)
                    Spacer(Modifier.height(6.dp))
                }
                if (state.voiceTake != null && !state.voiceConflict) {
                    SecondaryAction("重新上传本地录音", onClick = onUpload)
                    Spacer(Modifier.height(6.dp))
                } else if (state.voiceDraft?.active_audio != null) {
                    if (state.voiceDraft.asr_job?.retryable == true) {
                        SecondaryAction("重试转写任务", onClick = onRetryTranscription)
                    } else {
                        SecondaryAction("重新转写", onClick = onTranscribeExisting)
                    }
                    Spacer(Modifier.height(6.dp))
                }
                SecondaryAction("授权并重新录音", onClick = onRequestMicrophone)
                Spacer(Modifier.height(4.dp))
                TextButton(onClick = onTextFallback) { Text("改用文字回答") }
            }
            else -> {
                PrimaryAction(
                    text = "开始录音（最多 120 秒）",
                    enabled = active,
                    onClick = onRequestMicrophone,
                )
                Spacer(Modifier.height(6.dp))
                SecondaryAction("播放题目", onClick = onPlay)
                Spacer(Modifier.height(4.dp))
                TextButton(onClick = onTextFallback) { Text("改用文字回答") }
            }
        }
        if (state.voiceTakeIssue != null && phase != VoicePhase.ERROR) {
            Spacer(Modifier.height(6.dp))
            Text(
                state.voiceTakeIssue,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
    }
}

@Composable
private fun DecidingCard(vm: AppViewModel, detail: InterviewDetailDto, sessionId: String) {
    val job = detail.follow_up_job
    val hasError = job?.error_code != null || job?.state == "failed" || job?.state == "interrupted"
    InfoCard(
        title = "回答已保存，正在准备下一步",
        subtitle = "追问判定中；不会重复提交回答",
        accentColor = if (hasError) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.primary,
    ) {
        if (job != null) {
            KeyValueRow("任务", job.id.take(8) + "…")
            KeyValueRow("状态", job.state)
            job.error_code?.let { KeyValueRow("错误", it) }
            if (hasError) {
                val diag = remember(job, sessionId) {
                    ErrorDiagnosticsMapper.resolve(
                        code = job.error_code,
                        rawMessage = "追问判定未完成（状态：${job.state}）",
                        retryable = detail.allowed_actions.contains("retry_follow_up"),
                        diagnosticContext = "session=$sessionId · followUpJob=${job.id} · attempt=${job.attempt} · state=${job.state}",
                    )
                }
                Spacer(Modifier.height(4.dp))
                Text(
                    text = diag.userExplanation,
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.error,
                )
                DeveloperDiagnosticPanel(info = diag)
            }
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
        accentColor = when (detail.report_state) {
            "ready" -> MaterialTheme.colorScheme.secondary
            "failed" -> MaterialTheme.colorScheme.error
            else -> MaterialTheme.colorScheme.primary
        },
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
                    val diag = remember(job, sessionId) {
                        ErrorDiagnosticsMapper.resolve(
                            code = job.error_code,
                            rawMessage = "面试报告评分任务未完成（状态：${job.state}）",
                            retryable = detail.allowed_actions.contains("retry_report"),
                            diagnosticContext = "session=$sessionId · reportJob=${job.id} · attempt=${job.attempt} · state=${job.state}",
                        )
                    }
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
                    Spacer(Modifier.height(4.dp))
                    DeveloperDiagnosticPanel(info = diag)
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

private data class LaidOutAnswer(
    val answerId: String,
    val quote: String?,
    val layout: TextLayoutResult,
)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun InterviewReportScreen(vm: AppViewModel, state: AppState, sessionId: String) {
    val report = state.interviewReport?.takeIf { it.session_id == sessionId }
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
        if (report == null) {
            LazyColumn(
                modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
                verticalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                item {
                    EmptyHint(state.interviewReportError ?: "报告尚未加载。")
                    SecondaryAction("重新加载", onClick = { vm.loadInterviewReport(sessionId) })
                }
                val detail = state.interviewDetail?.takeIf { it.session_id == sessionId }
                if (detail != null) {
                    item { UnavailableReportCard(vm, detail, sessionId) }
                    items(detail.transcript, key = { it.turn_id }) { turn -> TurnCard(turn) }
                }
            }
        } else {
            InterviewReportBody(
                report = report,
                sessionId = sessionId,
                onNotify = { message, isError -> vm.notify(message, isError) },
                contentPadding = padding,
            )
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
internal fun InterviewReportBody(
    report: InterviewReportDto,
    sessionId: String = report.session_id,
    onNotify: (String, Boolean) -> Unit,
    modifier: Modifier = Modifier,
    contentPadding: PaddingValues = PaddingValues(0.dp),
    listState: LazyListState = rememberLazyListState(),
    answerTextStyle: TextStyle = MaterialTheme.typography.bodySmall,
    onAnswerLayoutCaptured: ((String, TextLayoutResult) -> Unit)? = null,
) {
    val answerRequester = remember { BringIntoViewRequester() }
    var selectedAnswer by rememberSaveable(sessionId) { mutableStateOf<String?>(null) }
    var selectedQuote by rememberSaveable(sessionId) { mutableStateOf<String?>(null) }
    var jumpQuestionIndex by rememberSaveable(sessionId) { mutableStateOf(-1) }
    var jumpNonce by rememberSaveable(sessionId) { mutableStateOf(0) }
    var activeAnswerLayout by remember { mutableStateOf<LaidOutAnswer?>(null) }
    val layout = remember(report) { buildReportLayoutIndex(report) }

    val jumpToEvidence: (String, String) -> Unit = { answerId, quote ->
        val index = layout.answerQuestionItemIndex[answerId]
        val body = layout.answerText[answerId]
        if (index == null || body == null) {
            onNotify("无法定位该原话：旧报告可能缺少对应回答或回答已不在当前报告中", true)
        } else {
            if (selectedAnswer != answerId || selectedQuote != quote) {
                activeAnswerLayout = null
            }
            selectedAnswer = answerId
            selectedQuote = quote
            jumpQuestionIndex = index
            jumpNonce += 1
            if (quoteRange(body, quote) == null) {
                onNotify("已定位到原回答，但引文未在当前报告原文中逐字出现", false)
            }
        }
    }

    LaunchedEffect(jumpNonce, report.report_id) {
        val targetAnswer = selectedAnswer
        val targetQuote = selectedQuote
        if (jumpNonce <= 0 || jumpQuestionIndex < 0 || targetAnswer == null || targetQuote == null) {
            return@LaunchedEffect
        }
        val targetBody = layout.answerText[targetAnswer] ?: return@LaunchedEffect
        listState.animateScrollToItem(jumpQuestionIndex)
        val laidOut = snapshotFlow { activeAnswerLayout }
            .first { current ->
                current != null &&
                    current.answerId == targetAnswer &&
                    current.quote == targetQuote &&
                    current.layout.layoutInput.text.text == targetBody
            }!!
        val viewportHeight = listState.layoutInfo.viewportSize.height.toFloat()
        val quoteRect = quoteLineBounds(laidOut.layout, targetBody, targetQuote, viewportHeight)
        runCatching {
            if (quoteRect != null) {
                answerRequester.bringIntoView(quoteRect)
            } else {
                answerRequester.bringIntoView()
            }
        }
    }

    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxSize().padding(contentPadding).padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
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
                            TextButton(
                                onClick = { jumpToEvidence(evidence.answer_id, evidence.quote) }
                            ) {
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
                            TextButton(
                                onClick = { jumpToEvidence(evidence.answer_id, evidence.quote) }
                            ) {
                                Text("原话：${evidence.quote}")
                            }
                        }
                    }
                }
            }
        }
        items(report.questions, key = { it.question_id }) { question ->
            ReportQuestionCard(
                question = question,
                selectedAnswer = selectedAnswer,
                selectedQuote = selectedQuote,
                answerRequester = answerRequester,
                answerTextStyle = answerTextStyle,
                onSelectedAnswerLayout = { answerId, quote, textLayout ->
                    if (answerId == selectedAnswer && quote == selectedQuote) {
                        activeAnswerLayout = LaidOutAnswer(answerId, quote, textLayout)
                        onAnswerLayoutCaptured?.invoke(answerId, textLayout)
                    }
                },
                onSelectedAnswerDisposed = { answerId ->
                    if (activeAnswerLayout?.answerId == answerId) {
                        activeAnswerLayout = null
                    }
                },
                onJump = jumpToEvidence,
            )
        }
        item { Spacer(Modifier.height(24.dp)) }
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
            val diag = remember(job, sessionId) {
                ErrorDiagnosticsMapper.resolve(
                    code = job.error_code,
                    rawMessage = "面试报告生成失败（状态：${job.state}）",
                    retryable = detail.allowed_actions.contains("retry_report"),
                    diagnosticContext = "session=$sessionId · reportJob=${job.id} · attempt=${job.attempt} · state=${job.state}",
                )
            }
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
            Spacer(Modifier.height(4.dp))
            DeveloperDiagnosticPanel(info = diag)
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun ReportQuestionCard(
    question: InterviewReportQuestionDto,
    selectedAnswer: String?,
    selectedQuote: String?,
    answerRequester: BringIntoViewRequester,
    answerTextStyle: TextStyle,
    onSelectedAnswerLayout: (String, String?, TextLayoutResult) -> Unit,
    onSelectedAnswerDisposed: (String) -> Unit,
    onJump: (String, String) -> Unit,
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
                    val selected = answer.answer_id == selectedAnswer
                    Row(horizontalArrangement = Arrangement.spacedBy(6.dp)) {
                        StatusChip(
                            when (answer.input_source) {
                                "asr" -> "来源：语音转写（原样提交）"
                                "asr_corrected" -> "来源：语音转写（人工核对）"
                                else -> "来源：文字输入"
                            },
                            MaterialTheme.colorScheme.secondary,
                        )
                    }
                    Column(Modifier.fillMaxWidth()) {
                        AnswerText(
                            answerId = answer.answer_id,
                            text = answer.text,
                            quote = if (selected) selectedQuote else null,
                            selected = selected,
                            requester = answerRequester,
                            style = answerTextStyle,
                            onSelectedAnswerLayout = onSelectedAnswerLayout,
                            onSelectedAnswerDisposed = onSelectedAnswerDisposed,
                        )
                    }
                }
                Spacer(Modifier.height(4.dp))
            }
        }
        if (question.dimensions.isNotEmpty()) {
            Spacer(Modifier.height(6.dp))
            Text("维度结果", style = MaterialTheme.typography.labelLarge, fontWeight = FontWeight.SemiBold)
            question.dimensions.forEach { dim -> DimensionRow(dim, onJump) }
        }
        question.follow_up_outcome?.let {
            Spacer(Modifier.height(4.dp))
            Text("追问结果：$it", style = MaterialTheme.typography.labelSmall)
        }
    }
}

@OptIn(ExperimentalFoundationApi::class)
@Composable
private fun AnswerText(
    answerId: String,
    text: String,
    quote: String?,
    selected: Boolean,
    requester: BringIntoViewRequester,
    style: TextStyle,
    onSelectedAnswerLayout: (String, String?, TextLayoutResult) -> Unit,
    onSelectedAnswerDisposed: (String) -> Unit,
) {
    val color = if (selected) MaterialTheme.colorScheme.primary
    else MaterialTheme.colorScheme.onSurfaceVariant
    val range = remember(text, quote, selected) {
        if (selected) quoteRange(text, quote.orEmpty()) else null
    }
    val highlightColor = MaterialTheme.colorScheme.primary.copy(alpha = 0.22f)
    val displayText = remember(text, range, highlightColor) {
        if (range == null) {
            AnnotatedString(text)
        } else {
            buildAnnotatedString {
                append(text.substring(0, range.first))
                withStyle(
                    SpanStyle(
                        background = highlightColor,
                        fontWeight = FontWeight.SemiBold,
                    )
                ) {
                    append(text.substring(range.first, range.last + 1))
                }
                append(text.substring(range.last + 1))
            }
        }
    }
    var localLayout by remember(answerId, text) { mutableStateOf<TextLayoutResult?>(null) }

    LaunchedEffect(selected, quote, displayText, localLayout) {
        val current = localLayout
        if (selected && current != null && current.layoutInput.text == displayText) {
            onSelectedAnswerLayout(answerId, quote, current)
        }
    }
    if (selected) {
        DisposableEffect(answerId) {
            onDispose { onSelectedAnswerDisposed(answerId) }
        }
    }

    val textModifier = if (selected) {
        Modifier.fillMaxWidth().bringIntoViewRequester(requester)
    } else {
        Modifier.fillMaxWidth()
    }
    Text(
        text = displayText,
        style = style,
        color = color,
        modifier = textModifier,
        onTextLayout = { localLayout = it },
    )
}

@Composable
private fun DimensionRow(
    dim: InterviewDimensionResultDto,
    onJump: (String, String) -> Unit,
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
            TextButton(onClick = { onJump(evidence.answer_id, evidence.quote) }) {
                Text("证据：${evidence.quote}")
            }
        }
    }
}
