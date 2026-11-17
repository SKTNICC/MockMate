package com.mockmate.app

import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * D-M03：确认计划后由外部 force-stop，再冷启动本测试。
 * 只读同一准备与计划；断言已确认状态仍在，且不重新触发生成。
 */
@RunWith(AndroidJUnit4::class)
class M1ColdStartRestoreTest {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private fun waitFor(text: String, timeout: Long = 30_000) {
        composeRule.waitUntil(timeoutMillis = timeout) {
            composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun click(text: String, index: Int = 0) {
        repeat(8) {
            if (composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()) return@repeat
            composeRule.waitForIdle()
            runCatching {
                composeRule.onRoot().performTouchInput { swipeUp() }
            }
            composeRule.waitForIdle()
        }
        waitFor(text)
        composeRule.onAllNodesWithText(text)[index].performScrollTo().performClick()
    }

    @Test
    fun confirmedPlanIsReadBackAfterForceStopAndColdStart() {
        // 冷启动后应直接恢复账号并进入准备列表，不需要重新登录。
        waitFor("新建面试准备", 40_000)
        click("打开")
        click("查看/生成计划")
        waitFor("计划摘要", 30_000)
        assertTrue(
            composeRule.onAllNodesWithText("已确认").fetchSemanticsNodes().isNotEmpty()
        )
        // 已确认计划页不应出现“生成计划”动作；若误触发新生成会显示该按钮。
        assertTrue(
            composeRule.onAllNodesWithText("生成计划").fetchSemanticsNodes().isEmpty()
        )
    }
}
