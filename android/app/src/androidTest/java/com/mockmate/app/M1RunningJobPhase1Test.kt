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
import androidx.lifecycle.Lifecycle
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * E03 / E-M04 第一阶段：用宿主机预置的隔离测试账号（已确认材料 + 匹配）启动慢速生成，
 * 在“执行中”切后台 6s 再回前台，确认恢复后仍显示同一任务；随后等待宿主机 force-stop。
 *
 * 账号由 tools/m1/e04_setup.py 创建；凭据通过 instrumentation 参数传入，不写入源码。
 */
@RunWith(AndroidJUnit4::class)
class M1RunningJobPhase1Test {

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

    private fun login() {
        require(username.isNotBlank()) { "缺少 instrumentation 参数 e04_user" }
        if (has("新建面试准备")) click("退出")
        waitFor("没有账号？注册", 20_000)
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        // 页面标题也是“登录”，最后一个才是提交按钮。
        val loginButtons = composeRule.onAllNodesWithText("登录").fetchSemanticsNodes().size
        composeRule.onAllNodesWithText("登录")[loginButtons - 1].performClick()
        waitFor("新建面试准备", 30_000)
    }

    @Test
    fun backgroundForegroundKeepsSameRunningJobThenWaitForHostForceStop() {
        login()
        click("打开")
        click("查看/生成计划")
        waitFor("生成计划", 60_000)
        click("生成计划")
        click("同意并继续")

        waitFor("任务状态：执行中", 60_000)
        Log.i("M1D_M07", "BACKGROUND_START ${System.currentTimeMillis()}")
        composeRule.activityRule.scenario.moveToState(Lifecycle.State.CREATED)
        Thread.sleep(6_000)
        Log.i("M1D_M07", "BACKGROUND_END ${System.currentTimeMillis()}")
        composeRule.activityRule.scenario.moveToState(Lifecycle.State.RESUMED)
        Log.i("M1D_M07", "FOREGROUND ${System.currentTimeMillis()}")

        // 回到前台后恢复原 job；平台不得因切后台而重新 POST 或丢失任务。
        waitFor("任务状态：执行中", 60_000)
        Log.i("M1D_M04", "GENERATION_STARTED")
        Thread.sleep(120_000)
    }
}
