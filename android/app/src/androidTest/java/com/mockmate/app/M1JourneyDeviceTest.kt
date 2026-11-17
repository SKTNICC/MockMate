package com.mockmate.app

import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.hasScrollAction
import androidx.compose.ui.test.hasSetTextAction
import androidx.compose.ui.test.hasText
import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.onRoot
import androidx.compose.ui.test.printToLog
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performScrollToNode
import androidx.compose.ui.test.performTextInput
import androidx.compose.ui.test.performTouchInput
import androidx.compose.ui.test.swipeDown
import androidx.compose.ui.test.swipeUp
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * 真机 + 真实后端（测试 provider）的完整计划旅程：
 * 注册 → 准备 → 简历/内置 JD 解析与确认 → 匹配 → 生成 6 题 → 确认 → 重开。
 * 不调用真实付费模型；不是“真实 AI 已验收”，只验证 UI/网络/持久化路径。
 */
@RunWith(AndroidJUnit4::class)
class M1JourneyDeviceTest {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    private val password = "MockMate-dev-2026"

    private fun waitFor(text: String, timeout: Long = 30_000) {
        try {
            composeRule.waitUntil(timeoutMillis = timeout) {
                composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()
            }
        } catch (failure: Throwable) {
            // 失败时把当前语义树写入 logcat，便于定位停留在哪个页面。
            composeRule.onRoot().printToLog("M1JOURNEY")
            throw failure
        }
    }

    private fun visible(text: String): Boolean =
        composeRule.onAllNodesWithText(text).fetchSemanticsNodes().isNotEmpty()

    /** 页面可能停在顶部或已被滚动；上下都试，避免只往下滑而错过顶部的操作按钮。 */
    private fun ensureVisible(text: String) {
        repeat(12) { attempt ->
            if (visible(text)) return
            composeRule.waitForIdle()
            if (visible(text)) return
            runCatching {
                composeRule.onRoot().performTouchInput {
                    if (attempt % 2 == 0) swipeUp() else swipeDown()
                }
            }
            composeRule.waitForIdle()
        }
        waitFor(text)
    }

    private fun click(text: String, index: Int = 0) {
        ensureVisible(text)
        runCatching {
            composeRule.onAllNodesWithText(text)[index].performScrollTo()
        }
        composeRule.onAllNodesWithText(text)[index].performClick()
    }

    private fun ensureLoggedOut() {
        val alreadyOnList = composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        if (alreadyOnList) {
            composeRule.onNodeWithText("退出").performClick()
        }
        waitFor("没有账号？注册", 20_000)
    }

    private fun register(username: String) {
        ensureLoggedOut()
        composeRule.onNodeWithText("没有账号？注册").performClick()
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput(username)
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(password)
        composeRule.onNodeWithText("注册并登录").performClick()
        waitFor("新建面试准备")
    }

    private fun createPreparation() {
        click("新建面试准备")
        click("创建准备")
        waitFor("粘贴新建")
    }

    private fun saveAndParseResume() {
        composeRule.onAllNodesWithText("粘贴新建")[0].performClick()
        composeRule.waitUntil(20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("真机旅程简历")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(
            "本科在读，计算机科学与技术专业。\n" +
                "课程项目：校园图书借阅系统，使用 Java 的 ArrayList 和 HashMap 保存并查询图书信息；" +
                "我参与了借阅模块与边界测试。\n" +
                "学习过 Java 集合基础和 JUnit 单元测试。\n" +
                "没有工作经历。"
        )
        click("保存原文")
        waitFor("结构化核对")
        click("AI 解析并生成结构")
        click("同意并继续")
        composeRule.waitUntil(40_000) {
            composeRule.onAllNodesWithText("自动提取").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText("确认并保存为不可变版本")[0]
            .performScrollTo()
            .performClick()
        waitFor("用于本次准备", 30_000)
        click("用于本次准备")
        waitFor("岗位 JD")
    }

    private fun saveAndParseTemplateJd() {
        click("内置 JD")
        waitFor("使用该模板创建 JD")
        click("使用该模板创建 JD")
        waitFor("结构化核对")
        click("AI 解析并生成结构")
        click("同意并继续")
        composeRule.waitUntil(40_000) {
            composeRule.onAllNodesWithText("自动提取").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText("确认并保存为不可变版本")[0]
            .performScrollTo()
            .performClick()
        waitFor("用于本次准备", 30_000)
        click("用于本次准备")
    }

    private fun matchGenerateConfirmAndReopen() {
        click("查看匹配分析")
        click("开始匹配分析")
        click("同意并继续")
        waitFor("匹配分析完成", 60_000)
        click("查看/生成计划")
        click("生成计划")
        click("同意并继续")
        waitFor("计划摘要", 90_000)
        click("确认并保存计划")
        waitFor("计划已确认并保存", 30_000)
        waitFor("已保存", 10_000)
        composeRule.onAllNodesWithText("已保存")[0].assertIsDisplayed()

        // 返回准备页再重新进入，验证同一计划可从服务端重开。
        click("返回")
        waitFor("查看/生成计划")
        click("查看/生成计划")
        waitFor("计划摘要", 30_000)
        assertTrue(composeRule.onAllNodesWithText("已确认").fetchSemanticsNodes().isNotEmpty())
    }

    private fun saveManualNoProjectResume() {
        click("粘贴新建")
        composeRule.waitUntil(20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("无项目简历")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput("BSc CS\nJava skill")
        click("保存原文")
        waitFor("结构化核对")

        click("添加教育")
        composeRule.waitUntil(10_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 3
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("BSc")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput("CS")

        click("添加技能")
        composeRule.waitUntil(10_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 6
        }
        composeRule.onAllNodes(hasSetTextAction())[3].performTextInput("Java")

        composeRule.onAllNodesWithText("确认并保存为不可变版本")[0]
            .performScrollTo()
            .performClick()
        waitFor("用于本次准备", 30_000)
        click("用于本次准备")
    }

    private fun saveSelfProvidedJd() {
        click("粘贴新建", index = 1)
        composeRule.waitUntil(20_000) {
            composeRule.onAllNodes(hasSetTextAction()).fetchSemanticsNodes().size >= 2
        }
        composeRule.onAllNodes(hasSetTextAction())[0].performTextInput("自备 JD")
        composeRule.onAllNodes(hasSetTextAction())[1].performTextInput(
            "Java 后端实习生\n" +
                "职责：完成 Java 功能开发并说明实现思路。\n" +
                "要求：理解 Java 集合的使用及基本边界。\n" +
                "要求：能够为简单功能设计边界测试。"
        )
        click("保存原文")
        waitFor("结构化核对")
        click("AI 解析并生成结构")
        click("同意并继续")
        composeRule.waitUntil(40_000) {
            composeRule.onAllNodesWithText("自动提取").fetchSemanticsNodes().isNotEmpty()
        }
        composeRule.onAllNodesWithText("确认并保存为不可变版本")[0]
            .performScrollTo()
            .performClick()
        waitFor("用于本次准备", 30_000)
        click("用于本次准备")
    }

    @Test
    fun fullPlanJourneyWithBuiltInJd() {
        val suffix = System.currentTimeMillis().toString().takeLast(7)
        register("journey$suffix")
        createPreparation()
        saveAndParseResume()
        saveAndParseTemplateJd()
        matchGenerateConfirmAndReopen()
    }

    @Test
    fun noProjectManualResumeAndSelfProvidedJdJourney() {
        val suffix = System.currentTimeMillis().toString().takeLast(7)
        register("noj$suffix")
        createPreparation()
        saveManualNoProjectResume()
        saveSelfProvidedJd()
        matchGenerateConfirmAndReopen()
    }
}
