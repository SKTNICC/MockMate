package com.mockmate.app

import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MockMateApi
import com.mockmate.app.core.network.TokenProvider
import com.mockmate.app.core.operation.ActiveJob
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.DeletePreparationPayload
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.operation.PendingOperationReplayer
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.core.operation.WritePipeline
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.M1Repository
import com.mockmate.app.data.RepositoryOperationReplayExecutor
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * D01：请求记录、未知回执、存储失败与恢复重放。
 * 全部经过 WritePipeline / PendingOperationReplayer / OperationCoordinator 的实际生产路径。
 */
class OperationCoordinatorTest {

    private fun tokenProvider() = object : TokenProvider {
        override fun token(): String = "test-token"
    }

    private fun api(engine: MockEngine) =
        MockMateApi(tokenProvider(), "http://test", engine)

    private fun materialJson(id: String = "m-1") = """
        {"id":"$id","revision":1,"kind":"resume","title":"synthetic","text":"body",
         "profile":null,"state":"text_ready","template_id":null,"template_version":null,
         "latest_job_id":null,"created_at":"2026-11-16T00:00:00Z","updated_at":"2026-11-16T00:00:00Z"}
    """.trimIndent()

    @Test
    fun sameLogicalOperationReusesKeyAndChangedPayloadCreatesNewOperation() = runBlocking {
        val store = InMemoryOperationStore()
        var counter = 0
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${++counter}" })

        val first = coordinator.prepare(
            "account-a", "editMaterial", "m1", 1,
            CreateTextMaterialPayload("resume", "title", "text"),
        ) as PrepareResult.Prepared
        val replay = coordinator.prepare(
            "account-a", "editMaterial", "m1", 1,
            CreateTextMaterialPayload("resume", "title", "text"),
        ) as PrepareResult.Prepared
        val changed = coordinator.prepare(
            "account-a", "editMaterial", "m1", 1,
            CreateTextMaterialPayload("resume", "title", "changed"),
        ) as PrepareResult.Prepared

        assertEquals(first.operation.operationKey, replay.operation.operationKey)
        assertFalse(first.operation.operationKey == changed.operation.operationKey)
        assertEquals(2, (coordinator.pending("account-a") as OperationLoad.Loaded).operations.pending.size)
        assertTrue(coordinator.pending("account-b") == OperationLoad.Empty)
    }

    @Test
    fun storageWriteFailureBlocksNetworkCall() = runBlocking {
        val store = InMemoryOperationStore().apply { failWrites = true }
        val coordinator = OperationCoordinator(store, keyFactory = { "storage-key" })
        val pipeline = WritePipeline(coordinator)
        var requests = 0
        val engine = MockEngine {
            requests += 1
            respond("", HttpStatusCode.NoContent)
        }
        val api = api(engine)
        try {
            val result = pipeline.perform(
                accountId = "a",
                type = "deletePreparation",
                payload = DeletePreparationPayload,
                targetId = "p1",
                revision = 0,
            ) { key -> M1Repository(api).deletePreparation("p1", key) }
            assertTrue(result is ApiResult.Err)
            assertEquals("STORAGE_UNAVAILABLE", (result as ApiResult.Err).code)
            assertEquals(0, requests)
        } finally {
            api.close()
        }
    }

    @Test
    fun corruptStorageIsNotTreatedAsFirstUseAndDoesNotClearData() = runBlocking {
        val store = InMemoryOperationStore().apply { corrupt = true }
        val coordinator = OperationCoordinator(store, keyFactory = { "corrupt-key" })
        val pipeline = WritePipeline(coordinator)
        var requests = 0
        val engine = MockEngine {
            requests += 1
            respond("", HttpStatusCode.NoContent)
        }
        val api = api(engine)
        try {
            val result = pipeline.perform(
                accountId = "a",
                type = "deletePreparation",
                payload = DeletePreparationPayload,
                targetId = "p1",
                revision = 0,
            ) { key -> M1Repository(api).deletePreparation("p1", key) }
            assertTrue(result is ApiResult.Err)
            assertEquals("OPERATION_LOG_CORRUPT", (result as ApiResult.Err).code)
            assertEquals(0, requests)
            val replayer = PendingOperationReplayer(coordinator, RepositoryOperationReplayExecutor(M1Repository(api)))
            val report = replayer.replay("a") { true }
            assertNotNull(report.storageError)
            assertTrue(store.corrupt)
        } finally {
            api.close()
        }
    }

    @Test
    fun malformedCreatedReceiptKeepsPendingAndReplaysSameKey() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "receipt-key" })
        val pipeline = WritePipeline(coordinator)
        val keys = mutableListOf<String?>()
        var call = 0
        val engine = MockEngine { request ->
            keys.add(request.headers["Idempotency-Key"])
            call += 1
            if (call == 1) {
                respond("{", HttpStatusCode.Created, headersOf("Content-Type", "application/json"))
            } else {
                respond(materialJson(), HttpStatusCode.Created, headersOf("Content-Type", "application/json"))
            }
        }
        val api = api(engine)
        try {
            val repository = M1Repository(api)
            val first = pipeline.perform(
                accountId = "a",
                type = "createTextMaterial",
                payload = CreateTextMaterialPayload("resume", "标题", "服务端已创建但回执损坏"),
            ) { key -> repository.createTextMaterial("resume", "标题", "服务端已创建但回执损坏", operationKey = key) }
            assertTrue(first is ApiResult.Err)
            val error = first as ApiResult.Err
            assertEquals("RECEIPT_UNREADABLE", error.code)
            assertEquals(201, error.httpStatus)
            assertTrue(OperationCoordinator.isUncertain(error.code, error.httpStatus, error.requestSent))
            assertEquals(1, (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.size)

            val report = PendingOperationReplayer(coordinator, RepositoryOperationReplayExecutor(repository))
                .replay("a") { true }
            assertEquals(1, report.completed)
            assertEquals(0, (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.size)
            assertEquals(listOf("receipt-key", "receipt-key"), keys)
        } finally {
            api.close()
        }
    }

    @Test
    fun deleteReplayUsesTheOriginalKeyAndPayload() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "delete-key" })
        val pipeline = WritePipeline(coordinator)
        val keys = mutableListOf<String?>()
        var call = 0
        val engine = MockEngine { request ->
            keys.add(request.headers["Idempotency-Key"])
            call += 1
            if (call == 1) {
                respond("", HttpStatusCode.InternalServerError)
            } else {
                respond("", HttpStatusCode.NoContent)
            }
        }
        val api = api(engine)
        try {
            val repository = M1Repository(api)
            val first = pipeline.perform(
                accountId = "a",
                type = "deletePreparation",
                payload = DeletePreparationPayload,
                targetId = "p1",
                revision = 0,
            ) { key -> repository.deletePreparation("p1", key) }
            assertTrue(first is ApiResult.Err)
            assertTrue(OperationCoordinator.isUncertain((first as ApiResult.Err).code, first.httpStatus))
            assertEquals(1, (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.size)

            val report = PendingOperationReplayer(coordinator, RepositoryOperationReplayExecutor(repository))
                .replay("a") { true }
            assertEquals(1, report.completed)
            assertEquals(0, (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.size)
            assertEquals(listOf("delete-key", "delete-key"), keys)
        } finally {
            api.close()
        }
    }

    @Test
    fun typedPayloadRoundTripsChineseLongTextAndSeparatorCharacters() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "roundtrip-key" })
        val pipeline = WritePipeline(coordinator)
        val text = "中文正文\u001f包含分隔符字面字符\n" + "长文本".repeat(2000)
        var call = 0
        val engine = MockEngine {
            call += 1
            if (call == 1) {
                respond("", HttpStatusCode.InternalServerError)
            } else {
                respond(materialJson(), HttpStatusCode.Created, headersOf("Content-Type", "application/json"))
            }
        }
        val api = api(engine)
        try {
            val repository = M1Repository(api)
            val first = pipeline.perform(
                accountId = "a",
                type = "createTextMaterial",
                payload = CreateTextMaterialPayload("resume", "中文标题", text),
            ) { key -> repository.createTextMaterial("resume", "中文标题", text, operationKey = key) }
            assertTrue(first is ApiResult.Err)
            val pending = (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.single()
            assertEquals(CreateTextMaterialPayload("resume", "中文标题", text), pending.payload)
            assertEquals("roundtrip-key", pending.operationKey)

            // 模拟进程重建：同一存储由新的 coordinator 读取，仍使用原 key/payload。
            val reopened = OperationCoordinator(store, keyFactory = { "should-not-be-used" })
            val report = PendingOperationReplayer(reopened, RepositoryOperationReplayExecutor(repository))
                .replay("a") { true }
            assertEquals(1, report.completed)
            assertEquals(0, (reopened.pending("a") as OperationLoad.Loaded).operations.pending.size)
        } finally {
            api.close()
        }
    }

    @Test
    fun legacyPendingRecordRemainsReadableAndReplaysWithOriginalKey() = runBlocking {
        val store = InMemoryOperationStore()
        store.save(
            "a",
            com.mockmate.app.core.operation.AccountOperations(
                pending = listOf(
                    PendingOperation(
                        operationKey = "legacy-key",
                        type = "deletePreparation",
                        targetId = "p1",
                        revision = 0,
                        bodyHash = "legacy-hash",
                        bodyJson = "p1",
                    )
                )
            ),
        )
        val coordinator = OperationCoordinator(store, keyFactory = { "new-key" })
        val keys = mutableListOf<String?>()
        val engine = MockEngine { request ->
            keys.add(request.headers["Idempotency-Key"])
            respond("", HttpStatusCode.NoContent)
        }
        val api = api(engine)
        try {
            val report = PendingOperationReplayer(
                coordinator,
                RepositoryOperationReplayExecutor(M1Repository(api)),
            ).replay("a") { true }
            assertEquals(1, report.completed)
            assertEquals(listOf("legacy-key"), keys)
            assertEquals(0, (coordinator.pending("a") as OperationLoad.Loaded).operations.pending.size)
        } finally {
            api.close()
        }
    }

    @Test
    fun cancellationDoesNotClearPendingOperation() = runBlocking {
        val store = InMemoryOperationStore()
        val coordinator = OperationCoordinator(store, keyFactory = { "cancel-key" })
        val pipeline = WritePipeline(coordinator)
        try {
            pipeline.perform<Unit>(
                accountId = "a",
                type = "deletePreparation",
                payload = DeletePreparationPayload,
                targetId = "p1",
                revision = 0,
            ) { throw CancellationException("cancelled") }
        } catch (expected: CancellationException) {
            // 取消必须继续传播，且保留 pending。
        }
        val pending = (coordinator.pending("a") as OperationLoad.Loaded).operations.pending
        assertEquals(1, pending.size)
        assertEquals("cancel-key", pending.single().operationKey)
    }

    @Test
    fun activeJobIsAccountScopedAndCanBeCleared() = runBlocking {
        val coordinator = OperationCoordinator(InMemoryOperationStore())
        val saved = coordinator.recordActiveJob(
            "a",
            ActiveJob(jobId = "job-1", kind = "plan", targetId = "p1", preparationId = "p1", revision = 1),
        )
        assertEquals(OperationSave.Ok, saved)
        assertEquals("job-1", coordinator.activeJob("a", "plan", "p1")?.jobId)
        assertEquals(null, coordinator.activeJob("b", "plan", "p1"))
        coordinator.completeActiveJob("a", "job-1")
        assertEquals(null, coordinator.activeJob("a", "plan", "p1"))
    }

    @Test
    fun uncertaintyClassificationKeepsOnlyUnknownReceipts() {
        assertTrue(OperationCoordinator.isUncertain("NETWORK_UNAVAILABLE", null))
        assertTrue(OperationCoordinator.isUncertain("RECEIPT_UNREADABLE", 201))
        assertTrue(OperationCoordinator.isUncertain(null, 500))
        assertTrue(OperationCoordinator.isUncertain(null, 429))
        assertFalse(OperationCoordinator.isUncertain("REVISION_CONFLICT", 409))
        assertFalse(OperationCoordinator.isUncertain("UNAUTHENTICATED", 401))
        assertFalse(OperationCoordinator.isUncertain("NETWORK_UNAVAILABLE", null, requestSent = false))
    }

    @Test
    fun apiClientUsesCallerSuppliedOperationKeyInsteadOfRandomKey() = runBlocking {
        val seen = mutableListOf<String?>()
        val engine = MockEngine { request ->
            seen.add(request.headers["Idempotency-Key"])
            respond("", HttpStatusCode.NoContent)
        }
        val api = api(engine)
        try {
            api.deleteMaterial("m1", "stable-key")
            api.deleteMaterial("m1", "stable-key")
            assertEquals(listOf("stable-key", "stable-key"), seen)
        } finally {
            api.close()
        }
    }
}
