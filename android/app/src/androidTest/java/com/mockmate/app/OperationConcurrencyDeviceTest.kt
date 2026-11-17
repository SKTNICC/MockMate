package com.mockmate.app

import android.content.Context
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.core.operation.SecureOperationStore
import java.util.UUID
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

/**
 * E02 设备补证：真实 Android Keystore 存储上，两个 SecureOperationStore / Coordinator 实例
 * 并发写同一隔离测试账号，全部原 key 必须保留，新实例可读回。不触碰用户账号。
 */
@RunWith(AndroidJUnit4::class)
class OperationConcurrencyDeviceTest {

    private val context: Context
        get() = InstrumentationRegistry.getInstrumentation().targetContext

    @Test
    fun concurrentWritesAcrossTwoSecureStoresKeepEveryKey() = runBlocking {
        val account = "op-concurrency-probe-${UUID.randomUUID()}"
        val storeA = SecureOperationStore(context)
        val storeB = SecureOperationStore(context)
        val sequence = AtomicInteger()
        val coordinatorA = OperationCoordinator(storeA, keyFactory = { "device-a-${sequence.incrementAndGet()}" })
        val coordinatorB = OperationCoordinator(storeB, keyFactory = { "device-b-${sequence.incrementAndGet()}" })

        val results = coroutineScope {
            (1..16).map { index ->
                async(Dispatchers.Default) {
                    val coordinator = if (index % 2 == 0) coordinatorA else coordinatorB
                    coordinator.prepare(
                        account,
                        "createTextMaterial",
                        null,
                        null,
                        CreateTextMaterialPayload(
                            kind = "resume",
                            title = "并发测试 $index",
                            text = "合成正文 $index",
                        ),
                    )
                }
            }.awaitAll()
        }

        assertTrue("所有并发 prepare 都必须成功落盘", results.all { it is PrepareResult.Prepared })
        val reopened = SecureOperationStore(context)
        val loaded = reopened.load(account)
        assertTrue(loaded is OperationLoad.Loaded)
        val operations = (loaded as OperationLoad.Loaded).operations
        assertEquals("并发写不能丢失任何原 key", 16, operations.pending.size)
        assertEquals("每个操作都必须有独立 key", 16, operations.pending.map { it.operationKey }.toSet().size)

        // 清理本测试账号，不触碰其他账号记录。
        assertEquals(OperationSave.Ok, reopened.save(account, AccountOperations()))
    }
}
