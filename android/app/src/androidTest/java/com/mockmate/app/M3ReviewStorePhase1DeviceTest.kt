package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.VoiceReviewDraft
import com.mockmate.app.core.audio.VoiceReviewRead
import com.mockmate.app.core.audio.VoiceReviewStore
import com.mockmate.app.core.session.SecureWrite
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T02/T04 冷启第一阶段：用真实 Keystore/加密 SharedPreferences 保存核对修改。
 * 宿主随后 force-stop，再由 Phase2 验证冷启动可读。
 */
@RunWith(AndroidJUnit4::class)
class M3ReviewStorePhase1DeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun savesEncryptedReviewDraftForColdStart() {
        val store = VoiceReviewStore(context)
        store.clear(ACCOUNT, SESSION, TURN)
        val draft = VoiceReviewDraft(
            sessionId = SESSION,
            turnId = TURN,
            takeId = "take-cold",
            assetId = "asset-cold",
            transcriptRevision = 1,
            text = "冷启动后仍应保留的核对修改",
            edited = true,
        )
        assertEquals(SecureWrite.Ok, store.write(ACCOUNT, SESSION, TURN, draft))
        val read = store.read(ACCOUNT, SESSION, TURN)
        assertTrue("phase1 read=$read", read is VoiceReviewRead.Present)
    }

    companion object {
        const val ACCOUNT = "m3-coldstart-review"
        const val SESSION = "m3-coldstart-session"
        const val TURN = "m3-coldstart-turn"
    }
}
