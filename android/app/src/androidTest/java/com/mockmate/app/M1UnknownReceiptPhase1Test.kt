package com.mockmate.app

import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.SecureOperationStore
import com.mockmate.app.core.session.SecureSessionStore
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * D-M06 第一阶段：服务端测试中间件完成写入但丢弃一次回执。
 * 断言客户端保留 pending，而不是把未知成功当成确定失败。
 */
@RunWith(AndroidJUnit4::class)
class M1UnknownReceiptPhase1Test {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private fun waitFor(text: String, timeout: Long = 30_000) {
        composeRule.waitUntil(timeoutMillis = timeout) {
            composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()
        }
    }

    private fun waitForSubstring(text: String, timeout: Long = 30_000) {
        composeRule.waitUntil(timeoutMillis = timeout) {
            composeRule.onAllNodesWithText(text, substring = true).fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun lostReceiptKeepsPendingOperation() {
        waitFor("新建面试准备", 40_000)
        composeRule.onAllNodesWithText("打开")[0].performScrollTo().performClick()
        waitFor("粘贴新建", 30_000)
        composeRule.onAllNodesWithText("粘贴新建")[0].performScrollTo().performClick()
        composeRule.waitUntil(20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        val suffix = System.currentTimeMillis().toString().takeLast(8)
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("未知回执-$suffix")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(
            "本科在读，计算机科学与技术专业。\n学习过 Java 集合基础。\n没有工作经历。"
        )
        composeRule.onNodeWithText("保存原文").performScrollTo().performClick()

        // 服务端返回 201 但正文为空；客户端必须显示未知回执，而不是确定失败。
        waitForSubstring("无法解析回执", 40_000)
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val accountId = SecureSessionStore(context).load()?.accountId
        assertTrue(accountId != null)
        val loaded = runBlocking { SecureOperationStore(context).load(accountId!!) }
        assertTrue(loaded is OperationLoad.Loaded)
        val pending = (loaded as OperationLoad.Loaded).operations.pending
        assertEquals(1, pending.size)
        assertEquals("createTextMaterial", pending.single().type)
    }
}
