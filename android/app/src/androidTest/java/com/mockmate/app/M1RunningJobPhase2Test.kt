package com.mockmate.app

import android.util.Log
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * E03 / E-M04 第二阶段：force-stop 后冷启动，仍在 running 的原 job 必须由唯一订阅
 * 自动查询到终态并显示计划摘要；测试期间不点击“只读查询最新状态”，也不产生新的生成 POST。
 */
@RunWith(AndroidJUnit4::class)
class M1RunningJobPhase2Test {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private val args = InstrumentationRegistry.getArguments()
    private val username = args.getString("e04_user").orEmpty()
    private val password = args.getString("e04_pass").orEmpty()

    private fun has(text: String): Boolean =
        composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()

    private fun waitFor(text: String, timeout: Long = 60_000) {
        composeRule.waitUntil(timeoutMillis = timeout) { has(text) }
    }

    private fun click(text: String, index: Int = 0) {
        repeat(10) {
            if (has(text)) return@repeat
            composeRule.waitForIdle()
            runCatching { composeRule.onRoot().performTouchInput { swipeUp() } }
            composeRule.waitForIdle()
        }
        waitFor(text)
        runCatching { composeRule.onAllNodesWithText(text)[index].performScrollTo() }
        composeRule.onAllNodesWithText(text)[index].performClick()
    }

    private fun loginIfNeeded() {
        if (has("新建面试准备")) return
        require(username.isNotBlank()) { "缺少 instrumentation 参数 e04_user" }
        waitFor("没有账号？注册", 20_000)
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        // 页面标题也是“登录”，最后一个才是提交按钮。
        val loginButtons = composeRule.onAllNodesWithText("登录").fetchSemanticsNodes().size
        composeRule.onAllNodesWithText("登录")[loginButtons - 1].performClick()
        waitFor("新建面试准备", 30_000)
    }

    @Test
    fun coldStartRunningJobAutoContinuesToTerminalWithoutManualQuery() {
        loginIfNeeded()
        click("打开")
        click("查看/生成计划")
        Log.i("M1D_M07", "COLD_START_PLAN_OPEN ${System.currentTimeMillis()}")

        // 关键：不点击“只读查询最新状态”。前台恢复建立的唯一订阅应自动看到终态。
        waitFor("计划摘要", 180_000)
        assertTrue(has("题库 3 + 生成 3"))
        Log.i("M1D_M07", "TERMINAL_VISIBLE ${System.currentTimeMillis()}")
    }
}
