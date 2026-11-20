package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.VoiceRecorder
import com.mockmate.app.core.audio.VoiceTakeStore
import java.io.File
import java.util.UUID
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T04 D03 第一阶段：真实麦克风录制中等待宿主 force-stop，留下未封存 `.part`。
 * 不能自行 stop，否则文件会被封存成完整 take，无法证明进程死亡路径。
 */
@RunWith(AndroidJUnit4::class)
class M3InterruptedRecordingPhase1DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun leavesUnsealedPartWhenProcessIsKilled() {
        val store = VoiceTakeStore(context)
        store.clear(ACCOUNT, SESSION, TURN)
        val takeId = UUID.randomUUID().toString()
        val file = store.file(ACCOUNT, SESSION, TURN, takeId)
        val marker = File(context.filesDir, "m3-part-ready")
        marker.delete()
        val recorder = VoiceRecorder(AndroidVoiceFileCipher(context))
        try {
            val start = recorder.start(file)
            assertTrue("start failed: ${start.message}", start.ok)
            val deadline = System.currentTimeMillis() + 20_000
            while (System.currentTimeMillis() < deadline && recorder.snapshot().samples < 32_000) {
                Thread.sleep(200)
            }
            assertTrue("未采集到足够样本", recorder.snapshot().samples >= 32_000)
            marker.writeText(takeId)
            // 宿主轮询 marker 后 force-stop；这里不调用 stop，不能让 .part 被封存。
            Thread.sleep(120_000)
        } finally {
            // 如果宿主未杀进程，至少清理本次真实采集，避免留下完整 take。
            runCatching { recorder.stop() }
            store.clear(ACCOUNT, SESSION, TURN)
            marker.delete()
        }
    }

    companion object {
        const val ACCOUNT = "m3-coldstart-part"
        const val SESSION = "m3-coldstart-part-session"
        const val TURN = "m3-coldstart-part-turn"
    }
}
