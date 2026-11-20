package com.mockmate.app

import android.content.Context
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onLast
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.test.swipeUp
import androidx.test.espresso.Espresso
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import java.io.File
import java.io.OutputStreamWriter
import java.net.HttpURLConnection
import java.net.URL
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID
import kotlin.math.sin
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.BeforeClass
import org.junit.FixMethodOrder
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith
import org.junit.runners.MethodSorters

/**
 * M3 模拟器链路：隔离 test provider + fixture PCM。
 * 不代表真实麦克风/真实语音供应商；后者必须真机与真实服务分别验收。
 */
@RunWith(AndroidJUnit4::class)
@FixMethodOrder(MethodSorters.NAME_ASCENDING)
class M3VoiceDeviceTest {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    companion object {
        private val BASE = BuildConfig.API_BASE_URL
        private const val PASSWORD = "MockMate-m3-2026"
        private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
        private lateinit var username: String
        private lateinit var username2: String
        private lateinit var username3: String
        private lateinit var plan: JsonObject
        private lateinit var plan2: JsonObject
        private lateinit var plan3: JsonObject

        @BeforeClass
        @JvmStatic
        fun prepareFixtureAndAccount() {
            val context = InstrumentationRegistry.getInstrumentation().targetContext
            val fixture = File(context.filesDir, "voice_test_fixture.pcm")
            fixture.writeBytes(tonePcm(1.2))
            context.getSharedPreferences("mockmate_voice_test", Context.MODE_PRIVATE)
                .edit()
                .putBoolean("fixture_enabled", true)
                .putString("fixture_path", fixture.absolutePath)
                .commit()
            val automation = InstrumentationRegistry.getInstrumentation().uiAutomation
            automation.executeShellCommand(
                "pm grant com.mockmate.app android.permission.RECORD_AUDIO"
            ).close()

            username = "devm3" + System.currentTimeMillis().toString().takeLast(8)
            plan = createPlanFor(username)
            username2 = "devm3b" + System.currentTimeMillis().toString().takeLast(7)
            plan2 = createPlanFor(username2)
            username3 = "devm3c" + System.currentTimeMillis().toString().takeLast(7)
            plan3 = createPlanFor(username3, count = 6)
        }

        private fun createPlanFor(user: String, count: Int = 2): JsonObject {
            api("POST", "/auth/register", body = mapOf("username" to user, "password" to PASSWORD))
            val token = api("POST", "/auth/login", body = mapOf("username" to user, "password" to PASSWORD))
                .jsonObject["access_token"]!!.jsonPrimitive.content
            val resumeSnapshot = createResume(token)
            val jdSnapshot = createJd(token)
            val prep = api(
                "POST", "/preparations", token,
                buildJsonObject {
                    put("role_id", "java-backend")
                    put("level", "intern")
                    put("main_question_count", count)
                    put("resume_snapshot_id", resumeSnapshot)
                    put("jd_snapshot_id", jdSnapshot)
                },
            )
            val prepId = prep.jsonObject["id"]!!.jsonPrimitive.content
            val matchJob = api(
                "POST", "/preparations/$prepId/match", token,
                buildJsonObject {
                    put("revision", 1)
                    put("external_processing_consent", true)
                },
            ).jsonObject["id"]!!.jsonPrimitive.content
            waitJob(token, matchJob)
            val planJob = api(
                "POST", "/preparations/$prepId/generate-plan", token,
                buildJsonObject {
                    put("revision", 1)
                    put("external_processing_consent", true)
                },
            ).jsonObject["id"]!!.jsonPrimitive.content
            val done = waitJob(token, planJob)
            val planId = done.jsonObject["result_ref"]!!.jsonObject["id"]!!.jsonPrimitive.content
            val publicPlan = api("GET", "/plans/$planId", token).jsonObject
            val revision = publicPlan["revision"]!!.jsonPrimitive.content
            api(
                "POST", "/plans/$planId/confirm", token,
                buildJsonObject { put("revision", revision.toInt()) },
            )
            return publicPlan
        }

        private fun tonePcm(seconds: Double): ByteArray {
            val rate = 16000
            val count = (rate * seconds).toInt()
            val buffer = ByteBuffer.allocate(count * 2).order(ByteOrder.LITTLE_ENDIAN)
            for (index in 0 until count) {
                buffer.putShort((1800 * sin(2 * Math.PI * 330 * index / rate)).toInt().toShort())
            }
            return buffer.array()
        }

        private fun createResume(token: String): String {
            val text = listOf(
                "本科在读，计算机科学与技术专业。",
                "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。",
                "学习过 Java 集合基础和 JUnit 单元测试。",
                "没有工作经历。",
            ).joinToString("\n")
            val material = api(
                "POST", "/materials/text", token,
                buildJsonObject {
                    put("kind", "resume")
                    put("title", "M3虚构简历")
                    put("text", text)
                },
            ).jsonObject
            val profile = buildJsonObject {
                put("kind", "resume")
                put("education", buildJsonArray {
                    add(buildJsonObject {
                        put("item_id", "edu-1")
                        put("degree", "本科在读")
                        put("major", "计算机科学与技术")
                        put("evidence", evidence("本科在读，计算机科学与技术专业。"))
                    })
                })
                put("work_experience", buildJsonArray { })
                put("projects", buildJsonArray {
                    add(buildJsonObject {
                        put("item_id", "project-1")
                        put("name", "校园图书借阅系统")
                        put(
                            "description",
                            "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。",
                        )
                        put("skill_codes", buildJsonArray { add(kotlinx.serialization.json.JsonPrimitive("java.core")) })
                        put(
                            "evidence",
                            evidence(
                                "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。"
                            ),
                        )
                    })
                })
                put("skills", buildJsonArray {
                    add(buildJsonObject {
                        put("item_id", "skill-1")
                        put("name", "Java 集合基础和 JUnit")
                        put("code", "java.core")
                        put("evidence", evidence("学习过 Java 集合基础和 JUnit 单元测试。"))
                    })
                })
            }
            val confirmed = api(
                "POST", "/materials/${material["id"]!!.jsonPrimitive.content}/confirm", token,
                buildJsonObject {
                    put("revision", material["revision"]!!.jsonPrimitive.content.toInt())
                    put("profile", profile)
                },
            )
            return confirmed.jsonObject["id"]!!.jsonPrimitive.content
        }

        private fun createJd(token: String): String {
            val lines = listOf(
                "内置练习岗位：Java 后端开发实习生。",
                "职责：在指导下完成 Java 功能开发并说明实现思路。",
                "要求：理解 Java 集合的使用及基本边界。",
                "要求：能够为简单功能设计边界测试。",
            )
            val material = api(
                "POST", "/materials/text", token,
                buildJsonObject {
                    put("kind", "jd")
                    put("title", "M3虚构JD")
                    put("text", lines.joinToString("\n"))
                },
            ).jsonObject
            val profile = buildJsonObject {
                put("kind", "jd")
                put("role_id", "java-backend")
                put("level", "intern")
                put("title", "Java 后端开发实习生")
                put("responsibilities", buildJsonArray {
                    add(buildJsonObject {
                        put("item_id", "responsibility-1")
                        put("text", "职责：在指导下完成 Java 功能开发并说明实现思路。")
                        put("evidence", evidence("职责：在指导下完成 Java 功能开发并说明实现思路。"))
                    })
                })
                put("requirements", buildJsonArray {
                    add(buildJsonObject {
                        put("item_id", "req-1")
                        put("text", "理解 Java 集合的使用及基本边界")
                        put("skill_codes", buildJsonArray { add(kotlinx.serialization.json.JsonPrimitive("java.core")) })
                        put("importance", "must")
                        put("evidence", evidence("要求：理解 Java 集合的使用及基本边界。"))
                    })
                    add(buildJsonObject {
                        put("item_id", "req-2")
                        put("text", "能够为简单功能设计边界测试")
                        put("skill_codes", buildJsonArray { add(kotlinx.serialization.json.JsonPrimitive("test.junit")) })
                        put("importance", "must")
                        put("evidence", evidence("要求：能够为简单功能设计边界测试。"))
                    })
                })
            }
            val confirmed = api(
                "POST", "/materials/${material["id"]!!.jsonPrimitive.content}/confirm", token,
                buildJsonObject {
                    put("revision", material["revision"]!!.jsonPrimitive.content.toInt())
                    put("profile", profile)
                },
            )
            return confirmed.jsonObject["id"]!!.jsonPrimitive.content
        }

        private fun evidence(quote: String): JsonObject = buildJsonObject {
            put("quote", quote)
            put("origin", "extracted")
        }

        private fun waitJob(token: String, jobId: String): JsonObject {
            val deadline = System.currentTimeMillis() + 120_000
            while (System.currentTimeMillis() < deadline) {
                val job = api("GET", "/jobs/$jobId", token).jsonObject
                val state = job["state"]!!.jsonPrimitive.content
                if (state in setOf("succeeded", "failed", "interrupted", "superseded")) {
                    assertTrue("隔离计划任务失败：$state ${job["error"]}", state == "succeeded")
                    return job
                }
                Thread.sleep(500)
            }
            throw AssertionError("等待隔离计划任务超时")
        }

        private fun api(
            method: String,
            path: String,
            token: String? = null,
            body: Any? = null,
        ): JsonObject {
            val connection = URL(BASE + path).openConnection() as HttpURLConnection
            connection.requestMethod = method
            connection.connectTimeout = 10_000
            connection.readTimeout = 30_000
            token?.let { connection.setRequestProperty("Authorization", "Bearer $it") }
            if (method != "GET") {
                connection.setRequestProperty("Idempotency-Key", UUID.randomUUID().toString())
                connection.setRequestProperty("Content-Type", "application/json")
                connection.doOutput = true
                val payload = when (body) {
                    null -> "{}"
                    is JsonObject -> body.toString()
                    is Map<*, *> -> buildJsonObject {
                        body.forEach { (key, value) -> put(key.toString(), value.toString()) }
                    }.toString()
                    else -> body.toString()
                }
                OutputStreamWriter(connection.outputStream, Charsets.UTF_8).use { it.write(payload) }
            }
            val code = connection.responseCode
            val stream = if (code in 200..299) connection.inputStream else connection.errorStream
            val text = stream?.bufferedReader(Charsets.UTF_8)?.use { it.readText() }.orEmpty()
            if (code !in 200..299) throw AssertionError("HTTP $code $method $path: $text")
            return json.parseToJsonElement(text).jsonObject
        }
    }

    private fun ensureLoginScreen() {
        val onList = composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        if (onList) {
            composeRule.onNodeWithText("退出").performClick()
        }
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("没有账号？注册").fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun scrollUntilText(text: String, timeoutMs: Long = 30_000) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()) return
            composeRule.onRoot().performTouchInput { swipeUp() }
            composeRule.waitForIdle()
            Thread.sleep(300)
        }
        // 最后再断言一次，失败信息会标出缺失文本。
        composeRule.onNodeWithText(text).assertIsDisplayed()
    }

    @Test
    fun t1_voiceQuestionRecordingFixtureReviewAndSubmit() {
        ensureLoginScreen()
        val fields = composeRule.onAllNodes(hasSetTextAction())
        fields[0].performTextInput(username)
        fields[1].performTextInput(PASSWORD)
        composeRule.onAllNodesWithText("登录").onLast().performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        }
        Espresso.closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithText("打开").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("准备设置与材料").fetchSemanticsNodes().isNotEmpty()
        }
        scrollUntilText("查看/生成计划")
        composeRule.onNodeWithText("查看/生成计划").performScrollTo().performClick()
        scrollUntilText("开始语音面试")
        composeRule.waitUntil(timeoutMillis = 30_000) {
            runCatching {
                composeRule.onNodeWithText("开始语音面试").assertIsEnabled()
            }.isSuccess
        }
        composeRule.onNodeWithText("开始语音面试").performScrollTo().assertIsDisplayed()
        composeRule.onNodeWithText("开始语音面试").performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) {
            composeRule.onAllNodesWithText("同意音频处理并开始").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("同意音频处理并开始").performClick()

        composeRule.waitUntil(timeoutMillis = 60_000) {
            composeRule.onAllNodesWithText("语音作答 · 第 1/2 题").fetchSemanticsNodes().isNotEmpty()
        }
        // 自动播题可能占用音频焦点；先停止播放，再开始 fixture 录音。
        composeRule.waitUntil(timeoutMillis = 60_000) {
            composeRule.onAllNodesWithText("开始录音（最多 120 秒）").fetchSemanticsNodes().isNotEmpty() ||
                composeRule.onAllNodesWithText("停止播放").fetchSemanticsNodes().isNotEmpty()
        }
        if (composeRule.onAllNodesWithText("停止播放").fetchSemanticsNodes().isNotEmpty()) {
            composeRule.onNodeWithText("停止播放").performClick()
        }
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("开始录音（最多 120 秒）").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("开始录音（最多 120 秒）")
            .performScrollTo().assertIsDisplayed().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) {
            composeRule.onAllNodesWithText("录音中").fetchSemanticsNodes().isNotEmpty()
        }
        Thread.sleep(500)
        assertTrue(
            composeRule.onAllNodesWithText("剩余", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        )
        assertTrue(
            composeRule.onAllNodesWithText("已录", substring = true)
                .fetchSemanticsNodes().isNotEmpty()
        )
        composeRule.onNodeWithText("停止录音（不自动提交）")
            .performScrollTo().assertIsDisplayed()
            .performSemanticsAction(SemanticsActions.OnClick)
        composeRule.waitUntil(timeoutMillis = 90_000) {
            composeRule.onAllNodesWithText("原始转写（只读，保留识别结果）")
                .fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("确认提交（原样转写）")
            .performScrollTo().assertIsDisplayed()
            .performSemanticsAction(SemanticsActions.OnClick)
        composeRule.waitUntil(timeoutMillis = 60_000) {
            composeRule.onAllNodesWithText("语音追问").fetchSemanticsNodes().isNotEmpty() ||
                composeRule.onAllNodesWithText("面试已收尾").fetchSemanticsNodes().isNotEmpty()
        }
        assertTrue(
            composeRule.onAllNodesWithText("语音追问").fetchSemanticsNodes().isNotEmpty() ||
                composeRule.onAllNodesWithText("面试已收尾").fetchSemanticsNodes().isNotEmpty()
        )
        val evidenceDir = File(
            InstrumentationRegistry.getInstrumentation().targetContext.filesDir,
            "m3-evidence",
        )
        evidenceDir.mkdirs()
        val image = composeRule.onRoot().captureToImage()
        val bitmap = image.asAndroidBitmap()
        File(evidenceDir, "voice-review-submitted.png").outputStream().use {
            bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)
        }
    }

    private fun nodeExists(text: String, substring: Boolean = false): Boolean =
        composeRule.onAllNodesWithText(text, substring = substring)
            .fetchSemanticsNodes().isNotEmpty()

    private fun scrollUntilAnyText(
        texts: List<String>,
        timeoutMs: Long = 30_000,
        substring: Boolean = false,
    ): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        var directionUp = true
        var attempts = 0
        while (System.currentTimeMillis() < deadline) {
            if (texts.any { nodeExists(it, substring) }) return true
            composeRule.onRoot().performTouchInput {
                if (directionUp) swipeUp() else swipeDown()
            }
            composeRule.waitForIdle()
            attempts += 1
            if (attempts % 4 == 0) directionUp = !directionUp
            Thread.sleep(250)
        }
        return texts.any { nodeExists(it, substring) }
    }

    private fun loginAs(user: String) {
        ensureLoginScreen()
        val fields = composeRule.onAllNodes(hasSetTextAction())
        fields[0].performTextInput(user)
        fields[1].performTextInput(PASSWORD)
        composeRule.onAllNodesWithText("登录").onLast().performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("新建面试准备") }
        Espresso.closeSoftKeyboard()
        composeRule.waitForIdle()
    }

    @Test
    fun t2_voiceWholeSessionAndReportSource() {
        loginAs(username2)
        composeRule.onNodeWithText("打开").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("准备设置与材料") }
        scrollUntilText("查看/生成计划")
        composeRule.onNodeWithText("查看/生成计划").performScrollTo().performClick()
        scrollUntilText("开始语音面试")
        composeRule.waitUntil(timeoutMillis = 30_000) {
            runCatching {
                composeRule.onNodeWithText("开始语音面试").assertIsEnabled()
            }.isSuccess
        }
        composeRule.onNodeWithText("开始语音面试").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) { nodeExists("同意音频处理并开始") }
        composeRule.onNodeWithText("同意音频处理并开始").performClick()
        assertTrue(
            scrollUntilAnyText(
                listOf("语音作答", "语音追问", "面试已收尾", "查看报告与逐题证据"),
                60_000,
                substring = true,
            )
        )

        for (round in 0 until 8) {
            val actionable = scrollUntilAnyText(
                listOf(
                    "开始录音（最多 120 秒）",
                    "面试已收尾",
                    "查看报告与逐题证据",
                    "回答已保存，正在准备下一步",
                ),
                60_000,
            )
            if (!actionable) continue
            if (nodeExists("面试已收尾") || nodeExists("查看报告与逐题证据")) break
            if (nodeExists("回答已保存，正在准备下一步")) {
                Thread.sleep(500)
                continue
            }
            if (!nodeExists("开始录音（最多 120 秒）")) continue
            if (nodeExists("停止播放")) {
                composeRule.onNodeWithText("停止播放").performClick()
                Thread.sleep(300)
            }
            composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("开始录音（最多 120 秒）") }
            composeRule.onNodeWithText("开始录音（最多 120 秒）")
                .performScrollTo().assertIsDisplayed().performClick()
            composeRule.waitUntil(timeoutMillis = 10_000) { nodeExists("录音中") }
            Thread.sleep(500)
            composeRule.onNodeWithText("停止录音（不自动提交）")
                .performScrollTo().assertIsDisplayed()
                .performSemanticsAction(SemanticsActions.OnClick)
            composeRule.waitUntil(timeoutMillis = 90_000) {
                nodeExists("原始转写（只读，保留识别结果）")
            }
            val confirmText = if (nodeExists("确认提交（人工核对文本）")) {
                "确认提交（人工核对文本）"
            } else {
                "确认提交（原样转写）"
            }
            composeRule.onNodeWithText(confirmText)
                .performScrollTo().assertIsDisplayed()
                .performSemanticsAction(SemanticsActions.OnClick)
            Thread.sleep(1_000)
        }

        var waited = 0
        while (waited < 120_000 && !nodeExists("查看报告与逐题证据")) {
            if (nodeExists("刷新报告状态")) {
                composeRule.onNodeWithText("刷新报告状态")
                    .performSemanticsAction(SemanticsActions.OnClick)
            }
            Thread.sleep(1_000)
            waited += 1_000
        }
        assertTrue(nodeExists("查看报告与逐题证据") || nodeExists("面试已收尾"))
        if (nodeExists("查看报告与逐题证据")) {
            composeRule.onNodeWithText("查看报告与逐题证据")
                .performScrollTo().performClick()
            composeRule.waitUntil(timeoutMillis = 60_000) {
                nodeExists("面试报告") && (nodeExists("维度结果") || nodeExists("单题分"))
            }
            assertTrue(
                nodeExists("来源：语音转写", substring = true)
            )
            val evidenceDir = File(
                InstrumentationRegistry.getInstrumentation().targetContext.filesDir,
                "m3-evidence",
            )
            evidenceDir.mkdirs()
            val image = composeRule.onRoot().captureToImage()
            val bitmap = image.asAndroidBitmap()
            File(evidenceDir, "voice-whole-session-report.png").outputStream().use {
                bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)
            }
        }
    }

    @Test
    fun t3_sixQuestionVoiceWholeSessionCoverageAndReport() {
        loginAs(username3)
        composeRule.onNodeWithText("打开").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("准备设置与材料") }
        scrollUntilText("查看/生成计划")
        composeRule.onNodeWithText("查看/生成计划").performScrollTo().performClick()
        scrollUntilText("开始语音面试")
        composeRule.waitUntil(timeoutMillis = 30_000) {
            runCatching {
                composeRule.onNodeWithText("开始语音面试").assertIsEnabled()
            }.isSuccess
        }
        composeRule.onNodeWithText("开始语音面试").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) { nodeExists("同意音频处理并开始") }
        composeRule.onNodeWithText("同意音频处理并开始").performClick()
        assertTrue(
            scrollUntilAnyText(
                listOf("语音作答", "语音追问", "面试已收尾", "查看报告与逐题证据"),
                90_000,
                substring = true,
            )
        )

        for (round in 0 until 20) {
            val actionable = scrollUntilAnyText(
                listOf(
                    "开始录音（最多 120 秒）",
                    "面试已收尾",
                    "查看报告与逐题证据",
                    "回答已保存，正在准备下一步",
                ),
                90_000,
            )
            if (!actionable) continue
            if (nodeExists("面试已收尾") || nodeExists("查看报告与逐题证据")) break
            if (nodeExists("回答已保存，正在准备下一步")) {
                Thread.sleep(500)
                continue
            }
            if (!nodeExists("开始录音（最多 120 秒）")) continue
            if (nodeExists("停止播放")) {
                composeRule.onNodeWithText("停止播放").performClick()
                Thread.sleep(300)
            }
            composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("开始录音（最多 120 秒）") }
            composeRule.onNodeWithText("开始录音（最多 120 秒）")
                .performScrollTo().assertIsDisplayed().performClick()
            composeRule.waitUntil(timeoutMillis = 10_000) { nodeExists("录音中") }
            Thread.sleep(500)
            composeRule.onNodeWithText("停止录音（不自动提交）")
                .performScrollTo().assertIsDisplayed()
                .performSemanticsAction(SemanticsActions.OnClick)
            composeRule.waitUntil(timeoutMillis = 90_000) {
                nodeExists("原始转写（只读，保留识别结果）")
            }
            val confirmText = if (nodeExists("确认提交（人工核对文本）")) {
                "确认提交（人工核对文本）"
            } else {
                "确认提交（原样转写）"
            }
            composeRule.onNodeWithText(confirmText)
                .performScrollTo().assertIsDisplayed()
                .performSemanticsAction(SemanticsActions.OnClick)
            Thread.sleep(1_000)
        }

        var waited = 0
        while (waited < 180_000 && !nodeExists("查看报告与逐题证据")) {
            if (nodeExists("刷新报告状态")) {
                composeRule.onNodeWithText("刷新报告状态")
                    .performSemanticsAction(SemanticsActions.OnClick)
            }
            Thread.sleep(1_000)
            waited += 1_000
        }
        assertTrue(nodeExists("查看报告与逐题证据") || nodeExists("面试已收尾"))

        // 页面链路结束后再用同一隔离 API 核对 6 题 3+3 覆盖，避免只凭 UI 文本。
        val token = api(
            "POST", "/auth/login",
            body = mapOf("username" to username3, "password" to PASSWORD),
        ).jsonObject["access_token"]!!.jsonPrimitive.content
        val planId = plan3["id"]!!.jsonPrimitive.content
        var sessionId: String? = null
        var report: JsonObject? = null
        val deadline = System.currentTimeMillis() + 180_000
        while (System.currentTimeMillis() < deadline) {
            val items = api("GET", "/interviews", token).jsonObject["items"]!!.jsonArray
            val item = items.map { it.jsonObject }
                .firstOrNull { it["plan_id"]!!.jsonPrimitive.content == planId }
            if (item != null) {
                sessionId = item["session_id"]!!.jsonPrimitive.content
                if (item["status"]!!.jsonPrimitive.content == "ended" &&
                    item["report_state"]!!.jsonPrimitive.content == "ready"
                ) {
                    report = api("GET", "/interviews/$sessionId/report", token).jsonObject
                    break
                }
            }
            Thread.sleep(1_000)
        }
        assertTrue("6 题语音场未结束或报告未 ready", sessionId != null && report != null)
        val reportObj = report!!
        val coverage = reportObj["coverage"]!!.jsonObject
        assertTrue(
            "coverage=$coverage",
            coverage["planned"]!!.jsonPrimitive.content == "6" &&
                coverage["answered"]!!.jsonPrimitive.content == "6" &&
                coverage["bank_planned"]!!.jsonPrimitive.content == "3" &&
                coverage["generated_planned"]!!.jsonPrimitive.content == "3",
        )
        val questions = reportObj["questions"]!!.jsonArray.map { it.jsonObject }
        assertEquals(6, questions.size)
        val sources = questions.flatMap { question ->
            question["turns"]!!.jsonArray.mapNotNull { turn ->
                turn.jsonObject["answer"]?.jsonObject?.get("input_source")?.jsonPrimitive?.content
            }
        }.toSet()
        assertTrue("报告缺少语音来源：$sources", "asr" in sources || "asr_corrected" in sources)

        if (nodeExists("查看报告与逐题证据")) {
            composeRule.onNodeWithText("查看报告与逐题证据")
                .performScrollTo().performClick()
            composeRule.waitUntil(timeoutMillis = 60_000) {
                nodeExists("面试报告") && (nodeExists("维度结果") || nodeExists("单题分"))
            }
            assertTrue(nodeExists("来源：语音转写", substring = true))
        }
        val evidenceDir = File(
            InstrumentationRegistry.getInstrumentation().targetContext.filesDir,
            "m3-evidence",
        )
        evidenceDir.mkdirs()
        val image = composeRule.onRoot().captureToImage()
        val bitmap = image.asAndroidBitmap()
        File(evidenceDir, "voice-six-session-report.png").outputStream().use {
            bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)
        }
    }

}
