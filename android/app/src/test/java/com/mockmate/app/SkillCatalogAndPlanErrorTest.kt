package com.mockmate.app

import com.mockmate.app.core.network.ErrorEnvelope
import com.mockmate.app.core.network.ErrorInfo
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.SkillCodeDto
import com.mockmate.app.core.network.SkillCodesResponse
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.SnapshotsResponse
import com.mockmate.app.core.network.TokenResponse
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.operation.InMemoryOperationStore
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.session.InMemoryDraftStorage
import com.mockmate.app.core.session.InMemoryInterviewDraftStorage
import com.mockmate.app.core.session.InMemorySessionStore
import com.mockmate.app.ui.AppViewModel
import com.mockmate.app.ui.Screen
import com.mockmate.app.ui.SkillCatalogStatus
import com.mockmate.app.ui.screens.PlanErrorFormatter
import io.ktor.client.engine.mock.MockEngine
import io.ktor.client.engine.mock.MockRequestHandleScope
import io.ktor.client.engine.mock.respond
import io.ktor.client.request.HttpRequestData
import io.ktor.client.request.HttpResponseData
import io.ktor.http.HttpHeaders
import io.ktor.http.HttpStatusCode
import io.ktor.http.headersOf
import java.util.concurrent.atomic.AtomicInteger
import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class SkillCatalogAndPlanErrorTest {

    private val json = Json { ignoreUnknownKeys = true; encodeDefaults = true; explicitNulls = false }
    private val jsonHeaders = headersOf(HttpHeaders.ContentType, "application/json")

    private fun MockRequestHandleScope.okJson(body: String): HttpResponseData =
        respond(body, HttpStatusCode.OK, jsonHeaders)

    private fun MockRequestHandleScope.errJson(
        status: HttpStatusCode,
        code: String,
        message: String,
        details: List<String> = emptyList(),
    ): HttpResponseData = respond(
        json.encodeToString(
            ErrorEnvelope(
                ErrorInfo(
                    code = code,
                    message = message,
                    retryable = false,
                    details = details,
                    request_id = "req-test",
                ),
            ),
        ),
        status,
        jsonHeaders,
    )

    private fun TestScope.drainAll(until: () -> Boolean = { true }) {
        val deadline = System.currentTimeMillis() + 5_000
        while (System.currentTimeMillis() < deadline) {
            testScheduler.runCurrent()
            if (until()) {
                Thread.sleep(10)
                testScheduler.runCurrent()
                if (until()) return
            } else {
                Thread.sleep(5)
            }
        }
    }

    private fun createVm(
        dispatcher: kotlinx.coroutines.CoroutineDispatcher,
        handler: suspend MockRequestHandleScope.(HttpRequestData) -> HttpResponseData,
    ): AppViewModel = AppViewModel(
        sessionStore = InMemorySessionStore(),
        drafts = InMemoryDraftStorage(),
        interviewDrafts = InMemoryInterviewDraftStorage(),
        operationStore = InMemoryOperationStore(),
        httpEngine = MockEngine(handler),
        ioDispatcher = dispatcher,
        bootstrapOnInit = false,
    )

    private fun commonResponse(
        request: HttpRequestData,
        scope: MockRequestHandleScope,
    ): HttpResponseData? = when (request.url.encodedPath) {
        "/v1/auth/login" -> scope.okJson(
            json.encodeToString(
                TokenResponse(
                    access_token = "tok-a",
                    token_type = "bearer",
                    expires_at = "2026-12-01T00:00:00Z",
                    user = UserDto("acc-a", "userA"),
                ),
            ),
        )
        "/v1/roles" -> scope.okJson("""{"items":[]}""")
        "/v1/preparations" -> scope.okJson("""{"items":[]}""")
        "/v1/materials" -> scope.okJson("""{"items":[]}""")
        else -> null
    }

    private fun materialDto(id: String) = MaterialDraftDto(
        id = id,
        revision = 1,
        kind = "resume",
        title = "材料-$id",
        text = "本科在读。\n技能：Java 基础。",
        profile = null,
        state = "text_ready",
        created_at = "2026-11-18T00:00:00Z",
        updated_at = "2026-11-18T00:00:00Z",
    )

    @Test
    fun planErrorFormatterFormatsCountsAndFeasibleOptions() {
        val sixShortToOne = PlanErrorFormatter.parseBankInsufficient(listOf("required=3", "available=1"))!!
        assertEquals(3, sixShortToOne.required)
        assertEquals(1, sixShortToOne.available)
        assertEquals(listOf(2), sixShortToOne.feasibleCounts)
        assertTrue(sixShortToOne.summary.contains("本场需要 3 道相关题库题，目前匹配到 1 道"))
        assertTrue(sixShortToOne.guidance.contains("2 题"))

        val sixShortToTwo = PlanErrorFormatter.parseBankInsufficient(listOf("required=3", "available=2"))!!
        assertEquals(listOf(2, 4), sixShortToTwo.feasibleCounts)
        assertTrue(sixShortToTwo.guidance.contains("2 题 或 4 题"))

        val zeroAvailable = PlanErrorFormatter.parseBankInsufficient(listOf("required=3", "available=0"))!!
        assertTrue(zeroAvailable.feasibleCounts.isEmpty())
        assertTrue(zeroAvailable.guidance.contains("当前题库覆盖不足"))
        assertTrue(!zeroAvailable.guidance.contains("2 题"))

        assertNull(PlanErrorFormatter.parseBankInsufficient(listOf("M1-JAVA-0001")))
        assertEquals(
            "JD 缺少显式技能要求",
            PlanErrorFormatter.userMessage("BANK_INSUFFICIENT", "JD 缺少显式技能要求", emptyList()),
        )
    }

    @Test
    fun lateSkillCatalogResponseFromPreviousMaterialCannotOverwriteCurrentReview() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val mat1CatalogEntered = CompletableDeferred<Unit>()
        val mat1CatalogRelease = CompletableDeferred<Unit>()
        val skillCallCount = AtomicInteger(0)

        val vm = createVm(dispatcher) { request ->
            commonResponse(request, this)?.let { return@createVm it }
            when (request.url.encodedPath) {
                "/v1/materials/mat-1" -> okJson(json.encodeToString(materialDto("mat-1")))
                "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                "/v1/materials/mat-2" -> okJson(json.encodeToString(materialDto("mat-2")))
                "/v1/materials/mat-2/snapshots" -> okJson("""{"items":[]}""")
                "/v1/skill-codes" -> {
                    val callIndex = skillCallCount.incrementAndGet()
                    if (callIndex == 1) {
                        mat1CatalogEntered.complete(Unit)
                        mat1CatalogRelease.await()
                        okJson(
                            json.encodeToString(
                                SkillCodesResponse(
                                    listOf(SkillCodeDto("stale.skill", "旧材料迟到技能")),
                                ),
                            ),
                        )
                    } else {
                        okJson(
                            json.encodeToString(
                                SkillCodesResponse(
                                    listOf(SkillCodeDto("java.core", "Java 基础")),
                                ),
                            ),
                        )
                    }
                }
                else -> error("Unexpected path ${request.url.encodedPath}")
            }
        }
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { mat1CatalogEntered.isCompleted }
            assertTrue(vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1"))

            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll { vm.state.value.skillCatalogStatus == SkillCatalogStatus.LOADED }
            assertEquals(listOf(SkillCodeDto("java.core", "Java 基础")), vm.state.value.skillCodes)

            // 放行材料 1 的迟到目录响应：不能覆盖材料 2 的目录
            mat1CatalogRelease.complete(Unit)
            drainAll()
            assertEquals(listOf(SkillCodeDto("java.core", "Java 基础")), vm.state.value.skillCodes)
            assertEquals(SkillCatalogStatus.LOADED, vm.state.value.skillCatalogStatus)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun skillCatalogFailureRetriesAndStaleConfirmRefreshesAndFlagsItem() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val skillCallCount = AtomicInteger(0)

        val vm = createVm(dispatcher) { request ->
            commonResponse(request, this)?.let { return@createVm it }
            when (request.url.encodedPath) {
                "/v1/materials/mat-1" -> okJson(json.encodeToString(materialDto("mat-1")))
                "/v1/materials/mat-1/snapshots" -> okJson("""{"items":[]}""")
                "/v1/skill-codes" -> when (skillCallCount.incrementAndGet()) {
                    1 -> errJson(HttpStatusCode.ServiceUnavailable, "SERVICE_DOWN", "目录暂时不可用")
                    2 -> okJson(
                        json.encodeToString(
                            SkillCodesResponse(
                                listOf(
                                    SkillCodeDto("java.core", "Java 基础"),
                                    SkillCodeDto("redis.cache", "Redis 缓存"),
                                ),
                            ),
                        ),
                    )
                    else -> okJson(
                        json.encodeToString(
                            SkillCodesResponse(listOf(SkillCodeDto("java.core", "Java 基础"))),
                        ),
                    )
                }
                "/v1/materials/mat-1/confirm" -> errJson(
                    HttpStatusCode.BadRequest,
                    "PROFILE_INVALID",
                    "技能编码不在已启用词表中",
                    details = listOf("section=requirements", "item_id=req-1", "field=skill_codes", "redis.cache"),
                )
                else -> error("Unexpected path ${request.url.encodedPath}")
            }
        }
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.skillCatalogStatus == SkillCatalogStatus.FAILED }
            assertEquals("目录暂时不可用", vm.state.value.skillCatalogError)

            vm.refreshSkillCodes()
            drainAll { vm.state.value.skillCatalogStatus == SkillCatalogStatus.LOADED }
            assertEquals(2, vm.state.value.skillCodes.size)

            vm.confirmProfile("mat-1", JsonObject(mapOf("kind" to JsonPrimitive("jd"))))
            drainAll {
                vm.state.value.invalidSkillItemId == "req-1" &&
                    vm.state.value.skillCodes.size == 1
            }
            assertEquals("req-1", vm.state.value.invalidSkillItemId)
            assertEquals("skill_codes", vm.state.value.invalidSkillField)
            assertEquals(listOf(SkillCodeDto("java.core", "Java 基础")), vm.state.value.skillCodes)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun bankInsufficientSetsReadablePlanErrorAndReturnsToSetup() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val prepDto = PreparationDto(
            id = "prep-1",
            revision = 1,
            role_id = "java-backend",
            level = "intern",
            main_question_count = 6,
            resume_snapshot_id = "snap-r",
            jd_snapshot_id = "snap-j",
            match_report_id = "mr-1",
            created_at = "2026-11-18T00:00:00Z",
            updated_at = "2026-11-18T00:00:00Z",
        )

        val vm = createVm(dispatcher) { request ->
            commonResponse(request, this)?.let { return@createVm it }
            when (request.url.encodedPath) {
                "/v1/preparations/prep-1" -> okJson(json.encodeToString(prepDto))
                "/v1/snapshots/snap-r", "/v1/snapshots/snap-j" -> errJson(HttpStatusCode.NotFound, "NOT_FOUND", "x")
                "/v1/preparations/prep-1/generate-plan" -> errJson(
                    HttpStatusCode.Conflict,
                    "BANK_INSUFFICIENT",
                    "符合岗位、级别、审核要求且与当前 JD/简历有技能关联的题库主问题不足",
                    details = listOf("required=3", "available=1"),
                )
                else -> error("Unexpected path ${request.url.encodedPath}")
            }
        }
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openPreparation("prep-1")
            drainAll { vm.state.value.stack.last() == Screen.Setup("prep-1") }

            vm.openPlan("prep-1")
            assertEquals(Screen.PlanHub("prep-1"), vm.state.value.stack.last())

            vm.generatePlan("prep-1")
            drainAll { vm.state.value.planError != null }

            val err = vm.state.value.planError!!
            assertEquals("BANK_INSUFFICIENT", err.code)
            assertTrue(err.message.contains("本场需要 3 道相关题库题，目前匹配到 1 道"))
            assertTrue(vm.state.value.message!!.text.contains("本场需要 3 道相关题库题，目前匹配到 1 道"))

            vm.returnToSetup("prep-1")
            assertEquals(Screen.Setup("prep-1"), vm.state.value.stack.last())
            assertNull(vm.state.value.planError)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    private fun snapshotDto(id: String, materialId: String, title: String) = SnapshotDto(
        id = id,
        material_id = materialId,
        source_revision = 1,
        kind = "resume",
        title = title,
        text = "本科在读。\n技能：Java 基础。",
        profile = JsonObject(mapOf("kind" to JsonPrimitive("resume"))),
        confirmed_at = "2026-11-18T00:01:00Z",
    )

    @Test
    fun lateConfirmGetFromMaterialACannotOverwriteMaterialB() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val opStore = InMemoryOperationStore()
        val postConfirmSnapshotsEntered = CompletableDeferred<Unit>()
        val postConfirmSnapshotsRelease = CompletableDeferred<Unit>()
        val matASnapshotsCalls = AtomicInteger(0)
        val matADetailCalls = AtomicInteger(0)
        var matAConfirmed = false

        val snapA = snapshotDto("snap-a", "mat-1", "材料A快照")
        val snapB = snapshotDto("snap-b", "mat-2", "材料B快照")
        val matADraft = materialDto("mat-1")
        val matAConfirmedDraft = matADraft.copy(state = "confirmed")
        val matBDraft = materialDto("mat-2")

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = opStore,
            httpEngine = MockEngine { request ->
                commonResponse(request, this)?.let { return@MockEngine it }
                when (request.url.encodedPath) {
                    "/v1/skill-codes" -> okJson(
                        json.encodeToString(SkillCodesResponse(listOf(SkillCodeDto("java.core", "Java 基础")))),
                    )
                    "/v1/materials/mat-1" -> {
                        val call = matADetailCalls.incrementAndGet()
                        val dto = if (matAConfirmed && call >= 2) matAConfirmedDraft else matADraft
                        okJson(json.encodeToString(dto))
                    }
                    "/v1/materials/mat-1/snapshots" -> {
                        val call = matASnapshotsCalls.incrementAndGet()
                        if (call == 2) {
                            postConfirmSnapshotsEntered.complete(Unit)
                            postConfirmSnapshotsRelease.await()
                        }
                        val items = if (matAConfirmed) listOf(snapA) else emptyList()
                        okJson(json.encodeToString(SnapshotsResponse(items)))
                    }
                    "/v1/materials/mat-1/confirm" -> {
                        matAConfirmed = true
                        okJson(json.encodeToString(snapA))
                    }
                    "/v1/materials/mat-2" -> okJson(json.encodeToString(matBDraft))
                    "/v1/materials/mat-2/snapshots" -> okJson(json.encodeToString(SnapshotsResponse(listOf(snapB))))
                    else -> error("Unexpected path ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1") }
            assertEquals(1, matASnapshotsCalls.get())

            vm.confirmProfile("mat-1", JsonObject(mapOf("kind" to JsonPrimitive("resume"))))
            drainAll { postConfirmSnapshotsEntered.isCompleted }

            // 在 A 确认后的快照 GET 挂起期间，返回并打开材料 B
            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll {
                vm.state.value.stack.last() == Screen.Review("mat-2", "prep-1") &&
                    vm.state.value.material?.id == "mat-2" &&
                    !vm.state.value.loading
            }
            assertEquals(listOf(snapB), vm.state.value.snapshots)
            assertNull(vm.state.value.message)

            // 放行 A 的迟到快照 GET：不得覆盖 B 的材料、快照、loading、message 或错误定位
            postConfirmSnapshotsRelease.complete(Unit)
            drainAll()

            assertEquals(Screen.Review("mat-2", "prep-1"), vm.state.value.stack.last())
            assertEquals("mat-2", vm.state.value.material?.id)
            assertEquals(listOf(snapB), vm.state.value.snapshots)
            assertFalse(vm.state.value.loading)
            assertNull(vm.state.value.busyLabel)
            assertNull(vm.state.value.message)
            assertNull(vm.state.value.invalidSkillItemId)
            // 离开后不应继续发起 A 的后续详情 GET
            assertEquals(1, matADetailCalls.get())

            // A 的本地幂等记录已完成清理，重开 A 可看到已确认快照
            val pending = OperationCoordinator(opStore, ioDispatcher = dispatcher)
                .pendingFor("acc-a", "confirmMaterial", "mat-1")
            assertNull(pending)

            vm.back()
            vm.openReview("mat-1", "prep-1")
            drainAll {
                vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1") &&
                    vm.state.value.material?.id == "mat-1" &&
                    !vm.state.value.loading
            }
            assertEquals("confirmed", vm.state.value.material?.state)
            assertEquals(listOf(snapA), vm.state.value.snapshots)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun lateConfirmPostBeforeLeavingCannotOverwriteMaterialB() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val opStore = InMemoryOperationStore()
        val confirmPostEntered = CompletableDeferred<Unit>()
        val confirmPostRelease = CompletableDeferred<Unit>()
        val matASnapshotsCalls = AtomicInteger(0)
        var matAConfirmed = false

        val snapA = snapshotDto("snap-a", "mat-1", "材料A快照")
        val snapB = snapshotDto("snap-b", "mat-2", "材料B快照")

        val vm = AppViewModel(
            sessionStore = InMemorySessionStore(),
            drafts = InMemoryDraftStorage(),
            interviewDrafts = InMemoryInterviewDraftStorage(),
            operationStore = opStore,
            httpEngine = MockEngine { request ->
                commonResponse(request, this)?.let { return@MockEngine it }
                when (request.url.encodedPath) {
                    "/v1/skill-codes" -> okJson(
                        json.encodeToString(SkillCodesResponse(listOf(SkillCodeDto("java.core", "Java 基础")))),
                    )
                    "/v1/materials/mat-1" -> okJson(
                        json.encodeToString(
                            materialDto("mat-1").copy(state = if (matAConfirmed) "confirmed" else "text_ready"),
                        ),
                    )
                    "/v1/materials/mat-1/snapshots" -> {
                        matASnapshotsCalls.incrementAndGet()
                        val items = if (matAConfirmed) listOf(snapA) else emptyList()
                        okJson(json.encodeToString(SnapshotsResponse(items)))
                    }
                    "/v1/materials/mat-1/confirm" -> {
                        if (!matAConfirmed) {
                            confirmPostEntered.complete(Unit)
                            confirmPostRelease.await()
                            matAConfirmed = true
                        }
                        okJson(json.encodeToString(snapA))
                    }
                    "/v1/materials/mat-2" -> okJson(json.encodeToString(materialDto("mat-2")))
                    "/v1/materials/mat-2/snapshots" -> okJson(json.encodeToString(SnapshotsResponse(listOf(snapB))))
                    else -> error("Unexpected path ${request.url.encodedPath}")
                }
            },
            ioDispatcher = dispatcher,
            bootstrapOnInit = false,
        )
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            // POST 回来前切页到 B，POST 迟到返回后不发 GET 也不污染 B
            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1") }
            vm.confirmProfile("mat-1", JsonObject(mapOf("kind" to JsonPrimitive("resume"))))
            drainAll { confirmPostEntered.isCompleted }

            vm.back()
            vm.openReview("mat-2", "prep-1")
            drainAll { vm.state.value.material?.id == "mat-2" && !vm.state.value.loading }

            confirmPostRelease.complete(Unit)
            drainAll()
            assertEquals("mat-2", vm.state.value.material?.id)
            assertEquals(listOf(snapB), vm.state.value.snapshots)
            assertNull(vm.state.value.message)
            assertFalse(vm.state.value.loading)
            assertEquals(1, matASnapshotsCalls.get())
            assertNull(
                OperationCoordinator(opStore, ioDispatcher = dispatcher)
                    .pendingFor("acc-a", "confirmMaterial", "mat-1"),
            )
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }

    @Test
    fun reopeningSameMaterialIsolatesStaleConfirmByNavigationEpochAndCurrentStayUpdates() = runTest {
        val dispatcher = StandardTestDispatcher(testScheduler)
        Dispatchers.setMain(dispatcher)
        val staleDetailEntered = CompletableDeferred<Unit>()
        val staleDetailRelease = CompletableDeferred<Unit>()
        val matADetailCalls = AtomicInteger(0)
        var confirmed = false

        val snapA = snapshotDto("snap-a", "mat-1", "材料A快照")

        val vm = createVm(dispatcher) { request ->
            commonResponse(request, this)?.let { return@createVm it }
            when (request.url.encodedPath) {
                "/v1/skill-codes" -> okJson(
                    json.encodeToString(SkillCodesResponse(listOf(SkillCodeDto("java.core", "Java 基础")))),
                )
                "/v1/materials/mat-1" -> {
                    val call = matADetailCalls.incrementAndGet()
                    if (call == 2) {
                        staleDetailEntered.complete(Unit)
                        staleDetailRelease.await()
                    }
                    okJson(
                        json.encodeToString(
                            materialDto("mat-1").copy(
                                state = if (confirmed) "confirmed" else "text_ready",
                                title = if (call == 2) "旧请求延迟返回" else "材料-mat-1",
                            ),
                        ),
                    )
                }
                "/v1/materials/mat-1/snapshots" -> okJson(
                    json.encodeToString(SnapshotsResponse(if (confirmed) listOf(snapA) else emptyList())),
                )
                "/v1/materials/mat-1/confirm" -> {
                    confirmed = true
                    okJson(json.encodeToString(snapA))
                }
                else -> error("Unexpected path ${request.url.encodedPath}")
            }
        }
        try {
            vm.authenticate(false, "userA", "password12345")
            drainAll { vm.state.value.account != null }

            vm.openReview("mat-1", "prep-1")
            drainAll { vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1") }

            // 在第 1 个导航代次发起确认，停在确认后的第 2 次详情 GET
            vm.confirmProfile("mat-1", JsonObject(mapOf("kind" to JsonPrimitive("resume"))))
            drainAll { staleDetailEntered.isCompleted }

            // 返回并重新打开同一个材料 mat-1（新导航代次）
            vm.back()
            vm.openReview("mat-1", "prep-1")
            drainAll {
                vm.state.value.stack.last() == Screen.Review("mat-1", "prep-1") &&
                    vm.state.value.material?.id == "mat-1" &&
                    !vm.state.value.loading
            }
            assertNull(vm.state.value.message)
            assertEquals("材料-mat-1", vm.state.value.material?.title)

            // 放行旧导航代次的详情 GET：不得改写当前页面的 title 或弹出旧成功提示
            staleDetailRelease.complete(Unit)
            drainAll()
            assertEquals("材料-mat-1", vm.state.value.material?.title)
            assertNull(vm.state.value.message)
            assertFalse(vm.state.value.loading)

            // 正常停留在当前页面确认：应更新快照、材料和成功提示
            vm.confirmProfile("mat-1", JsonObject(mapOf("kind" to JsonPrimitive("resume"))))
            drainAll { vm.state.value.message?.text == "已保存为不可变版本（快照）" }
            assertEquals(listOf(snapA), vm.state.value.snapshots)
            assertEquals("confirmed", vm.state.value.material?.state)
            assertFalse(vm.state.value.loading)
        } finally {
            vm.closeForTest()
            Dispatchers.resetMain()
        }
    }
}
