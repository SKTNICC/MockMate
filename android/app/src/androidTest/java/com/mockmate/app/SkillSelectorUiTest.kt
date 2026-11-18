package com.mockmate.app

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTextReplacement
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.mockmate.app.core.network.ErrorEnvelope
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.SkillCodeDto
import com.mockmate.app.core.network.SkillCodesResponse
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SnapshotsResponse
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppRoot
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.SkillCatalogStatus
import io.ktor.client.engine.HttpClientEngineBase
import io.ktor.client.engine.HttpClientEngineConfig
import io.ktor.client.engine.callContext
import io.ktor.client.plugins.HttpTimeout
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpProtocolVersion
import io.ktor.http.HttpStatusCode
import io.ktor.http.content.OutgoingContent
import io.ktor.http.content.TextContent
import io.ktor.http.headersOf
import io.ktor.util.InternalAPI
import io.ktor.util.date.GMTDate
import io.ktor.utils.io.ByteReadChannel
import java.util.concurrent.CopyOnWriteArrayList
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SkillSelectorUiTest {

    @get:Rule
    val composeRule = createComposeRule()

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }

    private class FakeHttpEngine(
        private val handler: suspend (HttpRequestData) -> Pair<HttpStatusCode, String>,
    ) : HttpClientEngineBase("fake") {
        override val config = HttpClientEngineConfig()
        override val dispatcher: CoroutineDispatcher = Dispatchers.Default
        override val supportedCapabilities = setOf(HttpTimeout.Plugin)

        @OptIn(InternalAPI::class)
        override suspend fun execute(data: HttpRequestData): HttpResponseData {
            val (status, body) = handler(data)
            return HttpResponseData(
                statusCode = status,
                requestTime = GMTDate(),
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
                version = HttpProtocolVersion.HTTP_1_1,
                body = ByteReadChannel(body),
                callContext = callContext(),
            )
        }
    }

    private fun requestBodyJson(data: HttpRequestData): JsonObject {
        val text = when (val body = data.body) {
            is TextContent -> body.text
            is OutgoingContent.ByteArrayContent -> body.bytes().decodeToString()
            else -> error("Unsupported request body type: ${body::class.simpleName}")
        }
        return json.parseToJsonElement(text).jsonObject
    }

    private fun authenticateAndWait(vm: AppViewModel) {
        composeRule.runOnUiThread {
            vm.authenticate(false, "tester", "password12345")
        }
        try {
            composeRule.waitUntil(5_000) {
                vm.state.value.account != null || vm.state.value.message?.isError == true
            }
        } catch (_: Throwable) {
            // 下方断言给出明确的 ViewModel 错误码和提示信息
        }
        val state = vm.state.value
        assertNotNull(
            "模拟登录未完成：code=${state.message?.code}, message=${state.message?.text}",
            state.account,
        )
    }

    private fun catalogJson(): String = json.encodeToString(
        SkillCodesResponse(
            listOf(
                SkillCodeDto("java.concurrency", "Java 并发"),
                SkillCodeDto("java.core", "Java 基础"),
                SkillCodeDto("test.junit", "JUnit 测试"),
            ),
        ),
    )

    private fun loginJson(): String = json.encodeToString(
        TokenResponse(
            access_token = "tok",
            token_type = "bearer",
            expires_at = "2026-12-01T00:00:00Z",
            user = UserDto("acc-1", "tester"),
        ),
    )

    @Test
    fun reviewScreenSearchSelectUnavailableTagAndClear() {
        val parsedResume = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("resume"),
                "education" to JsonArray(emptyList()),
                "work_experience" to JsonArray(emptyList()),
                "projects" to JsonArray(
                    listOf(
                        JsonObject(
                            mapOf(
                                "item_id" to JsonPrimitive("project-1"),
                                "name" to JsonPrimitive("图书系统"),
                                "description" to JsonPrimitive("使用 Java 集合"),
                                "skill_codes" to JsonArray(
                                    listOf(
                                        JsonPrimitive("legacy.removed"),
                                        JsonPrimitive("java.core"),
                                    ),
                                ),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("课程项目：图书系统"),
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
                                "name" to JsonPrimitive("单元测试"),
                                "code" to JsonPrimitive("test.junit"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("熟悉 JUnit 测试"),
                                        "origin" to JsonPrimitive("extracted"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        val draft = MaterialDraftDto(
            id = "mat-1",
            revision = 1,
            kind = "resume",
            title = "测试简历",
            text = "课程项目：图书系统\n熟悉 JUnit 测试",
            profile = parsedResume,
            state = "parsed",
            created_at = "2026-11-18T00:00:00Z",
            updated_at = "2026-11-18T00:00:00Z",
        )
        val confirmBodies = CopyOnWriteArrayList<JsonObject>()
        var confirmedSnapshot: SnapshotDto? = null
        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = FakeHttpEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> HttpStatusCode.OK to loginJson()
                    "/v1/roles", "/v1/preparations", "/v1/materials" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/materials/mat-1" -> HttpStatusCode.OK to json.encodeToString(
                        draft.copy(state = if (confirmedSnapshot != null) "confirmed" else "parsed"),
                    )
                    "/v1/materials/mat-1/snapshots" -> HttpStatusCode.OK to json.encodeToString(
                        SnapshotsResponse(listOfNotNull(confirmedSnapshot)),
                    )
                    "/v1/materials/mat-1/confirm" -> {
                        val body = requestBodyJson(request)
                        confirmBodies.add(body)
                        val snapshot = SnapshotDto(
                            id = "snap-1",
                            material_id = "mat-1",
                            source_revision = 1,
                            kind = "resume",
                            title = "测试简历",
                            text = draft.text ?: "",
                            profile = body["profile"]!!.jsonObject,
                            confirmed_at = "2026-11-18T00:05:00Z",
                        )
                        confirmedSnapshot = snapshot
                        HttpStatusCode.OK to json.encodeToString(snapshot)
                    }
                    "/v1/skill-codes" -> HttpStatusCode.OK to catalogJson()
                    else -> HttpStatusCode.NotFound to json.encodeToString(
                        ErrorEnvelope(ErrorInfo("NOT_FOUND", "not found", false)),
                    )
                }
            },
            bootstrapOnInit = false,
        )

        composeRule.setContent { AppRoot(vm) }
        authenticateAndWait(vm)
        composeRule.runOnUiThread { vm.openReview("mat-1", "prep-1") }

        composeRule.waitUntil(5_000) {
            vm.state.value.skillCatalogStatus == SkillCatalogStatus.LOADED &&
                composeRule.onAllNodesWithText("关联技能（可选）").fetchSemanticsNodes().isNotEmpty()
        }

        composeRule.onNodeWithText("不可用标签").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("此技能标签已不可用，请移除或替换。", substring = true)
            .performScrollTo()
            .assertIsDisplayed()
        composeRule.onNodeWithText("Java 基础").performScrollTo().assertIsDisplayed()

        composeRule.onNodeWithText("替换技能").performScrollTo().performClick()
        composeRule.onNodeWithText("搜索技能").performTextInput("并发")
        composeRule.onNodeWithText("Java 并发").performClick()
        composeRule.onNodeWithText("确定").performClick()

        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("不可用标签").fetchSemanticsNodes().isEmpty()
        }
        assertTrue(
            composeRule.onAllNodesWithText("此技能标签已不可用，请移除或替换。", substring = true)
                .fetchSemanticsNodes()
                .isEmpty(),
        )
        composeRule.onNodeWithText("Java 基础").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Java 并发").performScrollTo().assertIsDisplayed()

        composeRule.onNodeWithText("确认并保存为不可变版本").performScrollTo().performClick()
        composeRule.waitUntil(5_000) {
            confirmBodies.isNotEmpty() && vm.state.value.snapshots.isNotEmpty()
        }

        val profile = confirmBodies.single()["profile"]!!.jsonObject
        val project = profile["projects"]!!.jsonArray[0].jsonObject
        val projectCodes = project["skill_codes"]!!.jsonArray.map { it.jsonPrimitive.content }
        assertEquals(listOf("java.core", "java.concurrency"), projectCodes)
        assertEquals("课程项目：图书系统", project["evidence"]!!.jsonObject["quote"]?.jsonPrimitive?.content)
        val skill = profile["skills"]!!.jsonArray[0].jsonObject
        assertEquals("test.junit", skill["code"]?.jsonPrimitive?.content)
        composeRule.onNodeWithText("revision 1").performScrollTo().assertIsDisplayed()
        composeRule.runOnUiThread { vm.closeForTest() }
    }

    @Test
    fun jdMultiSelectSearchPreservesSelectionCancelKeepsOriginalAndClearSubmitsEmptyArray() {
        val parsedJd = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("jd"),
                "role_id" to JsonPrimitive("java-backend"),
                "level" to JsonPrimitive("intern"),
                "title" to JsonPrimitive("Java 后端实习生"),
                "responsibilities" to JsonArray(
                    listOf(
                        JsonObject(
                            mapOf(
                                "item_id" to JsonPrimitive("resp-1"),
                                "text" to JsonPrimitive("参与后端接口开发"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("职责：参与后端接口开发"),
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
                                "item_id" to JsonPrimitive("req-1"),
                                "text" to JsonPrimitive("掌握 Java 与并发编程"),
                                "skill_codes" to JsonArray(
                                    listOf(
                                        JsonPrimitive("legacy.removed"),
                                        JsonPrimitive("java.core"),
                                    ),
                                ),
                                "importance" to JsonPrimitive("must"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("要求：掌握 Java 与并发编程"),
                                        "origin" to JsonPrimitive("extracted"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        val draft = MaterialDraftDto(
            id = "mat-jd",
            revision = 1,
            kind = "jd",
            title = "Java 后端实习生",
            text = "职责：参与后端接口开发\n要求：掌握 Java 与并发编程",
            profile = parsedJd,
            state = "parsed",
            created_at = "2026-11-18T00:00:00Z",
            updated_at = "2026-11-18T00:00:00Z",
        )
        val confirmBodies = CopyOnWriteArrayList<JsonObject>()
        var snapshotCount = 0
        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = FakeHttpEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> HttpStatusCode.OK to loginJson()
                    "/v1/roles", "/v1/preparations", "/v1/materials" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/materials/mat-jd" -> HttpStatusCode.OK to json.encodeToString(draft)
                    "/v1/materials/mat-jd/snapshots" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/materials/mat-jd/confirm" -> {
                        val body = requestBodyJson(request)
                        confirmBodies.add(body)
                        snapshotCount += 1
                        val snapshot = SnapshotDto(
                            id = "snap-jd-$snapshotCount",
                            material_id = "mat-jd",
                            source_revision = 1,
                            kind = "jd",
                            title = "Java 后端实习生",
                            text = draft.text ?: "",
                            profile = body["profile"]!!.jsonObject,
                            confirmed_at = "2026-11-18T00:05:00Z",
                        )
                        HttpStatusCode.OK to json.encodeToString(snapshot)
                    }
                    "/v1/skill-codes" -> HttpStatusCode.OK to catalogJson()
                    else -> HttpStatusCode.NotFound to json.encodeToString(
                        ErrorEnvelope(ErrorInfo("NOT_FOUND", "not found", false)),
                    )
                }
            },
            bootstrapOnInit = false,
        )

        composeRule.setContent { AppRoot(vm) }
        authenticateAndWait(vm)
        composeRule.runOnUiThread { vm.openReview("mat-jd", "prep-1") }
        composeRule.waitUntil(5_000) {
            vm.state.value.skillCatalogStatus == SkillCatalogStatus.LOADED &&
                composeRule.onAllNodesWithText("替换技能").fetchSemanticsNodes().isNotEmpty()
        }

        // 取消替换应保留原条目和不可用标签
        composeRule.onNodeWithText("替换技能").performScrollTo().performClick()
        composeRule.onNodeWithText("Java 并发").performClick()
        composeRule.onNodeWithText("取消").performClick()
        composeRule.onNodeWithText("不可用标签").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Java 基础").performScrollTo().assertIsDisplayed()

        // 跨搜索多选保留既有合法选择与多次搜索勾选项
        composeRule.onNodeWithText("替换技能").performScrollTo().performClick()
        composeRule.onNodeWithText("搜索技能").performTextInput("并发")
        composeRule.onNodeWithText("Java 并发").performClick()
        composeRule.onNodeWithText("搜索技能").performTextReplacement("JUnit")
        composeRule.onNodeWithText("JUnit 测试").performClick()
        composeRule.onNodeWithText("确定").performClick()

        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("不可用标签").fetchSemanticsNodes().isEmpty()
        }
        composeRule.onNodeWithText("Java 基础").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("Java 并发").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("JUnit 测试").performScrollTo().assertIsDisplayed()

        // 主动清空多选后提交，skill_codes 应为空数组 []
        composeRule.onNodeWithText("选择技能").performScrollTo().performClick()
        composeRule.onNodeWithText("清空").performClick()
        composeRule.onNodeWithText("确定").performClick()
        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("Java 基础").fetchSemanticsNodes().isEmpty()
        }
        composeRule.onNodeWithText("确认并保存为不可变版本").performScrollTo().performClick()
        composeRule.waitUntil(5_000) { confirmBodies.isNotEmpty() }

        val req = confirmBodies.single()["profile"]!!.jsonObject["requirements"]!!.jsonArray[0].jsonObject
        assertEquals(0, req["skill_codes"]!!.jsonArray.size)
        composeRule.runOnUiThread { vm.closeForTest() }
    }

    @Test
    fun resumeSingleSelectReplaceClearAndCatalogFailurePreserveTags() {
        val parsedResume = JsonObject(
            mapOf(
                "kind" to JsonPrimitive("resume"),
                "education" to JsonArray(emptyList()),
                "work_experience" to JsonArray(emptyList()),
                "projects" to JsonArray(emptyList()),
                "skills" to JsonArray(
                    listOf(
                        JsonObject(
                            mapOf(
                                "item_id" to JsonPrimitive("skill-1"),
                                "name" to JsonPrimitive("后端技能"),
                                "code" to JsonPrimitive("legacy.removed"),
                                "evidence" to JsonObject(
                                    mapOf(
                                        "quote" to JsonPrimitive("熟悉后端并发开发"),
                                        "origin" to JsonPrimitive("extracted"),
                                    ),
                                ),
                            ),
                        ),
                    ),
                ),
            ),
        )
        val draft = MaterialDraftDto(
            id = "mat-single",
            revision = 1,
            kind = "resume",
            title = "单选测试简历",
            text = "熟悉后端并发开发",
            profile = parsedResume,
            state = "parsed",
            created_at = "2026-11-18T00:00:00Z",
            updated_at = "2026-11-18T00:00:00Z",
        )
        val skillCatalogCalls = AtomicInteger(0)
        val confirmBodies = CopyOnWriteArrayList<JsonObject>()
        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = FakeHttpEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> HttpStatusCode.OK to loginJson()
                    "/v1/roles", "/v1/preparations", "/v1/materials" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/materials/mat-single" -> HttpStatusCode.OK to json.encodeToString(draft)
                    "/v1/materials/mat-single/snapshots" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/materials/mat-single/confirm" -> {
                        val body = requestBodyJson(request)
                        confirmBodies.add(body)
                        val snapshot = SnapshotDto(
                            id = "snap-s-${confirmBodies.size}",
                            material_id = "mat-single",
                            source_revision = 1,
                            kind = "resume",
                            title = "单选测试简历",
                            text = draft.text ?: "",
                            profile = body["profile"]!!.jsonObject,
                            confirmed_at = "2026-11-18T00:05:00Z",
                        )
                        HttpStatusCode.OK to json.encodeToString(snapshot)
                    }
                    "/v1/skill-codes" -> if (skillCatalogCalls.incrementAndGet() == 1) {
                        HttpStatusCode.ServiceUnavailable to json.encodeToString(
                            ErrorEnvelope(ErrorInfo("SERVICE_DOWN", "目录暂时不可用", true)),
                        )
                    } else {
                        HttpStatusCode.OK to catalogJson()
                    }
                    else -> HttpStatusCode.NotFound to json.encodeToString(
                        ErrorEnvelope(ErrorInfo("NOT_FOUND", "not found", false)),
                    )
                }
            },
            bootstrapOnInit = false,
        )

        composeRule.setContent { AppRoot(vm) }
        authenticateAndWait(vm)
        composeRule.runOnUiThread { vm.openReview("mat-single", "prep-1") }

        // 目录临时不可用时，保留原有标签不静默清空，并展示重试入口
        composeRule.waitUntil(5_000) {
            vm.state.value.skillCatalogStatus == SkillCatalogStatus.FAILED &&
                composeRule.onAllNodesWithText("重新加载技能").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("已选技能").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("技能目录加载失败：目录暂时不可用", substring = true)
            .performScrollTo()
            .assertIsDisplayed()

        // 重试加载目录成功，单选替换失效标签并确认
        composeRule.onNodeWithText("重新加载技能").performScrollTo().performClick()
        composeRule.waitUntil(5_000) {
            vm.state.value.skillCatalogStatus == SkillCatalogStatus.LOADED &&
                composeRule.onAllNodesWithText("替换技能").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("替换技能").performScrollTo().performClick()
        composeRule.onNodeWithText("Java 并发").performClick()
        composeRule.onNodeWithText("确定").performClick()

        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("不可用标签").fetchSemanticsNodes().isEmpty()
        }
        composeRule.onNodeWithText("Java 并发").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("确认并保存为不可变版本").performScrollTo().performClick()
        composeRule.waitUntil(5_000) { confirmBodies.size >= 1 }
        val firstSkill = confirmBodies[0]["profile"]!!.jsonObject["skills"]!!.jsonArray[0].jsonObject
        assertEquals("java.concurrency", firstSkill["code"]?.jsonPrimitive?.content)

        // 清空单选标签并确认，skills[].code 应为 null
        composeRule.onNodeWithText("更换技能").performScrollTo().performClick()
        composeRule.onNodeWithText("清空").performClick()
        composeRule.onNodeWithText("确定").performClick()
        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("Java 并发").fetchSemanticsNodes().isEmpty()
        }
        composeRule.onNodeWithText("确认并保存为不可变版本").performScrollTo().performClick()
        composeRule.waitUntil(5_000) { confirmBodies.size >= 2 }
        val clearedSkill = confirmBodies[1]["profile"]!!.jsonObject["skills"]!!.jsonArray[0].jsonObject
        assertTrue(clearedSkill["code"] is JsonNull)
        composeRule.runOnUiThread { vm.closeForTest() }
    }

    @Test
    fun planHubDisplaysInsufficientBankCountAndReturnAction() {
        val prep = PreparationDto(
            id = "prep-1",
            revision = 1,
            role_id = "java-backend",
            level = "intern",
            main_question_count = 6,
            resume_snapshot_id = "snap-r",
            jd_snapshot_id = "snap-j",
            match_report_id = "mr-1",
            created_at = "2026-11-18T00:00:00Z",
            updated_at = "2026-11-18T00:00:00Z",
        )
        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            httpEngine = FakeHttpEngine { request ->
                when (request.url.encodedPath) {
                    "/v1/auth/login" -> HttpStatusCode.OK to loginJson()
                    "/v1/roles", "/v1/preparations", "/v1/materials" -> HttpStatusCode.OK to """{"items":[]}"""
                    "/v1/preparations/prep-1" -> HttpStatusCode.OK to json.encodeToString(prep)
                    "/v1/preparations/prep-1/generate-plan" -> HttpStatusCode.Conflict to json.encodeToString(
                        ErrorEnvelope(
                            ErrorInfo(
                                code = "BANK_INSUFFICIENT",
                                message = "题库不足",
                                retryable = false,
                                details = listOf("required=3", "available=1"),
                            ),
                        ),
                    )
                    else -> HttpStatusCode.OK to """{"items":[]}"""
                }
            },
            bootstrapOnInit = false,
        )

        composeRule.setContent { AppRoot(vm) }
        authenticateAndWait(vm)
        composeRule.runOnUiThread { vm.openPreparation("prep-1") }
        composeRule.waitUntil(5_000) { vm.state.value.stack.last() == Screen.Setup("prep-1") }
        composeRule.runOnUiThread { vm.openPlan("prep-1") }

        composeRule.onNodeWithText("生成计划").performClick()
        composeRule.onNodeWithText("同意并继续").performClick()
        composeRule.waitUntil(5_000) {
            composeRule.onAllNodesWithText("本场需要 3 道相关题库题，目前匹配到 1 道", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("回到准备检查材料关联").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("回到设置选择较少主问题（可选 2 题）").performScrollTo().performClick()
        composeRule.waitUntil(5_000) { vm.state.value.stack.last() == Screen.Setup("prep-1") }
        composeRule.runOnUiThread { vm.closeForTest() }
    }
}
