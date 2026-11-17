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
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * E-M03 / E01：同账号 P1→P2 的迟到终态隔离。
 * P1 为 6 题（列表第一，已匹配），P2 为 2 题（列表第二，未匹配）。
 * 成功用例不需要宿主编辑；冲突用例由宿主在看到 MISMATCH_READY 后提升 P1 revision。
 */
@RunWith(AndroidJUnit4::class)
class M1RestoreIsolationDeviceTest {

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

    private fun waitForOpenButtons(count: Int, timeout: Long = 30_000) {
        composeRule.waitUntil(timeoutMillis = timeout) {
            composeRule.onAllNodesWithText("打开").fetchSemanticsNodes().size >= count
        }
    }

    private fun click(text: String, index: Int = 0) {
        repeat(8) {
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
        require(username.isNotBlank()) { "缺少 instrumentation 参数 e04_user" }
        // 测试可能从上一个账号的已登录状态开始；必须切到隔离账号，不能沿用他人列表。
        if (has("新建面试准备")) click("退出")
        waitFor("没有账号？注册", 20_000)
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        val loginButtons = composeRule.onAllNodesWithText("登录").fetchSemanticsNodes().size
        composeRule.onAllNodesWithText("登录")[loginButtons - 1].performClick()
        waitFor("新建面试准备", 30_000)
    }

    private fun startP1Generation() {
        waitForOpenButtons(2)
        click("打开", 0) // P1（6 题、已匹配）稳定在列表第一
        click("查看/生成计划")
        waitFor("生成计划", 60_000)
        click("生成计划")
        click("同意并继续")
        waitFor("任务状态：执行中", 60_000)
    }

    private fun backToList() {
        click("返回")
        click("返回")
        waitForOpenButtons(2)
    }

    private fun openP2Plan() {
        click("打开", 1) // P2（2 题、未匹配）
        click("查看/生成计划")
        waitFor("生成个性化计划", 30_000)
    }

    @Test
    fun succeededP1PlanDoesNotProjectIntoP2AndCanBeReopened() {
        loginIfNeeded()
        startP1Generation()
        Log.i("M1D_M03", "P1_RUNNING ${System.currentTimeMillis()}")

        backToList()
        openP2Plan()
        Log.i("M1D_M03", "ON_P2 ${System.currentTimeMillis()}")

        val deadline = System.currentTimeMillis() + 210_000
        while (System.currentTimeMillis() < deadline) {
            assertTrue("P1 的计划摘要不得出现在 P2", !has("计划摘要"))
            assertTrue("P1 的终态消息不得出现在 P2", !has("旧任务已失效"))
            assertTrue("P2 不应显示 P1 的任务状态", !has("任务状态："))
            Thread.sleep(5_000)
        }
        Log.i("M1D_M03", "P2_CLEAN ${System.currentTimeMillis()}")

        backToList()
        click("打开", 0) // P1 仍是第一
        click("查看/生成计划")
        waitFor("计划摘要", 60_000)
        assertTrue(has("题库 3 + 生成 3"))
        Log.i("M1D_M03", "P1_REOPENED ${System.currentTimeMillis()}")
    }

    @Test
    fun supersededP1JobDoesNotProjectErrorIntoP2AndP1StaysWithoutPlan() {
        loginIfNeeded()
        startP1Generation()
        Log.i("M1D_M03", "MISMATCH_READY ${System.currentTimeMillis()}")

        backToList()
        openP2Plan()
        Log.i("M1D_M03", "ON_P2_MISMATCH ${System.currentTimeMillis()}")

        val deadline = System.currentTimeMillis() + 100_000
        while (System.currentTimeMillis() < deadline) {
            assertTrue("P1 的旧任务提示不得出现在 P2", !has("旧任务已失效"))
            assertTrue("P1 的 revision 冲突提示不得出现在 P2", !has("旧生成任务对应的是准备"))
            assertTrue("P2 不得出现任何计划摘要", !has("计划摘要"))
            assertTrue("P2 不应显示 P1 的任务状态", !has("任务状态："))
            Thread.sleep(5_000)
        }
        Log.i("M1D_M03", "P2_CLEAN_MISMATCH ${System.currentTimeMillis()}")

        backToList()
        click("打开", 0)
        click("查看/生成计划")
        try {
            composeRule.waitUntil(timeoutMillis = 60_000) {
                has("旧任务已失效") || has("旧生成任务对应的是准备") || has("任务状态：已失效")
            }
        } catch (failure: Throwable) {
            composeRule.onRoot().printToLog("M1D_M03")
            Log.i(
                "M1D_M03",
                "P1_STATE_FAILED summary=${has("计划摘要")} superseded=${has("旧任务已失效")} " +
                    "task=${has("任务状态：已失效")} planBtn=${has("生成计划")}",
            )
            throw failure
        }
        assertTrue("失效任务不得生成计划摘要", !has("计划摘要"))
        Log.i("M1D_M03", "P1_MISMATCH_VISIBLE ${System.currentTimeMillis()}")
    }
}
