package com.mockmate.app

import com.mockmate.app.core.network.MockMateApi
import com.mockmate.app.core.network.TokenProvider
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.M1Repository
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 第二轮独立验收 S01/S02 的最小正确行为回归。
 * 完整调用链（WritePipeline/PendingOperationReplayer）在 OperationCoordinatorTest 中覆盖。
 */
class Review2RegressionTest {

    @Test
    fun deletePreparationReplayReusesTheOriginalOperationKey() = runBlocking {
        val store = InMemoryOperationStore()
        var sequence = 0
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${++sequence}" })

        val original = coordinator.prepare(
            "account-a", "deletePreparation", "prep-1", 0,
            com.mockmate.app.core.operation.DeletePreparationPayload,
        ) as PrepareResult.Prepared
        val replay = coordinator.prepare(
            "account-a", "deletePreparation", "prep-1", 0,
            com.mockmate.app.core.operation.DeletePreparationPayload,
        ) as PrepareResult.Prepared

        assertEquals(original.operation.operationKey, replay.operation.operationKey)
        assertTrue(original.operation.operationKey == replay.operation.operationKey)
    }

    @Test
    fun createdButMalformedReceiptIsTreatedAsUncertain() = runBlocking {
        val engine = MockEngine {
            respond("{", HttpStatusCode.Created, headersOf("Content-Type", "application/json"))
        }
        val api = MockMateApi(
            tokenProvider = object : TokenProvider {
                override fun token(): String = "test-token"
            },
            baseUrl = "http://test",
            engine = engine,
        )
        try {
            val response = M1Repository(api).createTextMaterial(
                kind = "resume",
                title = "synthetic",
                text = "synthetic material",
                operationKey = "receipt-key",
            )
            assertTrue(response is ApiResult.Err)
            val error = response as ApiResult.Err
            assertEquals(201, error.httpStatus)
            assertEquals("RECEIPT_UNREADABLE", error.code)
            assertTrue(OperationCoordinator.isUncertain(error.code, error.httpStatus, error.requestSent))
        } finally {
            api.close()
        }
    }
}
