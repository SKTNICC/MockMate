package com.mockmate.app.ui.screens

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.add
import kotlinx.serialization.json.addJsonObject
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.put
import kotlinx.serialization.json.putJsonArray
import kotlinx.serialization.json.putJsonObject

internal data class FieldSpec(val key: String, val label: String)

internal data class ItemDraft(
    val section: String,
    val itemId: String,
    val fields: Map<String, String>,
    val quote: String,
    val origin: String,
    val skillCodes: List<String> = emptyList(),
    val importance: String = "must",
)

internal val SECTION_ORDER_RESUME = listOf("education", "work_experience", "projects", "skills")
internal val SECTION_ORDER_JD = listOf("responsibilities", "requirements")

internal val NULLABLE_FIELDS = setOf("degree", "major")

internal fun sectionLabel(section: String): String = when (section) {
    "education" -> "教育"
    "work_experience" -> "工作经历"
    "projects" -> "项目"
    "skills" -> "技能"
    "responsibilities" -> "职责"
    "requirements" -> "显式要求"
    else -> section
}

internal fun fieldSpecs(kind: String, section: String): List<FieldSpec> = when (section) {
    "education" -> listOf(FieldSpec("degree", "学历"), FieldSpec("major", "专业"))
    "work_experience" -> listOf(
        FieldSpec("organization", "单位"),
        FieldSpec("title", "职位"),
        FieldSpec("description", "描述"),
    )
    "projects" -> listOf(FieldSpec("name", "项目名"), FieldSpec("description", "描述"))
    "skills" -> listOf(FieldSpec("name", "技能名"))
    else -> if (kind == "jd") listOf(FieldSpec("text", "内容")) else emptyList()
}

internal fun supportsSkillSelection(section: String): Boolean =
    section == "skills" || section == "projects" || section == "requirements"

internal fun allowsMultipleSkills(section: String): Boolean =
    section == "projects" || section == "requirements"

internal fun sanitizeSkillSelection(
    selectedCodes: List<String>,
    availableCodes: Set<String>,
    multiSelect: Boolean,
): List<String> {
    val sanitized = selectedCodes
        .map { it.trim() }
        .filter { it.isNotEmpty() && it in availableCodes }
        .distinct()
    return if (multiSelect) sanitized else sanitized.take(1)
}

/**
 * 结构化核对的纯逻辑编解码器：把材料 profile 转成可编辑条目，再按契约重建 JSON。
 * 与服务端 schema 对齐；不依赖 Compose，便于 JVM 单测。
 */
internal object ProfileDraftCodec {

    fun parse(profile: JsonObject?, kind: String): List<ItemDraft> {
        if (profile == null) return emptyList()
        val sections = if (kind == "resume") SECTION_ORDER_RESUME else SECTION_ORDER_JD
        val result = mutableListOf<ItemDraft>()
        sections.forEach { section ->
            val array = profile[section] as? JsonArray ?: return@forEach
            array.forEach { element ->
                val obj = element as? JsonObject ?: return@forEach
                val fields = fieldSpecs(kind, section).associate { spec ->
                    spec.key to ((obj[spec.key] as? JsonPrimitive)?.contentOrNull ?: "")
                }
                val evidence = obj["evidence"] as? JsonObject
                val quote = (evidence?.get("quote") as? JsonPrimitive)?.contentOrNull ?: ""
                val origin = (evidence?.get("origin") as? JsonPrimitive)?.contentOrNull ?: "extracted"
                val codes = when (section) {
                    "skills" -> listOfNotNull(
                        (obj["code"] as? JsonPrimitive)?.contentOrNull?.trim()?.takeIf { it.isNotEmpty() },
                    )
                    "projects", "requirements" -> (obj["skill_codes"] as? JsonArray)
                        ?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull?.trim()?.takeIf { s -> s.isNotEmpty() } }
                        ?.distinct()
                        ?: emptyList()
                    else -> emptyList()
                }
                val itemId = (obj["item_id"] as? JsonPrimitive)?.contentOrNull ?: ""
                val importance = (obj["importance"] as? JsonPrimitive)?.contentOrNull ?: "must"
                result.add(ItemDraft(section, itemId, fields, quote, origin, codes, importance))
            }
        }
        return result
    }

    fun resolvedItemId(item: ItemDraft, items: List<ItemDraft>): String {
        if (item.itemId.isNotBlank()) return item.itemId
        val sectionItems = items.filter { it.section == item.section }
        val index = sectionItems.indexOf(item).let { if (it >= 0) it else 0 }
        return "${item.section}-${index + 1}"
    }

    fun build(
        kind: String,
        items: List<ItemDraft>,
        jdRoleId: String,
        jdLevel: String,
        jdTitle: String,
    ): JsonObject {
        val sections = if (kind == "resume") SECTION_ORDER_RESUME else SECTION_ORDER_JD
        return buildJsonObject {
            put("kind", kind)
            if (kind == "jd") {
                put("role_id", jdRoleId)
                put("level", jdLevel)
                put("title", jdTitle)
            }
            sections.forEach { section ->
                putJsonArray(section) {
                    items.filter { it.section == section }.forEachIndexed { index, item ->
                        addJsonObject {
                            put("item_id", item.itemId.ifBlank { "$section-${index + 1}" })
                            fieldSpecs(kind, section).forEach { spec ->
                                val value = item.fields[spec.key].orEmpty().trim()
                                if (spec.key in NULLABLE_FIELDS && value.isBlank()) {
                                    put(spec.key, JsonNull)
                                } else {
                                    put(spec.key, value)
                                }
                            }
                            if (section == "skills") {
                                val selectedCode = item.skillCodes
                                    .map { it.trim() }
                                    .firstOrNull { it.isNotEmpty() }
                                if (selectedCode == null) {
                                    put("code", JsonNull)
                                } else {
                                    put("code", selectedCode)
                                }
                            }
                            if (section == "projects" || section == "requirements") {
                                putJsonArray("skill_codes") {
                                    item.skillCodes
                                        .map { it.trim() }
                                        .filter { it.isNotEmpty() }
                                        .distinct()
                                        .forEach { add(it) }
                                }
                            }
                            if (section == "requirements") {
                                put("importance", item.importance)
                            }
                            putJsonObject("evidence") {
                                put("quote", item.quote.trim())
                                put("origin", item.origin)
                            }
                        }
                    }
                }
            }
        }
    }

    fun validate(kind: String, items: List<ItemDraft>, jdTitle: String): String? {
        if (kind == "jd" && jdTitle.isBlank()) return "JD 需要岗位标题"
        if (items.isEmpty()) return "至少需要一条结构化内容；可点击“添加”补充"
        items.forEach { item ->
            fieldSpecs(kind, item.section).forEach { spec ->
                if (spec.key !in NULLABLE_FIELDS && item.fields[spec.key].orEmpty().isBlank()) {
                    return "${sectionLabel(item.section)}的“${spec.label}”不能为空"
                }
            }
            if (item.quote.isBlank()) return "${sectionLabel(item.section)}的引用原文不能为空"
        }
        if (kind == "jd") {
            val hasResponsibility = items.any { it.section == "responsibilities" }
            val hasRequirement = items.any { it.section == "requirements" }
            if (!hasResponsibility || !hasRequirement) return "JD 至少需要一条职责和一条显式要求"
        } else if (items.none { it.section in SECTION_ORDER_RESUME }) {
            return "简历至少需要一条教育、工作、项目或技能信息"
        }
        return null
    }
}
