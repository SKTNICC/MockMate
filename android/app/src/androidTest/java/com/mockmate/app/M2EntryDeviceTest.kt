package com.mockmate.app

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * M2 设备入口冒烟：登录后从准备列表进入「面试记录」，确认新页面与空状态可见。
 * 依赖已启动的测试 API（adb reverse tcp:8000 tcp:8000）与合成账号注册。
 */
@RunWith(AndroidJUnit4::class)
class M2EntryDeviceTest {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private val password = "MockMate-dev-2026"

    private fun ensureLoggedOut() {
        val alreadyOnList = composeRule.onAllNodesWithText("新建面试准备")
            .fetchSemanticsNodes().isNotEmpty()
        if (alreadyOnList) {
            composeRule.onNodeWithText("退出").performClick()
        }
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodesWithText("没有账号？注册").fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun interviewHistoryEntryOpensFromPreparationList() {
        val suffix = System.currentTimeMillis().toString().takeLast(7)
        val username = "devm2$suffix"

        ensureLoggedOut()
        composeRule.onNodeWithText("没有账号？注册").performClick()
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        composeRule.onNodeWithText("注册并登录").performClick()

        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("面试记录").performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodesWithText("按开场时间倒序。原准备或计划被删除后，旧场仍可按 sessionId 查阅。")
                .fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("面试记录").assertIsDisplayed()
        composeRule.onNodeWithText("还没有面试记录。确认计划后可从计划页开始文字面试。")
            .assertIsDisplayed()
    }
}
