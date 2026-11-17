package com.mockmate.app.ui.screens

import android.content.Context
import android.net.Uri
import android.provider.OpenableColumns
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
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
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.UiMessage
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.KeyValueRow
import com.mockmate.app.ui.components.MessageBanner
import com.mockmate.app.ui.components.PrimaryAction
import com.mockmate.app.ui.components.SecondaryAction
import com.mockmate.app.ui.components.StatusChip

fun levelLabel(level: String): String = if (level == "intern") "实习" else "校招"

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
                title = { Text("MockMate 准备列表") },
                actions = {
                    TextButton(onClick = { vm.refreshPreparations() }) { Text("刷新") }
                    TextButton(onClick = { vm.logout() }) { Text("退出") }
                },
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
                    "当前账号：${state.account?.username ?: ""}",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(8.dp))
                PrimaryAction("新建面试准备", onClick = { vm.startNewPreparation() })
                Spacer(Modifier.height(8.dp))
                SecondaryAction("面试记录", onClick = { vm.openInterviewHistory() })
                Spacer(Modifier.height(8.dp))
            }
            if (state.preparations.isEmpty()) {
                item { EmptyHint("还没有准备。点击上方按钮创建第一份准备。") }
            }
            items(state.preparations, key = { it.id }) { prep ->
                PreparationCard(prep = prep, onOpen = { vm.openPreparation(prep.id) })
            }
            item {
                Text(
                    "计划保存后，退出或杀进程再登录仍可从本列表读回同一计划；本页不展示题干与答案。",
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.outline,
                    modifier = Modifier.padding(vertical = 16.dp),
                )
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

@Composable
private fun PreparationCard(prep: PreparationDto, onOpen: () -> Unit) {
    InfoCard(
        title = "${prep.role_id} · ${levelLabel(prep.level)} · ${prep.main_question_count} 题",
        subtitle = "更新于 ${prep.updated_at}",
    ) {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(
                text = if (prep.resume_snapshot_id != null) "简历已确认" else "缺简历",
                color = if (prep.resume_snapshot_id != null) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.error,
            )
            StatusChip(
                text = if (prep.jd_snapshot_id != null) "JD 已确认" else "缺 JD",
                color = if (prep.jd_snapshot_id != null) MaterialTheme.colorScheme.primary
                else MaterialTheme.colorScheme.error,
            )
            StatusChip(
                text = planStateLabel(if (prep.current_plan_id != null) "ready" else null),
                color = MaterialTheme.colorScheme.tertiary,
            )
        }
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
                title = { Text(if (preparationId == null) "新建面试准备" else "准备设置与材料") },
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
        modifier = modifier.fillMaxSize().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            Spacer(Modifier.height(4.dp))
            if (enabledRoles.isEmpty()) {
                InfoCard(title = "当前没有启用岗位") {
                    Text(
                        "服务端未启用任何岗位，无法创建准备。请联系负责人启用岗位；" +
                            "本轮不伪装多岗位支持。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                }
            } else {
                InfoCard(title = "选择岗位与级别") {
                    enabledRoles.forEach { item ->
                        Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
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
                    Text("级别", style = MaterialTheme.typography.labelMedium)
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
            InfoCard(title = "接下来") {
                Text(
                    "创建后选择并核对简历与 JD；两者都确认后才能匹配和生成计划。",
                    style = MaterialTheme.typography.bodySmall,
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

    val resumePicker = rememberFilePicker { uri, name, mime ->
        vm.uploadFile("resume", name.take(120), prep.id, uri, name, mime)
    }
    val jdPicker = rememberFilePicker { uri, name, mime ->
        vm.uploadFile("jd", name.take(120), prep.id, uri, name, mime)
    }

    LazyColumn(
        modifier = modifier.fillMaxSize().padding(horizontal = 16.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        item {
            InfoCard(title = "本次准备", subtitle = "修改岗位/级别/题量或材料会提升版本并使旧匹配、旧计划失效") {
                if (enabledRoles.isEmpty()) {
                    Text("没有启用岗位，请返回列表。", style = MaterialTheme.typography.bodySmall)
                } else {
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
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        levels.forEach { item ->
                            FilterChip(
                                selected = level == item,
                                onClick = { level = item },
                                label = { Text(levelLabel(item)) },
                            )
                        }
                    }
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        listOf(2, 4, 6).forEach { value ->
                            FilterChip(
                                selected = count == value,
                                onClick = { count = value },
                                label = { Text("$value 题") },
                            )
                        }
                    }
                    val changed = roleId != prep.role_id || level != prep.level || count != prep.main_question_count
                    Spacer(Modifier.height(8.dp))
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
                kind = "resume",
                title = "简历",
                snapshot = resume,
                materialId = prep.resume_snapshot_id,
                vm = vm,
                preparationId = prep.id,
                onPickExisting = { vm.openLibrary("resume", prep.id) },
                onNewText = { vm.openEditor("resume", null, prep.id) },
                onUpload = resumePicker,
                onOpenReview = { materialId -> vm.openReview(materialId, prep.id) },
                onUnbind = { vm.unbindMaterial("resume") },
            )
        }
        item {
            MaterialSlotCard(
                kind = "jd",
                title = "岗位 JD",
                snapshot = jd,
                materialId = prep.jd_snapshot_id,
                vm = vm,
                preparationId = prep.id,
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
            InfoCard(title = "下一步") {
                if (!ready) {
                    Text(
                        "简历与 JD 都确认后才可匹配与生成。缺失时服务端也会独立拒绝（MATERIALS_REQUIRED）。",
                        style = MaterialTheme.typography.bodySmall,
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
                Spacer(Modifier.height(12.dp))
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

@Composable
private fun MaterialSlotCard(
    kind: String,
    title: String,
    snapshot: SnapshotDto?,
    materialId: String?,
    vm: AppViewModel,
    preparationId: String,
    onPickExisting: () -> Unit,
    onNewText: () -> Unit,
    onUpload: () -> Unit,
    onOpenReview: (String) -> Unit,
    onUnbind: () -> Unit,
    showTemplateAction: Boolean = false,
    onUseTemplate: () -> Unit = {},
) {
    InfoCard(
        title = title,
        subtitle = if (materialId != null) "已绑定版本" else "尚未绑定已确认版本",
    ) {
        if (snapshot != null) {
            KeyValueRow("标题", snapshot.title)
            KeyValueRow("版本", "revision ${snapshot.source_revision}")
            KeyValueRow("确认时间", snapshot.confirmed_at)
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
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            AssistChip(onClick = onPickExisting, label = { Text("材料库") })
            AssistChip(onClick = onNewText, label = { Text("粘贴新建") })
            AssistChip(onClick = onUpload, label = { Text("上传文件") })
            if (showTemplateAction) {
                AssistChip(onClick = onUseTemplate, label = { Text("内置 JD") })
            }
        }
    }
}
