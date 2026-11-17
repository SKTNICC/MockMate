package com.mockmate.app.core.session

import android.content.Context
import java.security.MessageDigest

/**
 * M2 回答草稿：按 accountId/sessionId/turnId 加密保存，避免普通 SharedPreferences 泄露正文。
 * 只保存“尚未被服务端确认”的本机输入；成功确认后仅清理匹配的键。
 */
class InterviewDraftStore(context: Context) {
    private val prefs = AesGcmPrefs(
        context,
        preferencesName = "mockmate_interview_drafts",
        keyAlias = "mockmate_interview_drafts_key",
    )

    fun key(accountId: String, sessionId: String, turnId: String): String {
        val digest = MessageDigest.getInstance("SHA-256")
            .digest("$accountId/$sessionId/$turnId".toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }
            .take(32)
        return "draft_$digest"
    }

    fun get(accountId: String, sessionId: String, turnId: String): SecureRead =
        prefs.read(key(accountId, sessionId, turnId))

    fun put(accountId: String, sessionId: String, turnId: String, value: String): SecureWrite =
        prefs.write(key(accountId, sessionId, turnId), value)

    fun clear(accountId: String, sessionId: String, turnId: String): Boolean =
        prefs.remove(key(accountId, sessionId, turnId))
}
