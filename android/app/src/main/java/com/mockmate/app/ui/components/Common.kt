package com.mockmate.app.ui.components

import androidx.compose.foundation.BorderStroke
import androidx.compose.foundation.background
import androidx.compose.foundation.border
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.safeDrawingPadding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.shadow
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalAccessibilityManager
import androidx.compose.ui.platform.LocalClipboardManager
import androidx.compose.ui.semantics.LiveRegionMode
import androidx.compose.ui.semantics.liveRegion
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.text.AnnotatedString
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.data.M1Repository
import com.mockmate.app.ui.ErrorDiagnosticsMapper
import com.mockmate.app.ui.FriendlyErrorInfo
import com.mockmate.app.ui.UiMessage
import kotlinx.coroutines.delay

@Composable
fun InfoCard(
    title: String,
    subtitle: String? = null,
    modifier: Modifier = Modifier,
    accentColor: Color? = null,
    headerTrailing: (@Composable () -> Unit)? = null,
    content: @Composable () -> Unit,
) {
    val borderColor = accentColor?.copy(alpha = 0.45f)
        ?: MaterialTheme.colorScheme.outlineVariant
    Card(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(14.dp),
        border = BorderStroke(1.dp, borderColor),
        colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceVariant),
        elevation = CardDefaults.cardElevation(defaultElevation = 0.5.dp),
    ) {
        Column(Modifier.padding(15.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Column(Modifier.weight(1f)) {
                    Text(
                        title,
                        style = MaterialTheme.typography.titleMedium,
                        fontWeight = FontWeight.SemiBold,
                        color = accentColor ?: MaterialTheme.colorScheme.onSurface,
                    )
                    if (!subtitle.isNullOrBlank()) {
                        Spacer(Modifier.height(2.dp))
                        Text(
                            subtitle,
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                if (headerTrailing != null) {
                    Spacer(Modifier.width(8.dp))
                    headerTrailing()
                }
            }
            Spacer(Modifier.height(10.dp))
            content()
        }
    }
}

/**
 * 准备与面试四步流程向导条：让用户清晰感知当前处于哪一步、还差什么条件解锁下一步。
 * 步骤：1=准备材料，2=匹配分析，3=生成计划，4=模拟面试
 */
@Composable
fun PreparationStepHeader(
    currentStep: Int,
    materialsReady: Boolean,
    matchReady: Boolean,
    planConfirmed: Boolean,
    modifier: Modifier = Modifier,
    onSelectStep: ((Int) -> Unit)? = null,
) {
    val steps = listOf(
        Triple(1, "准备材料", materialsReady),
        Triple(2, "匹配分析", matchReady),
        Triple(3, "生成计划", planConfirmed),
        Triple(4, "模拟面试", false),
    )
    Surface(
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 10.dp, vertical = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            steps.forEachIndexed { index, (stepNum, label, completed) ->
                val isCurrent = currentStep == stepNum
                val clickable = onSelectStep != null && !isCurrent && (stepNum == 1 ||
                    (stepNum == 2 && materialsReady) ||
                    (stepNum == 3 && materialsReady))
                val badgeBg = when {
                    isCurrent -> MaterialTheme.colorScheme.primary
                    completed -> MaterialTheme.colorScheme.secondary
                    else -> MaterialTheme.colorScheme.outlineVariant
                }
                val badgeFg = when {
                    isCurrent -> MaterialTheme.colorScheme.onPrimary
                    completed -> MaterialTheme.colorScheme.onSecondary
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                }
                val textColor = when {
                    isCurrent -> MaterialTheme.colorScheme.primary
                    completed -> MaterialTheme.colorScheme.onSurface
                    else -> MaterialTheme.colorScheme.onSurfaceVariant
                }
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    modifier = Modifier
                        .then(
                            if (clickable) Modifier.clickable { onSelectStep?.invoke(stepNum) }
                            else Modifier
                        )
                        .padding(horizontal = 2.dp, vertical = 2.dp),
                ) {
                    Box(
                        modifier = Modifier
                            .size(20.dp)
                            .background(badgeBg, CircleShape),
                        contentAlignment = Alignment.Center,
                    ) {
                        Text(
                            text = if (completed && !isCurrent) "✓" else stepNum.toString(),
                            style = MaterialTheme.typography.labelSmall,
                            fontWeight = FontWeight.Bold,
                            color = badgeFg,
                        )
                    }
                    Spacer(Modifier.width(4.dp))
                    Text(
                        text = label,
                        style = MaterialTheme.typography.labelMedium,
                        fontWeight = if (isCurrent) FontWeight.Bold else FontWeight.Normal,
                        color = textColor,
                    )
                }
                if (index < steps.lastIndex) {
                    Text(
                        text = "›",
                        style = MaterialTheme.typography.labelMedium,
                        color = MaterialTheme.colorScheme.outline,
                    )
                }
            }
        }
    }
}

@Composable
fun KeyValueRow(label: String, value: String) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 3.dp),
        verticalAlignment = Alignment.Top,
    ) {
        Text(
            label,
            modifier = Modifier.width(88.dp),
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Text(
            value,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
fun StatusChip(text: String, color: Color = MaterialTheme.colorScheme.primary) {
    Surface(
        shape = RoundedCornerShape(50),
        color = color.copy(alpha = 0.14f),
        border = BorderStroke(0.5.dp, color.copy(alpha = 0.35f)),
    ) {
        Text(
            text,
            modifier = Modifier.padding(horizontal = 10.dp, vertical = 3.dp),
            style = MaterialTheme.typography.labelSmall,
            fontWeight = FontWeight.Medium,
            color = color,
        )
    }
}

/**
 * 双层报错的「第二层：开发者诊断面板」。
 * 展示错误码、HTTP状态、任务上下文、原始消息及此前被丢弃的 details 列表，并支持一键复制。
 */
@Composable
fun DeveloperDiagnosticPanel(
    info: FriendlyErrorInfo,
    modifier: Modifier = Modifier,
    defaultExpanded: Boolean = false,
) {
    var expanded by rememberSaveable(info.code, info.rawServerMessage) { mutableStateOf(defaultExpanded) }
    var copied by remember { mutableStateOf(false) }
    val clipboard = LocalClipboardManager.current

    Column(modifier = modifier.fillMaxWidth()) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.SpaceBetween,
        ) {
            TextButton(
                onClick = { expanded = !expanded },
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 6.dp, vertical = 2.dp),
            ) {
                Text(
                    text = if (expanded) "收起开发者诊断 ▴" else "</> 开发者诊断详情 ▾",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            if (expanded) {
                TextButton(
                    onClick = {
                        clipboard.setText(AnnotatedString(info.formatDiagnosticText()))
                        copied = true
                    },
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 8.dp, vertical = 2.dp),
                ) {
                    Text(
                        text = if (copied) "✓ 已复制诊断信息" else "复制诊断信息",
                        style = MaterialTheme.typography.labelSmall,
                    )
                }
            }
        }
        if (expanded) {
            Surface(
                modifier = Modifier.fillMaxWidth(),
                shape = RoundedCornerShape(10.dp),
                color = MaterialTheme.colorScheme.background,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
            ) {
                Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                    Text(
                        text = "🔧 开发者诊断详情 (Debug Info)",
                        style = MaterialTheme.typography.labelSmall,
                        fontWeight = FontWeight.Bold,
                        color = MaterialTheme.colorScheme.primary,
                    )
                    HorizontalDivider(color = MaterialTheme.colorScheme.outlineVariant)
                    DiagnosticRow(
                        label = "ErrorCode",
                        value = buildString {
                            append(info.code ?: "NONE")
                            info.httpStatus?.let { append(" (HTTP $it)") }
                            append(" · retryable=${info.retryable}")
                        },
                        highlight = true,
                    )
                    if (!info.diagnosticContext.isNullOrBlank()) {
                        DiagnosticRow(label = "Context", value = info.diagnosticContext)
                    }
                    if (info.rawServerMessage.isNotBlank()) {
                        DiagnosticRow(label = "RawMsg", value = info.rawServerMessage)
                    }
                    if (info.details.isNotEmpty()) {
                        Text(
                            text = "Details (${info.details.size}):",
                            style = MaterialTheme.typography.labelSmall,
                            fontFamily = FontFamily.Monospace,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                        info.details.forEachIndexed { idx, detail ->
                            Surface(
                                shape = RoundedCornerShape(6.dp),
                                color = MaterialTheme.colorScheme.surfaceVariant,
                                modifier = Modifier.fillMaxWidth(),
                            ) {
                                Text(
                                    text = "[${idx + 1}] $detail",
                                    style = MaterialTheme.typography.labelSmall,
                                    fontFamily = FontFamily.Monospace,
                                    color = MaterialTheme.colorScheme.onSurface,
                                    modifier = Modifier.padding(horizontal = 8.dp, vertical = 3.dp),
                                )
                            }
                        }
                    } else {
                        DiagnosticRow(label = "Details", value = "[] (无附加字段)")
                    }
                }
            }
        }
    }
}

@Composable
private fun DiagnosticRow(label: String, value: String, highlight: Boolean = false) {
    Row(Modifier.fillMaxWidth(), verticalAlignment = Alignment.Top) {
        Text(
            text = "$label: ",
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.width(74.dp),
        )
        Text(
            text = value,
            style = MaterialTheme.typography.labelSmall,
            fontFamily = FontFamily.Monospace,
            fontWeight = if (highlight) FontWeight.SemiBold else FontWeight.Normal,
            color = if (highlight) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurface,
        )
    }
}

@Composable
internal fun MessageToast(message: UiMessage, onTimeout: () -> Unit, modifier: Modifier = Modifier) {
    val accessibilityManager = LocalAccessibilityManager.current
    var showDevDetails by remember(message) { mutableStateOf(false) }

    LaunchedEffect(message, accessibilityManager, showDevDetails) {
        // 如果开发者展开了诊断详情，延长停留时间以便查看和复制
        val duration = when {
            showDevDetails -> 30_000L
            message.isError -> 8_000L
            else -> 4_000L
        }
        delay(
            accessibilityManager?.calculateRecommendedTimeoutMillis(
                originalTimeoutMillis = duration,
                containsText = true,
            ) ?: duration,
        )
        onTimeout()
    }

    val colors = MaterialTheme.colorScheme
    val shape = RoundedCornerShape(16.dp)

    if (!message.isError) {
        Surface(
            modifier = modifier
                .safeDrawingPadding()
                .padding(horizontal = 20.dp, vertical = 20.dp)
                .widthIn(max = 420.dp)
                .shadow(4.dp, shape)
                .semantics { liveRegion = LiveRegionMode.Polite },
            shape = shape,
            color = colors.inverseSurface,
        ) {
            Row(
                modifier = Modifier.padding(horizontal = 16.dp, vertical = 11.dp),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                Text(
                    text = "✓",
                    style = MaterialTheme.typography.labelLarge,
                    fontWeight = FontWeight.Bold,
                    color = colors.inversePrimary,
                )
                Text(
                    text = message.code?.let { "${message.text}（$it）" } ?: message.text,
                    style = MaterialTheme.typography.bodyMedium,
                    color = colors.inverseOnSurface,
                )
            }
        }
        return
    }

    val friendly = remember(message) { ErrorDiagnosticsMapper.fromMessage(message) }
    val rawFormatted = message.code?.let { "${message.text}（$it）" } ?: message.text

    Surface(
        modifier = modifier
            .safeDrawingPadding()
            .padding(horizontal = 16.dp, vertical = 16.dp)
            .widthIn(max = 440.dp)
            .fillMaxWidth()
            .shadow(6.dp, shape)
            .semantics { liveRegion = LiveRegionMode.Polite },
        shape = shape,
        color = colors.surface,
        border = BorderStroke(1.5.dp, colors.error),
    ) {
        Column(Modifier.padding(horizontal = 14.dp, vertical = 12.dp)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = "⚠️ ${friendly.userTitle}",
                    style = MaterialTheme.typography.titleSmall,
                    fontWeight = FontWeight.Bold,
                    color = colors.error,
                    modifier = Modifier.weight(1f),
                )
                TextButton(
                    onClick = onTimeout,
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 6.dp, vertical = 0.dp),
                ) {
                    Text("关闭", style = MaterialTheme.typography.labelSmall)
                }
            }
            if (friendly.userExplanation != message.text) {
                Text(
                    text = friendly.userExplanation,
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.onSurface,
                )
                Spacer(Modifier.height(2.dp))
            }
            // 保留原始文本与错误码节点，确保用户/开发者及自动化断言都能直接读取
            Text(
                text = rawFormatted,
                style = MaterialTheme.typography.bodySmall,
                color = if (friendly.userExplanation != message.text) colors.onSurfaceVariant else colors.onSurface,
            )
            friendly.suggestion?.let { hint ->
                Spacer(Modifier.height(4.dp))
                Text(
                    text = "💡 建议：$hint",
                    style = MaterialTheme.typography.labelSmall,
                    color = colors.primary,
                )
            }
            if (message.code != null || message.details.isNotEmpty() || message.diagnosticContext != null) {
                Spacer(Modifier.height(4.dp))
                Row(
                    modifier = Modifier.fillMaxWidth(),
                    horizontalArrangement = Arrangement.End,
                ) {
                    TextButton(
                        onClick = { showDevDetails = !showDevDetails },
                        contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 6.dp, vertical = 0.dp),
                    ) {
                        Text(
                            text = if (showDevDetails) "收起诊断 ▴" else "</> 诊断详情 ▾",
                            style = MaterialTheme.typography.labelSmall,
                            color = colors.onSurfaceVariant,
                        )
                    }
                }
                if (showDevDetails) {
                    DeveloperDiagnosticPanel(
                        info = friendly,
                        defaultExpanded = true,
                    )
                }
            }
        }
    }
}

@Composable
fun ConsentDialog(
    title: String,
    body: String,
    confirmLabel: String = "同意并继续",
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(title, fontWeight = FontWeight.SemiBold) },
        text = { Text(body, style = MaterialTheme.typography.bodyMedium) },
        confirmButton = { Button(onClick = onConfirm) { Text(confirmLabel) } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("取消") } },
    )
}

@Composable
fun PrimaryAction(
    text: String,
    onClick: () -> Unit,
    enabled: Boolean = true,
    modifier: Modifier = Modifier,
) {
    Button(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(10.dp),
        colors = ButtonDefaults.buttonColors(),
    ) {
        Text(text, fontWeight = FontWeight.Medium)
    }
}

@Composable
fun SecondaryAction(
    text: String,
    onClick: () -> Unit,
    enabled: Boolean = true,
    modifier: Modifier = Modifier,
) {
    OutlinedButton(
        onClick = onClick,
        enabled = enabled,
        modifier = modifier.fillMaxWidth(),
        shape = RoundedCornerShape(10.dp),
    ) {
        Text(text)
    }
}

object JobRecoveryPolicy {
    fun retryButtonText(job: JobDto): String = when (job.kind) {
        "match" -> "重试匹配分析（会再次调用模型）"
        "plan" -> "重试生成计划（会再次调用模型）"
        else -> "明确重试该任务（可能产生新的模型调用）"
    }
}

@Composable
fun JobRecoveryActions(
    job: JobDto?,
    onQuery: () -> Unit,
    onRetry: () -> Unit,
) {
    if (job == null || job.state == "succeeded") return
    val isRunning = job.state == "queued" || job.state == "running"
    val isError = job.state == "failed" || job.state == "interrupted"
    val accent = when {
        isRunning -> MaterialTheme.colorScheme.primary
        isError -> MaterialTheme.colorScheme.error
        else -> MaterialTheme.colorScheme.tertiary
    }
    val friendly = remember(job) { ErrorDiagnosticsMapper.fromJob(job) }

    InfoCard(
        title = "任务状态：${M1Repository.stateLabel(job.state)}",
        subtitle = when {
            isRunning -> "任务正在后台执行，离开页面不会丢失进度"
            job.state == "superseded" -> "当前设置或材料已更新，旧任务自动作废"
            else -> friendly.userTitle
        },
        accentColor = accent,
    ) {
        if (isRunning) {
            LoadingRow("正在等待服务端完成处理…")
            Spacer(Modifier.height(8.dp))
        } else if (isError) {
            Text(
                text = friendly.userExplanation,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurface,
            )
            friendly.suggestion?.let { suggestion ->
                Spacer(Modifier.height(4.dp))
                Text(
                    text = "💡 $suggestion",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.primary,
                )
            }
            Spacer(Modifier.height(4.dp))
        }
        job.error?.let {
            Text(
                "${it.message}（${it.code}）",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(8.dp))
        SecondaryAction(text = "只读查询最新状态", onClick = onQuery)
        if (job.retryable) {
            Spacer(Modifier.height(6.dp))
            PrimaryAction(
                text = JobRecoveryPolicy.retryButtonText(job),
                onClick = onRetry,
            )
        }
        if (!isRunning) {
            Spacer(Modifier.height(4.dp))
            DeveloperDiagnosticPanel(info = friendly)
        }
    }
}

@Composable
fun LoadingRow(label: String) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.Center) {
        CircularProgressIndicator(modifier = Modifier.size(18.dp), strokeWidth = 2.dp)
        Spacer(Modifier.width(10.dp))
        Text(label, style = MaterialTheme.typography.bodySmall)
    }
}

@Composable
fun EmptyHint(text: String) {
    Surface(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = 12.dp),
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surface,
        border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
    ) {
        Text(
            text,
            modifier = Modifier
                .fillMaxWidth()
                .padding(horizontal = 16.dp, vertical = 20.dp),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            textAlign = TextAlign.Center,
        )
    }
}
