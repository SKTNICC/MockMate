package com.mockmate.app.core.session

import android.content.Context

interface DraftStorage {
    fun get(accountId: String, name: String): String?
    fun put(accountId: String, name: String, value: String)
    fun clear(accountId: String, name: String)
    fun clearAllFor(accountId: String)
}

class InMemoryDraftStorage : DraftStorage {
    private val values = mutableMapOf<String, String>()
    private fun key(accountId: String, name: String) = "$accountId:$name"
    override fun get(accountId: String, name: String): String? = values[key(accountId, name)]
    override fun put(accountId: String, name: String, value: String) {
        values[key(accountId, name)] = value
    }
    override fun clear(accountId: String, name: String) {
        values.remove(key(accountId, name))
    }
    override fun clearAllFor(accountId: String) {
        values.keys.removeAll { it.startsWith("$accountId:") }
    }
}

/**
 * 按账号分区的本地草稿：退出只清理可见状态，不清除其他账号的草稿；
 * 草稿只代表“未上传/未确认”的本地输入，不能伪造已上传或已确认。
 */
class DraftStore(context: Context) : DraftStorage {
    private val prefs = context.getSharedPreferences("mockmate_drafts", Context.MODE_PRIVATE)

    private fun key(accountId: String, name: String) = "$accountId:$name"

    override fun get(accountId: String, name: String): String? = prefs.getString(key(accountId, name), null)

    override fun put(accountId: String, name: String, value: String) {
        prefs.edit().putString(key(accountId, name), value).apply()
    }

    override fun clear(accountId: String, name: String) {
        prefs.edit().remove(key(accountId, name)).apply()
    }

    override fun clearAllFor(accountId: String) {
        val editor = prefs.edit()
        prefs.all.keys.filter { it.startsWith("$accountId:") }.forEach { editor.remove(it) }
        editor.apply()
    }
}
