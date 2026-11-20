package com.mockmate.app

import android.app.Application
import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.QuestionPlayer
import com.mockmate.app.core.audio.VoicePhase
import com.mockmate.app.core.audio.VoiceRecorder
import com.mockmate.app.core.audio.VoiceReviewStore
import com.mockmate.app.core.audio.VoiceServices
import com.mockmate.app.core.audio.VoiceTakeStore
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import java.io.File
import kotlinx.serialization.builtins.serializer
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T04 D01 第二阶段：进程被杀后冷启动，本地 pending take 按原 key/body 重放；
 * 服务端已入库的同一 take 回放 201，随后只续一次 ASR 并进入核对。
 */
@RunWith(AndroidJUnit4::class)
class M3VoiceLostUploadPhase2DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    @Test
    fun coldStartReplaysPendingUploadAndContinuesSameTake() {
        val app = context.applicationContext as Application
        val prefs = context.getSharedPreferences(
            M3VoiceLostUploadPhase1DeviceTest.PREFS,
            Context.MODE_PRIVATE,
        )
        val user = requireNotNull(prefs.getString("user", null))
        val sessionId = requireNotNull(prefs.getString("session", null))
        val turnId = requireNotNull(prefs.getString("turn", null))
        val takeId = requireNotNull(prefs.getString("take", null))
        val marker = File(context.filesDir, M3VoiceLostUploadPhase1DeviceTest.MARKER)
        val store = VoiceTakeStore(context)
        assertTrue("缺少 phase1 完成标记", marker.exists())

        val token = M3DeviceApi.login(user)
        val accountId = M3DeviceApi.accountId(user)
        val detail = json.decodeFromString<InterviewDetailDto>(
            M3DeviceApi.detail(token, sessionId),
        )
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
            M3VoiceLostUploadPhase1DeviceTest.waitUntil(30_000) {
                vm.state.value.account?.id == accountId
            }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom(sessionId)),
                interviewDetail = detail,
            )
            vm.onVoiceRoomOpened(sessionId)
            M3VoiceLostUploadPhase1DeviceTest.waitUntil(90_000) {
                vm.state.value.voicePhase == VoicePhase.REVIEW &&
                    vm.state.value.voiceReviewText.isNotBlank()
            }
            assertTrue(
                "核对文本应来自服务端同一 take 的 ASR：${vm.state.value.voiceReviewText}",
                vm.state.value.voiceReviewText.isNotBlank(),
            )
            val draft = M3DeviceApi.voiceDraft(token, sessionId, turnId)
            assertEquals(
                takeId,
                draft["active_audio"]!!.jsonObject["take_id"]!!.jsonPrimitive.content,
            )
            assertEquals("1", draft["voice_revision"]!!.jsonPrimitive.content)
            val stored = store.read(accountId, sessionId, turnId)
            assertTrue(stored is com.mockmate.app.core.audio.VoiceTakeRead.Present)
            assertTrue((stored as com.mockmate.app.core.audio.VoiceTakeRead.Present).meta.uploaded)
            val evidence = File(context.filesDir, "m3-evidence").apply { mkdirs() }
            File(evidence, "lost-upload-recovered.json").writeText(
                """
                {
                  "session_id": "$sessionId",
                  "turn_id": "$turnId",
                  "take_id": "$takeId",
                  "voice_revision": "${draft["voice_revision"]!!.jsonPrimitive.content}",
                  "review_text": ${json.encodeToString(String.serializer(), vm.state.value.voiceReviewText)},
                  "active_asset_id": "${draft["active_audio"]!!.jsonObject["id"]!!.jsonPrimitive.content}"
                }
                """.trimIndent(),
            )
        } finally {
            runCatching { vm.closeForTest() }
            store.clear(accountId, sessionId, turnId)
            marker.delete()
            prefs.edit().clear().apply()
        }
    }
}
