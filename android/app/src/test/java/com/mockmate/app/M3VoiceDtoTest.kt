package com.mockmate.app

import com.mockmate.app.core.network.InterviewAnswerDto
import com.mockmate.app.core.network.InterviewAnswerRequest
import com.mockmate.app.core.network.SpeechAssetDto
import com.mockmate.app.core.network.StartInterviewRequest
import com.mockmate.app.core.network.VoiceConfigDto
import com.mockmate.app.core.network.VoiceDraftDto
import com.mockmate.app.core.network.VoiceTranscriptDto
import com.mockmate.app.core.network.VoiceUploadReceiptDto
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.OperationPayload
import com.mockmate.app.core.operation.StartInterviewPayload
import kotlinx.serialization.decodeFromString
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class M3VoiceDtoTest {
    private val json = Json {
        ignoreUnknownKeys = true
        explicitNulls = false
        encodeDefaults = true
    }

    @Test
    fun voiceDraftAndTranscriptDecode() {
        val draft = json.decodeFromString<VoiceDraftDto>(
            """
            {
              "session_id":"00000000-0000-4000-8000-000000000950",
              "turn_id":"00000000-0000-4000-8000-000000000954",
              "voice_revision":1,
              "active_audio":{
                "id":"00000000-0000-4000-8000-000000000960",
                "session_id":"00000000-0000-4000-8000-000000000950",
                "turn_id":"00000000-0000-4000-8000-000000000954",
                "take_id":"00000000-0000-4000-8000-000000000964",
                "sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "byte_length":64000,"duration_ms":2000,
                "format":"pcm_s16le/16000/mono","status":"stored",
                "expires_at":"2026-11-22T00:00:00Z","created_at":"2026-11-16T00:00:00Z"
              },
              "transcript":{
                "audio_asset_id":"00000000-0000-4000-8000-000000000960",
                "revision":1,"status":"ready","text":"我先检查相同键是否存在。",
                "provider":"deepseek","request_id":"req-1","error_code":null,
                "updated_at":"2026-11-16T00:00:00Z"
              },
              "asr_job":{"id":"00000000-0000-4000-8000-000000000962","state":"succeeded",
                "attempt":1,"retryable":false,"error_code":null}
            }
            """.trimIndent(),
        )
        assertEquals(1, draft.voice_revision)
        assertEquals("ready", draft.transcript?.status)
        assertEquals("stored", draft.active_audio?.status)
    }

    @Test
    fun answerAndReceiptSourcesRoundTrip() {
        val request = json.decodeFromString<InterviewAnswerRequest>(
            """
            {"revision":3,"turn_id":"t1","text":"核对文本",
             "input_source":"asr_corrected",
             "audio_asset_id":"a1","transcript_revision":1}
            """.trimIndent(),
        )
        assertEquals("asr_corrected", request.input_source)
        assertEquals(1, request.transcript_revision)

        val oldRequest = json.decodeFromString<InterviewAnswerRequest>(
            """{"revision":3,"turn_id":"t1","text":"文字"}"""
        )
        assertEquals("text", oldRequest.input_source)
        assertNull(oldRequest.audio_asset_id)

        val answer = json.decodeFromString<InterviewAnswerDto>(
            """{"answer_id":"x","text":"原话","submitted_at":"2026-11-16T00:00:00Z",
                "input_source":"asr","audio_asset_id":"a1","transcript_revision":1}"""
        )
        assertEquals("asr", answer.input_source)
        assertEquals(1, answer.transcript_revision)
    }

    @Test
    fun startRequestAndVoiceConfigHaveSafeDefaults() {
        val old = json.decodeFromString<StartInterviewRequest>(
            """{"plan_revision":2,"external_processing_consent":true}"""
        )
        assertEquals("text", old.mode)
        assertTrue(!old.audio_processing_consent)

        val voice = json.decodeFromString<StartInterviewRequest>(
            """{"plan_revision":2,"external_processing_consent":true,
                "mode":"voice","audio_processing_consent":true}"""
        )
        assertEquals("voice", voice.mode)

        val config = json.decodeFromString<VoiceConfigDto>(
            """{"available":true,"max_answer_seconds":120,
                "format":"pcm_s16le/16000/mono","tts_voice_id":"stella",
                "speech_provider":"deepseek"}"""
        )
        assertEquals(120, config.max_answer_seconds)
    }

    @Test
    fun uploadAndSpeechAssetsDecode() {
        val receipt = json.decodeFromString<VoiceUploadReceiptDto>(
            """
            {"session_id":"s","turn_id":"t","voice_revision":2,
             "audio":{"id":"a","session_id":"s","turn_id":"t","take_id":"k",
             "sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
             "byte_length":32000,"duration_ms":1000,"format":"pcm_s16le/16000/mono",
             "status":"stored","expires_at":"2026-11-22T00:00:00Z",
             "created_at":"2026-11-16T00:00:00Z"}}
            """.trimIndent(),
        )
        assertEquals(2, receipt.voice_revision)
        val speech = json.decodeFromString<SpeechAssetDto>(
            """
            {"id":"sp","session_id":"s","segment":"opening","turn_id":null,
             "status":"ready","voice_id":"stella","provider":"test",
             "format":"pcm_s16le/24000/mono","duration_ms":200,
             "sha256":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
             "job_id":"j","error_code":null,"synthetic":true,
             "created_at":"2026-11-16T00:00:00Z","updated_at":"2026-11-16T00:00:00Z"}
            """.trimIndent(),
        )
        assertTrue(speech.synthetic)
        assertEquals("opening", speech.segment)
    }

    @Test
    fun oldPendingOperationsDecodeWithSourceDefaults() {
        val oldStart = json.decodeFromString<OperationPayload>(
            """{"type":"startInterview","planRevision":2}"""
        ) as StartInterviewPayload
        assertEquals("text", oldStart.mode)
        assertTrue(!oldStart.audioProcessingConsent)

        val oldAnswer = json.decodeFromString<OperationPayload>(
            """{"type":"interviewAnswer","sessionId":"s","revision":1,
                "turnId":"t","text":"旧正文"}"""
        ) as InterviewAnswerPayload
        assertEquals("text", oldAnswer.inputSource)
        assertNull(oldAnswer.audioAssetId)
        assertNull(oldAnswer.transcriptRevision)

        val encoded = json.encodeToString<OperationPayload>(
            InterviewAnswerPayload("s", 1, "t", "语音", "asr", "a", 1)
        )
        val decoded = json.decodeFromString<OperationPayload>(encoded) as InterviewAnswerPayload
        assertEquals("asr", decoded.inputSource)
        assertEquals(1, decoded.transcriptRevision)
    }
}
