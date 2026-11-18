package com.mockmate.app.core.session

import android.content.Context
import java.security.MessageDigest

interface InterviewDraftStorage {
    fun get(accountId: String, sessionId: String, turnId: String): SecureRead
    fun put(accountId: String, sessionId: String, turnId: String, value: String): SecureWrite
    fun clear(accountId: String, sessionId: String, turnId: String): Boolean
}

class InMemoryInterviewDraftStorage : InterviewDraftStorage {
    private val values = mutableMapOf<String, String>()

    override fun get(accountId: String, sessionId: String, turnId: String): SecureRead =
        values["$accountId/$sessionId/$turnId"]?.let { SecureRead.Present(it) } ?: SecureRead.Absent

    override fun put(accountId: String, sessionId: String, turnId: String, value: String): SecureWrite {
        values["$accountId/$sessionId/$turnId"] = value
        return SecureWrite.Ok
    }

    override fun clear(accountId: String, sessionId: String, turnId: String): Boolean {
        values.remove("$accountId/$sessionId/$turnId")
        return true
    }
}

/**
 * M2 回答草稿：按 accountId/sessionId/turnId 加密保存，避免普通 SharedPreferences 泄露正文。
 * 只保存“尚未被服务端确认”的本机输入；成功确认后仅清理匹配的键。
 */
class InterviewDraftStore(context: Context) : InterviewDraftStorage {
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

    override fun get(accountId: String, sessionId: String, turnId: String): SecureRead =
        prefs.read(key(accountId, sessionId, turnId))

    override fun put(accountId: String, sessionId: String, turnId: String, value: String): SecureWrite =
        prefs.write(key(accountId, sessionId, turnId), value)

    override fun clear(accountId: String, sessionId: String, turnId: String): Boolean =
        prefs.remove(key(accountId, sessionId, turnId))
}
