package com.mockmate.app.ui

import android.app.Application
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.mockmate.app.BuildConfig
import com.mockmate.app.core.audio.AndroidVoiceFileCipher
import com.mockmate.app.core.audio.QuestionPlayer
import com.mockmate.app.core.audio.VoicePhase
import com.mockmate.app.core.audio.VoiceRecorder
import com.mockmate.app.core.audio.VoiceReviewDraft
import com.mockmate.app.core.audio.VoiceReviewRead
import com.mockmate.app.core.audio.VoiceReviewStore
import com.mockmate.app.core.audio.VoiceServices
import com.mockmate.app.core.audio.VoiceTakeMeta
import com.mockmate.app.core.audio.VoiceTakeRead
import com.mockmate.app.core.audio.VoiceTakeStore
import com.mockmate.app.core.network.JdTemplateDto
import com.mockmate.app.core.network.JobDto
import com.mockmate.app.core.network.MaterialDraftDto
import com.mockmate.app.core.network.MatchReportDto
import com.mockmate.app.core.network.MockMateApi
import com.mockmate.app.core.network.PreparationDto
import com.mockmate.app.core.network.PublicPlanDto
import com.mockmate.app.core.network.RoleDto
import com.mockmate.app.core.network.SkillCodeDto
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.UserDto
import com.mockmate.app.core.network.InterviewDetailDto
import com.mockmate.app.core.network.InterviewHistoryItemDto
import com.mockmate.app.core.network.InterviewReceiptDto
import com.mockmate.app.core.network.InterviewReportDto
import com.mockmate.app.core.network.SpeechAssetDto
import com.mockmate.app.core.network.VoiceConfigDto
import com.mockmate.app.core.network.VoiceDraftDto
import com.mockmate.app.core.network.VoiceTranscriptDto
import com.mockmate.app.core.operation.ActiveJob
import com.mockmate.app.core.operation.ConfirmMaterialPayload
import com.mockmate.app.core.operation.ConfirmPlanPayload
import com.mockmate.app.core.operation.CreatePreparationPayload
import com.mockmate.app.core.operation.CreateTextMaterialPayload
import com.mockmate.app.core.operation.DeleteMaterialPayload
import com.mockmate.app.core.operation.DeletePreparationPayload
import com.mockmate.app.core.operation.EditMaterialPayload
import com.mockmate.app.core.operation.EditPreparationPayload
import com.mockmate.app.core.operation.ExtractMaterialPayload
import com.mockmate.app.core.operation.GeneratePlanPayload
import com.mockmate.app.core.operation.InterviewAnswerPayload
import com.mockmate.app.core.operation.InterviewBasicReportPayload
import com.mockmate.app.core.operation.InterviewContinuePayload
import com.mockmate.app.core.operation.InterviewFinishPayload
import com.mockmate.app.core.operation.InterviewPausePayload
import com.mockmate.app.core.operation.InterviewResumePayload
import com.mockmate.app.core.operation.InterviewSkipPayload
import com.mockmate.app.core.operation.MatchPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationPayload
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.OperationStore
import com.mockmate.app.core.operation.ParseMaterialPayload
import com.mockmate.app.core.operation.PendingOperation
import com.mockmate.app.core.operation.PendingOperationReplayer
import com.mockmate.app.core.operation.RetryJobPayload
import com.mockmate.app.core.operation.SecureOperationStore
import com.mockmate.app.core.operation.StartInterviewPayload
import com.mockmate.app.core.operation.UploadFilePayload
import com.mockmate.app.core.operation.WritePipeline
import com.mockmate.app.core.session.ContextGeneration
import com.mockmate.app.core.session.DraftStorage
import com.mockmate.app.core.session.DraftStore
import com.mockmate.app.core.session.InterviewDraftStorage
import com.mockmate.app.core.session.InterviewDraftStore
import com.mockmate.app.core.session.RequestContext
import com.mockmate.app.core.session.SecureRead
import com.mockmate.app.core.session.SecureSessionStore
import com.mockmate.app.core.session.SecureWrite
import com.mockmate.app.core.session.SessionManager
import com.mockmate.app.core.session.SessionStore
import com.mockmate.app.data.ApiResult
import com.mockmate.app.data.JobWatch
import com.mockmate.app.data.M1Repository
import com.mockmate.app.data.RepositoryOperationReplayExecutor
import com.mockmate.app.data.RestoreCoordinator
import com.mockmate.app.data.RestoreJobService
import com.mockmate.app.data.RestoreOutcome
import com.mockmate.app.data.RestorePorts
import com.mockmate.app.data.RestoreScope
import com.mockmate.app.data.RestoreUiEffect
import com.mockmate.app.data.RestoreUiGate
import com.mockmate.app.ui.screens.PlanErrorFormatter
import io.ktor.client.engine.HttpClientEngine
import java.io.File
import java.security.MessageDigest
import java.util.UUID
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject

data class Account(val id: String, val username: String)

data class UiMessage(
    val text: String,
    val isError: Boolean = true,
    val code: String? = null,
    val details: List<String> = emptyList(),
    val httpStatus: Int? = null,
    val retryable: Boolean = false,
    val diagnosticContext: String? = null,
    val jobKind: String? = null,
)

/** 持续性后台任务状态条：显示在底部 Toast 位置，只弹出一次并持续到任务结束，不自动超时消失。 */
data class OngoingStatus(
    val jobId: String,
    val jobKind: String,
    val text: String,
)

/** 已无法随当前回合提交、但仍属于本机的加密草稿；只用于查看或复制。 */
data class LocalInterviewDraft(
    val turnId: String,
    val prompt: String?,
    val text: String,
)

enum class SkillCatalogStatus {
    IDLE,
    LOADING,
    LOADED,
    FAILED,
}

enum class MaterialJobLookupStatus {
    IDLE,
    LOADING,
    LOADED,
    FAILED,
}

data class MaterialJobLookup(
    val materialId: String,
    val revision: Int,
    val jobId: String,
    val status: MaterialJobLookupStatus,
    val errorMessage: String? = null,
)

data class PlanErrorState(
    val code: String?,
    val message: String,
    val details: List<String> = emptyList(),
    val rawServerMessage: String? = null,
    val httpStatus: Int? = null,
    val retryable: Boolean = false,
    val diagnosticContext: String? = null,
    val jobId: String? = null,
    val preparationId: String? = null,
    val inputRevision: Int? = null,
)

sealed interface Screen {
    data object Auth : Screen
    data object PreparationList : Screen
    data class Setup(val preparationId: String?) : Screen
    data class Library(val kind: String, val preparationId: String) : Screen
    data class Editor(val kind: String, val materialId: String?, val preparationId: String) : Screen
    data class Review(val materialId: String, val preparationId: String) : Screen
    data class Templates(val preparationId: String) : Screen
    data class Match(val preparationId: String) : Screen
    data class PlanHub(val preparationId: String) : Screen
    data object InterviewHistory : Screen
    data class InterviewRoom(val sessionId: String) : Screen
    data class InterviewReport(val sessionId: String) : Screen
}

data class AppState(
    val booting: Boolean = true,
    val account: Account? = null,
    val stack: List<Screen> = listOf(Screen.Auth),
    val loading: Boolean = false,
    val busyLabel: String? = null,
    val message: UiMessage? = null,
    val ongoingStatus: OngoingStatus? = null,
    val roles: List<RoleDto> = emptyList(),
    val skillCodes: List<SkillCodeDto> = emptyList(),
    val skillCatalogStatus: SkillCatalogStatus = SkillCatalogStatus.IDLE,
    val skillCatalogError: String? = null,
    val invalidSkillItemId: String? = null,
    val invalidSkillField: String? = null,
    val invalidSkillFocusNonce: Int = 0,
    val templates: List<JdTemplateDto> = emptyList(),
    val preparations: List<PreparationDto> = emptyList(),
    val materials: List<MaterialDraftDto> = emptyList(),
    val preparation: PreparationDto? = null,
    val resumeSnapshot: SnapshotDto? = null,
    val jdSnapshot: SnapshotDto? = null,
    val material: MaterialDraftDto? = null,
    val materialJobLookup: MaterialJobLookup? = null,
    val snapshots: List<SnapshotDto> = emptyList(),
    val matchReport: MatchReportDto? = null,
    val plan: PublicPlanDto? = null,
    val planError: PlanErrorState? = null,
    val job: JobDto? = null,
    val lastGeneratedPlanId: String? = null,
    val interviewDetail: InterviewDetailDto? = null,
    val interviewReport: InterviewReportDto? = null,
    val interviewReportError: String? = null,
    val interviewHistory: List<InterviewHistoryItemDto> = emptyList(),
    val interviewHistoryCursor: String? = null,
    val interviewHistoryLoading: Boolean = false,
    val interviewForPlan: InterviewHistoryItemDto? = null,
    val pendingInterviewTurnId: String? = null,
    val interviewOrphanDrafts: List<LocalInterviewDraft> = emptyList(),
    val voiceConfig: VoiceConfigDto? = null,
    val voiceDraft: VoiceDraftDto? = null,
    val voicePhase: VoicePhase = VoicePhase.IDLE,
    val voiceTurnId: String? = null,
    val voiceTake: VoiceTakeMeta? = null,
    val voiceTakeIssue: String? = null,
    /** 本地录音与服务端已确认录音/版本冲突；需要用户显式选择，不静默覆盖。 */
    val voiceConflict: Boolean = false,
    /** 核对草稿读取/保存失败的可见状态；与“没有草稿”不同。 */
    val voiceReviewStorageIssue: String? = null,
    /** 当前内存文本有尚未成功持久化的修改。 */
    val voiceReviewDirty: Boolean = false,
    val voiceRemainingSeconds: Int = 120,
    val voiceRecordedSeconds: Int = 0,
    val voiceReviewText: String = "",
    val voiceSpeechSynthetic: Boolean = false,
    val voicePlaybackTurnId: String? = null,
    val voiceError: String? = null,
    val voiceTextFallback: Boolean = false,
    val voiceFixtureActive: Boolean = false,
)

class AppViewModel internal constructor(
    private val sessionStore: SessionStore,
    private val drafts: DraftStorage,
    private val interviewDrafts: InterviewDraftStorage,
    operationStore: OperationStore,
    httpEngine: HttpClientEngine? = null,
    private val uriReader: (Uri) -> ByteArray? = { null },
    private val ioDispatcher: CoroutineDispatcher = Dispatchers.IO,
    keyFactory: () -> String = { java.util.UUID.randomUUID().toString() },
    bootstrapOnInit: Boolean = true,
    private val voiceServices: VoiceServices? = null,
) : ViewModel() {
    constructor(application: Application) : this(
        sessionStore = SecureSessionStore(application),
        drafts = DraftStore(application),
        interviewDrafts = InterviewDraftStore(application),
        operationStore = SecureOperationStore(application),
        uriReader = { uri ->
            application.contentResolver.openInputStream(uri)?.use { it.readBytes() }
        },
        voiceServices = VoiceServices(
            recorder = VoiceRecorder(AndroidVoiceFileCipher(application)),
            player = QuestionPlayer(application),
            takes = VoiceTakeStore(application, cipher = AndroidVoiceFileCipher(application)),
            reviews = VoiceReviewStore(application),
            cacheRoot = application.cacheDir,
            fixtureProvider = {
                if (!BuildConfig.DEBUG) {
                    null
                } else {
                    val prefs = application.getSharedPreferences(
                        "mockmate_voice_test", android.content.Context.MODE_PRIVATE
                    )
                    if (!prefs.getBoolean("fixture_enabled", false)) {
                        null
                    } else {
                        val path = prefs.getString("fixture_path", null)
                        if (path == null) null else File(path).takeIf { it.exists() }
                    }
                }
            },
        ),
    )

    private val operations = OperationCoordinator(
        store = operationStore,
        keyFactory = keyFactory,
        ioDispatcher = ioDispatcher,
    )
    private val session = SessionManager(sessionStore)
    private val api = MockMateApi(session, BuildConfig.API_BASE_URL, httpEngine)
    private val repository = M1Repository(api)
    private val writePipeline = WritePipeline(operations)
    private val replayer = PendingOperationReplayer(
        operations,
        RepositoryOperationReplayExecutor(repository),
    )
    private val restoreService = RestoreJobService(object : RestorePorts {
        override suspend fun job(id: String) = repository.job(id)
        override suspend fun preparation(id: String) = repository.preparation(id)
        override suspend fun plan(id: String) = repository.plan(id)
        override suspend fun matchReport(id: String) = repository.matchReport(id)
        override suspend fun material(id: String) = repository.material(id)
        override suspend fun snapshots(materialId: String) = repository.snapshots(materialId)
        override suspend fun interview(sessionId: String) = repository.interview(sessionId)
        override suspend fun interviewReport(sessionId: String) = repository.interviewReport(sessionId)
    })
    private val restoreGate = RestoreUiGate(
        currentScope = { currentScope() },
        clearActiveJob = { accountId, jobId -> clearJob(accountId, jobId) },
    )
    private val restoreCoordinator = RestoreCoordinator(
        service = restoreService,
        gate = restoreGate,
        fetchJob = { jobId -> repository.job(jobId) },
        currentScope = { currentScope() },
    )
    private val jobWatch = JobWatch(
        scope = viewModelScope,
        coordinator = restoreCoordinator,
        isVisible = { appVisible },
        onEffect = { effect -> applyEffect(effect) },
        onStopped = { _, jobId -> clearOngoingStatus(jobId) },
    )

    val state = androidx.compose.runtime.mutableStateOf(AppState())
    private var loadToken = 0
    private val contextGeneration = ContextGeneration()
    private val interviewRecoveryLocks = java.util.concurrent.ConcurrentHashMap<String, Mutex>()
    private val confirmedInterviewTurns = java.util.concurrent.ConcurrentHashMap<String, Boolean>()
    private val inFlightMaterialActionKeys = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    private val supersededMaterialRecoveries = java.util.concurrent.ConcurrentHashMap.newKeySet<String>()
    private var voiceTimerJob: kotlinx.coroutines.Job? = null
    private var voiceStopJob: kotlinx.coroutines.Job? = null
    private var lastAutoPlayedTurnId: String? = null
    private var voicePlaybackGeneration: Long = 0L
    private var voicePollGeneration: Long = 0L
    private var voiceDraftLoadGeneration: Long = 0L
    private var voiceOperationGeneration: Long = 0L
    @Volatile
    private var appVisible = true

    val accountId: String?
        get() = session.current?.accountId

    private fun currentRequestContext(): RequestContext? =
        contextGeneration.current(session.current?.accountId, session.generation)

    private fun isAccountCurrent(ctx: RequestContext?): Boolean =
        contextGeneration.isAccountCurrent(ctx, session.current?.accountId, session.generation)

    private fun isCurrent(ctx: RequestContext?): Boolean =
        contextGeneration.isCurrent(ctx, session.current?.accountId, session.generation)

    /** 捕获发起写请求时的页面身份；回包只允许落到同一页面同一对象。 */
    private fun pageSnapshot(): RestoreScope? = currentScope()

    private fun isScopeCurrent(scope: RestoreScope?): Boolean {
        if (scope == null) return false
        val ctx = RequestContext(
            scope.accountId,
            scope.sessionGeneration,
            scope.navigationEpoch,
        )
        if (!isCurrent(ctx)) return false
        val current = currentScope() ?: return false
        if (current.screen != scope.screen) return false
        if (scope.preparationId != null && current.preparationId != scope.preparationId) return false
        if (scope.materialId != null && current.materialId != scope.materialId) return false
        if (scope.interviewSessionId != null && current.interviewSessionId != scope.interviewSessionId) {
            return false
        }
        if (scope.screenKey != null && current.screenKey != scope.screenKey) return false
        return true
    }

    /**
     * 一次语音操作冻结的身份。挂起返回后必须同时核对账号/导航/场次/回合/take/asset/
     * revision 与操作代次，成功、失败和迟到 401 都不例外。
     */
    private enum class VoiceOperationKind {
        /** 只绑定场次/回合，例如播报。 */
        SESSION,

        /**
         * 新 take 上传。旧 active 资产在服务端 CAS 成功前存在是正常状态，
         * 这里只核对本地待上传 take 仍是同一段录音；expected revision 在请求体内冻结。
         */
        UPLOAD_TAKE,

        /** 已激活资产上的 ASR/播报操作，必须核对 active asset 与 voice revision。 */
        ACTIVE_ASSET,
    }

    private data class VoiceOperation(
        val ctx: RequestContext,
        val page: RestoreScope,
        val sessionId: String,
        val turnId: String?,
        val takeId: String? = null,
        val assetId: String? = null,
        val voiceRevision: Int? = null,
        val kind: VoiceOperationKind = VoiceOperationKind.SESSION,
        val operationGeneration: Long,
    )

    private val voiceOperationScope: Long
        get() = voiceOperationGeneration

    private fun isVoiceSessionCurrent(op: VoiceOperation): Boolean {
        if (!isScopeCurrent(op.page)) return false
        val detail = state.value.interviewDetail ?: return false
        return detail.session_id == op.sessionId
    }

    private fun isVoiceTurnCurrent(op: VoiceOperation): Boolean {
        if (!isVoiceSessionCurrent(op)) return false
        if (op.turnId == null) return true
        return state.value.interviewDetail?.current_turn?.turn_id == op.turnId
    }

    private fun isVoiceOperationCurrent(
        op: VoiceOperation,
        pollGeneration: Long? = null,
    ): Boolean {
        if (op.operationGeneration != voiceOperationGeneration) return false
        if (pollGeneration != null && pollGeneration != voicePollGeneration) return false
        if (!isVoiceTurnCurrent(op)) return false
        val draft = state.value.voiceDraft
        if (op.kind == VoiceOperationKind.UPLOAD_TAKE) {
            // 重录替换：服务端旧 active take 仍存在是 CAS 成功前的正常状态。
            // 只要求本机待上传 take 未被新录音替换；旧资产是否等于新 take 不能作为发送前提。
            if (op.takeId != null && state.value.voiceTake?.takeId != op.takeId) return false
            return true
        }
        if (op.assetId != null && draft?.active_audio?.id != op.assetId) return false
        if (op.voiceRevision != null && draft?.voice_revision != op.voiceRevision) return false
        if (op.takeId != null) {
            val activeTake = draft?.active_audio?.take_id
            val localTake = state.value.voiceTake?.takeId
            if (activeTake != null && activeTake != op.takeId) return false
            if (activeTake == null && localTake != null && localTake != op.takeId) return false
        }
        return true
    }

    /** 本地已保存/已加载的核对内容；读取失败必须与“没有草稿”区分。 */
    private sealed interface StoredVoiceReview {
        data object Absent : StoredVoiceReview
        data class Present(val text: String) : StoredVoiceReview
        data class Unavailable(val message: String) : StoredVoiceReview
    }

    private fun storedVoiceReview(
        accountId: String,
        sessionId: String,
        turnId: String,
        assetId: String,
        takeId: String,
        transcriptRevision: Int,
    ): StoredVoiceReview {
        val reviews = voiceServices?.reviews ?: return StoredVoiceReview.Absent
        return when (val read = reviews.read(accountId, sessionId, turnId)) {
            is VoiceReviewRead.Present -> {
                val matches = read.draft.let {
                    it.sessionId == sessionId &&
                        it.turnId == turnId &&
                        it.assetId == assetId &&
                        it.takeId == takeId &&
                        it.transcriptRevision == transcriptRevision
                }
                if (matches) StoredVoiceReview.Present(read.draft.text)
                else StoredVoiceReview.Absent
            }
            is VoiceReviewRead.Corrupt -> StoredVoiceReview.Unavailable(
                "本机核对草稿损坏或无法解密：${read.message}。原始数据仍保留，当前显示原始转写；可重试读取或继续修改后显式保存。",
            )
            is VoiceReviewRead.Failed -> StoredVoiceReview.Unavailable(
                "本机核对草稿读取失败：${read.message}。原始数据仍保留，当前显示原始转写；可重试读取或继续修改后显式保存。",
            )
            VoiceReviewRead.Absent -> StoredVoiceReview.Absent
        }
    }

    /** 同一回合/资产/版本下，内存中已有人工修改时不得被远程/存储重读覆盖。 */
    private fun hasInMemoryReviewEdit(
        base: AppState,
        turnId: String?,
        activeAssetId: String?,
        voiceRevision: Int?,
        originalText: String,
    ): Boolean =
        base.voiceTurnId == turnId &&
            base.voiceDraft?.active_audio?.id == activeAssetId &&
            base.voiceDraft?.voice_revision == voiceRevision &&
            base.voiceReviewText.isNotBlank() &&
            base.voiceReviewText != originalText

    /** 把核对修改写入加密存储；失败时必须让用户看到，而不是只写日志或假装已保存。 */
    private fun persistVoiceReviewText(text: String) {
        val services = voiceServices ?: return
        val account = accountId ?: return
        val detail = state.value.interviewDetail ?: return
        val turn = detail.current_turn ?: return
        val draft = state.value.voiceDraft ?: return
        val asset = draft.active_audio ?: return
        val transcript = draft.transcript ?: return
        if (transcript.status != "ready") return
        val review = VoiceReviewDraft(
            sessionId = detail.session_id,
            turnId = turn.turn_id,
            takeId = asset.take_id,
            assetId = asset.id,
            transcriptRevision = transcript.revision,
            text = text,
            edited = text != transcript.text.orEmpty(),
        )
        when (val write = services.reviews.write(account, detail.session_id, turn.turn_id, review)) {
            is SecureWrite.Ok -> update {
                it.copy(voiceReviewStorageIssue = null, voiceReviewDirty = false)
            }
            is SecureWrite.Failed -> update {
                it.copy(
                    voiceReviewStorageIssue =
                        "核对修改未保存到本机：${write.message}。当前文本仍保留在本次会话中，可重试保存；离开页面或冷启动后可能丢失。",
                )
            }
        }
    }

    private fun clearVoiceReview(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String? = null,
    ) {
        voiceServices?.reviews?.clear(accountId, sessionId, turnId, takeId)
    }

    private fun bumpNavigation() {
        contextGeneration.bumpNavigation()
        voiceOperationGeneration += 1
        voiceDraftLoadGeneration += 1
        stopVoicePlaybackInternal()
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            viewModelScope.launch { finishVoiceRecordingInterrupted() }
        }
        // 页面/账号切换后旧订阅立即停止；回到目标页时由恢复路径重新建立唯一订阅。
        jobWatch.cancelAll()
        clearOngoingStatus()
    }

    fun setAppVisible(visible: Boolean) {
        appVisible = visible
        if (visible) {
            resumeInterviewAfterForeground()
            restoreActiveJobs()
            loadVoiceConfig()
        } else {
            stopVoicePlaybackInternal()
            if (state.value.voicePhase == VoicePhase.RECORDING) {
                viewModelScope.launch {
                    finishVoiceRecordingInterrupted()
                }
            }
            jobWatch.cancelAll()
            clearOngoingStatus()
        }
    }

    /** 回到前台：先重放当前场待确认的写请求，再 GET 最新详情。 */
    private fun resumeInterviewAfterForeground() {
        val ctx = currentRequestContext() ?: return
        when (val top = state.value.stack.lastOrNull()) {
            is Screen.InterviewRoom -> {
                if (isCurrent(ctx)) refreshInterviewSession(top.sessionId, includeReport = false)
            }
            is Screen.InterviewReport -> {
                if (isCurrent(ctx)) refreshInterviewSession(top.sessionId, includeReport = true)
            }
            else -> Unit
        }
    }

    init {
        if (bootstrapOnInit) {
            viewModelScope.launch { bootstrap() }
        } else {
            update { it.copy(booting = false) }
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun <T> okOrNull(result: ApiResult<T>): T? = (result as? ApiResult.Ok<T>)?.value

    private fun update(block: (AppState) -> AppState) {
        state.value = block(state.value)
    }

    private fun buildDiagnosticContext(page: RestoreScope? = currentScope()): String? {
        val s = page ?: return null
        return buildString {
            append("screen=${s.screenKey ?: s.screen}")
            s.preparationId?.let { append(" · prep=$it") }
            s.materialId?.let { append(" · material=$it") }
            s.interviewSessionId?.let { append(" · session=$it") }
            if (s.jobId.isNotBlank()) append(" · job=${s.jobId}")
        }
    }

    private fun showError(
        result: ApiResult.Err,
        ctx: RequestContext? = null,
        page: RestoreScope? = null,
    ) {
        if (result.code == "UNAUTHENTICATED") {
            if (ctx != null && !isAccountCurrent(ctx)) {
                // 旧账号的迟到 401：直接丢弃，不能注销或污染当前新账号。
                return
            }
            if (ctx == null) {
                // 登录页的凭据错误。
                update {
                    it.copy(
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(
                            result.message,
                            isError = true,
                            code = result.code,
                            details = result.details,
                            httpStatus = result.httpStatus,
                            retryable = result.retryable,
                            diagnosticContext = "screen=auth",
                        ),
                    )
                }
                return
            }
            session.signOut()
            bumpNavigation()
            update {
                it.copy(
                    account = null,
                    stack = listOf(Screen.Auth),
                    loading = false,
                    busyLabel = null,
                    message = UiMessage(
                        "登录已过期，请重新登录",
                        isError = true,
                        code = result.code,
                        details = result.details,
                        httpStatus = result.httpStatus,
                    ),
                )
            }
            return
        }
        if (ctx != null && !isAccountCurrent(ctx)) return
        // 旧页面的错误不能显示在新页面；服务和账号状态已由原请求处理。
        if (page != null && !isScopeCurrent(page)) return
        update {
            it.copy(
                loading = false,
                busyLabel = null,
                message = UiMessage(
                    result.message,
                    isError = true,
                    code = result.code,
                    details = result.details,
                    httpStatus = result.httpStatus,
                    retryable = result.retryable,
                    diagnosticContext = buildDiagnosticContext(page ?: currentScope()),
                ),
            )
        }
    }

    private fun showInfo(text: String) {
        update { it.copy(loading = false, busyLabel = null, message = UiMessage(text, isError = false)) }
    }

    private suspend fun <T> performWrite(
        ctx: RequestContext,
        type: String,
        payload: OperationPayload,
        targetId: String? = null,
        revision: Int? = null,
        preparationId: String? = null,
        interviewSessionId: String? = null,
        fileName: String? = null,
        fileSha256: String? = null,
        fileMime: String? = null,
        onResult: suspend (T) -> OperationSave = { OperationSave.Ok },
        call: suspend (String) -> ApiResult<T>,
    ): ApiResult<T> = writePipeline.perform(
        accountId = ctx.accountId,
        type = type,
        payload = payload,
        targetId = targetId,
        revision = revision,
        preparationId = preparationId,
        interviewSessionId = interviewSessionId,
        fileName = fileName,
        fileSha256 = fileSha256,
        fileMime = fileMime,
        isContextCurrent = { isAccountCurrent(ctx) },
        onJob = { job -> recordJobResult(ctx.accountId, job, preparationId) },
        onResult = onResult,
        call = call,
    )

    private suspend fun recordJobResult(
        accountId: String,
        job: JobDto,
        preparationId: String?,
    ): OperationSave = operations.recordActiveJob(
        accountId,
        ActiveJob(
            jobId = job.id,
            kind = job.kind,
            targetId = job.target_id,
            preparationId = preparationId,
            revision = job.input_revision,
        ),
    )

    private suspend fun clearJob(accountId: String, jobId: String) {
        operations.completeActiveJob(accountId, jobId)
    }

    fun clearMessage() = update { it.copy(message = null) }

    private fun clearOngoingStatus(jobId: String? = null) {
        update { current ->
            val existing = current.ongoingStatus ?: return@update current
            if (jobId == null || existing.jobId == jobId) {
                current.copy(ongoingStatus = null)
            } else {
                current
            }
        }
    }

    private fun ongoingStatusText(jobKind: String, fallback: String): String = when (jobKind) {
        "extract" -> "正在提取文件文本；本页只查询状态，不会重复提交"
        "parse" -> "正在 AI 解析；本页只查询状态，不会重复提交"
        "match" -> "正在匹配分析；本页只查询状态，不会重复提交"
        "plan" -> "正在生成计划；本页只查询状态，不会重复提交"
        "follow_up" -> "正在判定追问；本页只查询状态，不会重复提交"
        "interview_report" -> "正在生成面试报告；本页只查询状态，不会重复提交"
        else -> fallback
    }

    // ------------------------------------------------------------ 启动与会话

    private suspend fun bootstrap() {
        val restored = session.restore()
        if (restored == null) {
            update { it.copy(booting = false, stack = listOf(Screen.Auth)) }
            return
        }
        val gen = session.generation
        val me = repository.me()
        if (session.generation != gen) return
        when (me) {
            is ApiResult.Ok -> {
                val user = me.value
                session.signIn(user.id, user.username, restored.token)
                bumpNavigation()
                update {
                    it.copy(
                        booting = false,
                        account = Account(user.id, user.username),
                        stack = listOf(Screen.PreparationList),
                        message = null,
                    )
                }
                refreshCatalog()
                refreshPreparations()
                restoreOperations(user.id)
            }
            is ApiResult.Err -> {
                if (me.code == "UNAUTHENTICATED" || me.httpStatus == 401) {
                    session.signOut()
                    bumpNavigation()
                    update { it.copy(booting = false, stack = listOf(Screen.Auth)) }
                    showInfo("会话已失效，请重新登录")
                } else {
                    // 断网或服务端暂时不可达不等于账号失效：保留 token 与账号，显示未同步。
                    update {
                        it.copy(
                            booting = false,
                            account = Account(restored.accountId, restored.username),
                            stack = listOf(Screen.PreparationList),
                            message = UiMessage(
                                "网络不可用，已保留登录状态；联网后可刷新，未清除本地会话",
                                isError = false,
                                code = me.code,
                            ),
                        )
                    }
                }
            }
        }
    }

    fun authenticate(register: Boolean, username: String, password: String) {
        if (username.isBlank() || password.isBlank()) {
            update { it.copy(message = UiMessage("请输入用户名和密码")) }
            return
        }
        val gen = session.generation
        update { it.copy(loading = true, busyLabel = if (register) "注册中…" else "登录中…") }
        viewModelScope.launch {
            if (register) {
                when (val result = repository.register(username.trim(), password)) {
                    is ApiResult.Err -> showError(result)
                    is ApiResult.Ok -> {
                        val login = repository.login(username.trim(), password)
                        if (session.generation != gen) return@launch
                        when (login) {
                            is ApiResult.Err -> showError(login)
                            is ApiResult.Ok -> afterLogin(login.value.access_token, login.value.user)
                        }
                    }
                }
            } else {
                when (val result = repository.login(username.trim(), password)) {
                    is ApiResult.Err -> showError(result)
                    is ApiResult.Ok -> {
                        if (session.generation != gen) return@launch
                        afterLogin(result.value.access_token, result.value.user)
                    }
                }
            }
        }
    }

    private fun afterLogin(token: String, user: UserDto) {
        session.signIn(user.id, user.username, token)
        bumpNavigation()
        update {
            it.copy(
                loading = false,
                busyLabel = null,
                account = Account(user.id, user.username),
                stack = listOf(Screen.PreparationList),
                preparations = emptyList(),
                materials = emptyList(),
                preparation = null,
                plan = null,
                message = null,
            )
        }
        refreshCatalog()
        refreshPreparations()
        loadVoiceConfig()
        restoreOperations(user.id)
    }

    fun logout() {
        val old = session.current ?: return
        stopVoicePlaybackInternal()
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            voiceTimerJob?.cancel()
            voiceTimerJob = null
            voiceServices?.recorder?.stop(interrupted = true)
        }
        // 先使旧账号代数失效、取消在途请求/轮询并清空可见私有状态；
        // 旧 401 或旧 logout 回包不能注销新账号。
        session.signOut()
        bumpNavigation()
        viewModelScope.coroutineContext.cancelChildren()
        update {
            AppState(
                booting = false,
                account = null,
                stack = listOf(Screen.Auth),
                roles = emptyList(),
                templates = emptyList(),
            )
        }
        viewModelScope.launch {
            repository.logout(old.token)
        }
    }

    private fun refreshCatalog() {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            val roles = repository.roles()
            if (!isAccountCurrent(ctx)) return@launch
            if (roles is ApiResult.Ok) update { it.copy(roles = roles.value) }
        }
    }

    fun refreshSkillCodes() {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        if (page.screen != RestoreJobService.SCREEN_REVIEW) return
        loadSkillCodesForScope(ctx, page)
    }

    private fun loadSkillCodesForScope(ctx: RequestContext, page: RestoreScope) {
        update {
            if (!isScopeCurrent(page)) it
            else it.copy(
                skillCatalogStatus = SkillCatalogStatus.LOADING,
                skillCatalogError = null,
            )
        }
        viewModelScope.launch {
            when (val result = repository.skillCodes()) {
                is ApiResult.Ok -> if (isScopeCurrent(page) && isAccountCurrent(ctx)) {
                    update {
                        if (!isScopeCurrent(page)) it
                        else it.copy(
                            skillCodes = result.value,
                            skillCatalogStatus = SkillCatalogStatus.LOADED,
                            skillCatalogError = null,
                        )
                    }
                }
                is ApiResult.Err -> if (isScopeCurrent(page) && isAccountCurrent(ctx)) {
                    if (result.code == "UNAUTHENTICATED") {
                        showError(result, ctx, page)
                    } else {
                        update {
                            if (!isScopeCurrent(page)) it
                            else it.copy(
                                skillCatalogStatus = SkillCatalogStatus.FAILED,
                                skillCatalogError = result.message,
                            )
                        }
                    }
                }
            }
        }
    }

    fun clearInvalidSkillHighlight() {
        update { it.copy(invalidSkillItemId = null, invalidSkillField = null) }
    }

    fun refreshTemplates() {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            when (val result = repository.jdTemplates()) {
                is ApiResult.Ok -> if (isAccountCurrent(ctx)) update { it.copy(templates = result.value) }
                is ApiResult.Err -> showError(result, ctx)
            }
        }
    }

    // ------------------------------------------------------------ 导航

    fun back(): Boolean {
        val stack = state.value.stack
        if (stack.size <= 1) return false
        if (stack.lastOrNull() is Screen.InterviewRoom) {
            stopVoicePlaybackInternal()
            if (state.value.voicePhase == VoicePhase.RECORDING) {
                viewModelScope.launch { finishVoiceRecordingInterrupted() }
            }
        }
        bumpNavigation()
        update {
            it.copy(
                stack = stack.dropLast(1),
                message = null,
                loading = false,
                busyLabel = null,
            )
        }
        // 回到旧页面时重新读取其当前服务端状态；旧操作的迟到回包已被页面代数挡住。
        when (val top = stack[stack.size - 2]) {
            is Screen.InterviewRoom -> loadInterview(top.sessionId)
            is Screen.InterviewReport -> refreshInterviewSession(top.sessionId, includeReport = true)
            else -> Unit
        }
        return true
    }

    private fun push(screen: Screen) {
        bumpNavigation()
        update {
            it.copy(
                stack = it.stack + screen,
                message = null,
                loading = false,
                busyLabel = null,
            )
        }
    }

    private fun replaceTop(screen: Screen) {
        bumpNavigation()
        update {
            it.copy(
                stack = it.stack.dropLast(1) + screen,
                message = null,
                loading = false,
                busyLabel = null,
            )
        }
    }

    private fun popTo(predicate: (Screen) -> Boolean) {
        bumpNavigation()
        update { current ->
            val index = current.stack.indexOfLast(predicate)
            if (index < 0) current else current.copy(
                stack = current.stack.take(index + 1),
                loading = false,
                busyLabel = null,
            )
        }
    }

    // ------------------------------------------------------------ 准备列表

    fun refreshPreparations() {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            when (val result = repository.preparations()) {
                is ApiResult.Ok -> if (isAccountCurrent(ctx)) update { it.copy(preparations = result.value) }
                is ApiResult.Err -> showError(result, ctx)
            }
        }
    }

    fun openPreparation(preparationId: String) {
        val token = ++loadToken
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "加载准备…") }
        viewModelScope.launch {
            when (val result = repository.preparation(preparationId)) {
                is ApiResult.Err -> if (loadToken == token) showError(result, ctx)
                is ApiResult.Ok -> {
                    if (loadToken != token || !isCurrent(ctx)) return@launch
                    update {
                        it.copy(
                            preparation = result.value,
                            loading = false,
                            busyLabel = null,
                            plan = null,
                            matchReport = null,
                            job = null,
                            resumeSnapshot = null,
                            jdSnapshot = null,
                            interviewForPlan = null,
                        )
                    }
                    loadSnapshotLabels(result.value, ctx)
                    if (isCurrent(ctx)) push(Screen.Setup(preparationId))
                }
            }
        }
    }

    fun startNewPreparation() {
        update {
            it.copy(
                preparation = null,
                plan = null,
                matchReport = null,
                job = null,
                interviewForPlan = null,
            )
        }
        push(Screen.Setup(null))
    }

    fun createPreparation(roleId: String, level: String, count: Int) {
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "创建准备…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "createPreparation",
                CreatePreparationPayload(roleId, level, count),
                targetId = null,
                revision = null,
            ) { key ->
                repository.createPreparation(roleId, level, count, null, null, key)
            }
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    if (!isAccountCurrent(ctx)) return@launch
                    update {
                        it.copy(
                            preparation = result.value,
                            loading = false,
                            busyLabel = null,
                            message = UiMessage("准备已创建，请选择并核对简历与 JD", isError = false),
                        )
                    }
                    replaceTop(Screen.Setup(result.value.id))
                    refreshPreparations()
                }
            }
        }
    }

    fun updatePreparation(roleId: String, level: String, count: Int) {
        val prep = state.value.preparation ?: return
        val resumeId = state.value.resumeSnapshot?.id ?: prep.resume_snapshot_id
        val jdId = state.value.jdSnapshot?.id ?: prep.jd_snapshot_id
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "保存设置…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "editPreparation",
                EditPreparationPayload(prep.revision, roleId, level, count, resumeId, jdId),
                targetId = prep.id,
                revision = prep.revision,
            ) { key ->
                repository.editPreparation(
                    prep.id, prep.revision, roleId, level, count, resumeId, jdId, key,
                )
            }
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    if (!isAccountCurrent(ctx)) return@launch
                    update {
                        it.copy(
                            preparation = result.value,
                            loading = false,
                            busyLabel = null,
                            matchReport = null,
                            plan = null,
                            message = UiMessage("设置已保存", isError = false),
                        )
                    }
                    refreshPreparations()
                }
            }
        }
    }

    fun deletePreparation(preparationId: String) {
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "删除准备…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "deletePreparation",
                DeletePreparationPayload,
                targetId = preparationId,
                revision = 0,
            ) { key -> repository.deletePreparation(preparationId, key) }
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    if (!isAccountCurrent(ctx)) return@launch
                    bumpNavigation()
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            preparation = null,
                            plan = null,
                            stack = listOf(Screen.PreparationList),
                            message = UiMessage("准备已删除", isError = false),
                        )
                    }
                    refreshPreparations()
                }
            }
        }
    }

    private suspend fun loadSnapshotLabels(prep: PreparationDto, ctx: RequestContext) {
        val resume = prep.resume_snapshot_id?.let { repository.snapshot(it) }
        val jd = prep.jd_snapshot_id?.let { repository.snapshot(it) }
        if (!isCurrent(ctx)) return
        update { current ->
            if (current.preparation?.id != prep.id) return@update current
            current.copy(
                resumeSnapshot = resume?.let { okOrNull(it) },
                jdSnapshot = jd?.let { okOrNull(it) },
            )
        }
    }

    // ------------------------------------------------------------ 材料库与编辑

    fun openLibrary(kind: String, preparationId: String) {
        push(Screen.Library(kind, preparationId))
        refreshMaterials()
    }

    fun openTemplates(preparationId: String) {
        push(Screen.Templates(preparationId))
        refreshTemplates()
    }

    fun notify(text: String, isError: Boolean = true) {
        update { it.copy(message = UiMessage(text, isError = isError), loading = false, busyLabel = null) }
    }

    fun extractMaterial(materialId: String) {
        val material = state.value.material
        if (material == null || material.id != materialId) {
            notify("材料尚未加载完成")
            return
        }
        val policy = com.mockmate.app.ui.screens.ReviewScreenPolicy.evaluate(
            material,
            state.value.job,
            state.value.materialJobLookup,
        )
        if (!policy.showExtractAction) return
        val actionKey = "extract:$materialId:${material.revision}"
        if (!inFlightMaterialActionKeys.add(actionKey)) return
        val ctx = currentRequestContext() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val page = pageSnapshot() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val entry = scopeForJob("extract", null, materialId) ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        update { it.copy(loading = true, busyLabel = "提取文件文本…", message = null) }
        viewModelScope.launch {
            try {
                val job = performWrite(
                    ctx,
                    "extractMaterial",
                    ExtractMaterialPayload(material.revision),
                    targetId = materialId,
                    revision = material.revision,
                ) { key -> repository.extractMaterial(materialId, material.revision, key) }
                if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
                when (job) {
                    is ApiResult.Err -> showError(job, ctx, page)
                    is ApiResult.Ok -> {
                        if (!syncMaterialWriteReceipt(ctx, entry, "extract", job.value)) return@launch
                        applyEffect(
                            restoreCoordinator.project(
                                entry.copy(jobId = job.value.id, jobAttempt = job.value.attempt),
                                job.value,
                            ),
                        )
                    }
                }
            } finally {
                inFlightMaterialActionKeys.remove(actionKey)
            }
        }
    }

    fun refreshMaterials() {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            when (val result = repository.materials()) {
                is ApiResult.Ok -> if (isAccountCurrent(ctx)) update { it.copy(materials = result.value) }
                is ApiResult.Err -> showError(result, ctx)
            }
        }
    }

    fun openEditor(kind: String, materialId: String?, preparationId: String) {
        update { it.copy(material = null, materialJobLookup = null, snapshots = emptyList()) }
        push(Screen.Editor(kind, materialId, preparationId))
        val ctx = currentRequestContext() ?: return
        if (materialId != null) {
            viewModelScope.launch {
                when (val result = repository.material(materialId)) {
                    is ApiResult.Ok -> if (isCurrent(ctx)) update { it.copy(material = result.value) }
                    is ApiResult.Err -> showError(result, ctx)
                }
            }
        }
    }

    fun openReview(materialId: String, preparationId: String) {
        update {
            it.copy(
                material = null,
                materialJobLookup = null,
                snapshots = emptyList(),
                job = null,
                invalidSkillItemId = null,
                invalidSkillField = null,
            )
        }
        val token = ++loadToken
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "加载材料…") }
        viewModelScope.launch {
            val material = repository.material(materialId)
            val snapshots = repository.snapshots(materialId)
            if (loadToken != token || !isCurrent(ctx)) return@launch
            if (material is ApiResult.Err) {
                showError(material, ctx)
                return@launch
            }
            val loadedMaterial = (material as ApiResult.Ok).value
            val latestJobId = loadedMaterial.latest_job_id
            val initialLookup = latestJobId?.let { jobId ->
                MaterialJobLookup(
                    materialId = loadedMaterial.id,
                    revision = loadedMaterial.revision,
                    jobId = jobId,
                    status = MaterialJobLookupStatus.LOADING,
                )
            }
            update {
                it.copy(
                    material = loadedMaterial,
                    materialJobLookup = initialLookup,
                    snapshots = okOrNull(snapshots) ?: emptyList(),
                    loading = false,
                    busyLabel = null,
                )
            }
            push(Screen.Review(materialId, preparationId))
            refreshSkillCodes()
            if (latestJobId == null) return@launch
            val reviewScope = currentScope()?.copy(materialRevision = loadedMaterial.revision) ?: return@launch
            fetchAndApplyLatestMaterialJob(reviewScope, loadedMaterial, latestJobId)
        }
    }

    private fun materialLookupKey(
        scope: RestoreScope,
        material: MaterialDraftDto,
        jobId: String,
    ): String =
        "lookup:${scope.accountId}:${scope.sessionGeneration}:${scope.navigationEpoch}:${material.id}:${material.revision}:$jobId"

    /** 核对页任务状态查询失败后的只读查询入口：只 GET，不重新 POST。 */
    fun queryLatestMaterialJob(materialId: String) {
        val material = state.value.material?.takeIf { it.id == materialId } ?: return
        val latestJobId = material.latest_job_id ?: return
        val reviewScope = currentScope()?.takeIf {
            it.screen == RestoreJobService.SCREEN_REVIEW && it.materialId == materialId
        }?.copy(materialRevision = material.revision) ?: return
        val lookupKey = materialLookupKey(reviewScope, material, latestJobId)
        if (!inFlightMaterialActionKeys.add(lookupKey)) return
        update { current ->
            if (current.material?.id == material.id &&
                current.material.revision == material.revision
            ) {
                current.copy(
                    materialJobLookup = MaterialJobLookup(
                        materialId = material.id,
                        revision = material.revision,
                        jobId = latestJobId,
                        status = MaterialJobLookupStatus.LOADING,
                    ),
                )
            } else {
                current
            }
        }
        viewModelScope.launch {
            fetchAndApplyLatestMaterialJob(
                reviewScope = reviewScope,
                expectedMaterial = material,
                latestJobId = latestJobId,
                lookupKeyAcquired = true,
            )
        }
    }

    private suspend fun fetchAndApplyLatestMaterialJob(
        reviewScope: RestoreScope,
        expectedMaterial: MaterialDraftDto,
        latestJobId: String,
        lookupKeyAcquired: Boolean = false,
        requireCurrentRevision: Boolean = false,
    ) {
        val lookupKey = materialLookupKey(reviewScope, expectedMaterial, latestJobId)
        if (!lookupKeyAcquired && !inFlightMaterialActionKeys.add(lookupKey)) return
        try {
            val jobResult = repository.job(latestJobId)
            if (!isScopeCurrent(reviewScope)) return
            val currentMaterial = state.value.material
            if (currentMaterial == null ||
                currentMaterial.id != expectedMaterial.id ||
                currentMaterial.revision != expectedMaterial.revision
            ) {
                return
            }
            when (jobResult) {
                is ApiResult.Err -> {
                    update { current ->
                        val mat = current.material
                        val canMarkFailed = current.job == null ||
                            current.job.id != latestJobId ||
                            current.job.state !in setOf("failed", "interrupted", "superseded")
                        if (mat != null &&
                            mat.id == currentMaterial.id &&
                            mat.revision == currentMaterial.revision &&
                            mat.latest_job_id == latestJobId &&
                            canMarkFailed
                        ) {
                            current.copy(
                                materialJobLookup = MaterialJobLookup(
                                    materialId = mat.id,
                                    revision = mat.revision,
                                    jobId = latestJobId,
                                    status = MaterialJobLookupStatus.FAILED,
                                    errorMessage = ErrorDiagnosticsMapper.sanitizeRawServerMessage(
                                        jobResult.message,
                                        jobResult.code,
                                    ),
                                ),
                            )
                        } else {
                            current
                        }
                    }
                }
                is ApiResult.Ok -> {
                    val restoredJob = jobResult.value
                    val matchesMaterial = restoredJob.target_id == currentMaterial.id &&
                        if (requireCurrentRevision) {
                            restoredJob.input_revision == currentMaterial.revision
                        } else {
                            restoredJob.input_revision == currentMaterial.revision ||
                                restoredJob.state == "superseded"
                        }
                    if (!matchesMaterial) {
                        update { current ->
                            if (current.material?.id == currentMaterial.id &&
                                current.material.revision == currentMaterial.revision
                            ) {
                                current.copy(
                                    materialJobLookup = MaterialJobLookup(
                                        materialId = currentMaterial.id,
                                        revision = currentMaterial.revision,
                                        jobId = latestJobId,
                                        status = MaterialJobLookupStatus.LOADED,
                                    ),
                                )
                            } else {
                                current
                            }
                        }
                        return
                    }
                    if (!InterviewUiMerge.shouldApplyJob(state.value.job, restoredJob)) {
                        update { current ->
                            if (current.material?.id == currentMaterial.id &&
                                current.material.revision == currentMaterial.revision &&
                                current.materialJobLookup?.status == MaterialJobLookupStatus.LOADING
                            ) {
                                current.copy(
                                    materialJobLookup = MaterialJobLookup(
                                        materialId = currentMaterial.id,
                                        revision = currentMaterial.revision,
                                        jobId = latestJobId,
                                        status = MaterialJobLookupStatus.LOADED,
                                    ),
                                )
                            } else {
                                current
                            }
                        }
                        return
                    }
                    val scopedEntry = reviewScope.copy(
                        jobId = restoredJob.id,
                        materialRevision = currentMaterial.revision,
                        jobAttempt = restoredJob.attempt,
                    )
                    when (restoredJob.state) {
                        "queued", "running" -> {
                            applyEffect(restoreCoordinator.project(scopedEntry, restoredJob))
                        }
                        "failed", "interrupted" -> {
                            update { current ->
                                if (current.material?.id == currentMaterial.id &&
                                    current.material.revision == currentMaterial.revision &&
                                    InterviewUiMerge.shouldApplyJob(current.job, restoredJob)
                                ) {
                                    current.copy(
                                        job = restoredJob,
                                        ongoingStatus = if (current.ongoingStatus?.jobId == restoredJob.id) {
                                            null
                                        } else {
                                            current.ongoingStatus
                                        },
                                        materialJobLookup = MaterialJobLookup(
                                            materialId = currentMaterial.id,
                                            revision = currentMaterial.revision,
                                            jobId = latestJobId,
                                            status = MaterialJobLookupStatus.LOADED,
                                        ),
                                    )
                                } else {
                                    current
                                }
                            }
                            jobWatch.cancel(reviewScope.accountId, restoredJob.id)
                        }
                        "superseded" -> {
                            if (restoredJob.input_revision >= currentMaterial.revision) {
                                // 当前版本的任务已失效：保留已显示原文，只读回读材料与快照。
                                rereadMaterialAfterSuperseded(
                                    reviewScope = reviewScope,
                                    expectedMaterial = currentMaterial,
                                    supersededJob = restoredJob,
                                )
                            } else {
                                update { current ->
                                    if (current.material?.id == currentMaterial.id &&
                                        current.material.revision == currentMaterial.revision &&
                                        InterviewUiMerge.shouldApplyJob(current.job, restoredJob)
                                    ) {
                                        current.copy(
                                            job = restoredJob,
                                            ongoingStatus = if (current.ongoingStatus?.jobId == restoredJob.id) {
                                                null
                                            } else {
                                                current.ongoingStatus
                                            },
                                            materialJobLookup = MaterialJobLookup(
                                                materialId = currentMaterial.id,
                                                revision = currentMaterial.revision,
                                                jobId = latestJobId,
                                                status = MaterialJobLookupStatus.LOADED,
                                            ),
                                        )
                                    } else {
                                        current
                                    }
                                }
                                jobWatch.cancel(reviewScope.accountId, restoredJob.id)
                            }
                        }
                        "succeeded" -> {
                            val latestMaterial = state.value.material?.takeIf {
                                it.id == currentMaterial.id && it.revision == currentMaterial.revision
                            } ?: return
                            val needsMaterialRefresh = restoredJob.input_revision == latestMaterial.revision &&
                                ((restoredJob.kind == "parse" && latestMaterial.profile == null) ||
                                    (restoredJob.kind == "extract" && latestMaterial.text.isNullOrBlank()))
                            if (needsMaterialRefresh) {
                                // 材料正文或结构化结果尚未到达前保持 LOADING，失败时由 Error 分支切为 FAILED。
                                update { current ->
                                    if (current.material?.id == latestMaterial.id &&
                                        current.material.revision == latestMaterial.revision &&
                                        InterviewUiMerge.shouldApplyJob(current.job, restoredJob)
                                    ) {
                                        current.copy(job = restoredJob)
                                    } else {
                                        current
                                    }
                                }
                                applyEffect(restoreCoordinator.project(scopedEntry, restoredJob))
                            } else {
                                update { current ->
                                    if (current.material?.id == latestMaterial.id &&
                                        current.material.revision == latestMaterial.revision &&
                                        InterviewUiMerge.shouldApplyJob(current.job, restoredJob)
                                    ) {
                                        current.copy(
                                            job = restoredJob,
                                            ongoingStatus = if (current.ongoingStatus?.jobId == restoredJob.id) {
                                                null
                                            } else {
                                                current.ongoingStatus
                                            },
                                            materialJobLookup = MaterialJobLookup(
                                                materialId = latestMaterial.id,
                                                revision = latestMaterial.revision,
                                                jobId = latestJobId,
                                                status = MaterialJobLookupStatus.LOADED,
                                            ),
                                        )
                                    } else {
                                        current
                                    }
                                }
                                jobWatch.cancel(reviewScope.accountId, restoredJob.id)
                            }
                        }
                    }
                }
            }
        } finally {
            inFlightMaterialActionKeys.remove(lookupKey)
        }
    }

    /**
     * 当前核对页所持版本的提取／解析任务变为 superseded 时，只读回读材料与快照。
     * 回读期间保留已显示原文并暂停会使用旧 revision 的操作；取得更高版本后复用
     * 现有 Job 查询入口，仍指向已知旧版本的任务指针则直接结束恢复。
     */
    private suspend fun rereadMaterialAfterSuperseded(
        reviewScope: RestoreScope,
        expectedMaterial: MaterialDraftDto,
        supersededJob: JobDto,
    ) {
        if (supersededJob.kind !in setOf("extract", "parse")) return
        if (!isScopeCurrent(reviewScope)) return
        val currentBefore = state.value.material
        if (currentBefore == null || currentBefore.id != expectedMaterial.id) return
        // 当前版本已同步到更新版本时不再回读；旧的失效任务指针已在本页解析过时也不重复读取。
        if (currentBefore.revision > expectedMaterial.revision) return
        val resolvedLookup = state.value.materialJobLookup
        if (state.value.job == null &&
            resolvedLookup?.status == MaterialJobLookupStatus.LOADED &&
            resolvedLookup.materialId == expectedMaterial.id &&
            resolvedLookup.revision == expectedMaterial.revision &&
            resolvedLookup.jobId == supersededJob.id
        ) {
            return
        }
        // 在途去重必须随页面切换：同一页重复触发合并，退出后新页面可以接管自己的回读。
        val recoveryKey =
            "superseded:${reviewScope.accountId}:${reviewScope.sessionGeneration}:" +
                "${reviewScope.navigationEpoch}:${expectedMaterial.id}:" +
                "${expectedMaterial.revision}:${supersededJob.id}"
        if (!supersededMaterialRecoveries.add(recoveryKey)) return
        try {
            update { current ->
                val mat = current.material
                if (mat != null &&
                    mat.id == expectedMaterial.id &&
                    mat.revision == expectedMaterial.revision
                ) {
                    current.copy(
                        job = null,
                        materialJobLookup = MaterialJobLookup(
                            materialId = mat.id,
                            revision = mat.revision,
                            jobId = supersededJob.id,
                            status = MaterialJobLookupStatus.LOADING,
                        ),
                    )
                } else {
                    current
                }
            }
            val materialResult = repository.material(expectedMaterial.id)
            val snapshotsResult = repository.snapshots(expectedMaterial.id)
            if (!isScopeCurrent(reviewScope)) return
            val currentMaterial = state.value.material
            if (currentMaterial == null ||
                currentMaterial.id != expectedMaterial.id ||
                currentMaterial.revision != expectedMaterial.revision
            ) {
                return
            }
            if (materialResult is ApiResult.Err) {
                markMaterialRereadFailed(expectedMaterial, supersededJob, materialResult)
                return
            }
            if (snapshotsResult is ApiResult.Err) {
                markMaterialRereadFailed(expectedMaterial, supersededJob, snapshotsResult)
                return
            }
            val reread = (materialResult as ApiResult.Ok).value
            if (reread.id != expectedMaterial.id ||
                reread.revision < expectedMaterial.revision
            ) {
                markMaterialRereadFailed(
                    expectedMaterial,
                    supersededJob,
                    ApiResult.Err("MATERIAL_MISMATCH", "回读到的材料版本不匹配", false),
                )
                return
            }
            val nextJobId = reread.latest_job_id
            val stalePointer = nextJobId == supersededJob.id &&
                reread.revision > expectedMaterial.revision
            val unresolvedPointer = nextJobId == supersededJob.id &&
                reread.revision == expectedMaterial.revision
            val nextLookup = when {
                nextJobId == null -> null
                stalePointer || unresolvedPointer -> MaterialJobLookup(
                    materialId = reread.id,
                    revision = reread.revision,
                    jobId = nextJobId,
                    status = MaterialJobLookupStatus.LOADED,
                )
                else -> MaterialJobLookup(
                    materialId = reread.id,
                    revision = reread.revision,
                    jobId = nextJobId,
                    status = MaterialJobLookupStatus.LOADING,
                )
            }
            update { current ->
                if (current.material?.id == expectedMaterial.id &&
                    current.material.revision == expectedMaterial.revision
                ) {
                    current.copy(
                        material = reread,
                        snapshots = okOrNull(snapshotsResult) ?: current.snapshots,
                        job = null,
                        ongoingStatus = if (current.ongoingStatus?.jobId == supersededJob.id) {
                            null
                        } else {
                            current.ongoingStatus
                        },
                        materialJobLookup = nextLookup,
                        loading = false,
                        busyLabel = null,
                    )
                } else {
                    current
                }
            }
            if (nextLookup != null &&
                nextLookup.status == MaterialJobLookupStatus.LOADING &&
                nextJobId != null
            ) {
                fetchAndApplyLatestMaterialJob(
                    reviewScope = reviewScope.copy(
                        materialId = reread.id,
                        materialRevision = reread.revision,
                        jobId = nextJobId,
                        jobAttempt = null,
                    ),
                    expectedMaterial = reread,
                    latestJobId = nextJobId,
                    requireCurrentRevision = true,
                )
            }
            jobWatch.cancel(reviewScope.accountId, supersededJob.id)
        } finally {
            // 成功、失败、取消或被页面切换丢弃都释放本页的在途键，避免长期挡住后续页面。
            supersededMaterialRecoveries.remove(recoveryKey)
        }
    }

    private fun markMaterialRereadFailed(
        expectedMaterial: MaterialDraftDto,
        supersededJob: JobDto,
        error: ApiResult.Err,
    ) {
        update { current ->
            val mat = current.material
            if (mat != null &&
                mat.id == expectedMaterial.id &&
                mat.revision == expectedMaterial.revision
            ) {
                current.copy(
                    job = null,
                    loading = false,
                    busyLabel = null,
                    materialJobLookup = MaterialJobLookup(
                        materialId = mat.id,
                        revision = mat.revision,
                        jobId = supersededJob.id,
                        status = MaterialJobLookupStatus.FAILED,
                        errorMessage = ErrorDiagnosticsMapper.sanitizeRawServerMessage(
                            error.message,
                            error.code,
                        ),
                    ),
                )
            } else {
                current
            }
        }
    }

    fun saveTextMaterial(kind: String, materialId: String?, title: String, text: String, preparationId: String) {
        if (title.isBlank() || text.isBlank()) {
            update { it.copy(message = UiMessage("标题和正文不能为空")) }
            return
        }
        val ctx = currentRequestContext() ?: return
        val revision = if (materialId == null) null else state.value.material?.revision
        if (materialId != null && revision == null) {
            update { it.copy(message = UiMessage("材料版本未知，请重新打开")) }
            return
        }
        val payload: OperationPayload = if (materialId == null) {
            CreateTextMaterialPayload(kind, title.trim(), text)
        } else {
            EditMaterialPayload(revision!!, title.trim(), text)
        }
        update { it.copy(loading = true, busyLabel = "保存材料…") }
        viewModelScope.launch {
            if (materialId != null) {
                val pending = operations.pendingFor(ctx.accountId, "editMaterial", materialId)
                if (pending != null && pending.payload != payload) {
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            message = UiMessage(
                                "上一次修改结果未知；请先重试原内容或确认服务端状态后再修改正文",
                                isError = true,
                                code = "PENDING_OPERATION",
                            ),
                        )
                    }
                    return@launch
                }
            }
            val result = if (materialId == null) {
                performWrite(
                    ctx,
                    "createTextMaterial",
                    payload,
                    targetId = null,
                    revision = null,
                    preparationId = preparationId,
                ) { key -> repository.createTextMaterial(kind, title.trim(), text, operationKey = key) }
            } else {
                performWrite(
                    ctx,
                    "editMaterial",
                    payload,
                    targetId = materialId,
                    revision = revision,
                    preparationId = preparationId,
                ) { key -> repository.editMaterial(materialId, revision!!, title.trim(), text, key) }
            }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    drafts.clear(ctx.accountId, "editor-$kind")
                    drafts.clear(ctx.accountId, "editor-title-$kind")
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            job = null,
                            materialJobLookup = null,
                            invalidSkillItemId = null,
                            invalidSkillField = null,
                        )
                    }
                    val saved = result.value
                    replaceTop(Screen.Review(saved.id, preparationId))
                    refreshSkillCodes()
                    refreshMaterials()
                    val screenRequestContext = currentRequestContext() ?: return@launch
                    val token = ++loadToken
                    update { it.copy(loading = true, busyLabel = "加载材料…") }
                    val material = repository.material(saved.id)
                    val snapshots = repository.snapshots(saved.id)
                    if (loadToken != token || !isCurrent(screenRequestContext)) return@launch
                    update {
                        it.copy(
                            material = okOrNull(material),
                            snapshots = okOrNull(snapshots) ?: emptyList(),
                            loading = false,
                            busyLabel = null,
                            message = UiMessage("材料已保存，请核对结构后确认", isError = false),
                        )
                    }
                }
            }
        }
    }

    fun uploadFile(
        kind: String,
        title: String,
        preparationId: String,
        uri: Uri,
        fileName: String,
        mimeType: String,
    ) {
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "读取并上传文件…") }
        viewModelScope.launch {
            val bytes = withContext(ioDispatcher) {
                runCatching {
                    uriReader(uri)
                }.getOrNull()
            }
            if (bytes == null) {
                update { it.copy(loading = false, busyLabel = null, message = UiMessage("无法读取所选文件")) }
                return@launch
            }
            if (bytes.size > 10 * 1024 * 1024) {
                update { it.copy(loading = false, busyLabel = null, message = UiMessage("文件超过 10 MiB 上限")) }
                return@launch
            }
            val fileSha = withContext(Dispatchers.IO) {
                java.security.MessageDigest.getInstance("SHA-256")
                    .digest(bytes)
                    .joinToString("") { "%02x".format(it) }
            }
            val safeTitle = title.trim().ifBlank { fileName }
            val uploaded = performWrite(
                ctx,
                "uploadFile",
                UploadFilePayload(kind, safeTitle, fileName, mimeType, fileSha),
                targetId = null,
                revision = null,
                preparationId = preparationId,
                fileName = fileName,
                fileSha256 = fileSha,
                fileMime = mimeType,
            ) { key -> repository.uploadFile(kind, safeTitle, fileName, mimeType, bytes, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (uploaded) {
                is ApiResult.Err -> showError(uploaded, ctx)
                is ApiResult.Ok -> {
                    val material = uploaded.value
                    update {
                        it.copy(
                            material = material,
                            materialJobLookup = null,
                            job = null,
                            loading = true,
                            busyLabel = "提取文件文本…",
                            invalidSkillItemId = null,
                            invalidSkillField = null,
                            message = UiMessage("文件已上传，正在提取文本（不调用 AI）", isError = false),
                        )
                    }
                    replaceTop(Screen.Review(material.id, preparationId))
                    refreshSkillCodes()
                    refreshMaterials()
                    val screenRequestContext = currentRequestContext() ?: return@launch
                    val entry = scopeForJob("extract", preparationId, material.id) ?: return@launch
                    val extract = performWrite(
                        screenRequestContext,
                        "extractMaterial",
                        ExtractMaterialPayload(material.revision),
                        targetId = material.id,
                        revision = material.revision,
                        preparationId = preparationId,
                    ) { key -> repository.extractMaterial(material.id, material.revision, key) }
                    if (!isAccountCurrent(screenRequestContext) || !isScopeCurrent(entry)) return@launch
                    when (extract) {
                        is ApiResult.Err -> showError(extract, screenRequestContext, entry)
                        is ApiResult.Ok -> {
                            if (!syncMaterialWriteReceipt(screenRequestContext, entry, "extract", extract.value)) {
                                return@launch
                            }
                            applyEffect(
                                restoreCoordinator.project(
                                    entry.copy(jobId = extract.value.id, jobAttempt = extract.value.attempt),
                                    extract.value,
                                ),
                            )
                        }
                    }
                }
            }
        }
    }

    fun useTemplate(template: JdTemplateDto, preparationId: String) {
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "复制内置模板…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "createTextMaterial",
                CreateTextMaterialPayload(
                    "jd",
                    template.title,
                    template.text,
                    template.id,
                    template.version,
                ),
                targetId = null,
                revision = null,
                preparationId = preparationId,
            ) { key ->
                repository.createTextMaterial(
                    kind = "jd",
                    title = template.title,
                    text = template.text,
                    templateId = template.id,
                    templateVersion = template.version,
                    operationKey = key,
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    val material = result.value
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            job = null,
                            materialJobLookup = null,
                            invalidSkillItemId = null,
                            invalidSkillField = null,
                        )
                    }
                    replaceTop(Screen.Review(material.id, preparationId))
                    refreshSkillCodes()
                    refreshMaterials()
                    val screenRequestContext = currentRequestContext() ?: return@launch
                    val token = ++loadToken
                    val detail = repository.material(material.id)
                    val snapshots = repository.snapshots(material.id)
                    if (loadToken != token || !isCurrent(screenRequestContext)) return@launch
                    update {
                        it.copy(
                            material = okOrNull(detail),
                            snapshots = okOrNull(snapshots) ?: emptyList(),
                        )
                    }
                }
            }
        }
    }

    fun parseMaterial(materialId: String, roleId: String, level: String) {
        val material = state.value.material
        if (material == null || material.id != materialId) {
            update { it.copy(message = UiMessage("材料尚未加载完成，请稍后再试")) }
            return
        }
        val policy = com.mockmate.app.ui.screens.ReviewScreenPolicy.evaluate(
            material,
            state.value.job,
            state.value.materialJobLookup,
        )
        if (!policy.showStartParseAction) return
        val actionKey = "parse:$materialId:${material.revision}"
        if (!inFlightMaterialActionKeys.add(actionKey)) return
        val ctx = currentRequestContext() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val page = pageSnapshot() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val entry = scopeForJob("parse", null, materialId) ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        update { it.copy(loading = true, busyLabel = "AI 解析中…", message = null) }
        viewModelScope.launch {
            try {
                val job = performWrite(
                    ctx,
                    "parseMaterial",
                    ParseMaterialPayload(material.revision, roleId, level),
                    targetId = materialId,
                    revision = material.revision,
                ) { key -> repository.parseMaterial(materialId, material.revision, roleId, level, key) }
                if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
                when (job) {
                    is ApiResult.Err -> showError(job, ctx, page)
                    is ApiResult.Ok -> {
                        if (!syncMaterialWriteReceipt(ctx, entry, "parse", job.value)) return@launch
                        applyEffect(
                            restoreCoordinator.project(
                                entry.copy(jobId = job.value.id, jobAttempt = job.value.attempt),
                                job.value,
                            ),
                        )
                    }
                }
            } finally {
                inFlightMaterialActionKeys.remove(actionKey)
            }
        }
    }

    fun confirmProfile(materialId: String, profile: JsonObject) {
        val material = state.value.material
        if (material == null || material.id != materialId) return
        val submittedRevision = material.revision
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        if (page.screen != RestoreJobService.SCREEN_REVIEW || page.materialId != materialId) return
        update {
            it.copy(
                loading = true,
                busyLabel = "保存核对结果…",
                invalidSkillItemId = null,
                invalidSkillField = null,
            )
        }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "confirmMaterial",
                ConfirmMaterialPayload(submittedRevision, profile),
                targetId = materialId,
                revision = submittedRevision,
            ) { key -> repository.confirmMaterial(materialId, submittedRevision, profile, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> {
                    if (result.code == "UNAUTHENTICATED") {
                        showError(result, ctx, page)
                        return@launch
                    }
                    if (!isScopeCurrent(page)) return@launch
                    if (result.code == "PROFILE_INVALID") {
                        val field = result.details.firstOrNull { it.startsWith("field=") }
                            ?.removePrefix("field=")
                        val itemId = result.details.firstOrNull { it.startsWith("item_id=") }
                            ?.removePrefix("item_id=")
                        if (field == "skill_codes" || field == "code" || result.message.contains("技能编码")) {
                            update { current ->
                                val currentMaterial = current.material
                                if (
                                    !isScopeCurrent(page) ||
                                    currentMaterial?.id != materialId ||
                                    currentMaterial.revision != submittedRevision
                                ) {
                                    current
                                } else {
                                    current.copy(
                                        invalidSkillItemId = itemId,
                                        invalidSkillField = field,
                                        invalidSkillFocusNonce = current.invalidSkillFocusNonce + 1,
                                    )
                                }
                            }
                            if (isScopeCurrent(page)) {
                                loadSkillCodesForScope(ctx, page)
                            }
                        }
                    }
                    showError(result, ctx, page)
                }
                is ApiResult.Ok -> {
                    if (!isScopeCurrent(page)) return@launch
                    val snapshots = repository.snapshots(materialId)
                    if (!isAccountCurrent(ctx)) return@launch
                    if (snapshots is ApiResult.Err && snapshots.code == "UNAUTHENTICATED") {
                        showError(snapshots, ctx, page)
                        return@launch
                    }
                    if (!isScopeCurrent(page)) return@launch
                    val refreshed = repository.material(materialId)
                    if (!isAccountCurrent(ctx)) return@launch
                    if (refreshed is ApiResult.Err && refreshed.code == "UNAUTHENTICATED") {
                        showError(refreshed, ctx, page)
                        return@launch
                    }
                    // 最终校验与状态写入在同一无挂起主线程片段完成，防止迟到回包覆盖新页面或新版本。
                    update { current ->
                        val currentMaterial = current.material
                        if (
                            !isScopeCurrent(page) ||
                            currentMaterial?.id != materialId ||
                            currentMaterial.revision != submittedRevision
                        ) {
                            current
                        } else {
                            current.copy(
                                snapshots = okOrNull(snapshots) ?: current.snapshots,
                                material = okOrNull(refreshed) ?: current.material,
                                invalidSkillItemId = null,
                                invalidSkillField = null,
                                loading = false,
                                busyLabel = null,
                                message = UiMessage("已保存为不可变版本（快照）", isError = false),
                            )
                        }
                    }
                }
            }
        }
    }

    fun bindSnapshot(kind: String, snapshot: SnapshotDto, preparationId: String) {
        val prep = state.value.preparation
        if (prep == null || prep.id != preparationId) {
            update { it.copy(message = UiMessage("准备尚未加载完成，请返回重试")) }
            return
        }
        val resumeId = if (kind == "resume") snapshot.id else (state.value.resumeSnapshot?.id ?: prep.resume_snapshot_id)
        val jdId = if (kind == "jd") snapshot.id else (state.value.jdSnapshot?.id ?: prep.jd_snapshot_id)
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "绑定材料版本…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "editPreparation",
                EditPreparationPayload(
                    prep.revision,
                    prep.role_id,
                    prep.level,
                    prep.main_question_count,
                    resumeId,
                    jdId,
                ),
                targetId = prep.id,
                revision = prep.revision,
            ) { key ->
                repository.editPreparation(
                    prep.id, prep.revision, prep.role_id, prep.level, prep.main_question_count,
                    resumeId, jdId, key,
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    update {
                        it.copy(
                            preparation = result.value,
                            plan = null,
                            matchReport = null,
                            loading = false,
                            busyLabel = null,
                            message = UiMessage("已绑定该材料版本", isError = false),
                        )
                    }
                    if (kind == "resume") {
                        update { it.copy(resumeSnapshot = snapshot) }
                    } else {
                        update { it.copy(jdSnapshot = snapshot) }
                    }
                    popTo { it is Screen.Setup }
                    refreshPreparations()
                }
            }
        }
    }

    fun unbindMaterial(kind: String) {
        val prep = state.value.preparation ?: return
        val resumeId = if (kind == "resume") null else (state.value.resumeSnapshot?.id ?: prep.resume_snapshot_id)
        val jdId = if (kind == "jd") null else (state.value.jdSnapshot?.id ?: prep.jd_snapshot_id)
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "editPreparation",
                EditPreparationPayload(
                    prep.revision,
                    prep.role_id,
                    prep.level,
                    prep.main_question_count,
                    resumeId,
                    jdId,
                ),
                targetId = prep.id,
                revision = prep.revision,
            ) { key ->
                repository.editPreparation(
                    prep.id, prep.revision, prep.role_id, prep.level, prep.main_question_count,
                    resumeId, jdId, key,
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    update {
                        it.copy(
                            preparation = result.value,
                            resumeSnapshot = if (kind == "resume") null else it.resumeSnapshot,
                            jdSnapshot = if (kind == "jd") null else it.jdSnapshot,
                            plan = null,
                            matchReport = null,
                            message = UiMessage("已清除该材料", isError = false),
                        )
                    }
                    refreshPreparations()
                }
            }
        }
    }

    // ------------------------------------------------------------ 匹配

    fun openMatch(preparationId: String) {
        val prep = state.value.preparation?.takeIf { it.id == preparationId }
        update {
            it.copy(
                job = null,
                matchReport = if (prep?.match_report_id == null) {
                    null
                } else {
                    it.matchReport?.takeIf { report -> report.id == prep.match_report_id }
                },
            )
        }
        push(Screen.Match(preparationId))
        val ctx = currentRequestContext() ?: return
        if (prep?.match_report_id != null) {
            viewModelScope.launch {
                when (val result = repository.matchReport(prep.match_report_id)) {
                    is ApiResult.Ok -> if (isCurrent(ctx)) update { current ->
                        val latestPrep = current.preparation
                        if (latestPrep == null ||
                            (latestPrep.id == preparationId && result.value.input_revision >= latestPrep.revision)
                        ) {
                            current.copy(matchReport = result.value)
                        } else {
                            current
                        }
                    }
                    is ApiResult.Err -> showError(result, ctx)
                }
            }
        }
        restoreJobFor(ctx, "match", preparationId)
    }

    fun runMatch(preparationId: String) {
        val prep = state.value.preparation ?: return
        if (prep.id != preparationId) return
        val ctx = currentRequestContext() ?: return
        val entry = scopeForJob("match", preparationId, preparationId) ?: return
        update { it.copy(loading = true, busyLabel = "匹配分析中…", matchReport = null, message = null) }
        viewModelScope.launch {
            val job = performWrite(
                ctx,
                "match",
                MatchPayload(prep.revision),
                targetId = preparationId,
                revision = prep.revision,
                preparationId = preparationId,
            ) { key -> repository.match(preparationId, prep.revision, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (job) {
                is ApiResult.Err -> showError(job, ctx)
                is ApiResult.Ok -> {
                    applyEffect(restoreCoordinator.project(entry.copy(jobId = job.value.id), job.value))
                }
            }
        }
    }

    // ------------------------------------------------------------ 计划

    fun openPlan(preparationId: String) {
        val prep = state.value.preparation?.takeIf { it.id == preparationId }
        update {
            it.copy(
                job = null,
                planError = null,
                plan = if (prep?.current_plan_id == null) {
                    null
                } else {
                    it.plan?.takeIf { plan -> plan.id == prep.current_plan_id }
                },
            )
        }
        push(Screen.PlanHub(preparationId))
        val ctx = currentRequestContext() ?: return
        if (prep == null) {
            val token = ++loadToken
            viewModelScope.launch {
                val loaded = repository.preparation(preparationId)
                if (loadToken != token || !isCurrent(ctx)) return@launch
                if (loaded is ApiResult.Ok) {
                    update { current ->
                        val existing = current.preparation
                        if (existing == null || existing.id != preparationId || loaded.value.revision >= existing.revision) {
                            current.copy(preparation = loaded.value)
                        } else {
                            current
                        }
                    }
                    loadPlanFor(loaded.value, ctx)
                } else if (loaded is ApiResult.Err) {
                    showError(loaded, ctx)
                }
            }
        } else {
            loadPlanFor(prep, ctx)
        }
        restoreJobFor(ctx, "plan", preparationId)
    }

    private fun loadPlanFor(prep: PreparationDto, ctx: RequestContext) {
        val planId = prep.current_plan_id ?: return
        viewModelScope.launch {
            when (val result = repository.plan(planId)) {
                is ApiResult.Ok -> if (isCurrent(ctx)) {
                    var applied = false
                    update { current ->
                        val latestPrep = current.preparation
                        if (latestPrep == null ||
                            (latestPrep.id == prep.id &&
                                result.value.preparation_id == prep.id &&
                                result.value.input_revision >= latestPrep.revision)
                        ) {
                            applied = true
                            current.copy(plan = result.value)
                        } else {
                            current
                        }
                    }
                    if (applied) loadInterviewForPlan(planId)
                }
                is ApiResult.Err -> showError(result, ctx)
            }
        }
    }

    /** 只读刷新计划摘要：重新读取准备与当前计划，不新开页面、不重新生成。 */
    fun refreshPlan(preparationId: String) {
        val token = ++loadToken
        val ctx = currentRequestContext() ?: return
        update { it.copy(planError = null) }
        viewModelScope.launch {
            val prep = repository.preparation(preparationId)
            if (loadToken != token || !isCurrent(ctx)) return@launch
            if (prep is ApiResult.Err) {
                showError(prep, ctx)
                return@launch
            }
            val prepValue = (prep as ApiResult.Ok).value
            var prepApplied = false
            update { current ->
                val latestPrep = current.preparation
                if (latestPrep == null || latestPrep.id != preparationId || prepValue.revision >= latestPrep.revision) {
                    prepApplied = true
                    current.copy(preparation = prepValue)
                } else {
                    current
                }
            }
            if (!prepApplied) return@launch
            val planId = prepValue.current_plan_id
            if (planId == null) {
                update { it.copy(plan = null, message = UiMessage("服务端尚未记录当前计划", isError = true)) }
                return@launch
            }
            when (val plan = repository.plan(planId)) {
                is ApiResult.Err -> showError(plan, ctx)
                is ApiResult.Ok -> if (isCurrent(ctx)) update { current ->
                    val latestPrep = current.preparation
                    if (latestPrep == null ||
                        (latestPrep.id == preparationId &&
                            plan.value.preparation_id == preparationId &&
                            plan.value.input_revision >= latestPrep.revision)
                    ) {
                        current.copy(
                            plan = plan.value,
                            message = UiMessage("计划摘要已刷新（未重新生成）", isError = false),
                        )
                    } else {
                        current
                    }
                }
            }
            loadInterviewForPlan(planId)
        }
    }

    fun generatePlan(preparationId: String) {
        val prep = state.value.preparation ?: return
        if (prep.id != preparationId) return
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val entry = scopeForJob("plan", preparationId, preparationId) ?: return
        update { it.copy(loading = true, busyLabel = "生成计划中…", plan = null, planError = null, message = null) }
        viewModelScope.launch {
            val job = performWrite(
                ctx,
                "generatePlan",
                GeneratePlanPayload(prep.revision),
                targetId = preparationId,
                revision = prep.revision,
                preparationId = preparationId,
            ) { key -> repository.generatePlan(preparationId, prep.revision, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (job) {
                is ApiResult.Err -> {
                    val formattedMessage = PlanErrorFormatter.userMessage(
                        job.code,
                        job.message,
                        job.details,
                    )
                    if (isScopeCurrent(page)) {
                        update { current ->
                            val latestPrep = current.preparation
                            if (latestPrep == null ||
                                (latestPrep.id == preparationId && latestPrep.revision == prep.revision)
                            ) {
                                current.copy(
                                    planError = PlanErrorState(
                                        code = job.code,
                                        message = formattedMessage,
                                        details = job.details,
                                        rawServerMessage = job.message,
                                        httpStatus = job.httpStatus,
                                        retryable = job.retryable,
                                        diagnosticContext = "POST /preparations/$preparationId/generate-plan · rev=${prep.revision}",
                                        jobId = null,
                                        preparationId = preparationId,
                                        inputRevision = prep.revision,
                                    ),
                                )
                            } else {
                                current
                            }
                        }
                    }
                    showError(job.copy(message = formattedMessage), ctx, page)
                }
                is ApiResult.Ok -> {
                    // POST 只负责创建任务；终态统一由恢复归并口读取，running 由 JobWatch 持续订阅。
                    applyEffect(restoreCoordinator.project(entry.copy(jobId = job.value.id), job.value))
                }
            }
        }
    }

    fun returnToSetup(preparationId: String) {
        val hasSetup = state.value.stack.any { it is Screen.Setup && it.preparationId == preparationId }
        if (hasSetup) {
            popTo { it is Screen.Setup && it.preparationId == preparationId }
        } else {
            replaceTop(Screen.Setup(preparationId))
        }
        update { it.copy(planError = null, message = null) }
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            val prep = repository.preparation(preparationId)
            if (!isCurrent(ctx)) return@launch
            if (prep is ApiResult.Ok) {
                update { it.copy(preparation = prep.value) }
                loadSnapshotLabels(prep.value, ctx)
            }
        }
    }

    fun retryPlanJob() {
        val job = state.value.job ?: return
        val prep = state.value.preparation ?: return
        if (!job.retryable) {
            update { it.copy(message = UiMessage("该任务不可重试或输入已过期")) }
            return
        }
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val entry = scopeForJob(job.kind, prep.id, job.target_id, job.id) ?: return
        update { it.copy(loading = true, busyLabel = "重试中…", message = null) }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "retryJob",
                RetryJobPayload(job.attempt),
                targetId = job.id,
                revision = job.attempt,
                preparationId = prep.id,
            ) { key -> repository.retryJob(job.id, job.attempt, key) }
            if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    applyEffect(restoreCoordinator.project(entry, result.value))
                }
            }
        }
    }

    /** parse/extract/match/plan 通用的显式重试入口；不自动重发。 */
    fun retryCurrentJob() {
        val job = state.value.job ?: return
        if (!job.retryable) {
            update { it.copy(message = UiMessage("该任务不可重试或输入已过期")) }
            return
        }
        val actionKey = "retry:${job.id}:${job.attempt}"
        if (!inFlightMaterialActionKeys.add(actionKey)) return
        val ctx = currentRequestContext() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val page = pageSnapshot() ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        val prep = state.value.preparation
        if (job.kind in setOf("match", "plan") && prep == null) {
            inFlightMaterialActionKeys.remove(actionKey)
            update { it.copy(message = UiMessage("准备尚未加载完成，请返回准备页重试")) }
            return
        }
        val entry = scopeForJob(job.kind, prep?.id, job.target_id, job.id) ?: run {
            inFlightMaterialActionKeys.remove(actionKey)
            return
        }
        update {
            it.copy(
                loading = true,
                busyLabel = "重试中…",
                planError = if (job.kind == "plan") null else it.planError,
                message = null,
            )
        }
        viewModelScope.launch {
            try {
                val result = performWrite(
                    ctx,
                    "retryJob",
                    RetryJobPayload(job.attempt),
                    targetId = job.id,
                    revision = job.attempt,
                    preparationId = prep?.id,
                ) { key -> repository.retryJob(job.id, job.attempt, key) }
                if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
                when (result) {
                    is ApiResult.Err -> showError(result, ctx, page)
                    is ApiResult.Ok -> {
                        if (result.value.kind in setOf("extract", "parse") &&
                            !syncMaterialWriteReceipt(ctx, entry, job.kind, result.value)
                        ) {
                            return@launch
                        }
                        applyEffect(
                            restoreCoordinator.project(
                                entry.copy(jobId = result.value.id, jobAttempt = result.value.attempt),
                                result.value,
                            ),
                        )
                    }
                }
            } finally {
                inFlightMaterialActionKeys.remove(actionKey)
            }
        }
    }

    fun confirmPlan(planId: String) {
        val plan = state.value.plan
        if (plan == null || plan.id != planId) return
        if (plan.state == "confirmed") {
            update { it.copy(message = UiMessage("该计划已确认", isError = false)) }
            return
        }
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "保存确认结果…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "confirmPlan",
                ConfirmPlanPayload(plan.revision),
                targetId = planId,
                revision = plan.revision,
            ) { key -> repository.confirmPlan(planId, plan.revision, key) }
            when (result) {
                is ApiResult.Err -> {
                    if (!isAccountCurrent(ctx)) return@launch
                    showError(result, ctx)
                    update {
                        it.copy(
                            message = UiMessage(
                                "确认未同步到服务器：${result.message}",
                                isError = true,
                                code = result.code,
                            ),
                        )
                    }
                }
                is ApiResult.Ok -> {
                    if (!isAccountCurrent(ctx)) return@launch
                    update {
                        it.copy(
                            plan = result.value,
                            loading = false,
                            busyLabel = null,
                            message = UiMessage("计划已确认并保存", isError = false),
                        )
                    }
                    refreshPreparations()
                    loadInterviewForPlan(planId)
                }
            }
        }
    }

    fun redoPreparation(preparationId: String? = null) {
        val prep = state.value.preparation?.takeIf {
            preparationId == null || it.id == preparationId
        } ?: return
        // 仅使用当前准备绑定的快照，避免引用其他页面残留的 resumeSnapshot/jdSnapshot。
        val resumeId = prep.resume_snapshot_id
        val jdId = prep.jd_snapshot_id
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "新建准备中…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "createPreparation",
                CreatePreparationPayload(
                    prep.role_id,
                    prep.level,
                    prep.main_question_count,
                    resumeId,
                    jdId,
                ),
                targetId = null,
                revision = null,
            ) { key ->
                repository.createPreparation(
                    prep.role_id,
                    prep.level,
                    prep.main_question_count,
                    resumeId,
                    jdId,
                    key,
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> {
                    if (isScopeCurrent(page)) {
                        showError(result, ctx, page)
                    }
                }
                is ApiResult.Ok -> {
                    if (!isScopeCurrent(page)) {
                        refreshPreparations()
                        return@launch
                    }
                    bumpNavigation()
                    val newCtx = currentRequestContext()
                    update {
                        it.copy(
                            preparation = result.value,
                            plan = null,
                            planError = null,
                            matchReport = null,
                            job = null,
                            resumeSnapshot = null,
                            jdSnapshot = null,
                            loading = false,
                            busyLabel = null,
                            interviewForPlan = null,
                            stack = listOf(Screen.PreparationList, Screen.Setup(result.value.id)),
                            message = UiMessage(
                                if (prep.current_plan_id != null) {
                                    "已沿用材料新建准备，旧计划保留只读"
                                } else {
                                    "已沿用材料新建准备，旧失败记录保留只读"
                                },
                                isError = false,
                            ),
                        )
                    }
                    if (newCtx != null) {
                        loadSnapshotLabels(result.value, newCtx)
                    }
                    refreshPreparations()
                }
            }
        }
    }

    // ------------------------------------------------------------ M2 文字面试

    fun openInterviewHistory() {
        update {
            it.copy(
                interviewHistory = emptyList(),
                interviewHistoryCursor = null,
                interviewHistoryLoading = false,
            )
        }
        push(Screen.InterviewHistory)
        loadInterviewHistory(append = false)
    }

    fun loadInterviewHistory(append: Boolean) {
        val ctx = currentRequestContext() ?: return
        val cursor = if (append) state.value.interviewHistoryCursor else null
        if (append && cursor == null) return
        update { it.copy(interviewHistoryLoading = true) }
        viewModelScope.launch {
            when (val result = repository.interviews(limit = 20, cursor = cursor)) {
                is ApiResult.Ok -> if (isAccountCurrent(ctx)) {
                    update {
                        it.copy(
                            interviewHistory = if (append) {
                                it.interviewHistory + result.value.items
                            } else {
                                result.value.items
                            },
                            interviewHistoryCursor = result.value.next_cursor,
                            interviewHistoryLoading = false,
                        )
                    }
                }
                is ApiResult.Err -> {
                    if (isAccountCurrent(ctx)) {
                        update { it.copy(interviewHistoryLoading = false) }
                        showError(result, ctx)
                    }
                }
            }
        }
    }

    fun openInterview(sessionId: String) {
        stopVoicePlaybackInternal()
        update {
            it.copy(
                interviewDetail = null,
                interviewReport = null,
                interviewReportError = null,
                pendingInterviewTurnId = null,
                interviewOrphanDrafts = emptyList(),
                voiceDraft = null,
                voicePhase = VoicePhase.IDLE,
                voiceTurnId = null,
                voiceTake = null,
                voiceTakeIssue = null,
                voiceReviewText = "",
                voiceSpeechSynthetic = false,
                voicePlaybackTurnId = null,
                voiceError = null,
                voiceTextFallback = false,
            )
        }
        lastAutoPlayedTurnId = null
        push(Screen.InterviewRoom(sessionId))
        refreshInterviewSession(sessionId, includeReport = false)
    }

    /**
     * 面试间/报告页的统一刷新：先按当前场重放待确认写请求，再读取最新详情。
     * 手动刷新、进场与回前台共用同一入口；同账号同场的并发恢复会合并。
     */
    fun loadInterview(sessionId: String) {
        refreshInterviewSession(sessionId, includeReport = false)
    }

    fun confirmPendingInterviewAnswer(sessionId: String) {
        refreshInterviewSession(sessionId, includeReport = false)
    }

    private fun refreshInterviewSession(sessionId: String, includeReport: Boolean) {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        if (page.interviewSessionId != sessionId) return
        update { it.copy(loading = true, busyLabel = "加载面试…") }
        viewModelScope.launch {
            val recoveryMessage = recoverInterviewSession(ctx, sessionId)
            if (!isScopeCurrent(page)) return@launch
            if (recoveryMessage != null) {
                update {
                    if (!isScopeCurrent(page)) it
                    else it.copy(message = recoveryMessage)
                }
            }
            // 即使详情 GET 失败，也让用户看到仍待确认的提交并提供手动重试入口。
            val pending = operations.pendingFor(ctx.accountId, "interviewAnswer", sessionId)
            if (!isScopeCurrent(page)) return@launch
            val pendingTurn = (pending?.payload as? InterviewAnswerPayload)?.turnId
            update {
                if (!isScopeCurrent(page)) it else it.copy(pendingInterviewTurnId = pendingTurn)
            }
            fetchInterviewAndOptionalReport(ctx, page, sessionId, includeReport)
        }
    }

    private suspend fun fetchInterviewAndOptionalReport(
        ctx: RequestContext,
        page: RestoreScope,
        sessionId: String,
        includeReport: Boolean,
    ) {
        when (val result = repository.interview(sessionId)) {
            is ApiResult.Ok -> {
                if (!isScopeCurrent(page)) return
                val pending = operations.pendingFor(
                    ctx.accountId, "interviewAnswer", sessionId
                )
                if (!isScopeCurrent(page)) return
                val pendingTurn = (pending?.payload as? InterviewAnswerPayload)?.turnId
                applyInterviewDetail(
                    accountId = ctx.accountId,
                    page = page,
                    sessionId = sessionId,
                    detail = result.value,
                    pendingTurnId = pendingTurn,
                    clearLoading = !includeReport,
                )
                if (!isScopeCurrent(page)) return
                if (result.value.mode == "voice") {
                    val currentTurn = result.value.current_turn
                    if (currentTurn != null) {
                        loadVoiceDraftFor(ctx, page, sessionId, currentTurn.turn_id)
                    } else if (result.value.status == "ended") {
                        update {
                            it.copy(
                                voicePhase = VoicePhase.IDLE,
                                voiceDraft = null,
                                voiceReviewText = "",
                                voiceTurnId = null,
                            )
                        }
                    }
                }
                restoreInterviewJobs(sessionId, page)
            }
            is ApiResult.Err -> {
                if (!isScopeCurrent(page)) return
                update {
                    if (!isScopeCurrent(page)) it else it.copy(loading = false, busyLabel = null)
                }
                showError(result, ctx, page)
                return
            }
        }
        if (includeReport && isScopeCurrent(page)) {
            fetchInterviewReport(ctx, page, sessionId)
            if (isScopeCurrent(page)) {
                update {
                    if (!isScopeCurrent(page)) it else it.copy(loading = false, busyLabel = null)
                }
            }
        }
    }

    private suspend fun fetchInterviewReport(
        ctx: RequestContext,
        page: RestoreScope,
        sessionId: String,
    ) {
        when (val result = repository.interviewReport(sessionId)) {
            is ApiResult.Ok -> if (isScopeCurrent(page)) {
                update {
                    it.copy(interviewReport = result.value, interviewReportError = null)
                }
            }
            is ApiResult.Err -> if (isScopeCurrent(page)) {
                update {
                    it.copy(
                        interviewReportError = result.message,
                        message = UiMessage(result.message, isError = true, code = result.code),
                    )
                }
            }
        }
    }

    private fun applyInterviewDetail(
        accountId: String,
        page: RestoreScope,
        sessionId: String,
        detail: InterviewDetailDto,
        pendingTurnId: String?,
        clearLoading: Boolean = false,
    ) {
        if (!isScopeCurrent(page)) return
        var cancelledJobId: String? = null
        update {
            if (!isScopeCurrent(page)) {
                it
            } else if (detail.session_id != sessionId ||
                !InterviewUiMerge.shouldApplyDetail(it.interviewDetail, detail)
            ) {
                if (clearLoading) it.copy(loading = false, busyLabel = null) else it
            } else {
                val existingOngoing = it.ongoingStatus
                val clearOngoing = existingOngoing != null && when (existingOngoing.jobKind) {
                    "follow_up" ->
                        (detail.follow_up_job?.id == existingOngoing.jobId &&
                            detail.follow_up_job.state in M1Repository.TERMINAL_STATES) ||
                            (detail.follow_up_job == null && detail.phase != "deciding_follow_up")
                    "interview_report" ->
                        (detail.report_job?.id == existingOngoing.jobId &&
                            detail.report_job.state in M1Repository.TERMINAL_STATES) ||
                            detail.report_state == "ready"
                    else -> false
                }
                if (clearOngoing) {
                    cancelledJobId = existingOngoing?.jobId
                }
                it.copy(
                    interviewDetail = detail,
                    pendingInterviewTurnId = pendingTurnId,
                    interviewOrphanDrafts = collectOrphanDrafts(
                        accountId,
                        sessionId,
                        detail,
                    ),
                    ongoingStatus = if (clearOngoing) null else existingOngoing,
                    loading = if (clearLoading) false else it.loading,
                    busyLabel = if (clearLoading) null else it.busyLabel,
                )
            }
        }
        cancelledJobId?.let { jobWatch.cancel(accountId, it) }
    }

    /**
     * 已解决回合里仍有本机密文草稿时，保留为只读草稿供查看/复制。
     * 若草稿与已保存回答完全相同，说明是提交成功后没来得及清理，直接清掉。
     */
    private fun collectOrphanDrafts(
        accountId: String,
        sessionId: String,
        detail: InterviewDetailDto,
    ): List<LocalInterviewDraft> {
        val currentTurnId = detail.current_turn?.turn_id
        val drafts = mutableListOf<LocalInterviewDraft>()
        for (turn in detail.transcript) {
            if (turn.turn_id == currentTurnId) continue
            val text = when (val read = interviewDrafts.get(accountId, sessionId, turn.turn_id)) {
                is SecureRead.Present -> read.value
                else -> null
            }
            if (text.isNullOrBlank()) continue
            if (turn.answer?.text == text) {
                markDraftResolved(accountId, sessionId, turn.turn_id)
                continue
            }
            drafts += LocalInterviewDraft(turn.turn_id, turn.prompt, text)
        }
        return drafts
    }

    private suspend fun recoverInterviewSession(ctx: RequestContext, sessionId: String): UiMessage? {
        if (!isAccountCurrent(ctx)) return null
        val key = "${ctx.accountId}/$sessionId"
        val mutex = interviewRecoveryLocks.computeIfAbsent(key) { Mutex() }
        return mutex.withLock {
            if (!isAccountCurrent(ctx)) return@withLock null
            val confirmedLegacyJobIds = confirmedLegacyJobIdsForSession(ctx, sessionId)
            val report = replayer.replay(
                accountId = ctx.accountId,
                isCurrent = { isAccountCurrent(ctx) },
                filter = { op -> belongsToInterviewSession(op, sessionId, confirmedLegacyJobIds) },
                onCompleted = { op, value -> onRecoveredOperation(ctx, op, value) },
            )
            val message = report.storageError?.let {
                "本地操作记录不可用：$it。为避免重复提交，未发送恢复请求；请保留数据并重新核对。"
            } ?: report.userMessage ?: report.rejected.firstOrNull()?.let {
                "原提交未生效：${it.message}。已刷新服务端状态，请按最新回合操作。"
            }
            when {
                message != null -> UiMessage(
                    message,
                    isError = true,
                    code = report.storageError?.let { "STORAGE_UNAVAILABLE" }
                        ?: report.rejected.firstOrNull()?.code
                        ?: "OPERATION_REPLAY_INCOMPLETE",
                )
                report.retained > 0 -> UiMessage(
                    "仍有 ${report.retained} 条提交结果未确认；已保留原请求，可稍后手动重试，不会自动重复发送。",
                    isError = false,
                    code = "INTERVIEW_RECEIPT_UNKNOWN",
                )
                else -> null
            }
        }
    }

    private suspend fun confirmedLegacyJobIdsForSession(
        ctx: RequestContext,
        sessionId: String,
    ): Set<String> {
        val loaded = operations.pending(ctx.accountId) as? OperationLoad.Loaded ?: return emptySet()
        if (!loaded.operations.pending.any(::isLegacyInterviewRetryCandidate)) return emptySet()
        return when (val detail = repository.interview(sessionId)) {
            is ApiResult.Ok -> setOfNotNull(
                detail.value.follow_up_job?.id,
                detail.value.report_job?.id,
            )
            is ApiResult.Err -> emptySet()
        }
    }

    private fun isLegacyInterviewRetryCandidate(op: PendingOperation): Boolean =
        op.interviewSessionId == null &&
            op.targetId != null &&
            (op.payload is RetryJobPayload || (op.payload == null && op.type == "retryJob"))

    private fun belongsToInterviewSession(
        op: PendingOperation,
        sessionId: String,
        confirmedLegacyJobIds: Set<String> = emptySet(),
    ): Boolean {
        if (op.interviewSessionId != null) return op.interviewSessionId == sessionId
        return when (val payload = op.payload) {
            is InterviewAnswerPayload -> payload.sessionId == sessionId
            is InterviewSkipPayload -> payload.sessionId == sessionId
            is InterviewPausePayload -> payload.sessionId == sessionId
            is InterviewResumePayload -> payload.sessionId == sessionId
            is InterviewContinuePayload -> payload.sessionId == sessionId
            is InterviewFinishPayload -> payload.sessionId == sessionId
            is InterviewBasicReportPayload -> payload.sessionId == sessionId
            is RetryJobPayload -> op.targetId != null && op.targetId in confirmedLegacyJobIds
            null -> op.type == "retryJob" && op.targetId != null && op.targetId in confirmedLegacyJobIds
            else -> false
        }
    }

    private suspend fun onRecoveredOperation(
        ctx: RequestContext,
        op: PendingOperation,
        value: Any?,
    ) {
        if (value !is InterviewReceiptDto) return
        if (value.job_id != null) {
            when (val job = repository.job(value.job_id)) {
                is ApiResult.Ok -> recordJobResult(ctx.accountId, job.value, null)
                is ApiResult.Err -> Unit
            }
        }
        val payload = op.payload as? InterviewAnswerPayload ?: return
        if (value.session_id == payload.sessionId && value.answer_id != null &&
            isAccountCurrent(ctx)
        ) {
            clearInterviewDraft(payload.sessionId, payload.turnId)
        }
    }

    fun startInterviewForPlan(preparationId: String, plan: PublicPlanDto, mode: String = "text") {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val voice = mode == "voice"
        if (voice && state.value.voiceConfig?.available != true) {
            notify("语音服务未配置，不能开始语音面试", isError = true)
            return
        }
        update {
            it.copy(
                loading = true,
                busyLabel = if (voice) "开始语音面试…" else "开始文字面试…",
            )
        }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "startInterview",
                StartInterviewPayload(plan.revision, mode, voice),
                targetId = plan.id,
                revision = plan.revision,
                preparationId = preparationId,
            ) { key -> repository.startInterview(plan.id, plan.revision, key, mode, voice) }
            if (!isAccountCurrent(ctx)) return@launch
            // 回执已经落到原账号；只有仍停留在原计划页且目标计划未变时才允许导航。
            if (!isScopeCurrent(page) || state.value.plan?.id != plan.id) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    if (mode == "voice" && result.value.mode != "voice") {
                        update {
                            it.copy(
                                message = UiMessage(
                                    "本计划已有原场（${if (result.value.mode == "voice") "语音" else "文字"}），同计划不会复制第二场。",
                                    isError = false,
                                    code = "INTERVIEW_ORIGINAL_MODE",
                                ),
                            )
                        }
                    }
                    openInterview(result.value.session_id)
                }
            }
        }
    }

    fun submitInterviewAnswer(
        sessionId: String,
        text: String,
        inputSource: String = "text",
        audioAssetId: String? = null,
        transcriptRevision: Int? = null,
    ) {
        val detail = state.value.interviewDetail
        val turn = detail?.current_turn
        if (detail == null || detail.session_id != sessionId || turn == null) {
            notify("当前没有可作答的回合，请刷新")
            return
        }
        if (state.value.pendingInterviewTurnId == turn.turn_id) {
            notify("正在确认提交结果；可离页，恢复后会按原 key 重放，不会新建重复操作", isError = false)
            return
        }
        if (text.isBlank()) {
            notify("回答不能为空白")
            return
        }
        if (text.codePointCount(0, text.length) > 4000) {
            notify("回答不能超过 4000 个字符；请先精简核对文本")
            return
        }
        if (inputSource != "text" && (audioAssetId == null || transcriptRevision == null)) {
            notify("语音来源缺少录音或转写版本，请刷新后重试")
            return
        }
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val submittedTakeId = if (inputSource == "text") null
        else state.value.voiceDraft?.active_audio?.take_id
        val jobKind = if (turn.kind == "main") "follow_up" else "interview_report"
        update {
            it.copy(
                loading = true,
                busyLabel = "提交回答…",
                voicePhase = if (inputSource == "text") it.voicePhase else VoicePhase.SUBMITTING,
            )
        }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewAnswer",
                InterviewAnswerPayload(
                    sessionId,
                    detail.revision,
                    turn.turn_id,
                    text,
                    inputSource,
                    audioAssetId,
                    transcriptRevision,
                ),
                targetId = sessionId,
                revision = detail.revision,
                onResult = { receipt ->
                    recordInterviewJob(ctx.accountId, receipt, sessionId, jobKind)
                },
            ) { key ->
                repository.submitInterviewAnswer(
                    sessionId,
                    detail.revision,
                    turn.turn_id,
                    text,
                    key,
                    inputSource,
                    audioAssetId,
                    transcriptRevision,
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            val okReceipt = (result as? ApiResult.Ok<InterviewReceiptDto>)?.value
            if (okReceipt != null && okReceipt.session_id == sessionId) {
                // 已确认的回执只清理匹配回合的草稿；草稿键归原账号原场。
                clearInterviewDraft(sessionId, turn.turn_id)
                if (inputSource != "text") {
                    // 只清理本次提交确认的 take；若用户已开始新录音，不能误删新草稿。
                    if (submittedTakeId != null) {
                        val cleared = clearVoiceTake(
                            ctx.accountId, sessionId, turn.turn_id, submittedTakeId,
                        )
                        if (!cleared) {
                            notify(
                                "回答已提交，但本机草稿清理失败；未删除已确认回答，请稍后刷新并重试",
                                isError = true,
                            )
                        }
                    }
                    resetVoiceUi()
                }
            }
            if (!isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> {
                    showError(result, ctx, page)
                    if (inputSource != "text") {
                        update { it.copy(voicePhase = VoicePhase.REVIEW) }
                    }
                    if (OperationCoordinator.isUncertain(
                            result.code,
                            result.httpStatus,
                            result.requestSent,
                        )
                    ) {
                        update {
                            it.copy(
                                pendingInterviewTurnId = turn.turn_id,
                                message = UiMessage(
                                    "正在确认提交结果；可离页，恢复后会按原 key 重放，不会新建重复操作",
                                    isError = false,
                                    code = "INTERVIEW_RECEIPT_UNKNOWN",
                                ),
                            )
                        }
                    } else {
                        update { it.copy(pendingInterviewTurnId = null) }
                    }
                }
                is ApiResult.Ok -> {
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            pendingInterviewTurnId = null,
                            voicePhase = VoicePhase.IDLE,
                            voiceReviewText = "",
                            voiceReviewStorageIssue = null,
                            voiceReviewDirty = false,
                            voiceConflict = false,
                            voiceError = null,
                            voiceTextFallback = false,
                            voiceFixtureActive = false,
                        )
                    }
                    loadInterview(sessionId)
                    result.value.job_id?.let { watchInterviewJob(page, sessionId, it) }
                }
            }
        }
    }

    fun skipInterviewTurn(sessionId: String) {
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            notify("请先停止录音，再跳过当前题", isError = true)
            return
        }
        stopVoicePlaybackInternal()
        val detail = state.value.interviewDetail
        val turn = detail?.current_turn
        if (detail == null || detail.session_id != sessionId || turn == null) {
            notify("当前没有可跳过的回合，请刷新")
            return
        }
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "跳过当前题…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewSkip",
                InterviewSkipPayload(sessionId, detail.revision, turn.turn_id),
                targetId = sessionId,
                revision = detail.revision,
                onResult = { receipt ->
                    recordInterviewJob(ctx.accountId, receipt, sessionId, "interview_report")
                },
            ) { key ->
                repository.skipInterviewTurn(sessionId, detail.revision, turn.turn_id, key)
            }
            if (!isAccountCurrent(ctx)) return@launch
            if (result is ApiResult.Ok) clearInterviewDraft(sessionId, turn.turn_id)
            if (!isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                    result.value.job_id?.let { watchInterviewJob(page, sessionId, it) }
                }
            }
        }
    }

    fun pauseInterview(sessionId: String) {
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            notify("请先停止录音，再暂停面试", isError = true)
            return
        }
        stopVoicePlaybackInternal()
        val ctx = currentRequestContext() ?: return
        val detail = state.value.interviewDetail ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "暂停面试…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewPause",
                InterviewPausePayload(sessionId, detail.revision),
                targetId = sessionId,
                revision = detail.revision,
            ) { key -> repository.pauseInterview(sessionId, detail.revision, key) }
            if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                }
            }
        }
    }

    fun resumeInterview(sessionId: String) {
        val ctx = currentRequestContext() ?: return
        val detail = state.value.interviewDetail ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "继续面试…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewResume",
                InterviewResumePayload(sessionId, detail.revision),
                targetId = sessionId,
                revision = detail.revision,
            ) { key -> repository.resumeInterview(sessionId, detail.revision, key) }
            if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                }
            }
        }
    }

    fun finishInterview(sessionId: String) {
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            notify("请先停止录音，再结束面试", isError = true)
            return
        }
        stopVoicePlaybackInternal()
        val ctx = currentRequestContext() ?: return
        val detail = state.value.interviewDetail ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "结束面试…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewFinish",
                InterviewFinishPayload(sessionId, detail.revision),
                targetId = sessionId,
                revision = detail.revision,
                onResult = { receipt ->
                    recordInterviewJob(ctx.accountId, receipt, sessionId, "interview_report")
                },
            ) { key -> repository.finishInterview(sessionId, detail.revision, key) }
            if (!isAccountCurrent(ctx)) return@launch
            if (result is ApiResult.Ok) {
                detail.transcript
                    .filter { it.status == "pending" }
                    .forEach { clearInterviewDraft(sessionId, it.turn_id) }
            }
            if (!isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                    result.value.job_id?.let { watchInterviewJob(page, sessionId, it) }
                }
            }
        }
    }

    fun continueWithoutFollowUp(sessionId: String) {
        val ctx = currentRequestContext() ?: return
        val detail = state.value.interviewDetail ?: return
        val job = detail.follow_up_job ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "保留主回答并继续…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewContinueWithoutFollowUp",
                InterviewContinuePayload(sessionId, detail.revision, job.id),
                targetId = sessionId,
                revision = detail.revision,
                onResult = { receipt ->
                    recordInterviewJob(ctx.accountId, receipt, sessionId, "interview_report")
                },
            ) { key ->
                repository.continueWithoutFollowUp(sessionId, detail.revision, job.id, key)
            }
            if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                    result.value.job_id?.let { watchInterviewJob(page, sessionId, it) }
                }
            }
        }
    }

    fun retryInterviewJob(sessionId: String, jobId: String, attempt: Int) {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "重试任务…", message = null) }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "retryJob",
                RetryJobPayload(attempt),
                targetId = jobId,
                revision = attempt,
                interviewSessionId = sessionId,
            ) { key -> repository.retryJob(jobId, attempt, key) }
            if (!isAccountCurrent(ctx) || !isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    loadInterview(sessionId)
                    watchInterviewJob(page, sessionId, jobId)
                }
            }
        }
    }

    fun createBasicInterviewReport(sessionId: String) {
        val ctx = currentRequestContext() ?: return
        val detail = state.value.interviewDetail ?: return
        val job = detail.report_job ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "生成基础报告…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewBasicReport",
                InterviewBasicReportPayload(sessionId, detail.revision, job.id),
                targetId = sessionId,
                revision = detail.revision,
            ) { key ->
                repository.createBasicInterviewReport(sessionId, detail.revision, job.id, key)
            }
            if (!isAccountCurrent(ctx)) return@launch
            val top = state.value.stack.lastOrNull()
            val sameSessionPage =
                (top is Screen.InterviewRoom && top.sessionId == sessionId) ||
                    (top is Screen.InterviewReport && top.sessionId == sessionId)
            if (!isScopeCurrent(page) && !sameSessionPage) {
                // 用户已离开：回执照常落库，原场历史可查到报告，不把新页面拉回旧场。
                if (state.value.stack.lastOrNull() == Screen.InterviewHistory) {
                    loadInterviewHistory(append = false)
                }
                return@launch
            }
            val tolerated = setOf("REPORT_NOT_AVAILABLE", "REPORT_PENDING")
            val originalError = (result as? ApiResult.Err)
                ?.takeIf { it.code !in tolerated }
            when (InterviewUiMerge.basicReportPolicy(top, sessionId)) {
                InterviewUiMerge.BasicReportPolicy.REFRESH_REPORT -> {
                    // 已在该场报告页：不重复 push，直接刷新详情与报告。
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            message = originalError?.let { err ->
                                UiMessage(err.message, isError = true, code = err.code)
                            } ?: it.message,
                        )
                    }
                    refreshInterviewSession(sessionId, includeReport = true)
                }
                InterviewUiMerge.BasicReportPolicy.OPEN_REPORT -> {
                    // 先 push 再加载，避免旧页面上下文发出的 GET 被归并闸门丢弃。
                    update { it.copy(loading = false, busyLabel = null) }
                    openInterviewReport(sessionId)
                    originalError?.let { err ->
                        update {
                            it.copy(message = UiMessage(err.message, isError = true, code = err.code))
                        }
                    }
                }
                InterviewUiMerge.BasicReportPolicy.LEAVE -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    if (result is ApiResult.Err) {
                        showError(result, ctx, page)
                    } else {
                        showInfo("基础报告已生成，可在面试记录中查看")
                    }
                }
            }
        }
    }

    fun openInterviewReport(sessionId: String) {
        val top = state.value.stack.lastOrNull()
        val sameReport = top is Screen.InterviewReport && top.sessionId == sessionId
        update {
            if (sameReport) {
                it.copy(interviewReportError = null)
            } else {
                it.copy(
                    interviewReport = null,
                    interviewReportError = null,
                    interviewOrphanDrafts = emptyList(),
                )
            }
        }
        if (!sameReport) {
            push(Screen.InterviewReport(sessionId))
        }
        refreshInterviewSession(sessionId, includeReport = true)
    }

    fun loadInterviewReport(sessionId: String) {
        refreshInterviewSession(sessionId, includeReport = true)
    }

    fun loadInterviewForPlan(planId: String) {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            when (val result = repository.interviews(limit = 1, planId = planId)) {
                is ApiResult.Ok -> if (isCurrent(ctx)) {
                    update { it.copy(interviewForPlan = result.value.items.firstOrNull()) }
                }
                is ApiResult.Err -> if (isCurrent(ctx)) {
                    update { it.copy(interviewForPlan = null) }
                }
            }
        }
    }

    private fun restoreInterviewJobs(sessionId: String, page: RestoreScope) {
        if (!isScopeCurrent(page)) return
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        viewModelScope.launch {
            for (kind in listOf("follow_up", "interview_report")) {
                val active = operations.activeJob(page.accountId, kind, sessionId) ?: continue
                if (!isScopeCurrent(page)) return@launch
                applyEffect(restoreCoordinator.restore(scopeForActiveJob(page, active)))
            }
            if (!isScopeCurrent(page)) return@launch
            val report = detail.report_job
            if (report != null && (report.state == "queued" || report.state == "running")) {
                watchReportJob(page, sessionId, report.id)
            }
            if (!isScopeCurrent(page)) return@launch
            val followUp = detail.follow_up_job
            if (followUp != null && (followUp.state == "queued" || followUp.state == "running")) {
                watchInterviewJob(page, sessionId, followUp.id)
            }
        }
    }

    private fun watchInterviewJob(page: RestoreScope, sessionId: String, jobId: String) {
        if (!isScopeCurrent(page)) return
        if (jobWatch.isWatching(page.accountId, jobId)) return
        viewModelScope.launch {
            val effect = restoreCoordinator.restore(
                page.copy(
                    jobId = jobId,
                    screen = RestoreJobService.SCREEN_INTERVIEW,
                    preparationId = null,
                    materialId = null,
                    interviewSessionId = sessionId,
                ),
            )
            if (!isScopeCurrent(page)) return@launch
            applyEffect(effect)
        }
    }

    private suspend fun recordInterviewJob(
        accountId: String,
        receipt: InterviewReceiptDto,
        sessionId: String,
        kind: String,
    ): OperationSave {
        val jobId = receipt.job_id ?: return OperationSave.Ok
        return operations.recordActiveJob(
            accountId,
            ActiveJob(
                jobId = jobId,
                kind = kind,
                targetId = sessionId,
                revision = receipt.revision,
            ),
        )
    }

    // ------------------------------------------------------------ 面试草稿（加密）

    fun interviewDraft(sessionId: String, turnId: String): String? {
        val account = accountId ?: return null
        return when (val read = interviewDrafts.get(account, sessionId, turnId)) {
            is SecureRead.Present -> read.value
            SecureRead.Absent -> null
            is SecureRead.Corrupt -> {
                notify("本机面试草稿无法解密，已保留原始数据；可继续输入后重新提交", isError = true)
                null
            }
            is SecureRead.Failed -> {
                notify("读取本机面试草稿失败：${read.message}", isError = true)
                null
            }
        }
    }

    fun saveInterviewDraft(sessionId: String, turnId: String, text: String): Boolean {
        val account = accountId ?: return false
        if (confirmedInterviewTurns.containsKey(draftTurnKey(account, sessionId, turnId))) {
            // 已确认提交的回合不再写入草稿；Compose 离页时也不能把刚清理的正文写回。
            interviewDrafts.clear(account, sessionId, turnId)
            return true
        }
        return when (val write = interviewDrafts.put(account, sessionId, turnId, text)) {
            SecureWrite.Ok -> true
            is SecureWrite.Failed -> {
                notify("草稿未保存：${write.message}", isError = true)
                false
            }
        }
    }

    fun clearInterviewDraft(sessionId: String, turnId: String) {
        val account = accountId ?: return
        markDraftResolved(account, sessionId, turnId)
    }

    private fun draftTurnKey(accountId: String, sessionId: String, turnId: String): String =
        "$accountId/$sessionId/$turnId"

    private fun markDraftResolved(accountId: String, sessionId: String, turnId: String) {
        confirmedInterviewTurns[draftTurnKey(accountId, sessionId, turnId)] = true
        interviewDrafts.clear(accountId, sessionId, turnId)
    }

    // ------------------------------------------------------------ M3 语音面试

    fun loadVoiceConfig() {
        val ctx = currentRequestContext() ?: return
        viewModelScope.launch {
            when (val result = repository.voiceConfig()) {
                is ApiResult.Ok -> if (isAccountCurrent(ctx)) {
                    update { it.copy(voiceConfig = result.value) }
                }
                is ApiResult.Err -> if (isAccountCurrent(ctx)) {
                    update { it.copy(voiceConfig = null) }
                }
            }
        }
    }

    fun onVoiceRoomOpened(sessionId: String) {
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        val turn = detail.current_turn ?: return
        if (detail.mode != "voice") return
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        viewModelScope.launch {
            loadVoiceDraftFor(ctx, page, sessionId, turn.turn_id)
        }
    }

    fun ensureVoicePrompt(sessionId: String, turnId: String) {
        val detail = state.value.interviewDetail ?: return
        if (detail.mode != "voice" || detail.status != "active") return
        if (lastAutoPlayedTurnId == turnId) return
        if (state.value.voicePhase !in setOf(
                VoicePhase.IDLE,
                VoicePhase.ERROR,
                VoicePhase.LOCAL_READY,
                VoicePhase.INTERRUPTED,
            )
        ) return
        lastAutoPlayedTurnId = turnId
        playVoiceSegment(sessionId, "turn", turnId)
    }

    fun playVoiceSegment(sessionId: String, segment: String, turnId: String?) {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        if (state.value.voicePhase == VoicePhase.RECORDING) {
            notify("录音期间不能播放题目；请先停止录音", isError = true)
            return
        }
        stopVoicePlaybackInternal()
        val op = VoiceOperation(
            ctx = ctx,
            page = page,
            sessionId = sessionId,
            turnId = turnId,
            operationGeneration = voiceOperationScope,
        )
        val generation = ++voicePlaybackGeneration
        update {
            it.copy(
                voicePhase = VoicePhase.TTS_LOADING,
                voicePlaybackTurnId = turnId,
                voiceError = null,
            )
        }
        viewModelScope.launch {
            val requested = repository.requestSpeech(
                sessionId,
                segment,
                turnId,
                UUID.randomUUID().toString(),
            )
            if (!isVoiceTurnCurrent(op) || generation != voicePlaybackGeneration) return@launch
            var asset: SpeechAssetDto? = when (requested) {
                is ApiResult.Ok -> requested.value
                is ApiResult.Err -> {
                    update { it.copy(voicePhase = VoicePhase.IDLE, voicePlaybackTurnId = null) }
                    showError(requested, ctx, page)
                    return@launch
                }
            }
            var polls = 0
            while (asset != null && asset.status != "ready" &&
                asset.status != "failed" && asset.status != "expired" && polls < 90
            ) {
                delay(1_000)
                polls += 1
                if (!isVoiceTurnCurrent(op) || generation != voicePlaybackGeneration) return@launch
                asset = when (val polled = repository.speechAsset(asset.id)) {
                    is ApiResult.Ok -> polled.value
                    is ApiResult.Err -> null
                }
            }
            if (!isVoiceTurnCurrent(op) || generation != voicePlaybackGeneration) return@launch
            if (asset == null || asset.status != "ready") {
                update { it.copy(voicePhase = VoicePhase.IDLE, voicePlaybackTurnId = null) }
                notify(
                    when (asset?.status) {
                        "failed" -> "语音生成失败：${asset.error_code ?: "未知错误"}；可再次播放以受控重试"
                        "expired" -> "语音缓存已过期，请重新播放"
                        else -> "语音尚未生成完成，请稍后重试"
                    },
                    isError = true,
                )
                return@launch
            }
            val readyAsset = asset
            val bytes = when (val content = repository.speechContent(readyAsset.id)) {
                is ApiResult.Ok -> content.value
                is ApiResult.Err -> {
                    if (isVoiceTurnCurrent(op) && generation == voicePlaybackGeneration) {
                        update { it.copy(voicePhase = VoicePhase.IDLE, voicePlaybackTurnId = null) }
                        showError(content, ctx, page)
                    }
                    return@launch
                }
            }
            if (!isVoiceTurnCurrent(op) || generation != voicePlaybackGeneration) return@launch
            val file = try {
                val owner = ctx.accountId
                val dir = File(File(File(filesCacheRoot(), "voice-speech"), owner), sessionId)
                dir.mkdirs()
                val target = File(dir, "${readyAsset.id}.wav")
                if (target.length() != bytes.size.toLong()) {
                    target.writeBytes(bytes)
                }
                target
            } catch (e: Exception) {
                update { it.copy(voicePhase = VoicePhase.IDLE, voicePlaybackTurnId = null) }
                notify("语音缓存写入失败：${e.javaClass.simpleName}", isError = true)
                return@launch
            }
            if (!isVoiceTurnCurrent(op) || generation != voicePlaybackGeneration) return@launch
            update {
                it.copy(
                    voicePhase = VoicePhase.PLAYING,
                    voicePlaybackTurnId = turnId,
                    voiceSpeechSynthetic = readyAsset.synthetic,
                    voiceError = null,
                )
            }
            val started = voiceServices?.player?.play(
                file = file,
                tag = "$generation/${readyAsset.id}",
                onFinished = { tag ->
                    if (tag.startsWith("$generation/") &&
                        generation == voicePlaybackGeneration &&
                        isVoiceTurnCurrent(op)
                    ) {
                        update {
                            if (it.voicePhase == VoicePhase.PLAYING) {
                                it.copy(
                                    voicePhase = phaseAfterPlayback(),
                                    voicePlaybackTurnId = null,
                                )
                            } else it
                        }
                    }
                },
                onError = { tag, message ->
                    if (tag.startsWith("$generation/") &&
                        generation == voicePlaybackGeneration &&
                        isVoiceTurnCurrent(op)
                    ) {
                        update {
                            it.copy(
                                voicePhase = phaseAfterPlayback(),
                                voicePlaybackTurnId = null,
                                voiceError = message,
                            )
                        }
                    }
                },
            ) ?: false
            if (started != true && isVoiceTurnCurrent(op) && generation == voicePlaybackGeneration) {
                update {
                    it.copy(
                        voicePhase = phaseAfterPlayback(),
                        voicePlaybackTurnId = null,
                    )
                }
            }
        }
    }

    fun stopVoicePlayback() {
        stopVoicePlaybackInternal()
        update {
            if (it.voicePhase == VoicePhase.PLAYING || it.voicePhase == VoicePhase.TTS_LOADING) {
                it.copy(
                    voicePhase = phaseAfterPlayback(),
                    voicePlaybackTurnId = null,
                )
            } else it
        }
    }

    private fun phaseAfterPlayback(): VoicePhase =
        if (state.value.voiceDraft?.transcript?.status == "ready") VoicePhase.REVIEW
        else VoicePhase.IDLE

    private fun stopVoicePlaybackInternal() {
        voicePlaybackGeneration += 1
        voiceServices?.player?.stop()
    }

    fun startVoiceRecording(sessionId: String) {
        val services = voiceServices ?: run {
            notify("本机录音组件不可用", isError = true)
            return
        }
        val recorder = services.recorder ?: run {
            notify("本机录音组件不可用", isError = true)
            return
        }
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        val turn = detail.current_turn
        if (detail.mode != "voice" || detail.status != "active" || turn == null ||
            detail.phase != "awaiting_answer"
        ) {
            notify("当前没有可录音的语音回合，请刷新")
            return
        }
        if (state.value.pendingInterviewTurnId == turn.turn_id) {
            android.util.Log.w("M3VoiceFlow", "record blocked: pending submit turn=${turn.turn_id}")
            notify("正在确认上一回合提交结果，不能开始新录音", isError = true)
            return
        }
        val account = accountId ?: return
        if (voiceStopJob?.isActive == true) {
            notify("正在保存上一段录音，请稍后再开始新录音", isError = true)
            return
        }
        // 先记住旧 take，但只有新录音真正启动后才清理；启动失败不能丢掉可恢复草稿。
        val previousTakeId = when (val prior = services.takes.read(account, sessionId, turn.turn_id)) {
            is VoiceTakeRead.Present -> prior.meta.takeId
            is VoiceTakeRead.MissingFile -> prior.meta.takeId
            is VoiceTakeRead.Corrupt -> prior.meta?.takeId
            else -> null
        }
        voiceOperationGeneration += 1
        voicePollGeneration += 1
        voiceDraftLoadGeneration += 1
        stopVoicePlaybackInternal()
        voiceTimerJob?.cancel()
        val takeId = UUID.randomUUID().toString()
        val file = services.takes.file(account, sessionId, turn.turn_id, takeId)
        val fixture = services.fixtureProvider()
        val started = if (fixture != null) {
            recorder.startFromFixture(file, fixture)
        } else {
            recorder.start(file)
        }
        if (!started.ok) {
            update {
                it.copy(
                    voicePhase = VoicePhase.ERROR,
                    voiceError = started.message,
                    voiceTurnId = turn.turn_id,
                )
            }
            return
        }
        if (previousTakeId != null) {
            services.takes.clear(account, sessionId, turn.turn_id, previousTakeId)
        }
        services.reviews.clear(account, sessionId, turn.turn_id)
        lastAutoPlayedTurnId = turn.turn_id
        android.util.Log.i(
            "M3VoiceFlow",
            "record started turn=${turn.turn_id} take=$takeId fixture=${fixture != null}",
        )
        update {
            it.copy(
                voicePhase = VoicePhase.RECORDING,
                voiceTurnId = turn.turn_id,
                voiceTake = null,
                voiceTakeIssue = null,
                voiceConflict = false,
                voiceDraft = it.voiceDraft?.copy(
                    active_audio = null,
                    transcript = null,
                    asr_job = null,
                ),
                voiceRemainingSeconds = 120,
                voiceRecordedSeconds = 0,
                voiceReviewText = "",
                voiceReviewStorageIssue = null,
                voiceReviewDirty = false,
                voiceError = null,
                voiceTextFallback = false,
                voiceFixtureActive = fixture != null,
            )
        }
        voiceTimerJob = viewModelScope.launch {
            while (isActive && state.value.voicePhase == VoicePhase.RECORDING) {
                delay(200)
                val snapshot = recorder.snapshot()
                if (!isActive || state.value.voicePhase != VoicePhase.RECORDING) break
                val remaining = ((120_000 - snapshot.elapsedMs).coerceAtLeast(0) / 1000).toInt()
                update {
                    if (it.voicePhase == VoicePhase.RECORDING) {
                        it.copy(
                            voiceRemainingSeconds = remaining,
                            voiceRecordedSeconds = (snapshot.elapsedMs / 1000).toInt(),
                            voiceError = snapshot.error,
                        )
                    } else it
                }
                if (snapshot.error != null) {
                    stopVoiceRecording(sessionId, interrupted = false)
                    break
                }
                if (snapshot.reachedLimit || snapshot.elapsedMs >= 120_000) {
                    stopVoiceRecording(sessionId, interrupted = false)
                    break
                }
            }
        }
    }

    fun stopVoiceRecording(sessionId: String, interrupted: Boolean = false) {
        android.util.Log.i("M3VoiceFlow", "stop requested session=$sessionId phase=${state.value.voicePhase} interrupted=$interrupted")
        if (state.value.voicePhase != VoicePhase.RECORDING) {
            android.util.Log.w(
                "M3VoiceFlow",
                "stop ignored: phase=${state.value.voicePhase}",
            )
            return
        }
        val turnId = state.value.voiceTurnId ?: return
        val services = voiceServices ?: return
        val recorder = services.recorder ?: return
        val account = accountId ?: return
        val sessionRevision = state.value.interviewDetail
            ?.takeIf { it.session_id == sessionId }?.revision
        val expectedVoiceRevision = state.value.voiceDraft?.voice_revision ?: 0
        val recordGeneration = voiceOperationGeneration
        voiceTimerJob?.cancel()
        voiceTimerJob = null
        update { it.copy(voicePhase = VoicePhase.LOCAL_READY) }
        val job = viewModelScope.launch {
            fun stillThisRecording(): Boolean =
                voiceOperationGeneration == recordGeneration &&
                    accountId == account &&
                    state.value.interviewDetail?.session_id == sessionId &&
                    state.value.interviewDetail?.current_turn?.turn_id == turnId

            val stop = withContext(ioDispatcher) { recorder.stop(interrupted) }
            android.util.Log.i("M3VoiceFlow", "recorder stop file=${stop.file?.name} bytes=${stop.byteLength} error=${stop.error}")
            if (!stillThisRecording()) {
                android.util.Log.w("M3VoiceFlow", "recorder stop superseded take=${stop.file?.name}")
                return@launch
            }
            val file = stop.file
            if (file == null || stop.byteLength <= 0 || !file.exists()) {
                if (stillThisRecording()) {
                    update {
                        it.copy(
                            voicePhase = VoicePhase.ERROR,
                            voiceError = if (stop.interrupted) "录音已中断且没有可保存的音频"
                            else "没有录到有效音频，请重新录音",
                        )
                    }
                }
                return@launch
            }
            val sha = try {
                withContext(ioDispatcher) {
                    MessageDigest.getInstance("SHA-256").digest(
                        services.takes.readAudio(
                            account,
                            sessionId,
                            turnId,
                            VoiceTakeMeta(
                                takeId = file.nameWithoutExtension,
                                operationKey = "",
                                sha256 = "",
                                byteLength = stop.byteLength,
                                durationMs = stop.durationMs.toInt(),
                                interrupted = stop.interrupted,
                                reachedLimit = stop.reachedLimit,
                                encrypted = true,
                            ),
                        ),
                    ).joinToString("") { "%02x".format(it) }
                }
            } catch (e: Exception) {
                android.util.Log.e("M3VoiceFlow", "read local recording failed ${e.javaClass.simpleName}")
                if (stillThisRecording()) {
                    update {
                        it.copy(
                            voicePhase = VoicePhase.ERROR,
                            voiceError = "读取本地录音失败：${e.javaClass.simpleName}",
                        )
                    }
                }
                return@launch
            }
            if (!stillThisRecording()) return@launch
            val meta = VoiceTakeMeta(
                takeId = file.nameWithoutExtension,
                operationKey = UUID.randomUUID().toString(),
                sha256 = sha,
                byteLength = stop.byteLength,
                durationMs = stop.durationMs.toInt(),
                interrupted = stop.interrupted,
                reachedLimit = stop.reachedLimit,
                format = "pcm_s16le/16000/mono",
                sessionRevision = sessionRevision,
                expectedVoiceRevision = expectedVoiceRevision,
                encrypted = true,
            )
            when (val written = services.takes.write(account, sessionId, turnId, meta)) {
                is com.mockmate.app.core.session.SecureWrite.Ok -> {
                    android.util.Log.i("M3VoiceFlow", "local take persisted take=${meta.takeId} sha=${meta.sha256.take(8)}")
                }
                is com.mockmate.app.core.session.SecureWrite.Failed -> {
                    android.util.Log.e("M3VoiceFlow", "local take persist failed ${written.message}")
                    if (stillThisRecording()) {
                        update {
                            it.copy(
                                voicePhase = VoicePhase.ERROR,
                                voiceError = "本地录音记录未保存：${written.message}",
                            )
                        }
                    }
                    return@launch
                }
            }
            val stillCurrent = stillThisRecording()
            if (stillCurrent) {
                update {
                    it.copy(
                        voicePhase = if (stop.interrupted) VoicePhase.INTERRUPTED else VoicePhase.LOCAL_READY,
                        voiceTake = meta,
                        voiceTakeIssue = null,
                        voiceConflict = false,
                        voiceError = stop.error,
                        voiceRemainingSeconds = if (stop.reachedLimit) 0 else it.voiceRemainingSeconds,
                        voiceRecordedSeconds = (stop.durationMs / 1000).toInt(),
                    )
                }
            }
            android.util.Log.i(
                "M3VoiceFlow",
                "recording stopped turn=$turnId bytes=${stop.byteLength} phase=${state.value.voicePhase} current=$stillCurrent",
            )
            if (stop.interrupted) {
                if (stillCurrent) {
                    notify("录音已停止并保存；回前台后可继续上传或重录，不会自动开始采集", isError = false)
                }
            } else if (stop.error == null && stillCurrent) {
                uploadVoiceTake(sessionId)
            }
        }
        voiceStopJob = job
        job.invokeOnCompletion { if (voiceStopJob === job) voiceStopJob = null }
    }

    private suspend fun finishVoiceRecordingInterrupted() {
        if (state.value.voicePhase != VoicePhase.RECORDING) return
        stopVoiceRecording(state.value.interviewDetail?.session_id ?: return, interrupted = true)
    }

    fun uploadVoiceTake(sessionId: String) {
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId }
        if (detail == null) {
            android.util.Log.w("M3VoiceFlow", "upload skipped: detail missing")
            return
        }
        val turn = detail.current_turn
        if (turn == null) {
            android.util.Log.w("M3VoiceFlow", "upload skipped: current turn null phase=${detail.phase}")
            return
        }
        val services = voiceServices ?: return
        val account = accountId ?: return
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val read = services.takes.read(account, sessionId, turn.turn_id)
        if (read !is VoiceTakeRead.Present) {
            android.util.Log.w("M3VoiceFlow", "upload skipped: local take unreadable type=${read::class.simpleName}")
            val message = when (read) {
                is VoiceTakeRead.MissingFile -> "本地录音文件已不存在，不能按空文件重发；请重新录音"
                is VoiceTakeRead.Corrupt -> "${read.message}；请重新录音"
                is VoiceTakeRead.UnsealedPart -> "发现上次录制中断留下的未封存片段，不能作为完整回答上传；请重新录音"
                is VoiceTakeRead.Failed -> read.message
                else -> "本地录音不可用，请重新录音"
            }
            update {
                it.copy(
                    voicePhase = VoicePhase.ERROR,
                    voiceTake = null,
                    voiceTakeIssue = message,
                    voiceError = message,
                )
            }
            return
        }

        // 首次网络发送前冻结完整请求；冷启动只能原 key、原 body 重放。
        val frozen = read.meta.copy(
            format = read.meta.format.ifBlank { "pcm_s16le/16000/mono" },
            sessionRevision = read.meta.sessionRevision ?: detail.revision,
            expectedVoiceRevision = read.meta.expectedVoiceRevision
                ?: state.value.voiceDraft?.voice_revision
                ?: 0,
            encrypted = read.meta.encrypted,
        )
        if (frozen != read.meta) {
            when (val written = services.takes.write(account, sessionId, turn.turn_id, frozen)) {
                is com.mockmate.app.core.session.SecureWrite.Ok -> Unit
                is com.mockmate.app.core.session.SecureWrite.Failed -> {
                    update {
                        it.copy(
                            voicePhase = VoicePhase.ERROR,
                            voiceError = "上传前无法保存原请求上下文：${written.message}",
                        )
                    }
                    return
                }
            }
        }
        update { it.copy(voiceTake = frozen, voicePhase = VoicePhase.UPLOADING, voiceError = null) }
        val op = VoiceOperation(
            ctx = ctx,
            page = page,
            sessionId = sessionId,
            turnId = turn.turn_id,
            takeId = frozen.takeId,
            kind = VoiceOperationKind.UPLOAD_TAKE,
            operationGeneration = voiceOperationScope,
        )
        viewModelScope.launch {
            val bytes = try {
                withContext(ioDispatcher) {
                    services.takes.readAudio(account, sessionId, turn.turn_id, frozen)
                }
            } catch (e: Exception) {
                if (isVoiceOperationCurrent(op)) {
                    update { it.copy(voicePhase = VoicePhase.ERROR, voiceError = "读取本地录音失败") }
                }
                return@launch
            }
            val actualSha = withContext(ioDispatcher) {
                MessageDigest.getInstance("SHA-256").digest(bytes)
                    .joinToString("") { "%02x".format(it) }
            }
            if (actualSha != frozen.sha256 || bytes.size != frozen.byteLength) {
                if (isVoiceOperationCurrent(op)) {
                    update {
                        it.copy(
                            voicePhase = VoicePhase.ERROR,
                            voiceTake = null,
                            voiceTakeIssue = "本地录音摘要不一致，不能按原文件重发；请重新录音",
                            voiceError = "本地录音摘要不一致",
                        )
                    }
                }
                return@launch
            }
            if (!isVoiceOperationCurrent(op)) return@launch
            android.util.Log.i(
                "M3VoiceFlow",
                "upload posting turn=${turn.turn_id} take=${frozen.takeId} expected=${frozen.expectedVoiceRevision} revision=${frozen.sessionRevision}",
            )
            val result = repository.uploadVoiceAudio(
                sessionId = sessionId,
                turnId = turn.turn_id,
                takeId = frozen.takeId,
                sessionRevision = frozen.sessionRevision ?: detail.revision,
                expectedVoiceRevision = frozen.expectedVoiceRevision ?: 0,
                sha256 = frozen.sha256,
                bytes = bytes,
                operationKey = frozen.operationKey,
            )
            when (result) {
                is ApiResult.Err -> {
                    if (!isVoiceOperationCurrent(op)) return@launch
                    if (result.code == "VOICE_REVISION_CONFLICT" ||
                        result.code == "AUDIO_TAKE_CONFLICT"
                    ) {
                        // 服务端可能已保存过原 take；重读草稿后决定是否继续转写。
                        val drafts = repository.voiceDraft(sessionId, turn.turn_id)
                        if (!isVoiceOperationCurrent(op)) return@launch
                        if (drafts is ApiResult.Ok) {
                            val active = drafts.value.active_audio
                            if (active != null && active.take_id == frozen.takeId) {
                                persistUploadedTake(account, sessionId, turn.turn_id, frozen, active.id, drafts.value.voice_revision)
                                update {
                                    it.copy(
                                        voiceDraft = drafts.value,
                                        voiceTake = state.value.voiceTake?.copy(
                                            uploaded = true,
                                            uploadedAssetId = active.id,
                                            uploadedVoiceRevision = drafts.value.voice_revision,
                                        ),
                                        voicePhase = VoicePhase.TRANSCRIBING,
                                    )
                                }
                                startTranscription(
                                    sessionId, turn.turn_id, active.id, drafts.value.voice_revision
                                )
                                return@launch
                            }
                        }
                    }
                    update { it.copy(voicePhase = VoicePhase.LOCAL_READY) }
                    val uncertain = result.code == "NETWORK_UNAVAILABLE" && result.requestSent
                    if (uncertain) {
                        notify(
                            "录音上传回执未确认；已保留原 key 和原请求，回到本回合或冷启动会按原请求恢复，不会换空文件重发。",
                            isError = false,
                        )
                    } else {
                        showError(result, ctx, page)
                    }
                }
                is ApiResult.Ok -> {
                    val receipt = result.value
                    if (receipt.audio.take_id != frozen.takeId) {
                        if (isVoiceOperationCurrent(op)) {
                            update {
                                it.copy(
                                    voicePhase = VoicePhase.ERROR,
                                    voiceError = "上传回执与本地 take 不一致，已保留本地录音",
                                )
                            }
                        }
                        return@launch
                    }
                    persistUploadedTake(
                        account, sessionId, turn.turn_id, frozen,
                        receipt.audio.id, receipt.voice_revision,
                    )
                    if (!isVoiceOperationCurrent(op)) return@launch
                    update {
                        it.copy(
                            voiceDraft = VoiceDraftDto(
                                session_id = sessionId,
                                turn_id = turn.turn_id,
                                voice_revision = receipt.voice_revision,
                                active_audio = receipt.audio,
                                transcript = null,
                                asr_job = null,
                            ),
                            voiceTake = state.value.voiceTake?.copy(
                                uploaded = true,
                                uploadedAssetId = receipt.audio.id,
                                uploadedVoiceRevision = receipt.voice_revision,
                            ),
                            voicePhase = VoicePhase.TRANSCRIBING,
                        )
                    }
                    startTranscription(
                        sessionId, turn.turn_id, receipt.audio.id, receipt.voice_revision
                    )
                }
            }
        }
    }

    private fun persistUploadedTake(
        accountId: String,
        sessionId: String,
        turnId: String,
        meta: VoiceTakeMeta,
        assetId: String,
        voiceRevision: Int,
    ) {
        // 迟到的上传回执不能覆盖已经开始的新录音元数据。
        val currentTakeId = when (val current = voiceServices?.takes?.read(accountId, sessionId, turnId)) {
            is VoiceTakeRead.Present -> current.meta.takeId
            is VoiceTakeRead.MissingFile -> current.meta.takeId
            is VoiceTakeRead.Corrupt -> current.meta?.takeId
            else -> null
        }
        if (currentTakeId != meta.takeId) {
            android.util.Log.w("M3VoiceFlow", "upload receipt ignored: local take changed or cleared")
            return
        }
        val stored = meta.copy(
            uploaded = true,
            uploadedAssetId = assetId,
            uploadedVoiceRevision = voiceRevision,
        )
        when (val written = voiceServices?.takes?.write(accountId, sessionId, turnId, stored)) {
            is com.mockmate.app.core.session.SecureWrite.Ok -> Unit
            is com.mockmate.app.core.session.SecureWrite.Failed -> android.util.Log.w(
                "M3VoiceFlow",
                "upload receipt persist failed: ${written.message}",
            )
            else -> Unit
        }
    }

    private fun startTranscription(
        sessionId: String,
        turnId: String,
        assetId: String,
        voiceRevision: Int,
    ) {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val op = VoiceOperation(
            ctx = ctx,
            page = page,
            sessionId = sessionId,
            turnId = turnId,
            assetId = assetId,
            voiceRevision = voiceRevision,
            kind = VoiceOperationKind.ACTIVE_ASSET,
            operationGeneration = voiceOperationScope,
        )
        val generation = ++voicePollGeneration
        update { it.copy(voicePhase = VoicePhase.TRANSCRIBING) }
        viewModelScope.launch {
            val created = repository.createAudioTranscription(
                assetId,
                voiceRevision,
                UUID.randomUUID().toString(),
            )
            if (!isVoiceOperationCurrent(op, generation)) return@launch
            when (created) {
                is ApiResult.Err -> {
                    update { it.copy(voicePhase = VoicePhase.ERROR) }
                    showError(created, ctx, page)
                }
                is ApiResult.Ok -> pollTranscript(op, generation)
            }
        }
    }

    private suspend fun pollTranscript(op: VoiceOperation, generation: Long) {
        val assetId = op.assetId ?: return
        for (attempt in 0 until 90) {
            if (!isVoiceOperationCurrent(op, generation)) return
            if (!appVisible) return
            when (val result = repository.audioTranscript(assetId)) {
                is ApiResult.Err -> {
                    if (!isVoiceOperationCurrent(op, generation)) return
                    update { it.copy(voicePhase = VoicePhase.ERROR) }
                    showError(result, op.ctx, op.page)
                    return
                }
                is ApiResult.Ok -> {
                    if (!isVoiceOperationCurrent(op, generation)) return
                    val transcript = result.value
                    if (transcript.audio_asset_id != assetId) return
                    when (transcript.status) {
                        "processing" -> Unit
                        "ready" -> {
                            val active = state.value.voiceDraft?.active_audio
                            val takeId = active?.take_id
                            val restored = if (active != null && takeId != null) {
                                storedVoiceReview(
                                    op.ctx.accountId,
                                    op.sessionId,
                                    op.turnId ?: return,
                                    active.id,
                                    takeId,
                                    transcript.revision,
                                )
                            } else StoredVoiceReview.Absent
                            val base = state.value
                            val keepMemory = hasInMemoryReviewEdit(
                                base,
                                op.turnId,
                                active?.id,
                                op.voiceRevision,
                                transcript.text.orEmpty(),
                            )
                            val text = when {
                                keepMemory -> base.voiceReviewText
                                restored is StoredVoiceReview.Present -> restored.text
                                else -> transcript.text.orEmpty()
                            }
                            update {
                                it.copy(
                                    voicePhase = VoicePhase.REVIEW,
                                    voiceReviewText = text,
                                    voiceReviewStorageIssue =
                                        (restored as? StoredVoiceReview.Unavailable)?.message,
                                    voiceReviewDirty = if (keepMemory) base.voiceReviewDirty else false,
                                    voiceDraft = it.voiceDraft?.copy(transcript = transcript),
                                    voiceError = null,
                                )
                            }
                            return
                        }
                        "empty" -> {
                            update {
                                it.copy(
                                    voicePhase = VoicePhase.ERROR,
                                    voiceDraft = it.voiceDraft?.copy(transcript = transcript),
                                    voiceError = "未识别到有效语音；可以重录或显式改用文字回答",
                                )
                            }
                            return
                        }
                        else -> {
                            val refreshed = when (
                                val loaded = repository.voiceDraft(op.sessionId, op.turnId ?: return)
                            ) {
                                is ApiResult.Ok -> loaded.value
                                is ApiResult.Err -> null
                            }
                            if (!isVoiceOperationCurrent(op, generation)) return
                            update {
                                val draft = refreshed?.takeIf { it.active_audio?.id == assetId }
                                it.copy(
                                    voicePhase = VoicePhase.ERROR,
                                    voiceDraft = (draft ?: it.voiceDraft?.copy(transcript = transcript))
                                        ?.copy(transcript = transcript),
                                    voiceError = "语音转写失败（${transcript.error_code ?: "未知错误"}）；原录音已保留，可显式重试或重录",
                                )
                            }
                            return
                        }
                    }
                }
            }
            delay(1_000)
        }
        if (isVoiceOperationCurrent(op, generation)) {
            update { it.copy(voicePhase = VoicePhase.ERROR, voiceError = "转写等待超时，原录音仍保留") }
        }
    }

    fun transcribeExisting(sessionId: String) {
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        val turn = detail.current_turn ?: return
        val draft = state.value.voiceDraft ?: run {
            onVoiceRoomOpened(sessionId)
            notify("正在读取语音草稿，请稍后重试", isError = false)
            return
        }
        val asset = draft.active_audio ?: return
        startTranscription(sessionId, turn.turn_id, asset.id, draft.voice_revision)
    }

    fun retryVoiceTranscription(sessionId: String) {
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        val turn = detail.current_turn ?: return
        val job = state.value.voiceDraft?.asr_job
        if (job == null) {
            transcribeExisting(sessionId)
            return
        }
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val asset = state.value.voiceDraft?.active_audio ?: return
        val voiceRevision = state.value.voiceDraft?.voice_revision ?: 0
        val op = VoiceOperation(
            ctx = ctx,
            page = page,
            sessionId = sessionId,
            turnId = turn.turn_id,
            assetId = asset.id,
            voiceRevision = voiceRevision,
            kind = VoiceOperationKind.ACTIVE_ASSET,
            operationGeneration = voiceOperationScope,
        )
        val generation = ++voicePollGeneration
        update { it.copy(voicePhase = VoicePhase.TRANSCRIBING) }
        viewModelScope.launch {
            // 轮询失败后内存中的 job 可能仍是旧 attempt；重试前读取当前任务状态。
            val current = when (val loaded = repository.job(job.id)) {
                is ApiResult.Ok -> loaded.value
                is ApiResult.Err -> {
                    if (!isVoiceOperationCurrent(op, generation)) return@launch
                    update { it.copy(voicePhase = VoicePhase.ERROR) }
                    showError(loaded, ctx, page)
                    return@launch
                }
            }
            if (!isVoiceOperationCurrent(op, generation)) return@launch
            if (!current.retryable) {
                update { it.copy(voicePhase = VoicePhase.ERROR) }
                notify(
                    "转写任务不可再自动重试（attempt=${current.attempt}，${current.error?.code ?: "无错误码"}）；请重录或改用文字回答",
                    isError = true,
                )
                return@launch
            }
            when (val result = repository.retryJob(current.id, current.attempt, UUID.randomUUID().toString())) {
                is ApiResult.Err -> {
                    if (!isVoiceOperationCurrent(op, generation)) return@launch
                    update { it.copy(voicePhase = VoicePhase.ERROR) }
                    showError(result, ctx, page)
                }
                is ApiResult.Ok -> {
                    if (!isVoiceOperationCurrent(op, generation)) return@launch
                    // 服务端已接受新 attempt；状态查询必须立即可见 queued/running。
                    pollTranscript(op, generation)
                }
            }
        }
    }

    fun submitVoiceReview(sessionId: String) {
        val detail = state.value.interviewDetail?.takeIf { it.session_id == sessionId } ?: return
        val turn = detail.current_turn ?: return
        val transcript = state.value.voiceDraft?.transcript ?: return
        val asset = state.value.voiceDraft?.active_audio ?: return
        if (transcript.status != "ready") {
            notify("转写尚未完成，不能提交", isError = true)
            return
        }
        val text = state.value.voiceReviewText
        if (text.isBlank()) {
            notify("核对文本不能为空白；可重录或改用文字回答", isError = true)
            return
        }
        val source = if (text == transcript.text) "asr" else "asr_corrected"
        submitInterviewAnswer(
            sessionId,
            text,
            inputSource = source,
            audioAssetId = asset.id,
            transcriptRevision = transcript.revision,
        )
    }

    fun updateVoiceReviewText(text: String) {
        if (text.codePointCount(0, text.length) <= 4000) {
            update { it.copy(voiceReviewText = text, voiceReviewDirty = true) }
            persistVoiceReviewText(text)
        }
    }

    /** 保存失败后用户显式重试；不删除原密文，也不静默降级为明文。 */
    fun retryVoiceReviewPersistence() {
        val text = state.value.voiceReviewText
        if (text.isBlank()) return
        persistVoiceReviewText(text)
    }

    /** 冲突后显式采用服务端当前录音，并丢弃本机已被取代的旧 take。 */
    fun useServerVoiceDraft(sessionId: String) {
        val account = accountId ?: return
        val turnId = state.value.voiceTurnId
            ?: state.value.interviewDetail?.current_turn?.turn_id
            ?: return
        voiceOperationGeneration += 1
        voicePollGeneration += 1
        voiceDraftLoadGeneration += 1
        state.value.voiceTake?.takeId?.let { takeId ->
            voiceServices?.takes?.clear(account, sessionId, turnId, takeId)
        }
        update {
            it.copy(
                voiceTake = null,
                voiceConflict = false,
                voiceTakeIssue = null,
                voiceError = null,
                voiceReviewStorageIssue = null,
                voiceReviewDirty = false,
            )
        }
        onVoiceRoomOpened(sessionId)
    }

    fun useVoiceTextFallback() {
        update { it.copy(voiceTextFallback = true, voicePhase = VoicePhase.IDLE) }
    }

    fun leaveVoiceTextFallback() {
        update { it.copy(voiceTextFallback = false) }
        val sessionId = (state.value.stack.lastOrNull() as? Screen.InterviewRoom)?.sessionId
        if (sessionId != null) onVoiceRoomOpened(sessionId)
    }

    fun discardVoiceTake(sessionId: String) {
        val account = accountId ?: return
        val turnId = state.value.voiceTurnId ?: state.value.interviewDetail?.current_turn?.turn_id
            ?: return
        val takeId = state.value.voiceTake?.takeId
        voiceOperationGeneration += 1
        voicePollGeneration += 1
        voiceServices?.takes?.clear(account, sessionId, turnId, takeId)
        voiceServices?.reviews?.clear(account, sessionId, turnId, takeId)
        update {
            it.copy(
                voiceTake = null,
                voicePhase = VoicePhase.IDLE,
                voiceTakeIssue = null,
                voiceConflict = false,
                voiceReviewText = "",
                voiceReviewStorageIssue = null,
                voiceReviewDirty = false,
                voiceError = null,
                voiceTextFallback = false,
            )
        }
    }

    fun onVoicePermissionDenied() {
        update {
            it.copy(
                voicePhase = VoicePhase.ERROR,
                voiceError = "未获得麦克风权限。可重新授权录音，或显式改用文字回答；不会假装已录音",
            )
        }
    }

    private suspend fun loadVoiceDraftFor(
        ctx: RequestContext,
        page: RestoreScope,
        sessionId: String,
        turnId: String,
    ) {
        if (!isScopeCurrent(page)) return
        if (state.value.voicePhase == VoicePhase.RECORDING) return
        if (state.value.voicePhase == VoicePhase.UPLOADING ||
            state.value.voicePhase == VoicePhase.TRANSCRIBING
        ) return
        val loadGeneration = ++voiceDraftLoadGeneration
        update { it.copy(voicePhase = VoicePhase.LOADING, voiceError = null) }
        val account = ctx.accountId
        val services = voiceServices
        val local = if (services == null) VoiceTakeRead.Absent else withContext(ioDispatcher) {
            services.takes.read(account, sessionId, turnId)
        }
        if (!isScopeCurrent(page) || loadGeneration != voiceDraftLoadGeneration) return
        var localTake: VoiceTakeMeta? = null
        var localIssue: String? = null
        when (local) {
            is VoiceTakeRead.Present -> {
                localTake = local.meta
                update { it.copy(voiceTake = local.meta, voiceTakeIssue = null) }
            }
            is VoiceTakeRead.MissingFile -> {
                update {
                    it.copy(
                        voiceTake = null,
                        voiceTakeIssue = "本地录音文件已不存在，不能按空文件重发；服务端已保存的录音仍可继续转写",
                    )
                }
            }
            is VoiceTakeRead.Corrupt -> {
                update {
                    it.copy(
                        voiceTake = null,
                        voiceTakeIssue = "${local.message}；请重新录音",
                    )
                }
            }
            is VoiceTakeRead.UnsealedPart -> {
                localIssue = "发现上次录制中断留下的未封存片段，不能作为完整回答；请重新录音"
                update { it.copy(voiceTake = null, voiceTakeIssue = localIssue) }
            }
            is VoiceTakeRead.Failed -> {
                localIssue = local.message
                update { it.copy(voiceTake = null, voiceTakeIssue = localIssue) }
            }
            VoiceTakeRead.Absent -> update {
                it.copy(voiceTake = null, voiceTakeIssue = null)
            }
        }
        if (!isScopeCurrent(page) || loadGeneration != voiceDraftLoadGeneration) return
        when (val result = repository.voiceDraft(sessionId, turnId)) {
            is ApiResult.Err -> {
                if (isScopeCurrent(page) && loadGeneration == voiceDraftLoadGeneration) {
                    update { it.copy(voicePhase = VoicePhase.IDLE) }
                    showError(result, ctx, page)
                }
            }
            is ApiResult.Ok -> {
                if (!isScopeCurrent(page) || loadGeneration != voiceDraftLoadGeneration) return
                val draft = result.value
                if (draft.session_id != sessionId || draft.turn_id != turnId) return
                val transcript = draft.transcript
                val active = draft.active_audio
                val base = state.value
                val sameTakeConfirmed = localTake != null && active?.take_id == localTake.takeId
                val pendingNewTake = localTake != null && !sameTakeConfirmed && !localTake.uploaded &&
                    (active == null || localTake.expectedVoiceRevision == draft.voice_revision)
                val staleLocalTake = localTake != null && !sameTakeConfirmed && !pendingNewTake
                val restoredReview = if (transcript?.status == "ready" && active != null) {
                    storedVoiceReview(
                        account,
                        sessionId,
                        turnId,
                        active.id,
                        active.take_id,
                        transcript.revision,
                    )
                } else StoredVoiceReview.Absent

                if (sameTakeConfirmed &&
                    (!localTake.uploaded ||
                        localTake.uploadedAssetId != active?.id ||
                        localTake.uploadedVoiceRevision != draft.voice_revision)
                ) {
                    // 上传 201 丢失后，服务端已确认同一 take：补记回执，后续只续 ASR，不重复入库。
                    val confirmed = localTake.copy(
                        uploaded = true,
                        uploadedAssetId = active?.id,
                        uploadedVoiceRevision = draft.voice_revision,
                    )
                    when (val written = voiceServices?.takes
                        ?.write(account, sessionId, turnId, confirmed)) {
                        is SecureWrite.Ok -> localTake = confirmed
                        is SecureWrite.Failed -> localIssue =
                            "服务端已确认本段录音，但本机记录未保存：${written.message}；重启后仍可从服务端恢复"
                        else -> Unit
                    }
                }

                val keepMemory = hasInMemoryReviewEdit(
                    base,
                    turnId,
                    active?.id,
                    draft.voice_revision,
                    transcript?.text.orEmpty(),
                )
                val next = when {
                    pendingNewTake -> base.copy(
                        voiceDraft = draft,
                        voicePhase = if (localTake?.interrupted == true) VoicePhase.INTERRUPTED
                        else VoicePhase.LOCAL_READY,
                        voiceTake = localTake,
                        voiceTakeIssue = null,
                        voiceConflict = false,
                        voiceReviewText = "",
                        voiceReviewStorageIssue = null,
                        voiceReviewDirty = false,
                        voiceTurnId = turnId,
                    )
                    staleLocalTake -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.ERROR,
                        voiceTake = localTake,
                        voiceConflict = true,
                        voiceError = "本机有一段旧版本录音，服务端已保存另一段录音；不会用旧 take 覆盖当前结果，也不会自动提交旧转写。请显式使用服务端当前录音或重新录音。",
                        voiceTakeIssue = null,
                        voiceReviewText = "",
                        voiceReviewStorageIssue = null,
                        voiceReviewDirty = false,
                        voiceTurnId = turnId,
                    )
                    transcript?.status == "ready" -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.REVIEW,
                        voiceReviewText = when {
                            keepMemory -> base.voiceReviewText
                            restoredReview is StoredVoiceReview.Present -> restoredReview.text
                            else -> transcript.text.orEmpty()
                        },
                        voiceReviewStorageIssue =
                            (restoredReview as? StoredVoiceReview.Unavailable)?.message,
                        voiceReviewDirty = if (keepMemory) base.voiceReviewDirty else false,
                        voiceTake = if (sameTakeConfirmed) localTake else null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    transcript?.status == "empty" -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.ERROR,
                        voiceError = "未识别到有效语音；可以重录或显式改用文字回答",
                        voiceTake = if (sameTakeConfirmed) localTake else null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    transcript?.status == "failed" -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.ERROR,
                        voiceError = "语音转写失败（${transcript.error_code ?: "未知错误"}）；原录音已保留，可显式重试或重录",
                        voiceTake = if (sameTakeConfirmed) localTake else null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    draft.asr_job?.state == "queued" || draft.asr_job?.state == "running" -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.TRANSCRIBING,
                        voiceTake = if (sameTakeConfirmed) localTake else null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    active != null -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.LOCAL_READY,
                        voiceTake = if (sameTakeConfirmed) localTake else null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    localTake != null -> base.copy(
                        voiceDraft = draft,
                        voicePhase = if (localTake.interrupted) VoicePhase.INTERRUPTED
                        else VoicePhase.LOCAL_READY,
                        voiceTake = localTake,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                    else -> base.copy(
                        voiceDraft = draft,
                        voicePhase = VoicePhase.IDLE,
                        voiceTake = null,
                        voiceTakeIssue = localIssue,
                        voiceConflict = false,
                        voiceTurnId = turnId,
                    )
                }
                update { current -> if (!isScopeCurrent(page)) current else next }
                when {
                    pendingNewTake -> {
                        // 未发送/回执未知的新 take：按冻结的原 key/body 恢复上传，不先展示旧转写。
                        uploadVoiceTake(sessionId)
                    }
                    staleLocalTake -> Unit
                    draft.asr_job?.state == "queued" || draft.asr_job?.state == "running" -> {
                        if (active != null) {
                            val op = VoiceOperation(
                                ctx = ctx,
                                page = page,
                                sessionId = sessionId,
                                turnId = turnId,
                                assetId = active.id,
                                voiceRevision = draft.voice_revision,
                                kind = VoiceOperationKind.ACTIVE_ASSET,
                                operationGeneration = voiceOperationScope,
                            )
                            val generation = ++voicePollGeneration
                            viewModelScope.launch { pollTranscript(op, generation) }
                        }
                    }
                    active != null && transcript == null && draft.asr_job == null -> {
                        // 上传已确认但回执/后续 ASR 在进程死亡时丢失：继续原操作，不重新上传。
                        startTranscription(sessionId, turnId, active.id, draft.voice_revision)
                    }
                }
            }
        }
    }

    private fun resetVoiceUi() {
        voiceTimerJob?.cancel()
        voiceTimerJob = null
        voiceOperationGeneration += 1
        voicePollGeneration += 1
        voiceDraftLoadGeneration += 1
        update {
            it.copy(
                voiceDraft = null,
                voicePhase = VoicePhase.IDLE,
                voiceTurnId = null,
                voiceTake = null,
                voiceTakeIssue = null,
                voiceConflict = false,
                voiceReviewText = "",
                voiceReviewStorageIssue = null,
                voiceReviewDirty = false,
                voiceError = null,
                voiceSpeechSynthetic = false,
                voicePlaybackTurnId = null,
                voiceTextFallback = false,
                voiceFixtureActive = false,
            )
        }
    }

    private fun clearVoiceTake(
        accountId: String,
        sessionId: String,
        turnId: String,
        takeId: String? = null,
    ): Boolean {
        val takeCleared = voiceServices?.takes?.clear(accountId, sessionId, turnId, takeId) ?: true
        val reviewCleared = voiceServices?.reviews?.clear(accountId, sessionId, turnId, takeId) ?: true
        if (!takeCleared || !reviewCleared) {
            android.util.Log.w(
                "M3VoiceFlow",
                "voice draft cleanup incomplete takeCleared=$takeCleared reviewCleared=$reviewCleared",
            )
        }
        return takeCleared && reviewCleared
    }

    private fun filesCacheRoot(): File =
        voiceServices?.cacheRoot ?: File(System.getProperty("java.io.tmpdir") ?: ".")



    private fun restoreOperations(accountId: String) {
        viewModelScope.launch {
            replayPendingOperations(accountId)
            restoreActiveJobs()
        }
    }

    private suspend fun replayPendingOperations(accountId: String) {
        val ctx = currentRequestContext() ?: return
        if (ctx.accountId != accountId) return
        val report = replayer.replay(
            accountId = accountId,
            isCurrent = { isAccountCurrent(ctx) },
            onCompleted = { op, value -> onRecoveredOperation(ctx, op, value) },
        )
        val storageError = report.storageError
        if (storageError != null) {
            // 重放提示同样只在原账号/原页面仍当前时展示，迟到结果不改另一个页面。
            if (isCurrent(ctx)) {
                update {
                    it.copy(
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(
                            "本地操作记录不可用：$storageError。为避免重复提交，未发送任何恢复请求；请保留数据并重新核对。",
                            isError = true,
                            code = "STORAGE_UNAVAILABLE",
                        ),
                    )
                }
            }
            return
        }
        val userMessage = report.userMessage
        if (userMessage != null && isCurrent(ctx)) {
            update {
                it.copy(
                    loading = false,
                    busyLabel = null,
                    message = UiMessage(userMessage, isError = true, code = "OPERATION_REPLAY_INCOMPLETE"),
                )
            }
        }
        if (report.completed > 0 && isAccountCurrent(ctx)) {
            refreshPreparations()
            refreshMaterials()
        }
    }

    private fun restoreActiveJobs() {
        val ctx = currentRequestContext() ?: return
        // 来源在读取本地记录和发起网络请求前捕获，等待结束后不会换成新页面。
        val base = currentScope() ?: return
        viewModelScope.launch {
            when (val loaded = operations.activeJobs(ctx.accountId)) {
                is OperationLoad.Corrupt ->
                    if (isCurrent(ctx)) notify("本地活跃任务记录损坏：${loaded.message}", isError = true)
                is OperationLoad.Failed ->
                    if (isCurrent(ctx)) notify("读取本地活跃任务失败：${loaded.message}", isError = true)
                OperationLoad.Empty -> Unit
                is OperationLoad.Loaded -> {
                    for (active in loaded.operations.jobs) {
                        if (!isCurrent(ctx)) return@launch
                        if (!activeJobMatchesPage(base, active)) continue
                        applyEffect(restoreCoordinator.restore(scopeForActiveJob(base, active)))
                    }
                }
            }
        }
    }

    private fun restoreJobFor(ctx: RequestContext, kind: String, preparationId: String) {
        val base = currentScope() ?: return
        viewModelScope.launch {
            val job = operations.activeJob(ctx.accountId, kind, preparationId) ?: return@launch
            if (!isAccountCurrent(ctx)) return@launch
            applyEffect(restoreCoordinator.restore(scopeForActiveJob(base, job)))
        }
    }

    /** 当前页面身份使用 Screen 自身的对象 ID，不依赖可能残留的全局 state.material。 */
    private fun currentScope(): RestoreScope? {
        val ctx = currentRequestContext() ?: return null
        val screen = state.value.stack.lastOrNull() ?: Screen.PreparationList
        val screenName = when (screen) {
            Screen.Auth -> "auth"
            Screen.PreparationList -> RestoreJobService.SCREEN_PREPARATION_LIST
            is Screen.Setup -> RestoreJobService.SCREEN_SETUP
            is Screen.Library -> RestoreJobService.SCREEN_LIBRARY
            is Screen.Editor -> RestoreJobService.SCREEN_EDITOR
            is Screen.Review -> RestoreJobService.SCREEN_REVIEW
            is Screen.Templates -> RestoreJobService.SCREEN_TEMPLATES
            is Screen.Match -> RestoreJobService.SCREEN_MATCH
            is Screen.PlanHub -> RestoreJobService.SCREEN_PLAN
            Screen.InterviewHistory -> "interview_history"
            is Screen.InterviewRoom -> RestoreJobService.SCREEN_INTERVIEW
            is Screen.InterviewReport -> RestoreJobService.SCREEN_INTERVIEW
        }
        val preparationId = when (screen) {
            is Screen.Setup -> screen.preparationId
            is Screen.Library -> screen.preparationId
            is Screen.Editor -> screen.preparationId
            is Screen.Review -> screen.preparationId
            is Screen.Templates -> screen.preparationId
            is Screen.Match -> screen.preparationId
            is Screen.PlanHub -> screen.preparationId
            Screen.InterviewHistory -> null
            is Screen.InterviewRoom -> null
            is Screen.InterviewReport -> null
            else -> state.value.preparation?.id
        }
        val materialId = when (screen) {
            is Screen.Review -> screen.materialId
            is Screen.Editor -> screen.materialId
            else -> null
        }
        val interviewSessionId = when (screen) {
            is Screen.InterviewRoom -> screen.sessionId
            is Screen.InterviewReport -> screen.sessionId
            else -> null
        }
        val screenKey = when (screen) {
            is Screen.InterviewRoom -> "interview_room"
            is Screen.InterviewReport -> "interview_report"
            else -> null
        }
        return RestoreScope(
            accountId = ctx.accountId,
            sessionGeneration = ctx.sessionGeneration,
            navigationEpoch = ctx.navigationEpoch,
            screen = screenName,
            preparationId = preparationId,
            materialId = materialId,
            interviewSessionId = interviewSessionId,
            screenKey = screenKey,
        )
    }

    private fun scopeForJob(
        kind: String,
        preparationId: String?,
        targetId: String,
        jobId: String = "",
    ): RestoreScope? {
        val base = currentScope() ?: return null
        val jobAttempt = state.value.job?.takeIf { jobId.isNotBlank() && it.id == jobId }?.attempt
        return when (kind) {
            "plan", "match" -> base.copy(
                jobId = jobId,
                preparationId = preparationId ?: base.preparationId,
                materialId = null,
                jobAttempt = jobAttempt,
            )
            "follow_up", "interview_report" -> base.copy(
                jobId = jobId,
                preparationId = null,
                materialId = null,
                interviewSessionId = targetId,
                jobAttempt = jobAttempt,
            )
            else -> base.copy(
                jobId = jobId,
                preparationId = preparationId ?: base.preparationId,
                materialId = targetId,
                materialRevision = state.value.material?.takeIf { it.id == targetId }?.revision,
                jobAttempt = jobAttempt,
            )
        }
    }

    private fun activeJobMatchesPage(base: RestoreScope, active: ActiveJob): Boolean =
        when (active.kind) {
            "plan" -> base.screen == RestoreJobService.SCREEN_PLAN &&
                (active.preparationId == null || base.preparationId == active.preparationId)
            "match" -> base.screen == RestoreJobService.SCREEN_MATCH &&
                (active.preparationId == null || base.preparationId == active.preparationId)
            "follow_up", "interview_report" ->
                base.screen == RestoreJobService.SCREEN_INTERVIEW &&
                    base.interviewSessionId == active.targetId
            else -> base.screen == RestoreJobService.SCREEN_REVIEW && base.materialId == active.targetId
        }

    private fun scopeForActiveJob(base: RestoreScope, active: ActiveJob): RestoreScope =
        when (active.kind) {
            "plan", "match" -> base.copy(
                jobId = active.jobId,
                preparationId = active.preparationId ?: base.preparationId,
                materialId = null,
            )
            "follow_up", "interview_report" -> base.copy(
                jobId = active.jobId,
                preparationId = null,
                materialId = null,
                interviewSessionId = active.targetId,
            )
            else -> base.copy(
                jobId = active.jobId,
                preparationId = active.preparationId ?: base.preparationId,
                materialId = active.targetId,
            )
        }

    private fun isStaleMaterialCallback(
        currentMat: MaterialDraftDto?,
        currentJob: JobDto?,
        incoming: JobDto,
    ): Boolean {
        if (incoming.kind !in setOf("extract", "parse")) return false
        if (currentMat == null) return false
        if (incoming.target_id != currentMat.id) return true
        if (incoming.input_revision < currentMat.revision && incoming.state != "superseded") {
            return true
        }
        if (incoming.kind == "extract" &&
            currentMat.latest_job_id != null &&
            currentMat.latest_job_id != incoming.id &&
            incoming.input_revision <= currentMat.revision
        ) {
            return true
        }
        if (incoming.kind == "extract" &&
            currentJob != null &&
            currentJob.kind == "parse" &&
            currentJob.target_id == currentMat.id &&
            incoming.input_revision <= currentJob.input_revision
        ) {
            return true
        }
        return false
    }

    /**
     * 在解析/提取写入回执进入状态投影前校验账号、页面、材料 ID、revision 与任务类型，
     * 并把本次权威回执 Job ID 一次性同步到 material.latest_job_id、materialJobLookup 与 job。
     */
    private fun syncMaterialWriteReceipt(
        ctx: RequestContext,
        scope: RestoreScope,
        expectedKind: String,
        receipt: JobDto,
    ): Boolean {
        if (!isAccountCurrent(ctx) || !isScopeCurrent(scope)) return false
        if (receipt.kind != expectedKind) return false
        var synced = false
        var previousJobId: String? = null
        update { current ->
            val mat = current.material
            if (mat == null ||
                mat.id != receipt.target_id ||
                mat.revision != receipt.input_revision ||
                (scope.materialId != null && scope.materialId != mat.id) ||
                (scope.materialRevision != null && scope.materialRevision != mat.revision) ||
                !InterviewUiMerge.shouldApplyJob(current.job, receipt)
            ) {
                return@update current
            }
            synced = true
            previousJobId = mat.latest_job_id
            val updatedMat = if (mat.latest_job_id == receipt.id) mat else mat.copy(latest_job_id = receipt.id)
            val needsResultFetch = receipt.state == "succeeded" &&
                ((receipt.kind == "parse" && updatedMat.profile == null) ||
                    (receipt.kind == "extract" && updatedMat.text.isNullOrBlank()))
            val lookupStatus = if (needsResultFetch) {
                MaterialJobLookupStatus.LOADING
            } else {
                MaterialJobLookupStatus.LOADED
            }
            current.copy(
                material = updatedMat,
                job = receipt,
                materialJobLookup = MaterialJobLookup(
                    materialId = updatedMat.id,
                    revision = updatedMat.revision,
                    jobId = receipt.id,
                    status = lookupStatus,
                ),
            )
        }
        if (synced && previousJobId != null && previousJobId != receipt.id) {
            jobWatch.cancel(ctx.accountId, previousJobId!!)
        }
        return synced
    }

    /** 唯一 UI 归并出口：Project/Inform/Error 只在原 scope 仍为当前页面时应用。 */
    private suspend fun applyEffect(effect: RestoreUiEffect) {
        when (effect) {
            RestoreUiEffect.Ignore -> Unit
            is RestoreUiEffect.Project -> {
                if (!isScopeCurrent(effect.result.scope)) return
                val outcome = effect.result.outcome as RestoreOutcome.Project
                // 同一场更低 revision 的详情不能覆盖已展示的新版本；旧任务结果也不回调 UI。
                if (outcome.interview != null &&
                    !InterviewUiMerge.shouldApplyDetail(state.value.interviewDetail, outcome.interview)
                ) {
                    return
                }
                if (outcome.job.kind !in setOf("follow_up", "interview_report") &&
                    !InterviewUiMerge.shouldApplyJob(state.value.job, outcome.job)
                ) {
                    return
                }
                if (outcome.job.kind in setOf("follow_up", "interview_report") &&
                    !InterviewUiMerge.interviewJobUpdateAllowed(
                        state.value.interviewDetail, outcome.job
                    )
                ) {
                    return
                }
                val currentPrep = state.value.preparation
                if (currentPrep != null &&
                    outcome.preparation != null &&
                    currentPrep.id == outcome.preparation.id &&
                    outcome.preparation.revision < currentPrep.revision
                ) {
                    return
                }
                if (currentPrep != null &&
                    outcome.matchReport != null &&
                    currentPrep.id == outcome.matchReport.preparation_id &&
                    outcome.matchReport.input_revision < currentPrep.revision
                ) {
                    return
                }
                if (currentPrep != null &&
                    outcome.plan != null &&
                    currentPrep.id == outcome.plan.preparation_id &&
                    outcome.plan.input_revision < currentPrep.revision
                ) {
                    return
                }
                val currentMat = state.value.material
                if (currentMat != null && outcome.material != null) {
                    if (outcome.material.id != currentMat.id ||
                        outcome.material.revision < currentMat.revision
                    ) {
                        return
                    }
                }
                if (isStaleMaterialCallback(currentMat, state.value.job, outcome.job)) {
                    return
                }
                // 回读到的权威材料已进入更高版本（或指向另一个最新任务）时，
                // 旧 Job 的结果只能标记其对应版本完成，不能继续呈现旧任务与旧完成提示。
                val readBackMaterial = outcome.material
                val materialJobSuperseded = readBackMaterial != null &&
                    outcome.job.kind in setOf("extract", "parse") &&
                    outcome.job.target_id == readBackMaterial.id &&
                    (
                        outcome.job.input_revision < readBackMaterial.revision ||
                            (
                                outcome.job.input_revision == readBackMaterial.revision &&
                                    readBackMaterial.latest_job_id != null &&
                                    readBackMaterial.latest_job_id != outcome.job.id
                                )
                        )
                val newerMaterial = readBackMaterial.takeIf { materialJobSuperseded }
                val latestMaterialJobId = newerMaterial?.latest_job_id
                val staleMaterialPointer = newerMaterial != null &&
                    latestMaterialJobId == outcome.job.id &&
                    outcome.job.input_revision < newerMaterial.revision
                val message = when {
                    outcome.interviewReport != null -> "报告已生成"
                    outcome.interview != null && outcome.job.kind == "interview_report" -> "报告状态已更新"
                    outcome.interview != null -> "面试状态已更新"
                    outcome.plan != null -> "计划已生成，请确认保存"
                    outcome.matchReport != null -> "匹配分析完成"
                    outcome.job.kind == "parse" -> "解析完成，请核对并确认结构；解析结果本身不是已确认事实"
                    outcome.job.kind == "extract" -> "文本提取完成，请核对结构后确认"
                    else -> outcome.message
                }
                update {
                    if (newerMaterial != null) {
                        // 旧任务不再作为当前任务展示；新版本有任务则从材料指针重新只读恢复。
                        val nextLookup = when {
                            latestMaterialJobId == null -> null
                            staleMaterialPointer -> MaterialJobLookup(
                                materialId = newerMaterial.id,
                                revision = newerMaterial.revision,
                                jobId = latestMaterialJobId,
                                status = MaterialJobLookupStatus.LOADED,
                            )
                            else -> MaterialJobLookup(
                                materialId = newerMaterial.id,
                                revision = newerMaterial.revision,
                                jobId = latestMaterialJobId,
                                status = MaterialJobLookupStatus.LOADING,
                            )
                        }
                        return@update it.copy(
                            material = newerMaterial,
                            materialJobLookup = nextLookup,
                            snapshots = outcome.snapshots ?: it.snapshots,
                            job = null,
                            ongoingStatus = if (it.ongoingStatus?.jobId == outcome.job.id) null else it.ongoingStatus,
                            loading = false,
                            busyLabel = null,
                            message = null,
                        )
                    }
                    val nextDetail = outcome.interview?.let { incoming ->
                        if (InterviewUiMerge.shouldApplyDetail(it.interviewDetail, incoming)) incoming
                        else it.interviewDetail
                    }
                    val nextJob = if (InterviewUiMerge.shouldApplyJob(it.job, outcome.job)) {
                        outcome.job
                    } else {
                        it.job
                    }
                    val mergedMaterial = when {
                        outcome.material == null -> it.material
                        it.material == null -> outcome.material
                        outcome.material.id != it.material.id -> outcome.material
                        outcome.material.revision > it.material.revision -> outcome.material
                        outcome.material.revision < it.material.revision -> it.material
                        else -> {
                            val keepText = outcome.material.text ?: it.material.text
                            val keepProfile = outcome.material.profile ?: it.material.profile
                            val keepState = when {
                                it.material.state == "parsed" && outcome.material.state != "parsed" -> it.material.state
                                it.material.state == "text_ready" && outcome.material.state == "uploaded" -> it.material.state
                                else -> outcome.material.state
                            }
                            outcome.material.copy(
                                text = keepText,
                                profile = keepProfile,
                                state = keepState,
                                latest_job_id = outcome.material.latest_job_id ?: it.material.latest_job_id,
                            )
                        }
                    }
                    val nextLookup = if (outcome.job.kind in setOf("extract", "parse") &&
                        mergedMaterial != null &&
                        outcome.job.target_id == mergedMaterial.id &&
                        outcome.job.input_revision == mergedMaterial.revision
                    ) {
                        MaterialJobLookup(
                            materialId = mergedMaterial.id,
                            revision = mergedMaterial.revision,
                            jobId = outcome.job.id,
                            status = MaterialJobLookupStatus.LOADED,
                        )
                    } else {
                        it.materialJobLookup
                    }
                    it.copy(
                        plan = outcome.plan ?: it.plan,
                        planError = if (outcome.plan != null) null else it.planError,
                        lastGeneratedPlanId = outcome.plan?.id ?: it.lastGeneratedPlanId,
                        preparation = outcome.preparation ?: it.preparation,
                        matchReport = outcome.matchReport ?: it.matchReport,
                        material = mergedMaterial,
                        materialJobLookup = nextLookup,
                        snapshots = outcome.snapshots ?: it.snapshots,
                        interviewDetail = nextDetail,
                        interviewReport = outcome.interviewReport ?: it.interviewReport,
                        interviewReportError = null,
                        job = nextJob,
                        ongoingStatus = if (it.ongoingStatus?.jobId == outcome.job.id) null else it.ongoingStatus,
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(message, isError = false),
                    )
                }
                if (newerMaterial != null) {
                    if (latestMaterialJobId != null && !staleMaterialPointer) {
                        fetchAndApplyLatestMaterialJob(
                            reviewScope = effect.result.scope.copy(
                                materialId = newerMaterial.id,
                                materialRevision = newerMaterial.revision,
                                jobId = latestMaterialJobId,
                                jobAttempt = null,
                            ),
                            expectedMaterial = newerMaterial,
                            latestJobId = latestMaterialJobId,
                            requireCurrentRevision = true,
                        )
                    }
                    jobWatch.cancel(effect.result.scope.accountId, outcome.job.id)
                    return
                }
                outcome.interview?.report_job?.let { summary ->
                    if (summary.state == "queued" || summary.state == "running") {
                        watchReportJob(
                            effect.result.scope,
                            outcome.interview.session_id,
                            summary.id,
                        )
                    }
                }
                jobWatch.cancel(effect.result.scope.accountId, outcome.job.id)
            }
            is RestoreUiEffect.Inform -> {
                if (!isScopeCurrent(effect.result.scope)) return
                val outcome = effect.result.outcome as RestoreOutcome.Inform
                val safeOutcomeMessage = if (outcome.job.error != null) {
                    ErrorDiagnosticsMapper.sanitizeRawServerMessage(
                        outcome.message,
                        outcome.job.error.code,
                    )
                } else {
                    outcome.message
                }
                if (outcome.job.kind in setOf("follow_up", "interview_report")) {
                    if (!InterviewUiMerge.interviewJobUpdateAllowed(
                            state.value.interviewDetail, outcome.job
                        )
                    ) {
                        return
                    }
                    val isOngoing = outcome.job.state == "queued" || outcome.job.state == "running"
                    if (isOngoing) {
                        val nextOngoing = OngoingStatus(
                            jobId = outcome.job.id,
                            jobKind = outcome.job.kind,
                            text = ongoingStatusText(outcome.job.kind, safeOutcomeMessage),
                        )
                        update {
                            it.copy(
                                loading = false,
                                busyLabel = null,
                                ongoingStatus = nextOngoing,
                            )
                        }
                        jobWatch.ensure(effect.result.scope, outcome.job)
                    } else {
                        update {
                            it.copy(
                                loading = false,
                                busyLabel = null,
                                ongoingStatus = if (it.ongoingStatus?.jobId == outcome.job.id) {
                                    null
                                } else {
                                    it.ongoingStatus
                                },
                                message = UiMessage(
                                    safeOutcomeMessage,
                                    isError = true,
                                    code = outcome.job.error?.code,
                                    details = outcome.job.error?.details ?: emptyList(),
                                    retryable = outcome.job.retryable,
                                    diagnosticContext = "jobId=${outcome.job.id} · kind=${outcome.job.kind} · state=${outcome.job.state} · attempt=${outcome.job.attempt}",
                                    jobKind = outcome.job.kind,
                                ),
                            )
                        }
                        jobWatch.cancel(effect.result.scope.accountId, outcome.job.id)
                        if (outcome.job.state == "failed" || outcome.job.state == "interrupted") {
                            refreshInterviewInBackground(effect.result.scope.interviewSessionId)
                        }
                    }
                    return
                }
                if (!InterviewUiMerge.shouldApplyJob(state.value.job, outcome.job)) return
                val currentPrep = state.value.preparation
                if (currentPrep != null &&
                    outcome.job.kind in setOf("match", "plan") &&
                    outcome.job.target_id == currentPrep.id &&
                    outcome.job.input_revision < currentPrep.revision &&
                    outcome.job.state != "superseded"
                ) {
                    return
                }
                if (currentPrep != null &&
                    outcome.job.kind in setOf("match", "plan") &&
                    outcome.job.target_id != currentPrep.id
                ) {
                    return
                }
                val currentMat = state.value.material
                if (isStaleMaterialCallback(currentMat, state.value.job, outcome.job)) {
                    return
                }
                if (currentMat != null &&
                    outcome.job.kind in setOf("extract", "parse") &&
                    outcome.job.target_id != currentMat.id
                ) {
                    return
                }
                val isOngoing = outcome.job.state == "queued" || outcome.job.state == "running"
                val planErr = outcome.job.error
                val formattedOutcomeMsg = if (outcome.job.kind == "plan" && planErr != null) {
                    PlanErrorFormatter.userMessage(planErr.code, safeOutcomeMessage, planErr.details)
                } else {
                    safeOutcomeMessage
                }
                val jobCtx = "jobId=${outcome.job.id} · kind=${outcome.job.kind} · state=${outcome.job.state} · attempt=${outcome.job.attempt} · target=${outcome.job.target_id} (rev=${outcome.job.input_revision})"
                val nextOngoing = if (isOngoing) {
                    OngoingStatus(
                        jobId = outcome.job.id,
                        jobKind = outcome.job.kind,
                        text = ongoingStatusText(outcome.job.kind, formattedOutcomeMsg),
                    )
                } else {
                    null
                }
                update {
                    val syncMaterial = outcome.job.kind in setOf("extract", "parse") &&
                        it.material != null &&
                        outcome.job.target_id == it.material.id &&
                        outcome.job.input_revision == it.material.revision
                    val nextMaterial = if (syncMaterial && it.material!!.latest_job_id != outcome.job.id) {
                        it.material.copy(latest_job_id = outcome.job.id)
                    } else {
                        it.material
                    }
                    val nextLookup = if (syncMaterial && nextMaterial != null) {
                        MaterialJobLookup(
                            materialId = nextMaterial.id,
                            revision = nextMaterial.revision,
                            jobId = outcome.job.id,
                            status = MaterialJobLookupStatus.LOADED,
                        )
                    } else {
                        it.materialJobLookup
                    }
                    it.copy(
                        material = nextMaterial,
                        job = outcome.job,
                        materialJobLookup = nextLookup,
                        planError = when {
                            outcome.job.kind == "plan" &&
                                outcome.job.state == "failed" &&
                                planErr != null &&
                                (currentPrep == null ||
                                    (outcome.job.target_id == currentPrep.id &&
                                        outcome.job.input_revision >= currentPrep.revision)) ->
                                PlanErrorState(
                                    code = planErr.code,
                                    message = formattedOutcomeMsg,
                                    details = planErr.details,
                                    rawServerMessage = ErrorDiagnosticsMapper.sanitizeRawServerMessage(
                                        planErr.message,
                                        planErr.code,
                                    ),
                                    retryable = outcome.job.retryable,
                                    diagnosticContext = jobCtx,
                                    jobId = outcome.job.id,
                                    preparationId = outcome.job.target_id,
                                    inputRevision = outcome.job.input_revision,
                                )
                            outcome.job.kind == "plan" &&
                                outcome.job.state in setOf("queued", "running", "superseded") -> null
                            else -> it.planError
                        },
                        ongoingStatus = if (isOngoing) {
                            nextOngoing
                        } else if (it.ongoingStatus?.jobId == outcome.job.id) {
                            null
                        } else {
                            it.ongoingStatus
                        },
                        loading = false,
                        busyLabel = null,
                        message = if (isOngoing) {
                            it.message
                        } else {
                            UiMessage(
                                formattedOutcomeMsg,
                                isError = true,
                                code = outcome.job.error?.code
                                    ?: if (outcome.job.state == "superseded") "SUPERSEDED" else null,
                                details = outcome.job.error?.details ?: emptyList(),
                                retryable = outcome.job.retryable,
                                diagnosticContext = jobCtx,
                                jobKind = outcome.job.kind,
                            )
                        },
                    )
                }
                if (outcome.job.state == "superseded" &&
                    outcome.job.kind in setOf("extract", "parse")
                ) {
                    val materialForRecovery = state.value.material
                    if (materialForRecovery != null &&
                        materialForRecovery.id == outcome.job.target_id &&
                        outcome.job.input_revision >= materialForRecovery.revision
                    ) {
                        // 首次打开、主动查询和运行中轮询收到失效任务时复用同一只读回读逻辑。
                        rereadMaterialAfterSuperseded(
                            reviewScope = effect.result.scope,
                            expectedMaterial = materialForRecovery,
                            supersededJob = outcome.job,
                        )
                        jobWatch.cancel(effect.result.scope.accountId, outcome.job.id)
                        return
                    }
                }
                if (isOngoing) {
                    // 唯一持续订阅：重复的前台恢复/手动查询不会叠加第二个轮询。
                    jobWatch.ensure(
                        effect.result.scope.copy(
                            jobId = outcome.job.id,
                            jobAttempt = outcome.job.attempt,
                        ),
                        outcome.job,
                    )
                } else {
                    jobWatch.cancel(effect.result.scope.accountId, outcome.job.id)
                }
            }
            is RestoreUiEffect.Error -> {
                if (!isScopeCurrent(effect.scope)) return
                val currentMat = state.value.material
                if (effect.scope.materialRevision != null &&
                    currentMat != null &&
                    currentMat.id == effect.scope.materialId &&
                    currentMat.revision != effect.scope.materialRevision
                ) {
                    return
                }
                val currentJob = state.value.job
                if (effect.scope.jobAttempt != null &&
                    currentJob != null &&
                    currentJob.id == effect.scope.jobId &&
                    effect.scope.jobAttempt < currentJob.attempt
                ) {
                    return
                }
                if (effect.scope.screen == RestoreJobService.SCREEN_REVIEW && effect.scope.materialId != null) {
                    if (currentMat != null &&
                        currentMat.id == effect.scope.materialId &&
                        currentMat.latest_job_id != null &&
                        effect.scope.jobId.isNotBlank() &&
                        currentMat.latest_job_id != effect.scope.jobId
                    ) {
                        return
                    }
                    if (currentJob != null &&
                        currentJob.target_id == effect.scope.materialId &&
                        currentJob.kind == "parse" &&
                        effect.scope.jobId.isNotBlank() &&
                        currentJob.id != effect.scope.jobId
                    ) {
                        return
                    }
                    val jobIdToMark = effect.scope.jobId.ifBlank { currentMat?.latest_job_id.orEmpty() }
                    update { current ->
                        val mat = current.material
                        val sameMat = mat != null && mat.id == effect.scope.materialId
                        val sameRevision = effect.scope.materialRevision == null ||
                            (mat != null && mat.revision == effect.scope.materialRevision)
                        val sameJobId = mat?.latest_job_id == null || mat.latest_job_id == jobIdToMark
                        val hasKnownTerminalFailure = current.job != null &&
                            current.job.id == jobIdToMark &&
                            current.job.state in setOf("failed", "interrupted", "superseded")
                        val alreadyHasMaterialResult = mat != null && current.job != null &&
                            current.job.id == jobIdToMark &&
                            current.job.state == "succeeded" &&
                            ((current.job.kind == "parse" && mat.profile != null) ||
                                (current.job.kind == "extract" && !mat.text.isNullOrBlank()))
                        if (sameMat &&
                            sameRevision &&
                            sameJobId &&
                            jobIdToMark.isNotBlank() &&
                            !hasKnownTerminalFailure &&
                            !alreadyHasMaterialResult
                        ) {
                            val updatedMat = if (mat!!.latest_job_id == null) mat.copy(latest_job_id = jobIdToMark) else mat
                            current.copy(
                                material = updatedMat,
                                materialJobLookup = MaterialJobLookup(
                                    materialId = mat.id,
                                    revision = mat.revision,
                                    jobId = jobIdToMark,
                                    status = MaterialJobLookupStatus.FAILED,
                                    errorMessage = ErrorDiagnosticsMapper.sanitizeRawServerMessage(
                                        effect.message,
                                        effect.code,
                                    ),
                                ),
                            )
                        } else {
                            current
                        }
                    }
                }
                if (effect.scope.jobId.isNotBlank() &&
                    !jobWatch.isWatching(effect.scope.accountId, effect.scope.jobId)
                ) {
                    clearOngoingStatus(effect.scope.jobId)
                }
                showError(
                    ApiResult.Err(effect.code, effect.message, false),
                    RequestContext(
                        effect.scope.accountId,
                        effect.scope.sessionGeneration,
                        effect.scope.navigationEpoch,
                    ),
                    effect.scope,
                )
            }
        }
    }

    /** 报告任务由 worker 在追问结清后创建；从详情里拿到 job_id 后再建立唯一订阅。 */
    private fun watchReportJob(scope: RestoreScope, sessionId: String, jobId: String) {
        if (!isScopeCurrent(scope)) return
        if (jobWatch.isWatching(scope.accountId, jobId)) return
        viewModelScope.launch {
            when (val job = repository.job(jobId)) {
                is ApiResult.Ok -> {
                    if (!isScopeCurrent(scope)) return@launch
                    val entry = scope.copy(
                        jobId = jobId,
                        screen = RestoreJobService.SCREEN_INTERVIEW,
                        interviewSessionId = sessionId,
                    )
                    val effect = restoreCoordinator.project(entry, job.value)
                    if (!isScopeCurrent(scope)) return@launch
                    applyEffect(effect)
                }
                is ApiResult.Err -> Unit
            }
        }
    }

    private fun refreshInterviewInBackground(sessionId: String?) {
        if (sessionId == null) return
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        if (page.interviewSessionId != sessionId) return
        viewModelScope.launch {
            when (val result = repository.interview(sessionId)) {
                is ApiResult.Ok -> {
                    if (!isScopeCurrent(page)) return@launch
                    val pending = operations.pendingFor(
                        ctx.accountId, "interviewAnswer", sessionId
                    )
                    if (!isScopeCurrent(page)) return@launch
                    val pendingTurn = (pending?.payload as? InterviewAnswerPayload)?.turnId
                    applyInterviewDetail(ctx.accountId, page, sessionId, result.value, pendingTurn)
                }
                is ApiResult.Err -> Unit
            }
        }
    }

    /** 任务失败/暂停后的显式查询入口：只 GET，不重新 POST。 */
    fun queryCurrentJob() {
        val job = state.value.job ?: return
        val ctx = currentRequestContext() ?: return
        val entry = scopeForJob(job.kind, state.value.preparation?.id, job.target_id, job.id) ?: return
        viewModelScope.launch {
            if (!isAccountCurrent(ctx)) return@launch
            applyEffect(restoreCoordinator.query(entry))
        }
    }

    // ------------------------------------------------------------ 本地草稿

    fun draft(name: String): String? = accountId?.let { drafts.get(it, name) }

    fun saveDraft(name: String, value: String) {
        accountId?.let { drafts.put(it, name, value) }
    }

    fun clearDraft(name: String) {
        accountId?.let { drafts.clear(it, name) }
    }

    fun dismissMessage(message: UiMessage) = update {
        if (it.message == message) it.copy(message = null) else it
    }

    internal fun closeForTest() {
        jobWatch.cancelAll()
        onCleared()
    }

    override fun onCleared() {
        jobWatch.cancelAll()
        api.close()
        super.onCleared()
    }

    companion object {
        fun factory(application: Application): ViewModelProvider.Factory =
            object : ViewModelProvider.Factory {
                @Suppress("UNCHECKED_CAST")
                override fun <T : ViewModel> create(modelClass: Class<T>): T =
                    AppViewModel(application) as T
            }
    }
}

