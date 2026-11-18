package com.mockmate.app.ui.screens

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.RoleDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.PreparationStepHeader
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip

fun levelLabel(level: String): String = if (level == "intern") "实习" else "校招"

fun roleDisplayName(roleId: String, roles: List<RoleDto> = emptyList()): String {
    roles.firstOrNull { it.id == roleId }?.name?.takeIf { it.isNotBlank() }?.let { return it }
    return when (roleId) {
        "java-backend" -> "Java 后端开发"
        else -> roleId
    }
}

fun planStateLabel(state: String?): String = when (state) {
    null, "" -> "尚无计划"
    "ready" -> "已有计划"
    "confirmed" -> "已有计划"
    else -> "已有计划"
}

@Composable
fun rememberFilePicker(onPicked: (Uri, String, String) -> Unit): () -> Unit {
    val context = LocalContext.current
    val launcher = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { uri ->
        if (uri != null) {
            var name = "upload"
            val mime = context.contentResolver.getType(uri) ?: "application/octet-stream"
            runCatching {
                context.contentResolver.query(
                    uri,
                    arrayOf(OpenableColumns.DISPLAY_NAME),
                    null,
                    null,
                    null,
                )?.use { cursor ->
                    if (cursor.moveToFirst() && cursor.columnCount > 0) {
                        name = cursor.getString(0) ?: name
                    }
                }
            }
            onPicked(uri, name, mime)
        }
    }
    return {
        launcher.launch(
            arrayOf(
                "application/pdf",
                "text/plain",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun PreparationListScreen(vm: AppViewModel, state: AppState) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("MockMate 准备列表", fontWeight = FontWeight.SemiBold) },
                actions = {
                    TextButton(onClick = { vm.refreshPreparations() }) { Text("刷新") }
                    TextButton(onClick = { vm.logout() }) { Text("退出") }
                },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            item {
                Spacer(Modifier.height(4.dp))
                Surface(
                    shape = RoundedCornerShape(14.dp),
                    color = MaterialTheme.colorScheme.surface,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(14.dp)) {
                        Row(
                            modifier = Modifier.fillMaxWidth(),
                            horizontalArrangement = Arrangement.SpaceBetween,
                            verticalAlignment = Alignment.CenterVertically,
                        ) {
                            Column {
                                Text(
                                    "欢迎回来，${state.account?.username ?: "候选人"}",
                                    style = MaterialTheme.typography.titleSmall,
                                    fontWeight = FontWeight.SemiBold,
                                )
                                Text(
                                    "当前账号：${state.account?.username ?: ""}",
                                    style = MaterialTheme.typography.bodySmall,
                                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                                )
                            }
                            StatusChip("4 步完成模拟面试", MaterialTheme.colorScheme.primary)
                        }
                        Spacer(Modifier.height(10.dp))
                        PrimaryAction("新建面试准备", onClick = { vm.startNewPreparation() })
                        Spacer(Modifier.height(8.dp))
                        SecondaryAction("面试记录", onClick = { vm.openInterviewHistory() })
                    }
                }
            }
            if (state.preparations.isEmpty()) {
                item { EmptyHint("还没有准备。点击上方按钮创建第一份准备。") }
            }
            items(state.preparations, key = { it.id }) { prep ->
                PreparationCard(
                    prep = prep,
                    roles = state.roles,
                    onOpen = { vm.openPreparation(prep.id) },
                )
            }
            item {
                Text(
                    "计划保存后，退出或杀进程再登录仍可从本列表读回同一计划；本页不展示题干与答案。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(vertical = 12.dp),
                )
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun PreparationCard(
    prep: PreparationDto,
    roles: List<RoleDto>,
    onOpen: () -> Unit,
) {
    val resumeOk = prep.resume_snapshot_id != null
    val jdOk = prep.jd_snapshot_id != null
    val planOk = prep.current_plan_id != null
    val nextStepHint = when {
        planOk -> "✓ 已生成面试计划，打开后可直接开始模拟面试"
        resumeOk && jdOk && prep.match_report_id != null -> "下一步：生成 3+3 面试计划"
        resumeOk && jdOk -> "下一步：执行岗位匹配度分析"
        !resumeOk && !jdOk -> "下一步：上传并确认简历与岗位 JD"
        !resumeOk -> "下一步：补充并确认个人简历"
        else -> "下一步：补充并确认目标岗位 JD"
    }

    InfoCard(
        title = "${roleDisplayName(prep.role_id, roles)} · ${levelLabel(prep.level)} · ${prep.main_question_count} 题",
        subtitle = "${prep.role_id} · 更新于 ${prep.updated_at}",
    ) {
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(6.dp),
        ) {
            StatusChip(
                text = if (resumeOk) "简历已确认" else "缺简历",
                color = if (resumeOk) MaterialTheme.colorScheme.secondary
                else MaterialTheme.colorScheme.error,
            )
            StatusChip(
                text = if (jdOk) "JD 已确认" else "缺 JD",
                color = if (jdOk) MaterialTheme.colorScheme.secondary
                else MaterialTheme.colorScheme.error,
            )
            StatusChip(
                text = planStateLabel(if (planOk) "ready" else null),
                color = if (planOk) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.tertiary,
            )
        }
        Spacer(Modifier.height(8.dp))
        Text(
            text = nextStepHint,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(8.dp))
        SecondaryAction("打开", onClick = onOpen)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SetupScreen(vm: AppViewModel, state: AppState, preparationId: String?) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = {
                    Text(
                        if (preparationId == null) "新建面试准备" else "准备设置与材料",
                        fontWeight = FontWeight.SemiBold,
                    )
                },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        if (preparationId == null) {
            NewPreparationPanel(vm, state, Modifier.padding(padding))
        } else {
            val prep = state.preparation
            if (prep == null || prep.id != preparationId) {
                Column(Modifier.padding(padding).padding(16.dp)) {
                    EmptyHint("准备尚未加载，请返回列表重新打开。")
                }
            } else {
                ExistingPreparationPanel(vm, state, prep, Modifier.padding(padding))
            }
        }
    }
}

@Composable
private fun NewPreparationPanel(vm: AppViewModel, state: AppState, modifier: Modifier) {
    val enabledRoles = state.roles.filter { it.enabled }
    var roleId by rememberSaveable { mutableStateOf(enabledRoles.firstOrNull()?.id ?: "") }
    var level by rememberSaveable { mutableStateOf("intern") }
    var count by rememberSaveable { mutableStateOf(6) }

    LaunchedEffect(enabledRoles) {
        if (roleId.isBlank() && enabledRoles.isNotEmpty()) roleId = enabledRoles.first().id
    }
    val role = enabledRoles.firstOrNull { it.id == roleId }
    val levels = role?.levels ?: listOf("intern")
    LaunchedEffect(roleId) {
        if (level !in levels) level = levels.first()
    }

    LazyColumn(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Spacer(Modifier.height(4.dp))
            PreparationStepHeader(
                currentStep = 1,
                materialsReady = false,
                matchReady = false,
                planConfirmed = false,
            )
        }
        item {
            if (enabledRoles.isEmpty()) {
                InfoCard(title = "当前没有启用岗位", accentColor = MaterialTheme.colorScheme.error) {
                    Text(
                        "服务端未启用任何岗位，无法创建准备。请联系负责人启用岗位；" +
                            "本轮不伪装多岗位支持。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            } else {
                InfoCard(
                    title = "选择岗位与级别",
                    subtitle = "设定本场模拟面试的目标岗位、招聘级别与主问题数量",
                ) {
                    Text("目标岗位", style = MaterialTheme.typography.labelMedium)
                    Spacer(Modifier.height(4.dp))
                    enabledRoles.forEach { item ->
                        Row(verticalAlignment = Alignment.CenterVertically) {
                            FilterChip(
                                selected = roleId == item.id,
                                onClick = { roleId = item.id },
                                label = { Text(item.name) },
                            )
                            if (item.sample_only) {
                                Spacer(Modifier.padding(horizontal = 4.dp))
                                StatusChip("开发样板", MaterialTheme.colorScheme.tertiary)
                            }
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("招聘级别", style = MaterialTheme.typography.labelMedium)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        levels.forEach { item ->
                            FilterChip(
                                selected = level == item,
                                onClick = { level = item },
                                label = { Text(levelLabel(item)) },
                            )
                        }
                    }
                    Spacer(Modifier.height(8.dp))
                    Text("主问题数量（题库与生成各半）", style = MaterialTheme.typography.labelMedium)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf(2, 4, 6).forEach { value ->
                            FilterChip(
                                selected = count == value,
                                onClick = { count = value },
                                label = { Text("$value 题（${value / 2}+${value / 2}）") },
                            )
                        }
                    }
                    Spacer(Modifier.height(12.dp))
                    PrimaryAction(
                        text = "创建准备",
                        onClick = { vm.createPreparation(roleId, level, count) },
                        enabled = roleId.isNotBlank() && !state.loading,
                    )
                }
            }
        }
        item {
            InfoCard(title = "接下来做什么？") {
                Text(
                    "1. 创建后添加个人简历与目标岗位 JD（支持上传文件、粘贴文本或使用内置练习 JD）。\n" +
                        "2. 在核对页可通过「AI 解析」一键提取结构，两份材料都确认后即可开始匹配分析与生成计划。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
private fun ExistingPreparationPanel(
    vm: AppViewModel,
    state: AppState,
    prep: PreparationDto,
    modifier: Modifier,
) {
    val enabledRoles = state.roles.filter { it.enabled }
    var roleId by rememberSaveable(prep.id) { mutableStateOf(prep.role_id) }
    var level by rememberSaveable(prep.id) { mutableStateOf(prep.level) }
    var count by rememberSaveable(prep.id) { mutableStateOf(prep.main_question_count) }
    var showDelete by rememberSaveable { mutableStateOf(false) }
    val resume = state.resumeSnapshot
    val jd = state.jdSnapshot
    val ready = prep.resume_snapshot_id != null && prep.jd_snapshot_id != null
    val matchDone = prep.match_report_id != null
    val planDone = prep.current_plan_id != null

    val resumePicker = rememberFilePicker { uri, name, mime ->
        vm.uploadFile("resume", name.take(120), prep.id, uri, name, mime)
    }
    val jdPicker = rememberFilePicker { uri, name, mime ->
        vm.uploadFile("jd", name.take(120), prep.id, uri, name, mime)
    }

    LazyColumn(
        modifier = modifier
            .fillMaxSize()
            .padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Spacer(Modifier.height(4.dp))
            PreparationStepHeader(
                currentStep = 1,
                materialsReady = ready,
                matchReady = matchDone,
                planConfirmed = planDone,
                onSelectStep = { step ->
                    when (step) {
                        2 -> if (ready) vm.openMatch(prep.id)
                        3 -> if (ready) vm.openPlan(prep.id)
                    }
                },
            )
        }
        item {
            InfoCard(
                title = "本次准备",
                subtitle = "修改岗位/级别/题量或材料会提升版本并使旧匹配、旧计划失效",
                headerTrailing = {
                    StatusChip(
                        "${roleDisplayName(roleId, state.roles)} · ${levelLabel(level)} · $count 题",
                        MaterialTheme.colorScheme.primary,
                    )
                },
            ) {
                if (enabledRoles.isEmpty()) {
                    Text("没有启用岗位，请返回列表。", style = MaterialTheme.typography.bodySmall)
                } else {
                    Text("目标岗位", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        enabledRoles.forEach { item ->
                            FilterChip(
                                selected = roleId == item.id,
                                onClick = { roleId = item.id },
                                label = { Text(item.name) },
                            )
                        }
                    }
                    val levels = enabledRoles.firstOrNull { it.id == roleId }?.levels ?: listOf("intern")
                    Text("级别与主问题题量（题库题 + AI 题各半）", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        levels.forEach { item ->
                            FilterChip(
                                selected = level == item,
                                onClick = { level = item },
                                label = { Text(levelLabel(item)) },
                            )
                        }
                        listOf(2, 4, 6).forEach { value ->
                            FilterChip(
                                selected = count == value,
                                onClick = { count = value },
                                label = { Text("$value 题") },
                            )
                        }
                    }
                    val changed = roleId != prep.role_id || level != prep.level || count != prep.main_question_count
                    Spacer(Modifier.height(6.dp))
                    PrimaryAction(
                        text = if (changed) "保存设置（会使旧计划失效）" else "设置已保存",
                        onClick = { vm.updatePreparation(roleId, level, count) },
                        enabled = changed && !state.loading,
                    )
                }
            }
        }
        item {
            MaterialSlotCard(
                stepLabel = "①",
                kind = "resume",
                title = "简历",
                snapshot = resume,
                materialId = prep.resume_snapshot_id,
                onPickExisting = { vm.openLibrary("resume", prep.id) },
                onNewText = { vm.openEditor("resume", null, prep.id) },
                onUpload = resumePicker,
                onOpenReview = { materialId -> vm.openReview(materialId, prep.id) },
                onUnbind = { vm.unbindMaterial("resume") },
            )
        }
        item {
            MaterialSlotCard(
                stepLabel = "②",
                kind = "jd",
                title = "岗位 JD",
                snapshot = jd,
                materialId = prep.jd_snapshot_id,
                onPickExisting = { vm.openLibrary("jd", prep.id) },
                onNewText = { vm.openEditor("jd", null, prep.id) },
                onUpload = jdPicker,
                onOpenReview = { materialId -> vm.openReview(materialId, prep.id) },
                onUnbind = { vm.unbindMaterial("jd") },
                showTemplateAction = true,
                onUseTemplate = { vm.openTemplates(prep.id) },
            )
        }
        item {
            InfoCard(
                title = "下一步",
                accentColor = if (ready) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.tertiary,
            ) {
                Surface(
                    shape = RoundedCornerShape(10.dp),
                    color = MaterialTheme.colorScheme.background,
                    border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                    modifier = Modifier.fillMaxWidth(),
                ) {
                    Column(Modifier.padding(10.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
                        Text(
                            text = if (prep.resume_snapshot_id != null) "✓ 1. 个人简历已核对并绑定" else "○ 1. 待添加并核对确认「个人简历」",
                            style = MaterialTheme.typography.bodySmall,
                            color = if (prep.resume_snapshot_id != null) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.error,
                            fontWeight = FontWeight.Medium,
                        )
                        Text(
                            text = if (prep.jd_snapshot_id != null) "✓ 2. 岗位 JD 已核对并绑定" else "○ 2. 待添加并核对确认「岗位 JD」",
                            style = MaterialTheme.typography.bodySmall,
                            color = if (prep.jd_snapshot_id != null) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.error,
                            fontWeight = FontWeight.Medium,
                        )
                    }
                }
                Spacer(Modifier.height(8.dp))
                if (!ready) {
                    Text(
                        "简历与 JD 都确认后才可匹配与生成。缺失时服务端也会独立拒绝（MATERIALS_REQUIRED）。",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    Text(
                        "材料版本已绑定。先做匹配分析，再生成 3+3 计划。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    SecondaryAction("查看匹配分析", onClick = { vm.openMatch(prep.id) })
                    Spacer(Modifier.height(8.dp))
                    PrimaryAction("查看/生成计划", onClick = { vm.openPlan(prep.id) })
                }
                Spacer(Modifier.height(10.dp))
                TextButton(onClick = { showDelete = true }) { Text("删除该准备") }
            }
        }
        item { Spacer(Modifier.height(24.dp)) }
    }

    if (showDelete) {
        AlertDialog(
            onDismissRequest = { showDelete = false },
            title = { Text("删除准备？") },
            text = { Text("将删除该准备的匹配、计划与任务记录；已确认材料版本会保留在材料库。") },
            confirmButton = {
                Button(onClick = {
                    showDelete = false
                    vm.deletePreparation(prep.id)
                }) { Text("删除") }
            },
            dismissButton = { TextButton(onClick = { showDelete = false }) { Text("取消") } },
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MaterialSlotCard(
    stepLabel: String,
    kind: String,
    title: String,
    snapshot: SnapshotDto?,
    materialId: String?,
    onPickExisting: () -> Unit,
    onNewText: () -> Unit,
    onUpload: () -> Unit,
    onOpenReview: (String) -> Unit,
    onUnbind: () -> Unit,
    showTemplateAction: Boolean = false,
    onUseTemplate: () -> Unit = {},
) {
    val isBound = materialId != null
    InfoCard(
        title = title,
        subtitle = if (isBound) "$stepLabel 已绑定版本" else "$stepLabel 尚未绑定已确认版本",
        accentColor = if (isBound) MaterialTheme.colorScheme.secondary else null,
        headerTrailing = {
            StatusChip(
                text = if (isBound) "✓ 已确认就绪" else "待添加确认",
                color = if (isBound) MaterialTheme.colorScheme.secondary else MaterialTheme.colorScheme.tertiary,
            )
        },
    ) {
        if (snapshot != null) {
            Surface(
                shape = RoundedCornerShape(10.dp),
                color = MaterialTheme.colorScheme.background,
                border = BorderStroke(1.dp, MaterialTheme.colorScheme.outlineVariant),
                modifier = Modifier.fillMaxWidth(),
            ) {
                Column(Modifier.padding(10.dp)) {
                    KeyValueRow("标题", snapshot.title)
                    KeyValueRow("版本", "revision ${snapshot.source_revision}")
                    KeyValueRow("确认时间", snapshot.confirmed_at)
                }
            }
            Spacer(Modifier.height(8.dp))
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                AssistChip(
                    onClick = { onOpenReview(snapshot.material_id) },
                    label = { Text("查看/核对") },
                )
                AssistChip(onClick = onUnbind, label = { Text("清除") })
            }
        } else {
            Text(
                "缺少已确认的${title}。可粘贴文本、上传 PDF/DOCX/TXT，或从材料库选择已确认版本。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        Spacer(Modifier.height(8.dp))
        Text(
            text = if (isBound) "更换或新建${title}：" else "添加${title}方式：",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
        Spacer(Modifier.height(4.dp))
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            AssistChip(onClick = onPickExisting, label = { Text("材料库") })
            AssistChip(onClick = onNewText, label = { Text("粘贴新建") })
            AssistChip(onClick = onUpload, label = { Text("上传文件") })
            if (showTemplateAction) {
                AssistChip(onClick = onUseTemplate, label = { Text("内置 JD") })
            }
        }
    }
}
