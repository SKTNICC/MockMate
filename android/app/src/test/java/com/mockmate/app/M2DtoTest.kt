package com.mockmate.app

import com.mockmate.app.core.network.InterviewAnswerRequest
import com.mockmate.app.core.network.InterviewCoverageDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewReceiptDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.InterviewReportQuestionDto
import com.mockmate.app.core.network.InterviewStartReceiptDto
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class M2DtoTest {
    private val json = Json { ignoreUnknownKeys = true; explicitNulls = false }

    @Test
    fun detailDecodesExposedTurnsAndJobs() {
        val raw = """
            {
              "session_id": "s1", "plan_id": "p1", "preparation_id": "prep1",
              "role_id": "java-backend", "level": "intern", "mode": "text",
              "status": "active", "phase": "awaiting_answer", "revision": 4,
              "started_at": "2026-11-17T00:00:00Z", "ended_at": null, "end_reason": null,
              "plan_summary": {
                "plan_id": "p1", "main_question_count": 6,
                "source_counts": {"bank": 3, "generated": 3},
                "material_versions": [
                  {"kind":"resume","snapshot_id":"r1","material_id":"m1","title":"简历","source_revision":1},
                  {"kind":"jd","snapshot_id":"j1","material_id":"m2","title":"JD","source_revision":1}
                ],
                "sample_only": true
              },
              "opening_text": "开场", "closing_text": null,
              "current_main_position": 1, "answered_main_count": 1, "skipped_main_count": 0,
              "current_turn": {
                "turn_id":"t2","question_id":"q1","position":1,"kind":"follow_up",
                "prompt":"追问内容","status":"pending","answer":null
              },
              "allowed_actions": ["pause","finish","answer","skip"],
              "transcript": [
                {"turn_id":"t1","question_id":"q1","position":1,"kind":"main","prompt":"主问题",
                 "status":"answered","answer":{"answer_id":"a1","text":"回答","submitted_at":"2026-11-17T00:00:00Z"}},
                {"turn_id":"t2","question_id":"q1","position":1,"kind":"follow_up","prompt":"追问内容",
                 "status":"pending","answer":null}
              ],
              "follow_up_job": {"id":"job1","state":"succeeded","attempt":1,"retryable":false,"error_code":null},
              "report_job": null, "report_state": "not_requested",
              "unknown_future_field": 1
            }
        """.trimIndent()
        val detail = json.decodeFromString<InterviewDetailDto>(raw)
        assertEquals("s1", detail.session_id)
        assertEquals(4, detail.revision)
        assertEquals("follow_up", detail.current_turn?.kind)
        assertEquals(2, detail.transcript.size)
        assertEquals("回答", detail.transcript[0].answer?.text)
        assertNull(detail.report_job)
        assertEquals("not_requested", detail.report_state)
        assertEquals(true, detail.plan_summary.sample_only)
    }

    @Test
    fun receiptAndStartResponseDecodeNullableJobId() {
        val receipt = json.decodeFromString<InterviewReceiptDto>(
            """{"session_id":"s1","revision":5,"answer_id":"a1","job_id":null}"""
        )
        assertNull(receipt.job_id)
        assertEquals("a1", receipt.answer_id)
        val start = json.decodeFromString<InterviewStartReceiptDto>(
            """{"session_id":"s1","revision":1}"""
        )
        assertEquals(1, start.revision)
    }

    @Test
    fun answerRequestEncodesSnakeCaseContractFields() {
        val encoded = json.encodeToString(
            InterviewAnswerRequest(revision = 3, turn_id = "t1", text = "回答\n带换行")
        )
        val decoded = json.parseToJsonElement(encoded)
        val obj = decoded as kotlinx.serialization.json.JsonObject
        assertEquals("3", obj["revision"].toString())
        assertEquals("\"t1\"", obj["turn_id"].toString())
        assertEquals("\"回答\\n带换行\"", obj["text"].toString())
    }

    @Test
    fun reportDecodesCoverageAndDimensionVerdicts() {
        val raw = """
            {
              "report_id":"r1","session_id":"s1","role_id":"java-backend","level":"intern",
              "material_versions":[
                {"kind":"resume","snapshot_id":"r1","material_id":"m1","title":"简历","source_revision":1},
                {"kind":"jd","snapshot_id":"j1","material_id":"m2","title":"JD","source_revision":1}
              ],
              "quality":"partial","rubric_policy_version":"m2-rubric-policy-v1",
              "created_at":"2026-11-17T00:00:00Z","end_reason":"early_finish",
              "coverage":{
                "planned":2,"presented":1,"answered":1,"skipped":0,"unanswered":0,"not_reached":1,
                "scored":0,"bank_planned":1,"bank_answered":1,"generated_planned":1,"generated_answered":0,
                "follow_up_presented":0,"follow_up_answered":0
              },
              "strengths":[],"improvements":[],
              "questions":[{
                "question_id":"q1","position":1,"source":"bank","outcome":"answered",
                "prompt":"题目","bank_version":1,"turns":[],
                "assessment_trust":"reviewed","rubric_version":"m1-explanation@1",
                "question_score":null,
                "dimensions":[{
                  "dimension_id":"accuracy","name":"准确性","weight":0.4,
                  "verdict":"uncertain","score":null,"evidence":[],
                  "explanation":"证据不足","improvement":"补充原话"
                }],
                "follow_up_outcome":"cancelled"
              }]
            }
        """.trimIndent()
        val report = json.decodeFromString<InterviewReportDto>(raw)
        assertEquals("partial", report.quality)
        assertEquals(1, report.coverage.not_reached)
        assertNull(report.questions[0].question_score)
        assertEquals("uncertain", report.questions[0].dimensions[0].verdict)
        assertEquals("cancelled", report.questions[0].follow_up_outcome)
    }

    @Test
    fun coverageDtoHasAllServerCounts() {
        val coverage = InterviewCoverageDto(
            planned = 6, presented = 6, answered = 5, skipped = 1, unanswered = 0,
            not_reached = 0, scored = 5, bank_planned = 3, bank_answered = 3,
            generated_planned = 3, generated_answered = 2,
            follow_up_presented = 2, follow_up_answered = 1,
        )
        val decoded = json.decodeFromString<InterviewCoverageDto>(json.encodeToString(coverage))
        assertEquals(6, decoded.planned)
        assertEquals(2, decoded.generated_answered)
    }
}
