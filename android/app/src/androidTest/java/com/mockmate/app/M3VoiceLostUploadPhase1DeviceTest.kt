package com.mockmate.app

import android.app.Application
import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.QuestionPlayer
import com.mockmate.app.core.audio.VoiceRecorder
import com.mockmate.app.core.audio.VoiceReviewStore
import com.mockmate.app.core.audio.VoiceServices
import com.mockmate.app.core.audio.VoiceTakeMeta
import com.mockmate.app.core.audio.VoiceTakeStore
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T04 D01 第一阶段：隔离 API 在 `/audio` 完成 201 后丢弃一次回执；客户端必须保留
 * 本地 take 与冻结 key/body，不能把未知结果当成成功或失败。宿主随后 force-stop。
 */
@RunWith(AndroidJUnit4::class)
class M3VoiceLostUploadPhase1DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    @Test
    fun droppedUploadReceiptLeavesPendingLocalTake() {
        val app = context.applicationContext as Application
        val user = "m3lost" + System.currentTimeMillis().toString().takeLast(8)
        val token = M3DeviceApi.register(user)
        val plan = M3DeviceApi.createPlan(user, 2)
        val started = M3DeviceApi.startVoiceInterview(token, plan)
        val sessionId = started["session_id"]!!.jsonPrimitive.content
        val detailJson = M3DeviceApi.detail(token, sessionId)
        val detail = json.decodeFromString<InterviewDetailDto>(detailJson)
        val turnId = detail.current_turn!!.turn_id
        val accountId = M3DeviceApi.accountId(user)

        val store = VoiceTakeStore(context)
        store.clear(accountId, sessionId, turnId)
        val takeId = UUID.randomUUID().toString()
        val pcm = M3DeviceApi.tonePcm(1.0)
        val file = store.file(accountId, sessionId, turnId, takeId)
        file.parentFile?.mkdirs()
        file.writeBytes(pcm)
        val meta = VoiceTakeMeta(
            takeId = takeId,
            operationKey = UUID.randomUUID().toString(),
            sha256 = MessageDigest.getInstance("SHA-256").digest(pcm)
                .joinToString("") { "%02x".format(it) },
            byteLength = pcm.size,
            durationMs = 1000,
            interrupted = false,
            reachedLimit = false,
            sessionRevision = detail.revision,
            expectedVoiceRevision = 0,
            encrypted = false,
        )
        assertEquals(com.mockmate.app.core.session.SecureWrite.Ok, store.write(accountId, sessionId, turnId, meta))
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit()
            .putString("user", user)
            .putString("session", sessionId)
            .putString("turn", turnId)
            .putString("take", takeId)
            .apply()

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = InMemoryOperationStore(),
            bootstrapOnInit = false,
            voiceServices = VoiceServices(
                recorder = VoiceRecorder(AndroidVoiceFileCipher(app)),
                player = QuestionPlayer(app),
                takes = store,
                reviews = VoiceReviewStore(app),
                cacheRoot = app.cacheDir,
            ),
        )
        try {
            vm.authenticate(false, user, M3DeviceApi.PASSWORD)
            waitUntil(30_000) { vm.state.value.account?.id == accountId }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom(sessionId)),
                interviewDetail = detail,
                voicePhase = com.mockmate.app.core.audio.VoicePhase.LOCAL_READY,
                voiceTurnId = turnId,
                voiceTake = meta,
            )
            vm.uploadVoiceTake(sessionId)
            Thread.sleep(3_000)
            android.util.Log.i(
                "M3LostUpload",
                "phase=${vm.state.value.voicePhase} message=${vm.state.value.message} " +
                    "voiceError=${vm.state.value.voiceError} take=${vm.state.value.voiceTake}",
            )
            waitUntil(30_000) { vm.state.value.message?.code == "RECEIPT_UNREADABLE" }
            assertNotNull(vm.state.value.voiceTake)
            assertFalse("未知回执不能标成已上传", vm.state.value.voiceTake!!.uploaded)
            val marker = File(context.filesDir, MARKER).apply { writeText(takeId) }
            assertTrue(marker.exists())
            // 宿主轮询 marker 后 force-stop；不要在这里清理或停止进程。
            Thread.sleep(120_000)
        } finally {
            runCatching { vm.closeForTest() }
        }
    }

    companion object {
        const val PREFS = "m3_lost_upload_test"
        const val MARKER = "m3-lost-upload-ready"
        fun waitUntil(timeoutMs: Long, condition: () -> Boolean) {
            val deadline = System.currentTimeMillis() + timeoutMs
            while (System.currentTimeMillis() < deadline && !condition()) Thread.sleep(200)
            assertTrue("等待条件超时", condition())
        }
    }
}
