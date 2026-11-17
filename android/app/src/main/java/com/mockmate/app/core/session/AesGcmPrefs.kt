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

/** 读取结果明确区分不存在、可用、损坏与失败；损坏时保留原密文，不清空用户数据。 */
sealed interface SecureRead {
    data object Absent : SecureRead
    data class Present(val value: String) : SecureRead
    data class Corrupt(val message: String) : SecureRead
    data class Failed(val message: String) : SecureRead
}

sealed interface SecureWrite {
    data object Ok : SecureWrite
    data class Failed(val message: String) : SecureWrite
}

/** Android Keystore AES/GCM 加密的字符串 SharedPreferences；不用于保存密码。 */
class AesGcmPrefs(
    context: Context,
    private val preferencesName: String,
    private val keyAlias: String,
) {
    private val prefs = context.getSharedPreferences(preferencesName, Context.MODE_PRIVATE)

    private fun keyStore(): KeyStore = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }

    private fun secretKey(): SecretKey {
        val store = keyStore()
        (store.getEntry(keyAlias, null) as? KeyStore.SecretKeyEntry)?.let { return it.secretKey }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore")
        generator.init(
            KeyGenParameterSpec.Builder(
                keyAlias,
                KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT,
            )
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
                .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
                .setKeySize(256)
                .build(),
        )
        return generator.generateKey()
    }

    fun read(key: String): SecureRead {
        val blob = try {
            prefs.getString(key, null)
        } catch (e: ClassCastException) {
            return SecureRead.Corrupt("本地记录类型损坏，已保留原始数据")
        } catch (e: Exception) {
            return SecureRead.Failed("读取本地安全存储失败：${e.javaClass.simpleName}")
        } ?: return SecureRead.Absent
        return try {
            val raw = Base64.decode(blob, Base64.NO_WRAP)
            if (raw.size <= IV_BYTES) return SecureRead.Corrupt("本地记录长度非法，已保留原始数据")
            val iv = raw.copyOfRange(0, IV_BYTES)
            val cipherText = raw.copyOfRange(IV_BYTES, raw.size)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, secretKey(), GCMParameterSpec(128, iv))
            SecureRead.Present(String(cipher.doFinal(cipherText), Charsets.UTF_8))
        } catch (e: Exception) {
            // 解密失败可能是密钥失效或密文损坏；保留密文，交由上层提供人工恢复路径。
            SecureRead.Corrupt("本地安全记录无法解密（${e.javaClass.simpleName}），已保留原始数据")
        }
    }

    fun write(key: String, value: String): SecureWrite {
        return try {
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.ENCRYPT_MODE, secretKey())
            val cipherText = cipher.doFinal(value.toByteArray(Charsets.UTF_8))
            val blob = Base64.encodeToString(cipher.iv + cipherText, Base64.NO_WRAP)
            if (!prefs.edit().putString(key, blob).commit()) {
                SecureWrite.Failed("本地安全存储写入未提交")
            } else {
                SecureWrite.Ok
            }
        } catch (e: Exception) {
            SecureWrite.Failed("本地安全存储写入失败：${e.javaClass.simpleName}")
        }
    }

    fun remove(key: String): Boolean = prefs.edit().remove(key).commit()

    companion object {
        private const val TRANSFORMATION = "AES/GCM/NoPadding"
        private const val IV_BYTES = 12
    }
}
