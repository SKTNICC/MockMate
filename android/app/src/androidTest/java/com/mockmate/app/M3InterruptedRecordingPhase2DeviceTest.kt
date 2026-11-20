package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.VoiceTakeRead
import com.mockmate.app.core.audio.VoiceTakeStore
import java.io.File
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** T04 D03 第二阶段：录制中被杀进程后只能识别为未封存片段，不能冒充完整回答。 */
@RunWith(AndroidJUnit4::class)
class M3InterruptedRecordingPhase2DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun killedRecordingIsUnsealedPartNotCompleteTake() {
        val store = VoiceTakeStore(context)
        val marker = File(context.filesDir, "m3-part-ready")
        try {
            assertTrue("缺少 phase1 完成标记", marker.exists())
            val read = store.read(
                M3InterruptedRecordingPhase1DeviceTest.ACCOUNT,
                M3InterruptedRecordingPhase1DeviceTest.SESSION,
                M3InterruptedRecordingPhase1DeviceTest.TURN,
            )
            assertTrue("被杀录制不得成为完整 take：$read", read is VoiceTakeRead.UnsealedPart)
            assertFalse(read is VoiceTakeRead.Present)
        } finally {
            store.clear(
                M3InterruptedRecordingPhase1DeviceTest.ACCOUNT,
                M3InterruptedRecordingPhase1DeviceTest.SESSION,
                M3InterruptedRecordingPhase1DeviceTest.TURN,
            )
            marker.delete()
        }
    }
}
