package com.mockmate.app

import com.mockmate.app.core.audio.InMemoryVoiceReviewStorage
import com.mockmate.app.core.audio.InMemoryVoiceTakeStorage
import com.mockmate.app.core.audio.VoicePhase
import com.mockmate.app.core.audio.VoiceReviewStorage
import com.mockmate.app.core.audio.VoiceServices
import com.mockmate.app.core.audio.VoiceTakeMeta
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewJobSummaryDto
import com.mockmate.app.core.network.InterviewPlanSummaryDto
import com.mockmate.app.core.network.InterviewTurnDto
import com.mockmate.app.core.network.SourceCountsDto
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.network.VoiceAudioAssetDto
import com.mockmate.app.core.network.VoiceDraftDto
import com.mockmate.app.core.network.VoiceTranscriptDto
import com.mockmate.app.core.network.VoiceUploadReceiptDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.toByteArray
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.security.MessageDigest
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * R00/R02/R03 正式回归：来自 2026-11-20 独立验收探针，直接驱动实际 AppViewModel
 * 与 Ktor MockEngine 的受控回包时序，不访问真实服务。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class M3RecoveryFixTest {
    @get:Rule
    val temporary = TemporaryFolder()

    private val json = Json { encodeDefaults = true; explicitNulls = false }
    private val headers = headersOf(HttpHeaders.ContentType, "application/json")

    private fun detail(sessionId: String, turnId: String = "turn-$sessionId") = InterviewDetailDto(
        session_id = sessionId,
        plan_id = "plan-$sessionId",
        preparation_id = "prep-$sessionId",
        role_id = "java-backend",
        level = "intern",
        mode = "voice",
        status = "active",
        phase = "awaiting_answer",
        revision = 1,
        started_at = "2026-11-20T00:00:00Z",
        plan_summary = InterviewPlanSummaryDto("plan-$sessionId", 2, SourceCountsDto(1, 1)),
        opening_text = "opening",
        current_main_position = 1,
        current_turn = InterviewTurnDto(turnId, "q-$sessionId", 1, "main", "question", "pending"),
        allowed_actions = listOf("answer", "pause", "finish"),
    )

    private fun audio(assetId: String, sessionId: String, turnId: String, takeId: String) =
        VoiceAudioAssetDto(
            id = assetId,
            session_id = sessionId,
            turn_id = turnId,
            take_id = takeId,
            sha256 = "a".repeat(64),
            byte_length = 32000,
            duration_ms = 1000,
            format = "pcm_s16le/16000/mono",
            status = "stored",
            expires_at = "2026-11-22T00:00:00Z",
            created_at = "2026-11-20T00:00:00Z",
        )

    private fun readyTranscript(assetId: String, text: String = "A-original") = VoiceTranscriptDto(
        audio_asset_id = assetId,
        revision = 1,
        status = "ready",
        text = text,
        updated_at = "2026-11-20T00:00:00Z",
    )

    private fun draft(
        sessionId: String,
        turnId: String,
        voiceRevision: Int,
        active: VoiceAudioAssetDto? = null,
        transcript: VoiceTranscriptDto? = null,
        job: InterviewJobSummaryDto? = null,
    ) = VoiceDraftDto(sessionId, turnId, voiceRevision, active, transcript, job)

    private fun TestScope.pump(ms: Long = 2_000) {
        val end = System.currentTimeMillis() + ms
        while (System.currentTimeMillis() < end) {
            testScheduler.runCurrent()
            Thread.sleep(5)
        }
        testScheduler.runCurrent()
    }

    private fun TestScope.awaitState(done: () -> Boolean) {
        testScheduler.runCurrent()
        val end = System.currentTimeMillis() + 8_000
        while (!done() && System.currentTimeMillis() < end) {
            testScheduler.runCurrent()
            Thread.sleep(5)
        }
        testScheduler.runCurrent()
        assertTrue("did not reach controlled await", done())
    }

    private fun vm(
        engine: MockEngine,
        dispatcher: TestDispatcher,
        services: VoiceServices? = null,
        io: kotlinx.coroutines.CoroutineDispatcher = dispatcher,
    ) = AppViewModel(
        sessionStore = InMemorySessionStore(),
        drafts = InMemoryDraftStorage(),
        interviewDrafts = InMemoryInterviewDraftStorage(),
        operationStore = InMemoryOperationStore(),
        ioDispatcher = io,
        bootstrapOnInit = false,
        httpEngine = engine,
        voiceServices = services,
    )

    private fun services(
        takes: InMemoryVoiceTakeStorage,
        reviews: VoiceReviewStorage = InMemoryVoiceReviewStorage(),
    ) = VoiceServices(
        recorder = null,
        player = null,
        takes = takes,
        reviews = reviews,
        cacheRoot = temporary.newFolder(),
    )

    private fun engine(handler: suspend io.ktor.client.engine.mock.MockRequestHandleScope.(io.ktor.client.request.HttpRequestData) -> io.ktor.client.request.HttpResponseData) =
        MockEngine(handler)

    @Test
    fun lateTranscriptCannotOverwriteAnotherSession() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val entered = CompletableDeferred<Unit>()
        val release = CompletableDeferred<Unit>()
        val engine = engine { req ->
            val body = when (req.url.encodedPath) {
                "/v1/auth/login" -> json.encodeToString(
                    TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                )
                "/v1/interviews/a/turns/turn-a/voice-draft" -> json.encodeToString(
                    draft("a", "turn-a", 1, audio("asset-a", "a", "turn-a", "take-a"), job = InterviewJobSummaryDto("asr-a", "running", 1))
                )
                "/v1/audio-assets/asset-a/transcript" -> {
                    entered.complete(Unit)
                    release.await()
                    json.encodeToString(readyTranscript("asset-a"))
                }
                "/v1/voice-config" -> """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}"""
                else -> """{"items":[]}"""
            }
            respond(body, HttpStatusCode.OK, headers)
        }
        val vm = vm(engine, dispatcher)
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom("a")),
                interviewDetail = detail("a"),
            )
            vm.onVoiceRoomOpened("a")
            awaitState { entered.isCompleted }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom("b")),
                interviewDetail = detail("b"),
                voicePhase = VoicePhase.IDLE,
                voiceReviewText = "B-draft",
            )
            release.complete(Unit)
            repeat(80) { testScheduler.runCurrent(); Thread.sleep(5) }
            assertEquals("B-draft", vm.state.value.voiceReviewText)
            assertEquals(VoicePhase.IDLE, vm.state.value.voicePhase)
        } finally {
            release.complete(Unit)
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reopeningSameReadyTakePreservesUnsubmittedCorrectionAndColdStart() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val reviews = InMemoryVoiceReviewStorage()
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val engine = engine { req ->
            val body = when (req.url.encodedPath) {
                "/v1/auth/login" -> json.encodeToString(
                    TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                )
                "/v1/interviews/a/turns/turn-a/voice-draft" -> json.encodeToString(
                    draft(
                        "a", "turn-a", 1,
                        audio("asset-a", "a", "turn-a", "take-a"),
                        readyTranscript("asset-a"),
                    )
                )
                "/v1/voice-config" -> """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}"""
                else -> """{"items":[]}"""
            }
            respond(body, HttpStatusCode.OK, headers)
        }
        val first = vm(engine, dispatcher, services(takes, reviews))
        try {
            first.authenticate(false, "userA", "pw")
            awaitState { first.state.value.account != null }
            first.state.value = first.state.value.copy(
                stack = listOf(Screen.InterviewRoom("a")),
                interviewDetail = detail("a"),
            )
            first.onVoiceRoomOpened("a")
            awaitState { first.state.value.voicePhase == VoicePhase.REVIEW }
            first.updateVoiceReviewText("my-unsubmitted-correction")
            first.onVoiceRoomOpened("a")
            awaitState { first.state.value.voicePhase == VoicePhase.REVIEW }
            assertEquals("my-unsubmitted-correction", first.state.value.voiceReviewText)
            first.closeForTest()

            // 新 ViewModel + 同一加密存储模拟进程重建/冷启动。
            val second = vm(engine, dispatcher, services(takes, reviews))
            try {
                second.authenticate(false, "userA", "pw")
                awaitState { second.state.value.account != null }
                second.state.value = second.state.value.copy(
                    stack = listOf(Screen.InterviewRoom("a")),
                    interviewDetail = detail("a"),
                )
                second.onVoiceRoomOpened("a")
                awaitState { second.state.value.voicePhase == VoicePhase.REVIEW }
                assertEquals("my-unsubmitted-correction", second.state.value.voiceReviewText)
            } finally {
                second.closeForTest()
            }
        } finally {
            Dispatchers.resetMain()
        }
    }

    @Test
    fun newTakeDoesNotInheritOldCorrection() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val reviews = InMemoryVoiceReviewStorage()
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val engine = engine { req ->
            val path = req.url.encodedPath
            val body = when {
                path == "/v1/auth/login" -> json.encodeToString(
                    TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                )
                path.endsWith("/voice-draft") -> json.encodeToString(
                    draft(
                        "a", "turn-a", 2,
                        audio("asset-b", "a", "turn-a", "take-b"),
                        readyTranscript("asset-b", "B-new-original"),
                    )
                )
                path == "/v1/voice-config" -> """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}"""
                else -> """{"items":[]}"""
            }
            respond(body, HttpStatusCode.OK, headers)
        }
        val vm = vm(engine, dispatcher, services(takes, reviews))
        try {
            reviews.write(
                "acc-a", "a", "turn-a",
                com.mockmate.app.core.audio.VoiceReviewDraft(
                    sessionId = "a", turnId = "turn-a", takeId = "take-a",
                    assetId = "asset-a", transcriptRevision = 1,
                    text = "old correction", edited = true,
                ),
            )
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom("a")),
                interviewDetail = detail("a"),
            )
            vm.onVoiceRoomOpened("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            assertEquals("B-new-original", vm.state.value.voiceReviewText)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun coldStartReplaysFrozenUploadBodyInsteadOfRebuildingRevision() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val root = temporary.newFolder()
        val takes = InMemoryVoiceTakeStorage(root)
        val pcm = ByteArray(3200) { (it % 251).toByte() }
        val sha = MessageDigest.getInstance("SHA-256").digest(pcm)
            .joinToString("") { "%02x".format(it) }
        val store = takes
        val file = store.file("acc-a", "a", "turn-a", "take-cold")
        file.parentFile?.mkdirs()
        file.writeBytes(pcm)
        store.write(
            "acc-a", "a", "turn-a",
            VoiceTakeMeta(
                takeId = "take-cold",
                operationKey = "key-cold",
                sha256 = sha,
                byteLength = pcm.size,
                durationMs = 1000,
                interrupted = false,
                reachedLimit = false,
                sessionRevision = 1,
                expectedVoiceRevision = 0,
                encrypted = false,
            ),
        )
        var uploaded = 0
        var seenKey: String? = null
        var seenExpectedOne = false
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(
                    json.encodeToString(
                        TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(draft("a", "turn-a", 1)),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploaded += 1
                    seenKey = req.headers["Idempotency-Key"]
                    val text = req.body.toByteArray().toString(Charsets.UTF_8)
                    seenExpectedOne = text.contains("expected_voice_revision") &&
                        Regex("expected_voice_revision\"?\\s*\\r\\n(?:[^\\r\\n]+\\r\\n)*\\r\\n1\\r\\n")
                            .containsMatchIn(text)
                    if (seenExpectedOne) {
                        respond(
                            """{"error":{"code":"VOICE_REVISION_CONFLICT","message":"changed","retryable":false}}""",
                            HttpStatusCode.Conflict, headers,
                        )
                    } else {
                        respond(
                            json.encodeToString(
                                VoiceUploadReceiptDto(
                                    "a", "turn-a", 2,
                                    audio("asset-cold", "a", "turn-a", "take-cold"),
                                )
                            ),
                            HttpStatusCode.Created, headers,
                        )
                    }
                }
                path.endsWith("/transcriptions") -> respond(
                    """{"id":"asr-cold","kind":"audio_asr","target_id":"asset-cold","input_revision":2,"state":"queued","attempt":1,"retryable":false,"created_at":"2026-11-20T00:00:00Z","updated_at":"2026-11-20T00:00:00Z"}""",
                    HttpStatusCode.Accepted, headers,
                )
                path == "/v1/audio-assets/asset-cold/transcript" -> respond(
                    json.encodeToString(readyTranscript("asset-cold", "cold-recovered")),
                    HttpStatusCode.OK, headers,
                )
                path == "/v1/voice-config" -> respond(
                    """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}""",
                    HttpStatusCode.OK, headers,
                )
                else -> respond("""{"items":[]}""", HttpStatusCode.OK, headers)
            }
        }
        val vm = vm(engine, dispatcher, services(takes), io = Dispatchers.IO)
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom("a")),
                interviewDetail = detail("a"),
            )
            vm.onVoiceRoomOpened("a")
            pump(2_000)
            assertTrue(
                "phase=${vm.state.value.voicePhase} err=${vm.state.value.voiceError} " +
                    "issue=${vm.state.value.voiceTakeIssue} uploads=$uploaded",
                vm.state.value.voicePhase == VoicePhase.REVIEW,
            )
            assertEquals(1, uploaded)
            assertEquals("key-cold", seenKey)
            assertTrue("原请求必须保留冻结的 expected_voice_revision=0", !seenExpectedOne)
            assertEquals("cold-recovered", vm.state.value.voiceReviewText)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun confirmedUploadIsContinuedWithoutReuploading() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val pcm = ByteArray(3200) { 1 }
        val sha = MessageDigest.getInstance("SHA-256").digest(pcm)
            .joinToString("") { "%02x".format(it) }
        val file = takes.file("acc-a", "a", "turn-a", "take-a")
        file.parentFile?.mkdirs()
        file.writeBytes(pcm)
        takes.write(
            "acc-a", "a", "turn-a",
            VoiceTakeMeta(
                takeId = "take-a", operationKey = "key-a", sha256 = sha,
                byteLength = pcm.size, durationMs = 1000,
                interrupted = false, reachedLimit = false,
                sessionRevision = 1, expectedVoiceRevision = 0, encrypted = false,
            ),
        )
        var uploads = 0
        var transcribes = 0
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(
                    json.encodeToString(
                        TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA"))
                    ), HttpStatusCode.OK, headers,
                )
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(
                        draft("a", "turn-a", 1, audio("asset-a", "a", "turn-a", "take-a"))
                    ), HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    respond("{}", HttpStatusCode.InternalServerError, headers)
                }
                path.endsWith("/transcriptions") -> {
                    transcribes += 1
                    respond(
                        """{"id":"asr-a","kind":"audio_asr","target_id":"asset-a","input_revision":1,"state":"queued","attempt":1,"retryable":false,"created_at":"2026-11-20T00:00:00Z","updated_at":"2026-11-20T00:00:00Z"}""",
                        HttpStatusCode.Accepted, headers,
                    )
                }
                path == "/v1/audio-assets/asset-a/transcript" -> respond(
                    json.encodeToString(readyTranscript("asset-a", "server-transcript")),
                    HttpStatusCode.OK, headers,
                )
                path == "/v1/voice-config" -> respond(
                    """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}""",
                    HttpStatusCode.OK, headers,
                )
                else -> respond("""{"items":[]}""", HttpStatusCode.OK, headers)
            }
        }
        val vm = vm(engine, dispatcher, services(takes), io = Dispatchers.IO)
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            vm.state.value = vm.state.value.copy(
                stack = listOf(Screen.InterviewRoom("a")),
                interviewDetail = detail("a"),
            )
            vm.onVoiceRoomOpened("a")
            pump(2_000)
            assertTrue(
                "phase=${vm.state.value.voicePhase} err=${vm.state.value.voiceError} " +
                    "issue=${vm.state.value.voiceTakeIssue} uploads=$uploads transcribes=$transcribes",
                vm.state.value.voicePhase == VoicePhase.REVIEW,
            )
            assertEquals("服务端已保存的 take 不能再次上传", 0, uploads)
            assertEquals(1, transcribes)
            assertEquals("server-transcript", vm.state.value.voiceReviewText)
            assertNotNull(vm.state.value.voiceDraft?.active_audio)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
