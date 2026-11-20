package com.mockmate.app.core.audio

import android.content.Context
import com.mockmate.app.core.session.AesGcmPrefs
import com.mockmate.app.core.session.SecureRead
import com.mockmate.app.core.session.SecureWrite
import java.io.File
import java.security.MessageDigest
import kotlinx.serialization.Serializable
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json

@Serializable
data class VoiceTakeMeta(
    val takeId: String,
    val operationKey: String,
    val sha256: String,
    val byteLength: Int,
    val durationMs: Int,
    val interrupted: Boolean,
    val reachedLimit: Boolean,
    val createdAt: Long = System.currentTimeMillis(),
    /** 首次上传前冻结的请求上下文；冷启动重放必须原 key、原 body。 */
    val format: String = "pcm_s16le/16000/mono",
    val sessionRevision: Int? = null,
    val expectedVoiceRevision: Int? = null,
    /** true 表示文件由 EncryptedFile 封装；旧版本遗留的 false 明文草稿按明确识别策略保留。 */
    val encrypted: Boolean = false,
    val uploadedAssetId: String? = null,
    val uploadedVoiceRevision: Int? = null,
    val uploaded: Boolean = false,
)

sealed interface VoiceTakeRead {
    data object Absent : VoiceTakeRead
    data class Present(val meta: VoiceTakeMeta, val file: File) : VoiceTakeRead
    data class MissingFile(val meta: VoiceTakeMeta) : VoiceTakeRead
    data class Corrupt(val meta: VoiceTakeMeta?, val message: String) : VoiceTakeRead
    data class Failed(val message: String) : VoiceTakeRead
    /** 录制/写入中断留下的 .part 片段：可识别，但不能冒充完整已封存 take。 */
    data class UnsealedPart(val part: File) : VoiceTakeRead
}

interface VoiceTakeStorage {
    fun read(accountId: String, sessionId: String, turnId: String): VoiceTakeRead
    fun write(accountId: String, sessionId: String, turnId: String, meta: VoiceTakeMeta): SecureWrite
    fun clear(accountId: String, sessionId: String, turnId: String, takeId: String? = null): Boolean
    fun file(accountId: String, sessionId: String, turnId: String, takeId: String): File
    fun partFile(accountId: String, sessionId: String, turnId: String, takeId: String): File
    fun findUnsealedPart(accountId: String, sessionId: String, turnId: String): File?
    /** 上传前读取明文 PCM；加密文件在此处受控流式解密，不把密文当成录音重发。 */
    fun readAudio(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
    ): ByteArray
}

class InMemoryVoiceTakeStorage(private val root: File) : VoiceTakeStorage {
    private val values = mutableMapOf<String, VoiceTakeMeta>()
    private fun key(accountId: String, sessionId: String, turnId: String) =
        "$accountId/$sessionId/$turnId"

    override fun read(accountId: String, sessionId: String, turnId: String): VoiceTakeRead {
        val meta = values[key(accountId, sessionId, turnId)] ?: run {
            val part = findUnsealedPart(accountId, sessionId, turnId)
            return if (part != null) VoiceTakeRead.UnsealedPart(part) else VoiceTakeRead.Absent
        }
        val file = file(accountId, sessionId, turnId, meta.takeId)
        if (!file.exists()) {
            val part = findUnsealedPart(accountId, sessionId, turnId)
            return if (part != null) VoiceTakeRead.UnsealedPart(part)
            else VoiceTakeRead.MissingFile(meta)
        }
        if (file.length() != meta.byteLength.toLong()) {
            return VoiceTakeRead.Corrupt(meta, "本地录音长度与记录不一致")
        }
        return VoiceTakeRead.Present(meta, file)
    }

    override fun write(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
    ): SecureWrite {
        values[key(accountId, sessionId, turnId)] = meta
        return SecureWrite.Ok
    }

    override fun clear(accountId: String, sessionId: String, turnId: String, takeId: String?): Boolean {
        val current = values[key(accountId, sessionId, turnId)]
        if (takeId != null && current != null && current.takeId != takeId) return false
        if (current != null) {
            values.remove(key(accountId, sessionId, turnId))
            file(accountId, sessionId, turnId, current.takeId).delete()
            partFile(accountId, sessionId, turnId, current.takeId).delete()
        }
        // 只清理指定 take 时不能扫全目录，否则会删掉刚开始的新录音 `.part`。
        if (takeId == null) findUnsealedPart(accountId, sessionId, turnId)?.delete()
        return true
    }

    override fun file(accountId: String, sessionId: String, turnId: String, takeId: String): File =
        File(File(File(File(root, accountId), sessionId), turnId), "$takeId.pcm")

    override fun partFile(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String,
    ): File = File(
        File(file(accountId, sessionId, turnId, takeId).parentFile, ".part-$takeId"),
        "$takeId.pcm",
    )

    override fun findUnsealedPart(accountId: String, sessionId: String, turnId: String): File? {
        val dir = file(accountId, sessionId, turnId, "unused").parentFile ?: return null
        val part = dir.listFiles()
            ?.filter { it.isDirectory && it.name.startsWith(".part-") }
            ?.flatMap { folder -> folder.listFiles()?.filter { it.isFile && it.name.endsWith(".pcm") } ?: emptyList() }
            ?.firstOrNull()
        if (part != null) return part
        // 没有元数据的旧版本 .pcm 孤儿也不能冒充完整已确认 take。
        return dir.listFiles()?.firstOrNull { it.isFile && it.name.endsWith(".pcm") }
    }

    override fun readAudio(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
    ): ByteArray = file(accountId, sessionId, turnId, meta.takeId).readBytes()
}

/**
 * 私有音频草稿：元数据经 Keystore 加密，PCM 文件使用 EncryptedFile 流式加密。
 * 旧版本明文 PCM 通过 meta.encrypted=false 明确识别并保持可读；新录音一律加密。
 */
class VoiceTakeStore(
    context: Context,
    private val root: File = context.filesDir,
    private val cipher: VoiceFileCipher = AndroidVoiceFileCipher(context),
) : VoiceTakeStorage {
    private val prefs = AesGcmPrefs(
        context,
        preferencesName = "mockmate_voice_takes",
        keyAlias = "mockmate_voice_takes_key",
    )
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    private fun digest(value: String): String =
        MessageDigest.getInstance("SHA-256")
            .digest(value.toByteArray(Charsets.UTF_8))
            .joinToString("") { "%02x".format(it) }

    private fun key(accountId: String, sessionId: String, turnId: String): String =
        "take_${digest("$accountId/$sessionId/$turnId").take(40)}"

    private fun turnDir(accountId: String, sessionId: String, turnId: String): File {
        val voiceRoot = File(root, "voice")
        val accountDir = File(voiceRoot, digest(accountId).take(32))
        val sessionDir = File(accountDir, digest(sessionId).take(32))
        return File(sessionDir, digest(turnId).take(32))
    }

    override fun file(accountId: String, sessionId: String, turnId: String, takeId: String): File =
        File(turnDir(accountId, sessionId, turnId), "$takeId.pcm")

    override fun partFile(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String,
    ): File = File(
        File(turnDir(accountId, sessionId, turnId), ".part-$takeId"),
        "$takeId.pcm",
    )

    override fun findUnsealedPart(accountId: String, sessionId: String, turnId: String): File? {
        val dir = turnDir(accountId, sessionId, turnId)
        val part = dir.listFiles()
            ?.filter { it.isDirectory && it.name.startsWith(".part-") }
            ?.flatMap { folder -> folder.listFiles()?.filter { it.isFile && it.name.endsWith(".pcm") } ?: emptyList() }
            ?.firstOrNull()
        if (part != null) return part
        return dir.listFiles()?.firstOrNull { it.isFile && it.name.endsWith(".pcm") }
    }

    override fun read(accountId: String, sessionId: String, turnId: String): VoiceTakeRead {
        val stored = when (val read = prefs.read(key(accountId, sessionId, turnId))) {
            SecureRead.Absent -> {
                val part = findUnsealedPart(accountId, sessionId, turnId)
                return if (part != null) VoiceTakeRead.UnsealedPart(part) else VoiceTakeRead.Absent
            }
            is SecureRead.Failed -> return VoiceTakeRead.Failed(read.message)
            is SecureRead.Corrupt -> return VoiceTakeRead.Corrupt(null, read.message)
            is SecureRead.Present -> read.value
        }
        val meta = try {
            json.decodeFromString<VoiceTakeMeta>(stored)
        } catch (e: Exception) {
            return VoiceTakeRead.Corrupt(null, "本地录音记录无法解析")
        }
        val file = file(accountId, sessionId, turnId, meta.takeId)
        if (!file.exists()) {
            val part = findUnsealedPart(accountId, sessionId, turnId)
            return if (part != null) VoiceTakeRead.UnsealedPart(part)
            else VoiceTakeRead.MissingFile(meta)
        }
        val integrity = try {
            inspect(meta, file)
        } catch (e: Exception) {
            return VoiceTakeRead.Corrupt(
                meta,
                "本地录音无法解密或已损坏（${e.javaClass.simpleName}），已保留原始数据",
            )
        }
        if (!integrity.ok) return VoiceTakeRead.Corrupt(meta, integrity.message)
        return VoiceTakeRead.Present(meta, file)
    }

    private data class Integrity(val ok: Boolean, val message: String, val bytes: Long = 0)

    private fun inspect(meta: VoiceTakeMeta, file: File): Integrity {
        if (!meta.encrypted) {
            if (file.length() != meta.byteLength.toLong()) {
                return Integrity(false, "本地录音长度与记录不一致，不能按完整录音重发")
            }
            val actual = MessageDigest.getInstance("SHA-256").digest(file.readBytes())
                .joinToString("") { "%02x".format(it) }
            return if (actual == meta.sha256) Integrity(true, "")
            else Integrity(false, "本地录音摘要不一致，不能按原文件重发")
        }
        val digest = MessageDigest.getInstance("SHA-256")
        var count = 0L
        cipher.openDecryptedInput(file).use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val read = input.read(buffer)
                if (read < 0) break
                digest.update(buffer, 0, read)
                count += read
            }
        }
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        if (count != meta.byteLength.toLong()) {
            return Integrity(false, "本地加密录音解密后长度与记录不一致", count)
        }
        return if (actual == meta.sha256) Integrity(true, "", count)
        else Integrity(false, "本地加密录音解密后摘要不一致", count)
    }

    override fun write(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
    ): SecureWrite = prefs.write(key(accountId, sessionId, turnId), json.encodeToString(meta))

    override fun readAudio(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
    ): ByteArray {
        val file = file(accountId, sessionId, turnId, meta.takeId)
        if (!meta.encrypted) return file.readBytes()
        return cipher.openDecryptedInput(file).use { it.readBytes() }
    }

    override fun clear(accountId: String, sessionId: String, turnId: String, takeId: String?): Boolean {
        val existing = read(accountId, sessionId, turnId)
        val meta = when (existing) {
            is VoiceTakeRead.Present -> existing.meta
            is VoiceTakeRead.MissingFile -> existing.meta
            is VoiceTakeRead.Corrupt -> existing.meta
            else -> null
        }
        if (takeId != null && meta != null && meta.takeId != takeId) return false
        if (meta != null) {
            file(accountId, sessionId, turnId, meta.takeId).delete()
            val part = partFile(accountId, sessionId, turnId, meta.takeId)
            part.delete()
            part.parentFile?.delete()
        }
        // 只清理指定 take 时不能扫全目录，否则会删掉刚开始的新录音 `.part`。
        if (takeId == null) {
            findUnsealedPart(accountId, sessionId, turnId)?.let { orphan ->
                orphan.delete()
                orphan.parentFile?.takeIf { it.name.startsWith(".part-") }?.delete()
            }
        }
        return prefs.remove(key(accountId, sessionId, turnId))
    }
}
