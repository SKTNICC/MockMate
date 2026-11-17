package com.mockmate.app

import com.mockmate.app.core.network.MockMateApi
import com.mockmate.app.core.network.TokenProvider
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.M1Repository
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class RepositoryPollingTest {
    private fun tokenProvider() = object : TokenProvider {
        override fun token(): String = "test-token"
    }

    @Test
    fun awaitJobDoesNotSendGetWhenPageIsNotVisible() = runBlocking {
        var requests = 0
        val engine = MockEngine {
            requests += 1
            respond("", HttpStatusCode.InternalServerError)
        }
        val api = MockMateApi(tokenProvider(), "http://test", engine)
        val repository = M1Repository(api)

        val result = repository.awaitJob("job-1", shouldPoll = { false })

        assertTrue(result is ApiResult.Err)
        assertEquals("JOB_POLL_PAUSED", (result as ApiResult.Err).code)
        assertEquals(0, requests)
        api.close()
    }

    @Test
    fun awaitJobStopsAtTerminalStateAndDoesNotRePost() = runBlocking {
        var requests = 0
        val engine = MockEngine {
            requests += 1
            respond(
                content = """
                    {"id":"job-1","kind":"plan","target_id":"p1","input_revision":1,
                     "state":"succeeded","attempt":1,"retryable":false,
                     "result_ref":{"entity_type":"plan","id":"plan-1"},"error":null,
                     "created_at":"2026-11-16T00:00:00Z","updated_at":"2026-11-16T00:00:00Z"}
                """.trimIndent(),
                status = HttpStatusCode.OK,
                headers = headersOf(HttpHeaders.ContentType, "application/json"),
            )
        }
        val api = MockMateApi(tokenProvider(), "http://test", engine)
        val repository = M1Repository(api)

        val result = repository.awaitJob("job-1", shouldPoll = { true })

        assertTrue(result is ApiResult.Ok)
        assertEquals("succeeded", (result as ApiResult.Ok).value.state)
        assertEquals(1, requests)
        api.close()
    }
}
