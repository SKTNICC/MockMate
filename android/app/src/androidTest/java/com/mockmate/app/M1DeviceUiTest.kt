package com.mockmate.app

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.assertTextContains
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performTextInput
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 真机 UI 自动化：验证中文输入、长文本、草稿持久与键盘聚焦时字段可见。
 * 说明：使用 Compose 语义注入文本（不是软键盘拼音组合），但运行在真实设备与真实后端上。
 */
@RunWith(AndroidJUnit4::class)
class M1DeviceUiTest {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private val password = "MockMate-dev-2026"

    /** 测试可从已登录状态开始：先退出，保证从登录页走完整流程。 */
    private fun ensureLoggedOut() {
        val alreadyOnList = composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        if (alreadyOnList) {
            composeRule.onNodeWithText("退出").performClick()
        }
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodesWithText("没有账号？注册").fetchSemanticsNodes().isNotEmpty()
        }
    }

    @Test
    fun chineseInputLongTextAndDraftPersistence() {
        val suffix = System.currentTimeMillis().toString().takeLast(7)
        val username = "devzh$suffix"

        ensureLoggedOut()
        composeRule.onNodeWithText("没有账号？注册").performClick()
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        composeRule.onNodeWithText("注册并登录").performClick()

        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("新建面试准备").performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodesWithText("创建准备").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("创建准备").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("简历").fetchSemanticsNodes().isNotEmpty()
        }

        // 打开简历材料的粘贴新建，验证中文输入与长文本
        composeRule.onAllNodesWithText("粘贴新建")[0].performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        val title = "中文简历草稿"
        val chinese = "本科在读，计算机科学与技术专业。\n" +
            "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；我参与了借阅模块与边界测试。\n" +
            "学习过 Java 集合基础和 JUnit 单元测试。\n没有工作经历。"
        val longTail = "补充说明：".repeat(40)
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(title)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(chinese + longTail)

        composeRule.onAllNodes(hasSetTextAction())[1]
            .assertTextContains(chinese.substring(0, 8), substring = true)
        composeRule.onNodeWithText("保存原文").assertIsDisplayed()

        // 输入时字段仍可见（imePadding/滚动）
        composeRule.onAllNodes(hasSetTextAction())[1].assertIsDisplayed()

        // 返回后草稿保存在账号分区，重新进入仍可读回
        composeRule.onNodeWithText("返回").performClick()
        composeRule.waitUntil(timeoutMillis = 15_000) {
            composeRule.onAllNodesWithText("粘贴新建").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText("粘贴新建")[0].performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        composeRule.onAllNodes(hasSetTextAction())[0].assertTextContains(title, substring = true)
        composeRule.onAllNodes(hasSetTextAction())[1].assertTextContains("没有工作经历", substring = true)
    }


    @Test
    fun manualProfileEntryAndConfirm() {
        val suffix = System.currentTimeMillis().toString().takeLast(7)
        val username = "devman$suffix"

        ensureLoggedOut()
        composeRule.onNodeWithText("没有账号？注册").performClick()
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        composeRule.onNodeWithText("注册并登录").performClick()

        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("新建面试准备").performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodesWithText("创建准备").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("创建准备").performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("粘贴新建").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText("粘贴新建")[0].performClick()
        composeRule.waitUntil(timeoutMillis = 20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("Manual Resume")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput("BSc CS\nJava skill")
        composeRule.onNodeWithText("保存原文").performClick()

        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("结构化核对").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onNodeWithText("添加教育").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 3
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("BSc")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput("CS")

        composeRule.onNodeWithText("添加技能").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 10_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 6
        }
        composeRule.onAllNodes(hasSetTextAction())[3].performTextInput("Java")

        composeRule.onNodeWithText("确认并保存为不可变版本").performScrollTo().performClick()
        composeRule.waitUntil(timeoutMillis = 30_000) {
            composeRule.onAllNodesWithText("已确认版本").fetchSemanticsNodes().isNotEmpty() &&
                composeRule.onAllNodesWithText("revision 1", substring = true).fetchSemanticsNodes().isNotEmpty()
        }
    }

}
