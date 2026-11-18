package com.mockmate.app

import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.layout.LayoutCoordinates
import androidx.compose.ui.layout.boundsInRoot
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.SemanticsActions
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import androidx.compose.ui.test.performScrollTo
import androidx.compose.ui.test.performSemanticsAction
import androidx.compose.ui.text.TextLayoutResult
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.test.ext.junit.runners.AndroidJUnit4
import com.mockmate.app.core.network.InterviewAnswerDto
import com.mockmate.app.core.network.InterviewCoverageDto
import com.mockmate.app.core.network.InterviewDimensionResultDto
import com.mockmate.app.core.network.InterviewEvidenceDto
import com.mockmate.app.core.network.InterviewFindingDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewReportQuestionDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.ui.screens.InterviewReportBody
import com.mockmate.app.ui.screens.quoteLineBounds
import com.mockmate.app.ui.theme.MockMateTheme
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class M2ReportScrollDeviceTest {

    @get:Rule
    val composeRule = createComposeRule()

    private fun longAnswerWithTailQuote(): String {
        val filler = List(45) { idx ->
            "第${idx + 1}段背景说明：我们在生产环境压测时持续观察线程池队列长度与连接池等待耗时，确保各阶段指标稳定。"
        }.joinToString("\n")
        return "$filler\n尾段关键结论：通过只读快照隔离后并发零超时 🎯。"
    }

    private fun secondAnswerWithEmojiAndRepeat(): String {
        val filler = List(30) { idx ->
            "补充过程${idx + 1}：排查缓存击穿与热点 Key 分布。"
        }.joinToString("\n")
        return "首段说明：「重复引文」在开头出现 😀。\n$filler\n末尾再次提到：「重复引文」出现在末尾。"
    }

    private fun sampleReport(
        longTailText: String,
        secondText: String,
    ): InterviewReportDto = InterviewReportDto(
        report_id = "report-scroll-test",
        session_id = "session-scroll",
        role_id = "java-backend",
        level = "intern",
        quality = "full",
        rubric_policy_version = "m2-rubric-policy-v1",
        created_at = "2026-11-18T00:10:00Z",
        end_reason = "completed",
        coverage = InterviewCoverageDto(
            planned = 3, presented = 3, answered = 3, skipped = 0, unanswered = 0,
            not_reached = 0, scored = 3, bank_planned = 2, bank_answered = 2,
            generated_planned = 1, generated_answered = 1,
            follow_up_presented = 1, follow_up_answered = 1,
        ),
        strengths = listOf(
            InterviewFindingDto(
                question_id = "q-3",
                position = 3,
                dimension_id = "depth",
                text = "尾段定位结论清晰",
                evidence = listOf(
                    InterviewEvidenceDto(
                        answer_id = "ans-3-fu",
                        quote = "尾段关键结论：通过只读快照隔离后并发零超时 🎯。",
                    )
                ),
            )
        ),
        improvements = listOf(
            InterviewFindingDto(
                question_id = "q-1",
                position = 1,
                dimension_id = "clarity",
                text = "重复引文定位首个匹配",
                evidence = listOf(
                    InterviewEvidenceDto(
                        answer_id = "ans-1",
                        quote = "「重复引文」",
                    )
                ),
            )
        ),
        questions = listOf(
            InterviewReportQuestionDto(
                question_id = "q-1",
                position = 1,
                source = "bank",
                outcome = "answered",
                prompt = "第一题主问题",
                assessment_trust = "verified",
                rubric_version = "r@1",
                turns = listOf(
                    InterviewTurnDto(
                        turn_id = "t-1",
                        question_id = "q-1",
                        position = 1,
                        kind = "main",
                        prompt = "第一题主问题",
                        status = "answered",
                        answer = InterviewAnswerDto("ans-1", secondText, "2026-11-18T00:01:00Z"),
                    )
                ),
                dimensions = listOf(
                    InterviewDimensionResultDto(
                        dimension_id = "dim-1",
                        name = "表达结构",
                        weight = 1.0,
                        verdict = "scored",
                        score = 3,
                        explanation = "说明清晰",
                        improvement = "补充边界",
                        evidence = listOf(
                            InterviewEvidenceDto("ans-1", "「重复引文」"),
                            InterviewEvidenceDto("ans-1", "报告里已找不到的引文片段"),
                        ),
                    )
                ),
            ),
            InterviewReportQuestionDto(
                question_id = "q-2",
                position = 2,
                source = "generated",
                outcome = "answered",
                prompt = "第二题主问题",
                assessment_trust = "verified",
                rubric_version = "r@1",
                turns = listOf(
                    InterviewTurnDto(
                        turn_id = "t-2",
                        question_id = "q-2",
                        position = 2,
                        kind = "main",
                        prompt = "第二题主问题",
                        status = "answered",
                        answer = InterviewAnswerDto("ans-2", "第二题简短回答", "2026-11-18T00:02:00Z"),
                    )
                ),
            ),
            InterviewReportQuestionDto(
                question_id = "q-3",
                position = 3,
                source = "bank",
                outcome = "answered",
                prompt = "第三题主问题",
                assessment_trust = "verified",
                rubric_version = "r@1",
                turns = listOf(
                    InterviewTurnDto(
                        turn_id = "t-3-main",
                        question_id = "q-3",
                        position = 3,
                        kind = "main",
                        prompt = "第三题主问题",
                        status = "answered",
                        answer = InterviewAnswerDto("ans-3-main", "第三题主回答概述", "2026-11-18T00:03:00Z"),
                    ),
                    InterviewTurnDto(
                        turn_id = "t-3-fu",
                        question_id = "q-3",
                        position = 3,
                        kind = "follow_up",
                        prompt = "第三题追问",
                        status = "answered",
                        answer = InterviewAnswerDto("ans-3-fu", longTailText, "2026-11-18T00:04:00Z"),
                    ),
                ),
                dimensions = listOf(
                    InterviewDimensionResultDto(
                        dimension_id = "dim-3",
                        name = "问题定位",
                        weight = 1.0,
                        verdict = "scored",
                        score = 4,
                        explanation = "定位准确",
                        improvement = "",
                        evidence = listOf(
                            InterviewEvidenceDto(
                                "ans-3-fu",
                                "尾段关键结论：通过只读快照隔离后并发零超时 🎯。",
                            ),
                            InterviewEvidenceDto("ans-missing", "缺失回答的证据"),
                        ),
                    )
                ),
            ),
        ),
    )

    private fun assertQuoteVisibleInViewport(
        listState: androidx.compose.foundation.lazy.LazyListState,
        viewportCoords: LayoutCoordinates?,
        capturedLayouts: Map<String, TextLayoutResult>,
        answerId: String,
        answerText: String,
        quote: String,
    ) {
        val vp = viewportCoords ?: error("viewport coordinates missing")
        val vpBounds = vp.boundsInRoot()
        val textLayout = capturedLayouts[answerId]
        assertNotNull("TextLayoutResult should be captured for $answerId", textLayout)
        val localRect = quoteLineBounds(textLayout!!, answerText, quote, vpBounds.height)
        assertNotNull("quoteLineBounds should resolve for $quote", localRect)

        // Text 节点的 positionInRoot 是未裁剪坐标，boundsInRoot 会被视口裁剪到 [0, 840]。
        val textNode = composeRule.onNodeWithText(answerText, substring = false).fetchSemanticsNode()
        val textTopInRoot = textNode.positionInRoot.y
        val quoteTopInRoot = textTopInRoot + localRect!!.top
        val quoteBottomInRoot = textTopInRoot + localRect.bottom
        assertTrue(
            "item=${listState.firstVisibleItemIndex}, offset=${listState.firstVisibleItemScrollOffset}, " +
                "textTop=$textTopInRoot, localRect=$localRect, " +
                "引文行 [$quoteTopInRoot, $quoteBottomInRoot] 必须与视口 [${vpBounds.top}, ${vpBounds.bottom}] 相交",
            quoteBottomInRoot > vpBounds.top && quoteTopInRoot < vpBounds.bottom,
        )
    }

    @Test
    fun clickingEvidenceScrollsLongAnswerTailIntoViewportAndHandlesRapidAndRepeatedClicks() {
        val longTail = longAnswerWithTailQuote()
        val secondText = secondAnswerWithEmojiAndRepeat()
        val report = sampleReport(longTail, secondText)
        val notifications = mutableListOf<String>()
        val capturedLayouts = ConcurrentHashMap<String, TextLayoutResult>()
        var viewportCoords by mutableStateOf<LayoutCoordinates?>(null)
        lateinit var listState: androidx.compose.foundation.lazy.LazyListState
        lateinit var scope: CoroutineScope

        composeRule.setContent {
            MockMateTheme {
                listState = rememberLazyListState()
                scope = rememberCoroutineScope()
                Box(
                    modifier = Modifier
                        .width(260.dp)
                        .height(320.dp)
                        .testTag("report_viewport")
                        .onGloballyPositioned { viewportCoords = it },
                ) {
                    InterviewReportBody(
                        report = report,
                        onNotify = { msg, _ -> notifications += msg },
                        listState = listState,
                        answerTextStyle = MaterialTheme.typography.bodySmall.copy(fontSize = 14.sp),
                        onAnswerLayoutCaptured = { id, layout -> capturedLayouts[id] = layout },
                    )
                }
            }
        }

        // 1. 滚到「优势」卡片后点击其中的尾段引文：应跨过多题并滚到超长追问回答的末尾所在行
        val tailQuote = "尾段关键结论：通过只读快照隔离后并发零超时 🎯。"
        composeRule.runOnIdle { scope.launch { listState.scrollToItem(2) } }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("原话：$tailQuote").performScrollTo().performClick()
        composeRule.waitForIdle()
        assertQuoteVisibleInViewport(listState, viewportCoords, capturedLayouts, "ans-3-fu", longTail, tailQuote)

        // 2. 滚回「优势/待改进」，不等待中间滚动完成即快速交替点击两个证据，最终位置和高亮应对应最后一次点击
        composeRule.runOnIdle { scope.launch { listState.scrollToItem(2) } }
        composeRule.waitForIdle()
        composeRule.mainClock.autoAdvance = false
        composeRule.onNodeWithText("原话：「重复引文」").performSemanticsAction(SemanticsActions.OnClick)
        composeRule.mainClock.advanceTimeByFrame()
        composeRule.onNodeWithText("原话：$tailQuote").performSemanticsAction(SemanticsActions.OnClick)
        composeRule.mainClock.autoAdvance = true
        composeRule.waitForIdle()
        assertQuoteVisibleInViewport(listState, viewportCoords, capturedLayouts, "ans-3-fu", longTail, tailQuote)

        // 3. 手动滚离后，重复点击同一证据仍应重新滚回引文行
        composeRule.runOnIdle { scope.launch { listState.scrollToItem(2) } }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("原话：$tailQuote").performScrollTo().performClick()
        composeRule.waitForIdle()
        assertQuoteVisibleInViewport(listState, viewportCoords, capturedLayouts, "ans-3-fu", longTail, tailQuote)

        // 4. 点击第 3 题中缺失回答的证据给出提示
        composeRule.onNodeWithText("证据：缺失回答的证据").performScrollTo().performClick()
        composeRule.waitForIdle()
        assertTrue(notifications.any { it.contains("无法定位该原话") })

        // 5. 滚到第 1 题逐维度证据，点击「重复引文」和「缺失引文」
        composeRule.runOnIdle { scope.launch { listState.scrollToItem(4) } }
        composeRule.waitForIdle()
        composeRule.onNodeWithText("证据：「重复引文」").performScrollTo().performClick()
        composeRule.waitForIdle()
        assertQuoteVisibleInViewport(listState, viewportCoords, capturedLayouts, "ans-1", secondText, "「重复引文」")

        composeRule.onNodeWithText("证据：报告里已找不到的引文片段").performScrollTo().performClick()
        composeRule.waitForIdle()
        assertTrue(notifications.any { it.contains("引文未在当前报告原文中逐字出现") })
    }
}
