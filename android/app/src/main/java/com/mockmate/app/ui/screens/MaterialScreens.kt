package com.mockmate.app.ui.screens

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
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
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.JdTemplateDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.SnapshotDto
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
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

private fun materialStateLabel(state: String): String = when (state) {
    "uploaded" -> "待提取文本"
    "text_ready" -> "文本待核对"
    "parsed" -> "已有结构化结果"
    else -> state
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun LibraryScreen(vm: AppViewModel, state: AppState, kind: String, preparationId: String) {
    val title = if (kind == "resume") "选择简历" else "选择岗位 JD"
    val items = state.materials.filter { it.kind == kind }
    val picker = rememberFilePicker { uri, name, mime ->
        vm.uploadFile(kind, name.take(120), preparationId, uri, name, mime)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(title) },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
                actions = { TextButton(onClick = { vm.refreshMaterials() }) { Text("刷新") } },
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
                    title = "只能选择已确认版本",
                    subtitle = "解析成功不等于已确认；未确认材料不能用于匹配或生成",
                ) {
                    Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                        AssistChip(
                            onClick = { vm.openEditor(kind, null, preparationId) },
                            label = { Text("粘贴新建") },
                        )
                        AssistChip(onClick = picker, label = { Text("上传文件") })
                        if (kind == "jd") {
                            AssistChip(
                                onClick = { vm.openTemplates(preparationId) },
                                label = { Text("内置 JD 模板") },
                            )
                        }
                    }
                }
            }
            if (items.isEmpty()) {
                item { EmptyHint("还没有${if (kind == "resume") "简历" else " JD"}材料。") }
            }
            items(items, key = { it.id }) { material ->
                MaterialListCard(
                    material = material,
                    onOpen = { vm.openReview(material.id, preparationId) },
                    onEdit = { vm.openEditor(kind, material.id, preparationId) },
                )
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

@Composable
private fun MaterialListCard(
    material: MaterialDraftDto,
    onOpen: () -> Unit,
    onEdit: () -> Unit,
) {
    InfoCard(title = material.title, subtitle = "revision ${material.revision} · 更新于 ${material.updated_at}") {
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            StatusChip(materialStateLabel(material.state))
            if (material.template_id != null) StatusChip("内置模板 v${material.template_version}", MaterialTheme.colorScheme.tertiary)
        }
        Spacer(Modifier.height(8.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            AssistChip(onClick = onOpen, label = { Text("查看/核对") })
            AssistChip(onClick = onEdit, label = { Text("编辑原文") })
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun EditorScreen(
    vm: AppViewModel,
    state: AppState,
    kind: String,
    materialId: String?,
    preparationId: String,
) {
    val material = state.material?.takeIf { it.id == materialId }
    var title by rememberSaveable(materialId) {
        mutableStateOf(if (materialId == null) vm.draft("editor-title-$kind") ?: "" else "")
    }
    var text by rememberSaveable(materialId) {
        mutableStateOf(if (materialId == null) vm.draft("editor-$kind") ?: "" else "")
    }
    var loaded by remember(materialId) { mutableStateOf(materialId == null) }

    LaunchedEffect(material?.id, material?.revision) {
        if (!loaded && material != null && material.id == materialId) {
            title = material.title
            text = material.text ?: ""
            loaded = true
        }
    }

    val codePoints = text.codePointCount(0, text.length)
    val overLimit = codePoints > 20000
    val kindLabel = if (kind == "resume") "简历" else "岗位 JD"

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(if (materialId == null) "新建$kindLabel" else "编辑$kindLabel") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp),
        ) {
            Text(
                "扩展名、类型与提取结果会共同校验；支持 PDF/DOCX/TXT 与粘贴文本。保存后请到核对页确认结构。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = title,
                onValueChange = {
                    title = it.take(120)
                    vm.saveDraft("editor-title-$kind", title)
                },
                label = { Text("标题（1–120 字符）") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
                keyboardOptions = KeyboardOptions(imeAction = ImeAction.Next),
            )
            Spacer(Modifier.height(8.dp))
            OutlinedTextField(
                value = text,
                onValueChange = {
                    text = it
                    vm.saveDraft("editor-$kind", it)
                },
                label = { Text("原文（中文输入可直接使用）") },
                modifier = Modifier.fillMaxWidth().heightIn(min = 220.dp, max = 420.dp),
                supportingText = {
                    Text(
                        "$codePoints / 20000 字符" + if (overLimit) " · 超出上限" else "",
                        color = if (overLimit) MaterialTheme.colorScheme.error else MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                },
            )
            Spacer(Modifier.height(12.dp))
            PrimaryAction(
                text = "保存原文",
                enabled = !state.loading && title.isNotBlank() && text.isNotBlank() && !overLimit,
                onClick = {
                    vm.saveTextMaterial(kind, materialId, title, text, preparationId)
                },
            )
            if (materialId != null) {
                Spacer(Modifier.height(8.dp))
                Text(
                    "修改原文会清除旧解析结果并提升版本；旧任务不会覆盖新版本。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Spacer(Modifier.height(24.dp))
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun TemplatesScreen(vm: AppViewModel, state: AppState, preparationId: String) {
    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("内置练习 JD") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        LazyColumn(
            modifier = Modifier.fillMaxSize().padding(padding).padding(horizontal = 16.dp),
            verticalArrangement = Arrangement.spacedBy(10.dp),
        ) {
            item {
                Text(
                    "内置模板是开发样例，不是真实在招职位；使用时会复制为你的 JD 材料，可修改后核对确认。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.padding(vertical = 8.dp),
                )
            }
            if (state.templates.isEmpty()) {
                item { EmptyHint("服务端没有启用的内置模板。") }
            }
            items(state.templates, key = { it.id + it.version }) { template ->
                TemplateCard(template) { vm.useTemplate(template, preparationId) }
            }
            item { Spacer(Modifier.height(24.dp)) }
        }
    }
}

@Composable
private fun TemplateCard(template: JdTemplateDto, onUse: () -> Unit) {
    InfoCard(
        title = template.title,
        subtitle = "${template.label} · ${template.role_id} · ${levelLabel(template.level)} · v${template.version}",
    ) {
        Box(Modifier.heightIn(max = 160.dp)) {
            Column(Modifier.verticalScroll(rememberScrollState())) {
                Text(template.text, style = MaterialTheme.typography.bodySmall)
            }
        }
        Spacer(Modifier.height(8.dp))
        SecondaryAction("使用该模板创建 JD", onClick = onUse)
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun ReviewScreen(
    vm: AppViewModel,
    state: AppState,
    materialId: String,
    preparationId: String,
) {
    val material = state.material?.takeIf { it.id == materialId }
    val kind = material?.kind ?: "resume"
    val prep = state.preparation
    val items = remember(material?.id, material?.revision, material?.profile) {
        mutableStateListOf<ItemDraft>().apply { addAll(ProfileDraftCodec.parse(material?.profile, kind)) }
    }
    var showParseConsent by rememberSaveable { mutableStateOf(false) }
    var jdTitle by rememberSaveable(material?.id) { mutableStateOf(material?.title ?: "") }

    LaunchedEffect(material?.title) {
        if (material != null && jdTitle.isBlank()) jdTitle = material.title
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text("核对${if (kind == "resume") "简历" else "JD"}") },
                navigationIcon = { TextButton(onClick = { vm.back() }) { Text("返回") } },
            )
        },
    ) { padding ->
        Column(
            modifier = Modifier
                .fillMaxSize()
                .padding(padding)
                .imePadding()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 16.dp),
        ) {
            if (material == null) {
                EmptyHint("材料加载中…")
            } else {
                InfoCard(
                    title = material.title,
                    subtitle = "revision ${material.revision} · ${materialStateLabel(material.state)}",
                ) {
                    Box(Modifier.heightIn(max = 200.dp)) {
                        Column(Modifier.verticalScroll(rememberScrollState())) {
                            Text(material.text ?: "（尚未提取文本）", style = MaterialTheme.typography.bodySmall)
                        }
                    }
                    if (material.text == null && material.state == "uploaded") {
                        Spacer(Modifier.height(8.dp))
                        SecondaryAction(text = "提取文件文本", onClick = { vm.extractMaterial(materialId) })
                    }
                }
                Spacer(Modifier.height(8.dp))
                JobRecoveryActions(
                    job = state.job,
                    onQuery = { vm.queryCurrentJob() },
                    onRetry = { vm.retryCurrentJob() },
                )
                Spacer(Modifier.height(10.dp))
                InfoCard(
                    title = "结构化核对",
                    subtitle = "引用必须逐字来自上方原文；手动补充的事实请标为“我手动确认”",
                ) {
                    if (kind == "jd") {
                        OutlinedTextField(
                            value = jdTitle,
                            onValueChange = { jdTitle = it },
                            label = { Text("岗位标题") },
                            singleLine = true,
                            modifier = Modifier.fillMaxWidth(),
                        )
                    }
                    val sections = if (kind == "resume") SECTION_ORDER_RESUME else SECTION_ORDER_JD
                    sections.forEach { section ->
                        Text(
                            sectionLabel(section),
                            style = MaterialTheme.typography.labelLarge,
                            modifier = Modifier.padding(top = 10.dp),
                        )
                        val sectionItems = items.withIndex().filter { it.value.section == section }
                        sectionItems.forEach { indexed ->
                            val item = indexed.value
                            ProfileItemEditor(
                                item = item,
                                kind = kind,
                                onChange = { updated -> items[indexed.index] = updated },
                                onRemove = { items.removeAt(indexed.index) },
                                defaultQuote = material.text?.lineSequence()?.firstOrNull { it.isNotBlank() }?.take(60) ?: "",
                            )
                        }
                        Spacer(Modifier.height(4.dp))
                        AssistChip(
                            onClick = {
                                val firstLine =
                                    material.text?.lineSequence()?.firstOrNull { it.isNotBlank() }?.take(60) ?: ""
                                items.add(
                                    ItemDraft(
                                        section = section,
                                        itemId = "",
                                        fields = emptyMap(),
                                        quote = firstLine,
                                        origin = "user_confirmed",
                                    ),
                                )
                            },
                            label = { Text("添加${sectionLabel(section)}") },
                        )
                    }
                    Spacer(Modifier.height(10.dp))
                    PrimaryAction(
                        text = "确认并保存为不可变版本",
                        enabled = !state.loading,
                        onClick = {
                            val localError = ProfileDraftCodec.validate(kind, items.toList(), jdTitle)
                            if (localError != null) {
                                vm.notify(localError)
                            } else {
                                val profile = ProfileDraftCodec.build(
                                    kind,
                                    items.toList(),
                                    jdRoleId = prep?.role_id ?: "java-backend",
                                    jdLevel = prep?.level ?: "intern",
                                    jdTitle = jdTitle.ifBlank { material.title },
                                )
                                vm.confirmProfile(materialId, profile)
                            }
                        },
                    )
                    if (prep == null) {
                        Spacer(Modifier.height(6.dp))
                        Text(
                            "未处于准备流程中：确认后的版本可在材料库中复用。",
                            style = MaterialTheme.typography.bodySmall,
                            color = MaterialTheme.colorScheme.onSurfaceVariant,
                        )
                    }
                }
                Spacer(Modifier.height(10.dp))
                InfoCard(title = "AI 解析（可选）", subtitle = "解析会把你确认发送的材料文本交给服务端配置的外部模型") {
                    Text(
                        "不解析也可以直接手动填写并确认。解析成功不代表事实已确认，仍需你核对。",
                        style = MaterialTheme.typography.bodySmall,
                    )
                    Spacer(Modifier.height(8.dp))
                    SecondaryAction(text = "AI 解析并生成结构", onClick = { showParseConsent = true })
                }
                Spacer(Modifier.height(10.dp))
                InfoCard(title = "已确认版本", subtitle = "可以复用一个已确认版本；新版本不会覆盖旧版本") {
                    if (state.snapshots.isEmpty()) {
                        EmptyHint("还没有已确认版本。")
                    } else {
                        state.snapshots.forEach { snapshot ->
                            SnapshotRow(snapshot) { vm.bindSnapshot(kind, snapshot, preparationId) }
                        }
                    }
                }
            }
            Spacer(Modifier.height(24.dp))
        }
    }

    if (showParseConsent) {
        ConsentDialog(
            title = "确认发送材料文本？",
            body = "将把本材料的文本发送给服务端配置的外部模型用于解析。原文件不会发送。你可以拒绝并在下方手动填写结构后确认。",
            onConfirm = {
                showParseConsent = false
                val roleId = prep?.role_id
                val level = prep?.level
                if (roleId == null || level == null) {
                    vm.notify("请先从某个准备进入材料核对，再使用 AI 解析")
                } else {
                    vm.parseMaterial(materialId, roleId, level)
                }
            },
            onDismiss = { showParseConsent = false },
        )
    }
}

@Composable
private fun ProfileItemEditor(
    item: ItemDraft,
    kind: String,
    onChange: (ItemDraft) -> Unit,
    onRemove: () -> Unit,
    defaultQuote: String,
) {
    Column(
        Modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
    ) {
        fieldSpecs(kind, item.section).forEach { spec ->
            OutlinedTextField(
                value = item.fields[spec.key].orEmpty(),
                onValueChange = { value ->
                    onChange(item.copy(fields = item.fields + (spec.key to value)))
                },
                label = { Text(spec.label) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
        }
        if (item.section == "projects" || item.section == "requirements") {
            OutlinedTextField(
                value = item.skillCodes,
                onValueChange = { onChange(item.copy(skillCodes = it)) },
                label = { Text("技能码（逗号分隔，可空）") },
                singleLine = true,
                modifier = Modifier.fillMaxWidth(),
            )
            Spacer(Modifier.height(4.dp))
        }
        OutlinedTextField(
            value = item.quote,
            onValueChange = { onChange(item.copy(quote = it)) },
            label = { Text("引用原文（必须与原文逐字一致）") },
            modifier = Modifier.fillMaxWidth(),
            supportingText = { Text("示例：${defaultQuote.take(40)}") },
        )
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
            FilterChip(
                selected = item.origin == "extracted",
                onClick = { onChange(item.copy(origin = "extracted")) },
                label = { Text("自动提取") },
            )
            FilterChip(
                selected = item.origin == "user_confirmed",
                onClick = { onChange(item.copy(origin = "user_confirmed")) },
                label = { Text("我手动确认") },
            )
            TextButton(onClick = onRemove) { Text("删除") }
        }
    }
}

@Composable
private fun SnapshotRow(snapshot: SnapshotDto, onUse: () -> Unit) {
    Column(Modifier.fillMaxWidth().padding(vertical = 6.dp)) {
        KeyValueRow("版本", "revision ${snapshot.source_revision}")
        KeyValueRow("确认时间", snapshot.confirmed_at)
        KeyValueRow("标题", snapshot.title)
        Spacer(Modifier.height(4.dp))
        AssistChip(onClick = onUse, label = { Text("用于本次准备") })
    }
}
