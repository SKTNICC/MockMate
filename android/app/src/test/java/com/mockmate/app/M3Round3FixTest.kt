package com.mockmate.app

import com.mockmate.app.core.audio.InMemoryVoiceReviewStorage
import com.mockmate.app.core.audio.InMemoryVoiceTakeStorage
import com.mockmate.app.core.audio.VoicePhase
import com.mockmate.app.core.audio.VoiceReviewDraft
import com.mockmate.app.core.audio.VoiceReviewRead
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
import com.mockmate.app.core.session.SecureWrite
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.respond
import io.ktor.client.engine.mock.toByteArray
import io.ktor.client.request.HttpRequestData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.io.File
import java.security.MessageDigest
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
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * T01–T03 正式回归：把第二轮独立验收的外部 VM 探针转为本仓库测试，并覆盖
 * 丢 201 同 take 确认、真实过期 take 冲突、核对存储 Absent/Failed/Corrupt、
 * 保存失败可见与重试、冷启保留未保存修改。
 */
@OptIn(ExperimentalCoroutinesApi::class)
class M3Round3FixTest {
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

    private fun TestScope.awaitState(timeoutMs: Long = 8_000, done: () -> Boolean) {
        testScheduler.runCurrent()
        val end = System.currentTimeMillis() + timeoutMs
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

    private fun engine(
        handler: suspend io.ktor.client.engine.mock.MockRequestHandleScope.(HttpRequestData) -> io.ktor.client.request.HttpResponseData,
    ) = MockEngine(handler)

    private fun newTake(
        takes: InMemoryVoiceTakeStorage,
        takeId: String = "take-new",
        operationKey: String = "new-key",
        expectedVoiceRevision: Int? = 1,
    ): VoiceTakeMeta {
        val pcm = ByteArray(6400) { (it % 251).toByte() }
        val file = takes.file("acc-a", "a", "turn-a", takeId)
        file.parentFile?.mkdirs()
        file.writeBytes(pcm)
        val meta = VoiceTakeMeta(
            takeId = takeId,
            operationKey = operationKey,
            sha256 = MessageDigest.getInstance("SHA-256").digest(pcm)
                .joinToString("") { "%02x".format(it) },
            byteLength = pcm.size,
            durationMs = 200,
            interrupted = false,
            reachedLimit = false,
            sessionRevision = 1,
            expectedVoiceRevision = expectedVoiceRevision,
            uploaded = false,
        )
        takes.write("acc-a", "a", "turn-a", meta)
        return meta
    }

    private fun loginResponse() = json.encodeToString(
        TokenResponse("tok-a", "bearer", "2026-12-01T00:00:00Z", UserDto("acc-a", "userA")),
    )

    private fun jobResponse(assetId: String) =
        """{"id":"asr-$assetId","kind":"audio_asr","target_id":"$assetId","input_revision":2,"state":"queued","attempt":1,"retryable":false,"created_at":"2026-11-20T00:00:00Z","updated_at":"2026-11-20T00:00:00Z"}"""

    private fun openRoom(vm: AppViewModel) {
        vm.state.value = vm.state.value.copy(
            stack = listOf(Screen.InterviewRoom("a")),
            interviewDetail = detail("a"),
        )
    }

    @Test
    fun newTakeUploadIsNotBlockedByOldActiveAsset() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val pending = newTake(takes)
        var uploads = 0
        var seenKey: String? = null
        var seenExpectedRevision = false
        var seenTakeId = false
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(
                        draft(
                            "a", "turn-a", 1,
                            audio("asset-old", "a", "turn-a", "take-old"),
                            readyTranscript("asset-old", "old-original"),
                        ),
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    seenKey = req.headers["Idempotency-Key"]
                    val text = req.body.toByteArray().toString(Charsets.UTF_8)
                    seenExpectedRevision = Regex(
                        "expected_voice_revision\"?\\s*\\r\\n(?:[^\\r\\n]+\\r\\n)*\\r\\n1\\r\\n",
                    ).containsMatchIn(text)
                    seenTakeId = text.contains(pending.takeId)
                    respond(
                        json.encodeToString(
                            VoiceUploadReceiptDto(
                                "a", "turn-a", 2,
                                audio("asset-new", "a", "turn-a", pending.takeId),
                            ),
                        ),
                        HttpStatusCode.Created, headers,
                    )
                }
                path == "/v1/audio-assets/asset-new/transcriptions" -> respond(
                    jobResponse("asset-new"), HttpStatusCode.Accepted, headers,
                )
                path == "/v1/audio-assets/asset-new/transcript" -> respond(
                    json.encodeToString(readyTranscript("asset-new", "new-transcript")),
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
            openRoom(vm)
            vm.state.value = vm.state.value.copy(
                voicePhase = VoicePhase.LOCAL_READY,
                voiceTake = pending,
                voiceTurnId = "turn-a",
                voiceDraft = draft(
                    "a", "turn-a", 1,
                    audio("asset-old", "a", "turn-a", "take-old"),
                    readyTranscript("asset-old", "old-original"),
                ),
            )
            vm.uploadVoiceTake("a")
            pump(3_000)
            assertEquals("新 take 必须真正发出上传请求", 1, uploads)
            assertEquals("new-key", seenKey)
            assertTrue("必须沿用冻结的 expected_voice_revision=1", seenExpectedRevision)
            assertTrue("上传体必须携带本地新 take", seenTakeId)
            assertTrue(
                "phase=${vm.state.value.voicePhase} err=${vm.state.value.voiceError}",
                vm.state.value.voicePhase == VoicePhase.REVIEW ||
                    vm.state.value.voicePhase == VoicePhase.TRANSCRIBING,
            )
            assertEquals("new-transcript", vm.state.value.voiceReviewText)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun uploadNetworkFailureKeepsFrozenRequestAndRetrySucceeds() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val pending = newTake(takes)
        var uploads = 0
        val keys = mutableListOf<String?>()
        val bodies = mutableListOf<String>()
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(
                        draft(
                            "a", "turn-a", 1,
                            audio("asset-old", "a", "turn-a", "take-old"),
                            readyTranscript("asset-old", "old-original"),
                        ),
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    keys += req.headers["Idempotency-Key"]
                    bodies += req.body.toByteArray().toString(Charsets.UTF_8)
                    if (uploads == 1) {
                        respond(
                            """{"error":{"code":"NETWORK_UNAVAILABLE","message":"offline","retryable":true}}""",
                            HttpStatusCode.ServiceUnavailable, headers,
                        )
                    } else {
                        respond(
                            json.encodeToString(
                                VoiceUploadReceiptDto(
                                    "a", "turn-a", 2,
                                    audio("asset-new", "a", "turn-a", pending.takeId),
                                ),
                            ),
                            HttpStatusCode.Created, headers,
                        )
                    }
                }
                path == "/v1/audio-assets/asset-new/transcriptions" -> respond(
                    jobResponse("asset-new"), HttpStatusCode.Accepted, headers,
                )
                path == "/v1/audio-assets/asset-new/transcript" -> respond(
                    json.encodeToString(readyTranscript("asset-new", "recovered-after-network")),
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
            openRoom(vm)
            vm.state.value = vm.state.value.copy(
                voicePhase = VoicePhase.LOCAL_READY,
                voiceTake = pending,
                voiceTurnId = "turn-a",
                voiceDraft = draft(
                    "a", "turn-a", 1,
                    audio("asset-old", "a", "turn-a", "take-old"),
                    readyTranscript("asset-old", "old-original"),
                ),
            )
            vm.uploadVoiceTake("a")
            pump(2_000)
            assertEquals(1, uploads)
            assertEquals(VoicePhase.LOCAL_READY, vm.state.value.voicePhase)
            assertEquals("take-new", vm.state.value.voiceTake?.takeId)

            vm.uploadVoiceTake("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            assertEquals(2, uploads)
            assertEquals("两次重试必须沿用同一 Idempotency-Key", keys.toSet().size, 1)
            assertEquals(keys[0], keys[1])
            assertTrue(
                "重试不得重建 expected revision",
                Regex(
                    "expected_voice_revision\"?\\s*\\r\\n(?:[^\\r\\n]+\\r\\n)*\\r\\n1\\r\\n",
                ).containsMatchIn(bodies[1]),
            )
            assertEquals("recovered-after-network", vm.state.value.voiceReviewText)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun coldReopenResumesPendingNewTakeInsteadOfShowingOldTranscript() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        newTake(takes)
        var uploads = 0
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(
                        draft(
                            "a", "turn-a", 1,
                            audio("asset-old", "a", "turn-a", "take-old"),
                            readyTranscript("asset-old", "old-original"),
                        ),
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    respond(
                        """{"error":{"code":"NETWORK_UNAVAILABLE","message":"isolated","retryable":true}}""",
                        HttpStatusCode.ServiceUnavailable, headers,
                    )
                }
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
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            pump(3_000)
            assertTrue("应继续恢复待上传的新 take", uploads >= 1)
            assertFalse(
                "冷启不得把旧 ready 转写当成当前核对内容",
                vm.state.value.voicePhase == VoicePhase.REVIEW &&
                    vm.state.value.voiceReviewText == "old-original",
            )
            assertEquals("", vm.state.value.voiceReviewText)
            assertEquals("take-new", vm.state.value.voiceTake?.takeId)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun lostUploadReceiptIsRecognizedAsSameTakeAndOnlyTranscribesOnce() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        newTake(takes, expectedVoiceRevision = 0)
        var uploads = 0
        var transcriptions = 0
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
                path.endsWith("/voice-draft") -> respond(
                    // 服务端已保存同一 take，客户端因 201 丢失仍标记 uploaded=false。
                    json.encodeToString(
                        draft(
                            "a", "turn-a", 1,
                            audio("asset-new", "a", "turn-a", "take-new"),
                        ),
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    respond("{}", HttpStatusCode.InternalServerError, headers)
                }
                path == "/v1/audio-assets/asset-new/transcriptions" -> {
                    transcriptions += 1
                    respond(jobResponse("asset-new"), HttpStatusCode.Accepted, headers)
                }
                path == "/v1/audio-assets/asset-new/transcript" -> respond(
                    json.encodeToString(readyTranscript("asset-new", "recovered-transcript")),
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
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            assertEquals("已确认的同一 take 不能再上传", 0, uploads)
            assertEquals(1, transcriptions)
            assertEquals("recovered-transcript", vm.state.value.voiceReviewText)
            val stored = takes.read("acc-a", "a", "turn-a")
            assertTrue(stored is com.mockmate.app.core.audio.VoiceTakeRead.Present)
            assertTrue((stored as com.mockmate.app.core.audio.VoiceTakeRead.Present).meta.uploaded)
            assertEquals("asset-new", stored.meta.uploadedAssetId)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun staleLocalTakeDoesNotOverwriteNewerServerTake() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        newTake(takes, expectedVoiceRevision = 0)
        var uploads = 0
        var serverRevision = 2
        val engine = engine { req ->
            val path = req.url.encodedPath
            when {
                path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
                path.endsWith("/voice-draft") -> respond(
                    json.encodeToString(
                        draft(
                            "a", "turn-a", serverRevision,
                            audio("asset-server", "a", "turn-a", "take-server"),
                            readyTranscript("asset-server", "server-new-transcript"),
                        ),
                    ),
                    HttpStatusCode.OK, headers,
                )
                path.endsWith("/audio") -> {
                    uploads += 1
                    respond("{}", HttpStatusCode.InternalServerError, headers)
                }
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
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            pump(2_000)
            assertEquals("过期本地 take 绝不能自动上传覆盖", 0, uploads)
            assertEquals(VoicePhase.ERROR, vm.state.value.voicePhase)
            assertTrue(vm.state.value.voiceConflict)
            assertEquals("", vm.state.value.voiceReviewText)

            vm.useServerVoiceDraft("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            assertEquals("server-new-transcript", vm.state.value.voiceReviewText)
            assertFalse(vm.state.value.voiceConflict)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    private class ToggleReviewStore : VoiceReviewStorage {
        private val delegate = InMemoryVoiceReviewStorage()
        var failWrite = false
        var readMode: String = "normal"
        var lastDraft: VoiceReviewDraft? = null
        var writes = 0

        override fun read(accountId: String, sessionId: String, turnId: String): VoiceReviewRead =
            when (readMode) {
                "failed" -> VoiceReviewRead.Failed("isolated read failure")
                "corrupt" -> VoiceReviewRead.Corrupt("isolated corrupt")
                else -> delegate.read(accountId, sessionId, turnId)
            }

        override fun write(
            accountId: String,
            sessionId: String,
            turnId: String,
            draft: VoiceReviewDraft,
        ): SecureWrite {
            writes += 1
            lastDraft = draft
            if (failWrite) return SecureWrite.Failed("isolated write failure")
            return delegate.write(accountId, sessionId, turnId, draft)
        }

        override fun clear(
            accountId: String,
            sessionId: String,
            turnId: String,
            takeId: String?,
        ): Boolean = delegate.clear(accountId, sessionId, turnId, takeId)
    }

    private fun readyEngine(summary: String = "A-original") = engine { req ->
        val path = req.url.encodedPath
        when {
            path == "/v1/auth/login" -> respond(loginResponse(), HttpStatusCode.OK, headers)
            path.endsWith("/voice-draft") -> respond(
                json.encodeToString(
                    draft(
                        "a", "turn-a", 1,
                        audio("asset-a", "a", "turn-a", "take-a"),
                        readyTranscript("asset-a", summary),
                    ),
                ),
                HttpStatusCode.OK, headers,
            )
            path == "/v1/voice-config" -> respond(
                """{"available":true,"max_answer_seconds":120,"format":"pcm_s16le/16000/mono","tts_voice_id":"stella","speech_provider":"test"}""",
                HttpStatusCode.OK, headers,
            )
            else -> respond("""{"items":[]}""", HttpStatusCode.OK, headers)
        }
    }

    @Test
    fun reviewSaveFailureIsVisibleKeepsTextAndRetrySucceeds() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val reviews = ToggleReviewStore().apply { failWrite = true }
        val vm = vm(readyEngine(), dispatcher, services(takes, reviews))
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            vm.updateVoiceReviewText("my corrected answer")
            assertEquals("my corrected answer", vm.state.value.voiceReviewText)
            assertNotNull("保存失败必须对用户可见", vm.state.value.voiceReviewStorageIssue)
            assertTrue(vm.state.value.voiceReviewDirty)
            assertTrue(
                "失败不能声称已持久化",
                reviews.read("acc-a", "a", "turn-a") is VoiceReviewRead.Absent,
            )

            reviews.failWrite = false
            vm.retryVoiceReviewPersistence()
            assertNull(vm.state.value.voiceReviewStorageIssue)
            assertFalse(vm.state.value.voiceReviewDirty)
            assertEquals("my corrected answer", reviews.lastDraft?.text)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reviewReadFailureIsVisibleAndDoesNotClobberInMemoryEdit() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val reviews = ToggleReviewStore().apply { readMode = "failed" }
        val vm = vm(readyEngine("original-transcript"), dispatcher, services(takes, reviews))
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            assertNotNull("读取失败必须与没有草稿区分", vm.state.value.voiceReviewStorageIssue)
            assertEquals("original-transcript", vm.state.value.voiceReviewText)

            reviews.failWrite = false
            vm.updateVoiceReviewText("in-memory edit")
            reviews.readMode = "failed"
            vm.onVoiceRoomOpened("a")
            pump(2_000)
            assertEquals("读取失败不能覆盖内存中的修改", "in-memory edit", vm.state.value.voiceReviewText)
            assertNotNull(vm.state.value.voiceReviewStorageIssue)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun permissionDenialIsExplicitAndDoesNotFakeRecording() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val vm = vm(readyEngine(), dispatcher)
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            vm.onVoicePermissionDenied()
            assertEquals(VoicePhase.ERROR, vm.state.value.voicePhase)
            assertTrue(vm.state.value.voiceError?.contains("权限") == true)
            assertNull(vm.state.value.voiceTake)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun corruptReviewDraftIsReportedAsCorruptNotAbsent() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val takes = InMemoryVoiceTakeStorage(temporary.newFolder())
        val reviews = ToggleReviewStore().apply { readMode = "corrupt" }
        val vm = vm(readyEngine(), dispatcher, services(takes, reviews))
        try {
            vm.authenticate(false, "userA", "pw")
            awaitState { vm.state.value.account != null }
            openRoom(vm)
            vm.onVoiceRoomOpened("a")
            awaitState { vm.state.value.voicePhase == VoicePhase.REVIEW }
            val issue = vm.state.value.voiceReviewStorageIssue
            assertNotNull(issue)
            assertTrue(issue!!.contains("损坏"))
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
