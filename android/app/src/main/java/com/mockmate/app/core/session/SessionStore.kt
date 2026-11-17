package com.mockmate.app.core.session

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class StoredSession(
    val accountId: String,
    val username: String,
    val token: String,
)

/**
 * token 只在 Android Keystore 支持的 AES/GCM 加密后落盘；不写日志。
 * 退出登录立即清除；换账号不会读到上一个账号的 token。
 */
class SecureSessionStore(context: Context) {
    private val prefs = context.getSharedPreferences("mockmate_session", Context.MODE_PRIVATE)

    private fun keyStore(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun secretKey(): SecretKey {
        val store = keyStore()
        (store.getEntry(KEY_ALIAS, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                KEY_ALIAS,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    fun save(session: StoredSession): Boolean = runCatching {
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, secretKey())
        val payload = buildString {
            append(session.accountId).append('\u001f')
            append(session.username).append('\u001f')
            append(session.token)
        }.toByteArray(Charsets.UTF_8)
        val cipherText = cipher.doFinal(payload)
        val blob = Base64.encodeToString(cipher.iv + cipherText, Base64.NO_WRAP)
        prefs.edit().putString(KEY_SESSION, blob).apply()
        true
    }.getOrDefault(false)

    fun load(): StoredSession? = runCatching {
        val blob = prefs.getString(KEY_SESSION, null) ?: return null
        val raw = Base64.decode(blob, Base64.NO_WRAP)
        if (raw.size <= IV_BYTES) return null
        val iv = raw.copyOfRange(0, IV_BYTES)
        val cipherText = raw.copyOfRange(IV_BYTES, raw.size)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, iv))
        val payload = String(cipher.doFinal(cipherText), Charsets.UTF_8).split('\u001f')
        if (payload.size != 3) return null
        StoredSession(payload[0], payload[1], payload[2])
    }.getOrNull()

    fun clear() {
        prefs.edit().remove(KEY_SESSION).apply()
    }

    companion object {
        private const val KEY_ALIAS = "mockmate_session_key"
        private const val KEY_SESSION = "session_blob"
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
    }
}

/** 进程内当前会话；token 只供网络层读取。 */
class SessionManager(private val store: SecureSessionStore) : com.mockmate.app.core.network.TokenProvider {
    @Volatile
    var current: StoredSession? = null
        private set

    /** 账号切换代数：用于丢弃迟到回包。 */
    @Volatile
    var generation: Int = 0
        private set

    override fun token(): String? = current?.token

    fun restore(): StoredSession? {
        current = store.load()
        return current
    }

    fun signIn(accountId: String, username: String, token: String) {
        current = StoredSession(accountId, username, token)
        store.save(current!!)
        generation += 1
    }

    fun signOut() {
        current = null
        store.clear()
        generation += 1
    }
}
