package com.mockmate.app.core.audio

import android.content.Context
import androidx.security.crypto.EncryptedFile
import androidx.security.crypto.MasterKey
import java.io.File
import java.io.InputStream
import java.io.OutputStream

/**
 * 录音文件的加密封装。使用 AndroidX EncryptedFile（Tink 流式 AEAD + Android Keystore），
 * 不自定义密文格式；密钥只保存在 Keystore，不进入 Preferences、日志或上传请求。
 */
interface VoiceFileCipher {
    fun openEncryptedOutput(target: File): OutputStream
    fun openDecryptedInput(source: File): InputStream
}

class AndroidVoiceFileCipher(context: Context) : VoiceFileCipher {
    private val appContext = context.applicationContext
    private val masterKey: MasterKey by lazy {
        MasterKey.Builder(appContext)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
    }

    private fun encrypted(file: File): EncryptedFile =
        EncryptedFile.Builder(
            appContext,
            file,
            masterKey,
            EncryptedFile.FileEncryptionScheme.AES256_GCM_HKDF_4KB,
        ).build()

    override fun openEncryptedOutput(target: File): OutputStream =
        encrypted(target).openFileOutput()

    override fun openDecryptedInput(source: File): InputStream =
        encrypted(source).openFileInput()
}
