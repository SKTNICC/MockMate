package com.mockmate.app.ui.screens

import androidx.compose.ui.geometry.Rect
import androidx.compose.ui.text.TextLayoutResult
import com.mockmate.app.core.network.InterviewReportDto

/**
 * 报告 LazyColumn 的实际列表项索引。概览和覆盖度固定占前两项，
 * 「优势」「待改进」是可选卡片；题目项按此偏移计算，避免点击证据后滚错位置。
 */
data class ReportLayoutIndex(
    val questionItemIndex: Map<String, Int>,
    val answerQuestionItemIndex: Map<String, Int>,
    val answerText: Map<String, String>,
)

fun buildReportLayoutIndex(report: InterviewReportDto): ReportLayoutIndex {
    var item = 2 // 报告概览 + 覆盖度
    if (report.strengths.isNotEmpty()) item += 1
    if (report.improvements.isNotEmpty()) item += 1
    val questionIndex = mutableMapOf<String, Int>()
    val answerIndex = mutableMapOf<String, Int>()
    val answerText = mutableMapOf<String, String>()
    report.questions.forEach { question ->
        questionIndex[question.question_id] = item
        question.turns.forEach { turn ->
            turn.answer?.let { answer ->
                answerIndex[answer.answer_id] = item
                answerText[answer.answer_id] = answer.text
            }
        }
        item += 1
    }
    return ReportLayoutIndex(questionIndex, answerIndex, answerText)
}

/** 首个逐字匹配位置；换行、中文与四字节表情都按原字符串子串处理。 */
fun quoteRange(text: String, quote: String): IntRange? {
    if (quote.isEmpty()) return null
    val start = text.indexOf(quote)
    return if (start >= 0) start until start + quote.length else null
}

/**
 * 将引文字符串偏移映射为文本局部坐标中的行矩形，供 BringIntoViewRequester 滚动定位。
 * 优先保证引文起始行可见；跨行引文在整体可放入视口时合并所有对应行，否则聚焦起始行。
 */
fun quoteLineBounds(
    layout: TextLayoutResult,
    text: String,
    quote: String,
    viewportHeightPx: Float = 0f,
): Rect? {
    val range = quoteRange(text, quote) ?: return null
    if (layout.lineCount <= 0 || text.isEmpty() || layout.layoutInput.text.text != text) {
        return null
    }
    val maxIndex = (layout.layoutInput.text.length - 1).coerceAtLeast(0)
    val startOffset = range.first.coerceIn(0, maxIndex)
    val endOffset = range.last.coerceIn(startOffset, maxIndex)
    val startLine = layout.getLineForOffset(startOffset)
    val endLine = layout.getLineForOffset(endOffset)
    val top = layout.getLineTop(startLine)
    val startBottom = layout.getLineBottom(startLine)
    val endBottom = layout.getLineBottom(endLine)
    val bottom = if (viewportHeightPx > 0f && (endBottom - top) <= viewportHeightPx) {
        endBottom
    } else {
        startBottom
    }
    val width = layout.size.width.toFloat().coerceAtLeast(1f)
    return Rect(0f, top, width, bottom.coerceAtLeast(top + 1f))
}
