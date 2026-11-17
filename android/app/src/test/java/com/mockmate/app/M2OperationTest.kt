package com.mockmate.app

import com.mockmate.app.core.operation.AccountOperations
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.InterviewBasicReportPayload
import com.mockmate.app.core.operation.InterviewContinuePayload
import com.mockmate.app.core.operation.InterviewFinishPayload
import com.mockmate.app.core.operation.InterviewPausePayload
import com.mockmate.app.core.operation.InterviewResumePayload
import com.mockmate.app.core.operation.InterviewSkipPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationPayload
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.operation.PrepareResult
import com.mockmate.app.core.operation.StartInterviewPayload
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class M2OperationTest {
    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true }

    @Test
    fun answerPayloadReusesKeyOnlyForIdenticalBody() = runBlocking {
        val store = InMemoryOperationStore()
        var counter = 0
        val coordinator = OperationCoordinator(store, keyFactory = { "key-${++counter}" })
        val payload = InterviewAnswerPayload("s1", 3, "t1", "原回答")
        val first = coordinator.prepare(
            accountId = "acc",
            type = "interviewAnswer",
            targetId = "s1",
            revision = 3,
            payload = payload,
        ) as PrepareResult.Prepared
        val same = coordinator.prepare(
            accountId = "acc",
            type = "interviewAnswer",
            targetId = "s1",
            revision = 3,
            payload = payload,
        ) as PrepareResult.Prepared
        val changed = coordinator.prepare(
            accountId = "acc",
            type = "interviewAnswer",
            targetId = "s1",
            revision = 3,
            payload = payload.copy(text = "不同回答"),
        ) as PrepareResult.Prepared
        assertEquals(first.operation.operationKey, same.operation.operationKey)
        assertNotEquals(first.operation.operationKey, changed.operation.operationKey)
        assertEquals(2, store.snapshot("acc").pending.size)
    }

    @Test
    fun allM2PayloadsRoundTripWithM1Payloads() {
        val payloads: List<OperationPayload> = listOf(
            CreateTextMaterialPayload("resume", "标题", "正文"),
            StartInterviewPayload(2),
            InterviewAnswerPayload("s1", 3, "t1", "回答"),
            InterviewSkipPayload("s1", 3, "t1"),
            InterviewPausePayload("s1", 4),
            InterviewResumePayload("s1", 5),
            InterviewContinuePayload("s1", 6, "job1"),
            InterviewFinishPayload("s1", 7),
            InterviewBasicReportPayload("s1", 8, "job2"),
        )
        val operations = AccountOperations(
            pending = payloads.mapIndexed { index, payload ->
                PendingOperation(
                    operationKey = "key-$index",
                    type = "type-$index",
                    targetId = "target",
                    revision = 1,
                    bodyHash = "hash-$index",
                    payload = payload,
                )
            },
        )
        val encoded = json.encodeToString(AccountOperations.serializer(), operations)
        val decoded = json.decodeFromString(AccountOperations.serializer(), encoded)
        assertEquals(operations.pending.size, decoded.pending.size)
        assertTrue(decoded.pending[1].payload is StartInterviewPayload)
        assertTrue(decoded.pending[2].payload is InterviewAnswerPayload)
        assertTrue(decoded.pending[8].payload is InterviewBasicReportPayload)
        assertEquals(
            "回答",
            (decoded.pending[2].payload as InterviewAnswerPayload).text,
        )
    }

    @Test
    fun oldPendingRecordWithoutPayloadStillDecodes() {
        val raw = """
            {
              "pending":[{"operationKey":"k1","type":"match","targetId":"prep1",
                "revision":1,"bodyHash":"h","bodyJson":"1"}],
              "jobs":[]
            }
        """.trimIndent()
        val decoded = json.decodeFromString(AccountOperations.serializer(), raw)
        assertEquals(1, decoded.pending.size)
        assertEquals(null, decoded.pending[0].payload)
    }
}
