package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.VoiceRecorder
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T05 真机实际采集：真实 AudioRecord 连续运行接近 120 秒，验证样本上限、
 * 单调时间、加密封存/解密长度与停止原因。不经过 ViewModel，不提交任何答案；
 * 环境声音只用于采集管线，不能当作真人回答或 ASR 质量证据。
 */
@RunWith(AndroidJUnit4::class)
class M3LongRecordingDeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun realMicrophoneRunsToLimitAndSealsExactlyOnce() {
        val root = File(context.filesDir, "m3-long-recording-${UUID.randomUUID()}")
        root.mkdirs()
        val target = File(root, "long-take.pcm")
        val cipher = AndroidVoiceFileCipher(context)
        val recorder = VoiceRecorder(cipher)
        val startedAt = System.nanoTime()
        try {
            val start = recorder.start(target)
            assertTrue("start failed: ${start.message}", start.ok)
            var snapshot = recorder.snapshot()
            val deadline = System.currentTimeMillis() + 130_000
            while (System.currentTimeMillis() < deadline && !snapshot.reachedLimit) {
                Thread.sleep(500)
                snapshot = recorder.snapshot()
            }
            val stop = recorder.stop()
            val wallMs = (System.nanoTime() - startedAt) / 1_000_000
            assertNull("capture error=${stop.error}", stop.error)
            assertNotNull(stop.file)
            assertTrue(stop.file!!.exists())
            assertTrue("样本为空", stop.sampleCount > 0)
            assertTrue("未达到 120 秒样本上限: ${stop.sampleCount}", stop.reachedLimit)
            assertEquals(VoiceRecorder.MAX_SAMPLES, stop.sampleCount)
            assertEquals(3_840_000, stop.byteLength)
            assertEquals(120_000L, stop.durationMs)
            assertEquals(stop.sampleCount * 2, stop.byteLength.toLong())
            val decrypted = cipher.openDecryptedInput(stop.file!!).use { it.readBytes() }
            assertEquals(stop.byteLength, decrypted.size)
            val sha = MessageDigest.getInstance("SHA-256").digest(decrypted)
                .joinToString("") { "%02x".format(it) }
            assertTrue(sha.length == 64)
            val evidence = File(context.filesDir, "m3-evidence").apply { mkdirs() }
            File(evidence, "long-recording.json").writeText(
                """
                {
                  "wall_ms": $wallMs,
                  "sample_count": ${stop.sampleCount},
                  "byte_length": ${stop.byteLength},
                  "duration_ms": ${stop.durationMs},
                  "reached_limit": ${stop.reachedLimit},
                  "decrypted_bytes": ${decrypted.size},
                  "sha256": "$sha"
                }
                """.trimIndent(),
            )
            // 达到样本/时长上限后停止，不自动提交任何答案。
            assertTrue(stop.reachedLimit)
        } finally {
            runCatching { recorder.stop() }
            root.deleteRecursively()
        }
    }
}
