package com.mockmate.app.core.audio

import android.content.Context
import com.mockmate.app.core.session.AesGcmPrefs
import com.mockmate.app.core.session.SecureRead
import com.mockmate.app.core.session.SecureWrite
import java.security.MessageDigest
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

/**
 * 用户对原始转写的核对修改。与原始转写分开保存，绑定 account/session/turn/take/
 * asset/transcript revision；只有同一身份恢复时才覆盖 UI，提交确认或明确丢弃后清理。
 */
@Serializable
data class VoiceReviewDraft(
    val sessionId: String,
    val turnId: String,
    val takeId: String,
    val assetId: String,
    val transcriptRevision: Int,
    val text: String,
    val edited: Boolean,
    val updatedAt: Long = System.currentTimeMillis(),
)

sealed interface VoiceReviewRead {
    data object Absent : VoiceReviewRead
    data class Present(val draft: VoiceReviewDraft) : VoiceReviewRead
    data class Corrupt(val message: String) : VoiceReviewRead
    data class Failed(val message: String) : VoiceReviewRead
}

interface VoiceReviewStorage {
    fun read(accountId: String, sessionId: String, turnId: String): VoiceReviewRead
    fun write(accountId: String, sessionId: String, turnId: String, draft: VoiceReviewDraft): SecureWrite
    fun clear(accountId: String, sessionId: String, turnId: String, takeId: String? = null): Boolean
}

class InMemoryVoiceReviewStorage : VoiceReviewStorage {
    private val values = mutableMapOf<String, VoiceReviewDraft>()

    private fun key(accountId: String, sessionId: String, turnId: String) =
        "$accountId/$sessionId/$turnId"

    override fun read(accountId: String, sessionId: String, turnId: String): VoiceReviewRead =
        values[key(accountId, sessionId, turnId)]
            ?.let { VoiceReviewRead.Present(it) }
            ?: VoiceReviewRead.Absent

    override fun write(
        accountId: String,
        sessionId: String,
        turnId: String,
        draft: VoiceReviewDraft,
    ): SecureWrite {
        values[key(accountId, sessionId, turnId)] = draft
        return SecureWrite.Ok
    }

    override fun clear(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String?,
    ): Boolean {
        val key = key(accountId, sessionId, turnId)
        val current = values[key]
        if (takeId != null && current != null && current.takeId != takeId) return false
        values.remove(key)
        return true
    }
}

class VoiceReviewStore(context: Context) : VoiceReviewStorage {
    private val prefs = AesGcmPrefs(
        context,
        preferencesName = "mockmate_voice_reviews",
        keyAlias = "mockmate_voice_reviews_key",
    )
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    private fun digest(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private fun key(accountId: String, sessionId: String, turnId: String): String =
        "review_${digest("$accountId/$sessionId/$turnId").take(40)}"

    override fun read(accountId: String, sessionId: String, turnId: String): VoiceReviewRead =
        when (val read = prefs.read(key(accountId, sessionId, turnId))) {
            SecureRead.Absent -> VoiceReviewRead.Absent
            is SecureRead.Failed -> VoiceReviewRead.Failed(read.message)
            is SecureRead.Corrupt -> VoiceReviewRead.Corrupt(read.message)
            is SecureRead.Present -> try {
                VoiceReviewRead.Present(json.decodeFromString<VoiceReviewDraft>(read.value))
            } catch (e: Exception) {
                VoiceReviewRead.Corrupt("本机核对草稿无法解析，已保留原始数据")
            }
        }

    override fun write(
        accountId: String,
        sessionId: String,
        turnId: String,
        draft: VoiceReviewDraft,
    ): SecureWrite = prefs.write(key(accountId, sessionId, turnId), json.encodeToString(draft))

    override fun clear(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String?,
    ): Boolean {
        if (takeId != null) {
            val current = read(accountId, sessionId, turnId)
            if (current is VoiceReviewRead.Present && current.draft.takeId != takeId) return false
        }
        return prefs.remove(key(accountId, sessionId, turnId))
    }
}
