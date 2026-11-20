package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.VoiceReviewRead
import com.mockmate.app.core.audio.VoiceReviewStore
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/** T02/T04 冷启第二阶段：进程被杀后仍能读取同一加密核对修改，并自行清理。 */
@RunWith(AndroidJUnit4::class)
class M3ReviewStorePhase2DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun encryptedReviewSurvivesColdStart() {
        val store = VoiceReviewStore(context)
        try {
            val read = store.read(
                M3ReviewStorePhase1DeviceTest.ACCOUNT,
                M3ReviewStorePhase1DeviceTest.SESSION,
                M3ReviewStorePhase1DeviceTest.TURN,
            )
            assertTrue("cold-start read=$read", read is VoiceReviewRead.Present)
            assertEquals(
                "冷启动后仍应保留的核对修改",
                (read as VoiceReviewRead.Present).draft.text,
            )
        } finally {
            store.clear(
                M3ReviewStorePhase1DeviceTest.ACCOUNT,
                M3ReviewStorePhase1DeviceTest.SESSION,
                M3ReviewStorePhase1DeviceTest.TURN,
            )
        }
    }
}
