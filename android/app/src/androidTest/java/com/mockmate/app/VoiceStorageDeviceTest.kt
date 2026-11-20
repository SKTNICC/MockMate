package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.VoiceRecorder
import com.mockmate.app.core.audio.VoiceTakeMeta
import com.mockmate.app.core.audio.VoiceTakeRead
import com.mockmate.app.core.audio.VoiceTakeStore
import com.mockmate.app.core.session.SecureWrite
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * R05 设备证据：真实 Android Keystore + EncryptedFile 的录音文件加解密、旧明文识别、
 * 密文损坏与中断 .part 识别。只使用合成字节，测试结束清理自己的记录。
 */
@RunWith(AndroidJUnit4::class)
class VoiceStorageDeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun sha(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    @Test
    fun fixtureRecorderEncryptsSealsAndDecryptsThroughRename() {
        val root = File(context.filesDir, "voice-recorder-test-${UUID.randomUUID()}")
        val cipher = AndroidVoiceFileCipher(context)
        val fixture = File(root, "fixture.pcm")
        val target = File(root, "take.pcm")
        root.mkdirs()
        val pcm = ByteArray(6400) { (it % 251).toByte() }
        fixture.writeBytes(pcm)
        try {
            val recorder = VoiceRecorder(cipher)
            assertTrue(recorder.startFromFixture(target, fixture).ok)
            val stop = recorder.stop()
            assertTrue("stop error=${stop.error}", stop.file != null && stop.file!!.exists())
            val decrypted = cipher.openDecryptedInput(stop.file!!).use { it.readBytes() }
            assertTrue(decrypted.contentEquals(pcm.copyOf(minOf(pcm.size, stop.byteLength))))
        } finally {
            root.deleteRecursively()
        }
    }

    @Test
    fun encryptedRecordingRoundTripLegacyCorruptionAndPartAreDistinct() {
        val root = File(context.filesDir, "voice-test-${UUID.randomUUID()}")
        val cipher = AndroidVoiceFileCipher(context)
        val store = VoiceTakeStore(context, root, cipher)
        val account = "voice-test-${UUID.randomUUID()}"
        val session = "session-1"
        val turn = "turn-1"
        val take = "take-${UUID.randomUUID()}"
        val bytes = ByteArray(3200) { (it % 251).toByte() }
        val meta = VoiceTakeMeta(
            takeId = take,
            operationKey = "key-$take",
            sha256 = sha(bytes),
            byteLength = bytes.size,
            durationMs = 100,
            interrupted = false,
            reachedLimit = false,
            sessionRevision = 1,
            expectedVoiceRevision = 0,
            encrypted = true,
        )
        try {
            val file = store.file(account, session, turn, take)
            file.parentFile?.mkdirs()
            cipher.openEncryptedOutput(file).use { it.write(bytes) }
            assertEquals(SecureWrite.Ok, store.write(account, session, turn, meta))
            val present = store.read(account, session, turn)
            assertTrue(present is VoiceTakeRead.Present)
            assertTrue(
                store.readAudio(account, session, turn, meta).contentEquals(bytes)
            )

            // 密文损坏必须明确失败，不能按空文件/半截文件重发。
            file.writeBytes(ByteArray(64) { 7 })
            assertTrue(store.read(account, session, turn) is VoiceTakeRead.Corrupt)
            store.clear(account, session, turn)

            // 旧版本明文草稿仍按 encrypted=false 明确识别并可读。
            val legacy = bytes.copyOf(1600)
            val legacyMeta = meta.copy(
                takeId = "legacy-$take",
                sha256 = sha(legacy),
                byteLength = legacy.size,
                encrypted = false,
            )
            val legacyFile = store.file(account, session, turn, legacyMeta.takeId)
            legacyFile.writeBytes(legacy)
            assertEquals(SecureWrite.Ok, store.write(account, session, turn, legacyMeta))
            val legacyRead = store.read(account, session, turn)
            assertTrue(legacyRead is VoiceTakeRead.Present)
            assertTrue(
                store.readAudio(account, session, turn, legacyMeta).contentEquals(legacy)
            )
            store.clear(account, session, turn)

            // 进程被杀留下的 .part 只能识别为未封存片段。
            val part = store.partFile(account, session, turn, "interrupted-$take")
            part.parentFile?.mkdirs()
            part.writeBytes(ByteArray(128) { 1 })
            assertTrue(store.read(account, session, turn) is VoiceTakeRead.UnsealedPart)
            store.clear(account, session, turn)
        } finally {
            store.clear(account, session, turn)
            root.deleteRecursively()
        }
    }
}
