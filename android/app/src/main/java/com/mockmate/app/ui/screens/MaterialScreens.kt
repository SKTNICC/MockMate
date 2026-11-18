package com.mockmate.app.ui.screens

import androidx.compose.foundation.ExperimentalFoundationApi
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
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
import androidx.compose.foundation.relocation.BringIntoViewRequester
import androidx.compose.foundation.relocation.bringIntoViewRequester
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.AssistChip
import androidx.compose.material3.Button
import androidx.compose.material3.Checkbox
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.FilterChip
import androidx.compose.material3.InputChip
import androidx.compose.material3.InputChipDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.RadioButton
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.mockmate.app.core.network.JdTemplateDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.SkillCodeDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.ui.AppState
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.SkillCatalogStatus
import com.mockmate.app.ui.components.ConsentDialog
import com.mockmate.app.ui.components.EmptyHint
import com.mockmate.app.ui.components.InfoCard
import com.mockmate.app.ui.components.JobRecoveryActions
import com.mockmate.app.ui.components.KeyValueRow
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

@OptIn(ExperimentalMaterial3Api::class, ExperimentalFoundationApi::class)
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
    val items = remember(material?.id, material?.revision) {
        mutableStateListOf<ItemDraft>().apply { addAll(ProfileDraftCodec.parse(material?.profile, kind)) }
    }
    var hasManualEdits by remember(material?.id, material?.revision) { mutableStateOf(false) }
    var appliedProfile by remember(material?.id, material?.revision) { mutableStateOf(material?.profile) }
    var showParseConsent by rememberSaveable { mutableStateOf(false) }
    var jdTitle by rememberSaveable(material?.id, material?.revision) { mutableStateOf(material?.title ?: "") }

    LaunchedEffect(material?.title) {
        if (material != null && jdTitle.isBlank()) jdTitle = material.title
    }
    LaunchedEffect(material?.id, material?.revision, material?.profile) {
        val incoming = material?.profile
        if (incoming != null && incoming != appliedProfile) {
            if (!hasManualEdits || items.isEmpty()) {
                items.clear()
                items.addAll(ProfileDraftCodec.parse(incoming, kind))
                hasManualEdits = false
            }
            appliedProfile = incoming
        }
    }

    val skillCatalogMap = remember(state.skillCodes) {
        state.skillCodes.associateBy { it.code }
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
                            val resolvedId = ProfileDraftCodec.resolvedItemId(item, items)
                            val isFlaggedInvalid = state.invalidSkillItemId != null &&
                                state.invalidSkillItemId == resolvedId
                            val bringIntoViewRequester = remember(resolvedId) { BringIntoViewRequester() }
                            LaunchedEffect(state.invalidSkillFocusNonce, isFlaggedInvalid) {
                                if (isFlaggedInvalid) {
                                    bringIntoViewRequester.bringIntoView()
                                }
                            }
                            ProfileItemEditor(
                                item = item,
                                kind = kind,
                                availableSkills = state.skillCodes,
                                skillCatalogMap = skillCatalogMap,
                                catalogStatus = state.skillCatalogStatus,
                                catalogError = state.skillCatalogError,
                                isFlaggedInvalid = isFlaggedInvalid,
                                onReloadCatalog = { vm.refreshSkillCodes() },
                                onChange = { updated ->
                                    hasManualEdits = true
                                    if (isFlaggedInvalid && updated.skillCodes != item.skillCodes) {
                                        vm.clearInvalidSkillHighlight()
                                    }
                                    items[indexed.index] = updated
                                },
                                onRemove = {
                                    hasManualEdits = true
                                    if (isFlaggedInvalid) vm.clearInvalidSkillHighlight()
                                    items.removeAt(indexed.index)
                                },
                                defaultQuote = material.text?.lineSequence()?.firstOrNull { it.isNotBlank() }?.take(60) ?: "",
                                modifier = Modifier.bringIntoViewRequester(bringIntoViewRequester),
                            )
                        }
                        Spacer(Modifier.height(4.dp))
                        AssistChip(
                            onClick = {
                                hasManualEdits = true
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

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun ProfileItemEditor(
    item: ItemDraft,
    kind: String,
    availableSkills: List<SkillCodeDto>,
    skillCatalogMap: Map<String, SkillCodeDto>,
    catalogStatus: SkillCatalogStatus,
    catalogError: String?,
    isFlaggedInvalid: Boolean,
    onReloadCatalog: () -> Unit,
    onChange: (ItemDraft) -> Unit,
    onRemove: () -> Unit,
    defaultQuote: String,
    modifier: Modifier = Modifier,
) {
    var showSkillPicker by remember { mutableStateOf(false) }

    Column(
        modifier
            .fillMaxWidth()
            .padding(vertical = 6.dp),
    ) {
        if (isFlaggedInvalid) {
            Text(
                "此条目包含已失效或未启用的技能标签，请移除或替换后重新确认。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
            Spacer(Modifier.height(4.dp))
        }
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
        if (supportsSkillSelection(item.section)) {
            SkillAssociationEditor(
                selectedCodes = item.skillCodes,
                multiSelect = allowsMultipleSkills(item.section),
                availableSkills = availableSkills,
                skillCatalogMap = skillCatalogMap,
                catalogStatus = catalogStatus,
                catalogError = catalogError,
                onReloadCatalog = onReloadCatalog,
                onOpenPicker = { showSkillPicker = true },
                onRemoveCode = { code ->
                    onChange(item.copy(skillCodes = item.skillCodes.filterNot { it == code }))
                },
                onClearAll = { onChange(item.copy(skillCodes = emptyList())) },
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

    if (showSkillPicker) {
        SkillPickerDialog(
            multiSelect = allowsMultipleSkills(item.section),
            initialSelection = item.skillCodes,
            availableSkills = availableSkills,
            onConfirm = { nextCodes ->
                onChange(item.copy(skillCodes = nextCodes))
                showSkillPicker = false
            },
            onDismiss = { showSkillPicker = false },
        )
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun SkillAssociationEditor(
    selectedCodes: List<String>,
    multiSelect: Boolean,
    availableSkills: List<SkillCodeDto>,
    skillCatalogMap: Map<String, SkillCodeDto>,
    catalogStatus: SkillCatalogStatus,
    catalogError: String?,
    onReloadCatalog: () -> Unit,
    onOpenPicker: () -> Unit,
    onRemoveCode: (String) -> Unit,
    onClearAll: () -> Unit,
) {
    Text("关联技能（可选）", style = MaterialTheme.typography.labelMedium)
    Spacer(Modifier.height(4.dp))
    val hasUnavailableTag = catalogStatus == SkillCatalogStatus.LOADED &&
        selectedCodes.any { skillCatalogMap[it] == null }
    if (selectedCodes.isNotEmpty()) {
        FlowRow(
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(4.dp),
        ) {
            selectedCodes.forEach { code ->
                val matched = skillCatalogMap[code]
                val unavailable = catalogStatus == SkillCatalogStatus.LOADED && matched == null
                val label = when {
                    matched != null -> matched.label
                    unavailable -> "不可用标签"
                    else -> "已选技能"
                }
                InputChip(
                    selected = !unavailable,
                    onClick = { onRemoveCode(code) },
                    label = { Text(label) },
                    trailingIcon = { Text("×") },
                    colors = if (unavailable) {
                        InputChipDefaults.inputChipColors(
                            containerColor = MaterialTheme.colorScheme.errorContainer,
                            labelColor = MaterialTheme.colorScheme.onErrorContainer,
                            trailingIconColor = MaterialTheme.colorScheme.onErrorContainer,
                        )
                    } else {
                        InputChipDefaults.inputChipColors()
                    },
                )
            }
        }
        if (hasUnavailableTag) {
            Spacer(Modifier.height(4.dp))
            Text(
                "此技能标签已不可用，请移除或替换。",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
        }
        Spacer(Modifier.height(4.dp))
    }

    when (catalogStatus) {
        SkillCatalogStatus.IDLE, SkillCatalogStatus.LOADING -> {
            Text(
                "正在加载可选技能…",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
        SkillCatalogStatus.FAILED -> {
            Text(
                "技能目录加载失败：${catalogError ?: "请稍后重试"}",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.error,
            )
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                AssistChip(onClick = onReloadCatalog, label = { Text("重新加载技能") })
                if (selectedCodes.isNotEmpty()) {
                    TextButton(onClick = onClearAll) { Text("清除已选") }
                }
            }
        }
        SkillCatalogStatus.LOADED -> {
            if (availableSkills.isEmpty()) {
                Text(
                    "当前没有可用的技能标签。",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                if (selectedCodes.isNotEmpty()) {
                    TextButton(onClick = onClearAll) { Text("移除标签") }
                }
            } else {
                Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                    val actionLabel = when {
                        hasUnavailableTag -> "替换技能"
                        multiSelect || selectedCodes.isEmpty() -> "选择技能"
                        else -> "更换技能"
                    }
                    AssistChip(onClick = onOpenPicker, label = { Text(actionLabel) })
                    if (selectedCodes.isNotEmpty()) {
                        TextButton(onClick = onClearAll) { Text("清除") }
                    }
                }
            }
        }
    }
}

@Composable
private fun SkillPickerDialog(
    multiSelect: Boolean,
    initialSelection: List<String>,
    availableSkills: List<SkillCodeDto>,
    onConfirm: (List<String>) -> Unit,
    onDismiss: () -> Unit,
) {
    val availableCodeSet = remember(availableSkills) {
        availableSkills.map { it.code }.toSet()
    }
    var query by remember { mutableStateOf("") }
    var draftSelection by remember(initialSelection) {
        mutableStateOf(sanitizeSkillSelection(initialSelection, availableCodeSet, multiSelect))
    }
    val filtered = remember(query, availableSkills) {
        val needle = query.trim()
        if (needle.isBlank()) availableSkills
        else availableSkills.filter { it.label.contains(needle, ignoreCase = true) }
    }

    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(if (multiSelect) "选择关联技能（可多选）" else "选择关联技能（单选）") },
        text = {
            Column(Modifier.fillMaxWidth()) {
                OutlinedTextField(
                    value = query,
                    onValueChange = { query = it },
                    label = { Text("搜索技能") },
                    singleLine = true,
                    modifier = Modifier.fillMaxWidth(),
                )
                Spacer(Modifier.height(8.dp))
                if (filtered.isEmpty()) {
                    Text(
                        "没有匹配的技能标签",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                } else {
                    Column(
                        Modifier
                            .fillMaxWidth()
                            .heightIn(max = 260.dp)
                            .verticalScroll(rememberScrollState()),
                    ) {
                        filtered.forEach { skill ->
                            val selected = skill.code in draftSelection
                            val toggle = {
                                draftSelection = if (multiSelect) {
                                    if (selected) draftSelection - skill.code
                                    else (draftSelection + skill.code).distinct()
                                } else {
                                    if (selected) emptyList() else listOf(skill.code)
                                }
                            }
                            Row(
                                modifier = Modifier
                                    .fillMaxWidth()
                                    .clickable { toggle() }
                                    .padding(vertical = 4.dp),
                                verticalAlignment = Alignment.CenterVertically,
                            ) {
                                if (multiSelect) {
                                    Checkbox(checked = selected, onCheckedChange = { toggle() })
                                } else {
                                    RadioButton(selected = selected, onClick = toggle)
                                }
                                Text(skill.label, style = MaterialTheme.typography.bodyMedium)
                            }
                        }
                    }
                }
            }
        },
        confirmButton = {
            Button(
                onClick = {
                    onConfirm(sanitizeSkillSelection(draftSelection, availableCodeSet, multiSelect))
                },
            ) { Text("确定") }
        },
        dismissButton = {
            Row(horizontalArrangement = Arrangement.spacedBy(8.dp)) {
                TextButton(onClick = { draftSelection = emptyList() }) { Text("清空") }
                TextButton(onClick = onDismiss) { Text("取消") }
            }
        },
    )
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
