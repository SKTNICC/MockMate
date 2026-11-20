package com.mockmate.app

import android.content.Context
import android.media.AudioManager
import android.speech.tts.TextToSpeech
import androidx.compose.ui.graphics.asAndroidBitmap
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertIsEnabled
import androidx.compose.ui.test.captureToImage
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onLast
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeUp
import androidx.test.espresso.Espresso
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import androidx.test.uiautomator.By
import androidx.test.uiautomator.UiDevice
import java.io.File
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * R07 真机物理麦克风最小验证：禁用 debug fixture，使用 AudioRecord 实际采集；
 * 用设备 TTS 通过扬声器发出虚构短句，形成“扬声器→物理麦克风”的受控声学回路。
 * 这不是真人说话，也不能替代 6 题真人整场；它只验证真机麦克风、加密落盘、上传、
 * 真实 ASR 与真实播报链路。结果按此范围记录。
 */
@RunWith(AndroidJUnit4::class)
class M3RealMicDeviceTest {
    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    private fun nodeExists(text: String, substring: Boolean = false): Boolean =
        composeRule.onAllNodesWithText(text, substring = substring)
            .fetchSemanticsNodes().isNotEmpty()

    private fun scrollUntilText(text: String, timeoutMs: Long = 30_000) {
        val deadline = System.currentTimeMillis() + timeoutMs
        while (System.currentTimeMillis() < deadline) {
            if (nodeExists(text)) return
            composeRule.onRoot().performTouchInput { swipeUp() }
            composeRule.waitForIdle()
            Thread.sleep(250)
        }
        composeRule.onNodeWithText(text).assertIsDisplayed()
    }

    @Before
    fun prepareRealMicrophone() {
        context.getSharedPreferences("mockmate_voice_test", Context.MODE_PRIVATE)
            .edit()
            .putBoolean("fixture_enabled", false)
            .apply()
        InstrumentationRegistry.getInstrumentation().uiAutomation.executeShellCommand(
            "pm grant com.mockmate.app android.permission.RECORD_AUDIO"
        ).close()
        val audio = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
        audio.setStreamVolume(
            AudioManager.STREAM_MUSIC,
            audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC),
            0,
        )
    }

    private fun allowSystemPermissionDialog() {
        val device = UiDevice.getInstance(InstrumentationRegistry.getInstrumentation())
        val deadline = System.currentTimeMillis() + 15_000
        val labels = listOf(
            "仅在使用该应用时允许",
            "使用应用时允许",
            "使用时允许",
            "允许",
            "While using the app",
            "Allow",
        )
        while (System.currentTimeMillis() < deadline) {
            for (label in labels) {
                val node = device.findObject(By.textContains(label)) ?: continue
                node.click()
                return
            }
            Thread.sleep(200)
        }
    }

    private fun speakThroughDevice(text: String) {
        val ready = CountDownLatch(1)
        var tts: TextToSpeech? = null
        try {
            tts = TextToSpeech(context) { status ->
                if (status == TextToSpeech.SUCCESS) ready.countDown()
            }
            ready.await(15, TimeUnit.SECONDS)
            val engine = tts ?: return
            engine.language = Locale.SIMPLIFIED_CHINESE
            engine.speak(text, TextToSpeech.QUEUE_FLUSH, null, "r07-real-mic")
            Thread.sleep(1_500)
        } finally {
            // 让语音继续播放；测试结束再释放。
            Thread.sleep(4_000)
            tts?.stop()
            tts?.shutdown()
        }
    }

    @Test
    fun realMicrophoneAcousticLoopbackReachesReview() {
        val username = InstrumentationRegistry.getArguments().getString("username")
            ?: throw AssertionError("缺少 -e username")
        val password = InstrumentationRegistry.getArguments().getString("password")
            ?: throw AssertionError("缺少 -e password（使用本机生成的测试口令）")

        if (nodeExists("退出")) composeRule.onNodeWithText("退出").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("没有账号？注册") }
        val fields = composeRule.onAllNodes(hasSetTextAction())
        fields[0].performTextInput(username)
        fields[1].performTextInput(password)
        composeRule.onAllNodesWithText("登录").onLast().performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("新建面试准备") }
        Espresso.closeSoftKeyboard()
        composeRule.waitForIdle()
        composeRule.onNodeWithText("打开").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("准备设置与材料") }
        scrollUntilText("查看/生成计划")
        composeRule.onNodeWithText("查看/生成计划").performScrollTo().performClick()
        scrollUntilText("开始语音面试")
        composeRule.waitUntil(timeoutMillis = 30_000) {
            runCatching { composeRule.onNodeWithText("开始语音面试").assertIsEnabled() }.isSuccess
        }
        composeRule.onNodeWithText("开始语音面试").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) { nodeExists("同意音频处理并开始") }
        composeRule.onNodeWithText("同意音频处理并开始").performClick()
        composeRule.waitUntil(timeoutMillis = 60_000) {
            nodeExists("开始录音（最多 120 秒）") || nodeExists("停止播放")
        }
        if (nodeExists("停止播放")) {
            composeRule.onNodeWithText("停止播放").performClick()
        }
        composeRule.waitUntil(timeoutMillis = 30_000) { nodeExists("开始录音（最多 120 秒）") }
        composeRule.onNodeWithText("开始录音（最多 120 秒）")
            .performScrollTo().assertIsDisplayed().performClick()
        allowSystemPermissionDialog()
        composeRule.waitUntil(timeoutMillis = 20_000) { nodeExists("录音中") }
        Thread.sleep(1_000)
        speakThroughDevice("这是一次真机麦克风受控测试，内容是虚构的，不包含真实个人材料。")
        composeRule.onNodeWithText("停止录音（不自动提交）")
            .performScrollTo().assertIsDisplayed()
            .performSemanticsAction(SemanticsActions.OnClick)
        composeRule.waitUntil(timeoutMillis = 120_000) {
            nodeExists("原始转写（只读，保留识别结果）") ||
                nodeExists("未识别到有效语音", substring = true) ||
                nodeExists("语音转写失败", substring = true)
        }
        assertTrue(
            "真机物理麦克风未得到可用转写",
            nodeExists("原始转写（只读，保留识别结果）"),
        )
        assertTrue("真机测试不应启用 fixture", !nodeExists("当前录音来自调试 fixture", substring = true))
        val evidenceDir = File(context.filesDir, "m3-real-evidence")
        evidenceDir.mkdirs()
        val bitmap = composeRule.onRoot().captureToImage().asAndroidBitmap()
        File(evidenceDir, "real-mic-review.png").outputStream().use {
            bitmap.compress(android.graphics.Bitmap.CompressFormat.PNG, 100, it)
        }
    }
}
