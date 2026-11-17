package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.ActiveJob
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.operation.SecureOperationStore
import com.mockmate.app.core.session.AesGcmPrefs
import com.mockmate.app.core.session.SecureRead
import com.mockmate.app.core.session.SecureWrite
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * S03 真机存储边界：真实 Android Keystore 加解密、损坏保留与跨实例读取。
 * 只使用合成记录，不写真实材料；测试结束清理自己创建的键。
 */
@RunWith(AndroidJUnit4::class)
class StorageDeviceTest {

    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun keystoreRoundTripAndCorruptionKeepsRawData() {
        val prefs = AesGcmPrefs(context, "mockmate_storage_probe", "mockmate_storage_probe_key")
        val value = "中文测试\u001f分隔符\n" + "长文本".repeat(200)
        assertEquals(SecureWrite.Ok, prefs.write("record", value))
        val read = prefs.read("record")
        assertTrue(read is SecureRead.Present)
        assertEquals(value, (read as SecureRead.Present).value)

        // 模拟密文损坏：保留原始密文，不当作“首次无记录”。
        val raw = context.getSharedPreferences("mockmate_storage_probe", Context.MODE_PRIVATE)
        assertTrue(raw.edit().putString("record", "not-a-valid-ciphertext").commit())
        val corrupt = prefs.read("record")
        assertTrue(corrupt is SecureRead.Corrupt)
        assertEquals("not-a-valid-ciphertext", raw.getString("record", null))

        // 重新写入可用记录后恢复；最后清理测试键。
        assertEquals(SecureWrite.Ok, prefs.write("record", "恢复后的记录"))
        assertTrue(prefs.read("record") is SecureRead.Present)
        prefs.remove("record")
    }

    @Test
    fun secureOperationStoreSurvivesNewInstanceAndKeepsTypedPayload() = runBlocking {
        val account = "storage-probe-account"
        val store = SecureOperationStore(context)
        val operations = AccountOperations(
            pending = listOf(
                PendingOperation(
                    operationKey = "storage-probe-key",
                    type = "deletePreparation",
                    targetId = "prep-1",
                    revision = 0,
                    bodyHash = "synthetic-hash",
                    payload = com.mockmate.app.core.operation.DeletePreparationPayload,
                )
            ),
            jobs = listOf(
                ActiveJob(
                    jobId = "job-1",
                    kind = "plan",
                    targetId = "prep-1",
                    preparationId = "prep-1",
                    revision = 1,
                )
            ),
        )
        assertEquals(OperationSave.Ok, store.save(account, operations))

        // 新实例模拟进程重建，仍能读回同一条记录。
        val reopened = SecureOperationStore(context)
        val loaded = reopened.load(account)
        assertTrue(loaded is OperationLoad.Loaded)
        val value = (loaded as OperationLoad.Loaded).operations
        assertEquals("storage-probe-key", value.pending.single().operationKey)
        assertNotNull(value.pending.single().payload)
        assertEquals("job-1", value.jobs.single().jobId)

        // 清理本测试账号的记录，不触碰其他账号。
        assertEquals(OperationSave.Ok, reopened.save(account, AccountOperations()))
    }
}
