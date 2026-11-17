package com.mockmate.app.core.session

import android.content.Context

/**
 * 按账号分区的本地草稿：退出只清理可见状态，不清除其他账号的草稿；
 * 草稿只代表“未上传/未确认”的本地输入，不能伪造已上传或已确认。
 */
class DraftStore(context: Context) {
    private val prefs = context.getSharedPreferences("mockmate_drafts", Context.MODE_PRIVATE)

    private fun key(accountId: String, name: String) = "$accountId:$name"

    fun get(accountId: String, name: String): String? = prefs.getString(key(accountId, name), null)

    fun put(accountId: String, name: String, value: String) {
        prefs.edit().putString(key(accountId, name), value).apply()
    }

    fun clear(accountId: String, name: String) {
        prefs.edit().remove(key(accountId, name)).apply()
    }

    fun clearAllFor(accountId: String) {
        val editor = prefs.edit()
        prefs.all.keys.filter { it.startsWith("$accountId:") }.forEach { editor.remove(it) }
        editor.apply()
    }
}
