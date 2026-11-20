package com.mockmate.app

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.VoiceRecorder
import java.io.File
import java.util.UUID
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * T05 权限拒绝：宿主先 revoke RECORD_AUDIO 再运行本类。必须明确失败，
 * 不创建可冒充已录音的完整文件；运行后由宿主恢复 grant。
 */
@RunWith(AndroidJUnit4::class)
class M3PermissionDeniedDeviceTest {
    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun revokedMicrophonePermissionFailsWithoutFakeRecording() {
        val denied = context.checkSelfPermission(Manifest.permission.RECORD_AUDIO) !=
            PackageManager.PERMISSION_GRANTED
        org.junit.Assume.assumeTrue("运行前必须撤销 RECORD_AUDIO 权限（本机 shell 无此权限）", denied)

        val root = File(context.filesDir, "m3-permission-${UUID.randomUUID()}")
        root.mkdirs()
        val target = File(root, "denied.pcm")
        val recorder = VoiceRecorder(AndroidVoiceFileCipher(context))
        try {
            val start = recorder.start(target)
            assertFalse("权限被拒绝时不得假装已开始录音", start.ok)
            assertTrue("失败原因应可见", start.message.isNotBlank())
            assertFalse("权限拒绝不能留下完整录音文件", target.exists() && target.length() > 44)
            assertFalse(recorder.isRecording)
        } finally {
            runCatching { recorder.stop() }
            root.deleteRecursively()
        }
    }
}
