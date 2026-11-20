package com.mockmate.app

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
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put

/**
 * M3 设备测试用最小 API 客户端：隔离 test provider 下创建虚构账号、计划与语音场。
 * 与 M3VoiceDeviceTest 的内联 helper 等价，供冷启动/故障相位测试复用。
 */
object M3DeviceApi {
    val base: String = BuildConfig.API_BASE_URL
    const val PASSWORD = "MockMate-m3-2026"
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    fun register(user: String): String {
        api("POST", "/auth/register", body = mapOf("username" to user, "password" to PASSWORD))
        return login(user)
    }

    fun login(user: String): String =
        api("POST", "/auth/login", body = mapOf("username" to user, "password" to PASSWORD))
            .jsonObject["access_token"]!!.jsonPrimitive.content

    fun accountId(user: String): String {
        val token = login(user)
        return api("GET", "/me", token).jsonObject["id"]!!.jsonPrimitive.content
    }

    fun createPlan(user: String, count: Int = 2): JsonObject {
        val token = if (runCatching { login(user) }.isSuccess) login(user) else register(user)
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
        // confirm 后 revision 会推进；直接开始面试必须使用最新 plan revision。
        return api("GET", "/plans/$planId", token).jsonObject
    }

    fun startVoiceInterview(token: String, plan: JsonObject): JsonObject =
        api(
            "POST", "/plans/${plan["id"]!!.jsonPrimitive.content}/interviews", token,
            buildJsonObject {
                put("plan_revision", plan["revision"]!!.jsonPrimitive.content.toInt())
                put("external_processing_consent", true)
                put("mode", "voice")
                put("audio_processing_consent", true)
            },
        ).jsonObject

    fun detail(token: String, sessionId: String): String =
        raw("GET", "/interviews/$sessionId", token)

    fun voiceDraft(token: String, sessionId: String, turnId: String): JsonObject =
        api("GET", "/interviews/$sessionId/turns/$turnId/voice-draft", token).jsonObject

    fun tonePcm(seconds: Double): ByteArray {
        val rate = 16000
        val count = (rate * seconds).toInt()
        val buffer = ByteBuffer.allocate(count * 2).order(ByteOrder.LITTLE_ENDIAN)
        for (index in 0 until count) {
            buffer.putShort((1800 * sin(2 * Math.PI * 330 * index / rate)).toInt().toShort())
        }
        return buffer.array()
    }

    private fun waitJob(token: String, jobId: String): JsonObject {
        val deadline = System.currentTimeMillis() + 120_000
        while (System.currentTimeMillis() < deadline) {
            val job = api("GET", "/jobs/$jobId", token).jsonObject
            val state = job["state"]!!.jsonPrimitive.content
            if (state in setOf("succeeded", "failed", "interrupted", "superseded")) {
                check(state == "succeeded") { "隔离计划任务失败：$state ${job["error"]}" }
                return job
            }
            Thread.sleep(500)
        }
        error("等待隔离计划任务超时")
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

    private fun api(method: String, path: String, token: String? = null, body: Any? = null): JsonObject =
        json.parseToJsonElement(raw(method, path, token, body)).jsonObject

    private fun raw(method: String, path: String, token: String? = null, body: Any? = null): String {
        val connection = URL(base + path).openConnection() as HttpURLConnection
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
        if (code !in 200..299) error("HTTP $code $method $path: $text")
        return text
    }
}
