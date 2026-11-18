package com.mockmate.app.ui.screens

internal data class PlanInsufficientDetails(
    val required: Int,
    val available: Int,
    val feasibleCounts: List<Int>,
    val summary: String,
    val guidance: String,
)

internal object PlanErrorFormatter {
    private val ALLOWED_MAIN_QUESTION_COUNTS = listOf(2, 4, 6)

    fun parseBankInsufficient(details: List<String>): PlanInsufficientDetails? {
        var required: Int? = null
        var available: Int? = null
        for (entry in details) {
            when {
                entry.startsWith("required=") -> required = entry.removePrefix("required=").toIntOrNull()
                entry.startsWith("available=") -> available = entry.removePrefix("available=").toIntOrNull()
            }
        }
        if (required == null || available == null || required <= 0 || available < 0) {
            return null
        }
        val feasible = ALLOWED_MAIN_QUESTION_COUNTS.filter { it / 2 <= available }
        val summary = "本场需要 $required 道相关题库题，目前匹配到 $available 道。"
        val guidance = if (available == 0 || feasible.isEmpty()) {
            "当前题库覆盖不足，没有匹配到与当前材料技能关联的已审核题库题。请回到准备检查并关联对应技能，或等待补充对应题库。"
        } else {
            val options = feasible.joinToString(" 或 ") { "$it 题" }
            "你可以回到准备检查材料技能关联，或回到设置选择较少主问题（当前可选 $options）。"
        }
        return PlanInsufficientDetails(
            required = required,
            available = available,
            feasibleCounts = feasible,
            summary = summary,
            guidance = guidance,
        )
    }

    fun userMessage(code: String?, serverMessage: String, details: List<String>): String {
        if (code == "BANK_INSUFFICIENT") {
            parseBankInsufficient(details)?.let { parsed ->
                return "${parsed.summary}${parsed.guidance}"
            }
        }
        return serverMessage
    }
}
