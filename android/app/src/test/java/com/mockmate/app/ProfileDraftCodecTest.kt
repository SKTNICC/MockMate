package com.mockmate.app

import com.mockmate.app.ui.screens.ItemDraft
import com.mockmate.app.ui.screens.ProfileDraftCodec
import com.mockmate.app.ui.screens.sanitizeSkillSelection
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class ProfileDraftCodecTest {

    private val resumeProfile = JsonObject(
        mapOf(
            "kind" to JsonPrimitive("resume"),
            "education" to JsonArray(
                listOf(
                    JsonObject(
                        mapOf(
                            "item_id" to JsonPrimitive("edu-1"),
                            "degree" to JsonPrimitive("本科在读"),
                            "major" to JsonPrimitive("计算机科学与技术"),
                            "evidence" to JsonObject(
                                mapOf(
                                    "quote" to JsonPrimitive("本科在读，计算机科学与技术专业。"),
                                    "origin" to JsonPrimitive("extracted"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            "work_experience" to JsonArray(emptyList()),
            "projects" to JsonArray(
                listOf(
                    JsonObject(
                        mapOf(
                            "item_id" to JsonPrimitive("project-1"),
                            "name" to JsonPrimitive("图书借阅系统"),
                            "description" to JsonPrimitive("使用 Java 集合实现借阅模块"),
                            "skill_codes" to JsonArray(
                                listOf(JsonPrimitive("java.core"), JsonPrimitive("test.junit")),
                            ),
                            "evidence" to JsonObject(
                                mapOf(
                                    "quote" to JsonPrimitive("课程项目：校园图书借阅系统"),
                                    "origin" to JsonPrimitive("extracted"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
            "skills" to JsonArray(
                listOf(
                    JsonObject(
                        mapOf(
                            "item_id" to JsonPrimitive("skill-1"),
                            "name" to JsonPrimitive("Java 集合"),
                            "code" to JsonPrimitive("java.core"),
                            "evidence" to JsonObject(
                                mapOf(
                                    "quote" to JsonPrimitive("学习过 Java 集合"),
                                    "origin" to JsonPrimitive("extracted"),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    @Test
    fun parseResumeProfileKeepsFieldsAndEvidence() {
        val items = ProfileDraftCodec.parse(resumeProfile, "resume")
        assertEquals(3, items.size)
        assertEquals("education", items[0].section)
        assertEquals("本科在读", items[0].fields["degree"])
        assertEquals("本科在读，计算机科学与技术专业。", items[0].quote)
        assertEquals("项目", com.mockmate.app.ui.screens.sectionLabel("projects"))
        assertEquals("图书借阅系统", items[1].fields["name"])
        assertEquals(listOf("java.core", "test.junit"), items[1].skillCodes)
        assertEquals("skills", items[2].section)
        assertEquals("Java 集合", items[2].fields["name"])
        assertEquals(listOf("java.core"), items[2].skillCodes)
    }

    @Test
    fun buildResumeProfileMatchesContractShape() {
        val items = ProfileDraftCodec.parse(resumeProfile, "resume")
        val built = ProfileDraftCodec.build("resume", items, "java-backend", "intern", "简历")
        assertEquals("resume", built["kind"]?.jsonPrimitive?.content)
        val education = built["education"]!!.jsonArray[0].jsonObject
        assertEquals("edu-1", education["item_id"]?.jsonPrimitive?.content)
        assertEquals("extracted", education["evidence"]!!.jsonObject["origin"]?.jsonPrimitive?.content)
        val project = built["projects"]!!.jsonArray[0].jsonObject
        val codes = project["skill_codes"]!!.jsonArray.map { it.jsonPrimitive.content }
        assertEquals(listOf("java.core", "test.junit"), codes)
        val skill = built["skills"]!!.jsonArray[0].jsonObject
        assertEquals("java.core", skill["code"]?.jsonPrimitive?.content)
        assertNull(built["role_id"])
    }

    @Test
    fun nullableFieldsAndEmptySkillBecomeJsonNull() {
        val item = ItemDraft(
            section = "skills",
            itemId = "skill-1",
            fields = mapOf("name" to "Java"),
            quote = "学习过 Java",
            origin = "user_confirmed",
        )
        val built = ProfileDraftCodec.build("resume", listOf(item), "r", "intern", "简历")
        val skill = built["skills"]!!.jsonArray[0].jsonObject
        assertTrue(skill["code"] is JsonNull)
        assertEquals("skill-1", skill["item_id"]?.jsonPrimitive?.content)
    }

    @Test
    fun buildJdProfileIncludesRoleLevelTitleAndImportance() {
        val items = listOf(
            ItemDraft(
                section = "responsibilities",
                itemId = "r1",
                fields = mapOf("text" to "完成 Java 功能开发"),
                quote = "职责：完成 Java 功能开发",
                origin = "extracted",
            ),
            ItemDraft(
                section = "requirements",
                itemId = "q1",
                fields = mapOf("text" to "理解集合边界"),
                quote = "要求：理解集合边界",
                origin = "extracted",
                skillCodes = listOf("java.core", "java.core"),
                importance = "must",
            ),
        )
        val built = ProfileDraftCodec.build("jd", items, "java-backend", "intern", "Java 实习")
        assertEquals("java-backend", built["role_id"]?.jsonPrimitive?.content)
        assertEquals("intern", built["level"]?.jsonPrimitive?.content)
        val requirement = built["requirements"]!!.jsonArray[0].jsonObject
        assertEquals("must", requirement["importance"]?.jsonPrimitive?.content)
        assertEquals(
            listOf("java.core"),
            requirement["skill_codes"]!!.jsonArray.map { it.jsonPrimitive.content },
        )
    }

    @Test
    fun parseAndBuildPreservesOldJdIdsEvidenceImportanceAndEmptySkillCodes() {
        val jdProfile = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("jd"),
                "role_id" to JsonPrimitive("java-backend"),
                "level" to JsonPrimitive("intern"),
                "title" to JsonPrimitive("后端实习"),
                "responsibilities" to JsonArray(
                    listOf(
                        JsonObject(
                            mapOf(
                                "item_id" to JsonPrimitive("resp-custom"),
                                "text" to JsonPrimitive("参与服务开发"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("职责：参与服务开发"),
                                        "origin" to JsonPrimitive("extracted"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
                "requirements" to JsonArray(
                    listOf(
                        JsonObject(
                            mapOf(
                                "item_id" to JsonPrimitive("req-custom"),
                                "text" to JsonPrimitive("了解 Redis"),
                                "skill_codes" to JsonArray(emptyList()),
                                "importance" to JsonPrimitive("nice"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("要求：了解 Redis"),
                                        "origin" to JsonPrimitive("extracted"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        val items = ProfileDraftCodec.parse(jdProfile, "jd")
        assertEquals("resp-custom", items[0].itemId)
        assertEquals("req-custom", items[1].itemId)
        assertEquals("nice", items[1].importance)
        assertEquals(emptyList<String>(), items[1].skillCodes)
        val rebuilt = ProfileDraftCodec.build("jd", items, "java-backend", "intern", "后端实习")
        val rebuiltReq = rebuilt["requirements"]!!.jsonArray[0].jsonObject
        assertEquals("req-custom", rebuiltReq["item_id"]?.jsonPrimitive?.content)
        assertEquals("nice", rebuiltReq["importance"]?.jsonPrimitive?.content)
        assertEquals("要求：了解 Redis", rebuiltReq["evidence"]!!.jsonObject["quote"]?.jsonPrimitive?.content)
        assertEquals(0, rebuiltReq["skill_codes"]!!.jsonArray.size)
    }

    @Test
    fun validateRejectsMissingRequiredFieldQuoteAndSections() {
        val base = ItemDraft(
            section = "education",
            itemId = "",
            fields = mapOf("degree" to "本科", "major" to "CS"),
            quote = "本科在读",
            origin = "user_confirmed",
        )
        assertNull(ProfileDraftCodec.validate("resume", listOf(base), ""))
        val skillWithoutCode = ItemDraft(
            section = "skills",
            itemId = "",
            fields = mapOf("name" to "Redis"),
            quote = "熟悉 Redis",
            origin = "user_confirmed",
            skillCodes = emptyList(),
        )
        assertNull(ProfileDraftCodec.validate("resume", listOf(skillWithoutCode), ""))
        val projectWithBlankName = ItemDraft(
            section = "projects",
            itemId = "",
            fields = mapOf("name" to "", "description" to "描述"),
            quote = "课程项目",
            origin = "user_confirmed",
        )
        assertEquals(
            "项目的“项目名”不能为空",
            ProfileDraftCodec.validate("resume", listOf(projectWithBlankName), ""),
        )
        assertEquals(
            "教育的引用原文不能为空",
            ProfileDraftCodec.validate("resume", listOf(base.copy(quote = " ")), ""),
        )
        assertEquals(
            "至少需要一条结构化内容；可点击“添加”补充",
            ProfileDraftCodec.validate("resume", emptyList(), ""),
        )
        assertEquals(
            "JD 至少需要一条职责和一条显式要求",
            ProfileDraftCodec.validate("jd", listOf(base.copy(section = "responsibilities", fields = mapOf("text" to "开发"))), "标题"),
        )
        assertEquals(
            "JD 需要岗位标题",
            ProfileDraftCodec.validate("jd", listOf(base), "  "),
        )
    }

    @Test
    fun sanitizeSkillSelectionDropsUnavailableCodesPreservesOrderAndSingleSelectRule() {
        val available = setOf("java.core", "java.concurrency", "test.junit")
        assertEquals(
            listOf("java.core", "test.junit"),
            sanitizeSkillSelection(
                selectedCodes = listOf("legacy.removed", "java.core", "java.core", " ", "test.junit", "other.gone"),
                availableCodes = available,
                multiSelect = true,
            ),
        )
        assertEquals(
            listOf("java.core"),
            sanitizeSkillSelection(
                selectedCodes = listOf("legacy.removed", "java.core", "java.concurrency"),
                availableCodes = available,
                multiSelect = false,
            ),
        )
        assertEquals(
            emptyList<String>(),
            sanitizeSkillSelection(
                selectedCodes = listOf("legacy.removed"),
                availableCodes = available,
                multiSelect = false,
            ),
        )
        assertEquals(
            emptyList<String>(),
            sanitizeSkillSelection(
                selectedCodes = listOf("java.core"),
                availableCodes = emptySet(),
                multiSelect = true,
            ),
        )
    }

    @Test
    fun emptyProjectSkillCodesSerializesAsEmptyArray() {
        val projectWithoutSkills = ItemDraft(
            section = "projects",
            itemId = "project-1",
            fields = mapOf("name" to "图书系统", "description" to "Java"),
            quote = "图书系统",
            origin = "user_confirmed",
            skillCodes = emptyList(),
        )
        val built = ProfileDraftCodec.build("resume", listOf(projectWithoutSkills), "r", "intern", "简历")
        val project = built["projects"]!!.jsonArray[0].jsonObject
        assertEquals(0, project["skill_codes"]!!.jsonArray.size)
    }
}
