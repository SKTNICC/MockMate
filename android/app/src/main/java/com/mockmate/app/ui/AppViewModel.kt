package com.mockmate.app.ui

import android.app.Application
import android.net.Uri
import androidx.lifecycle.ViewModel
import androidx.lifecycle.ViewModelProvider
import androidx.lifecycle.viewModelScope
import com.mockmate.app.BuildConfig
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
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelChildren
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

data class PlanErrorState(
    val code: String?,
    val message: String,
    val details: List<String> = emptyList(),
    val rawServerMessage: String? = null,
    val httpStatus: Int? = null,
    val retryable: Boolean = false,
    val diagnosticContext: String? = null,
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
) : ViewModel() {
    constructor(application: Application) : this(
        sessionStore = SecureSessionStore(application),
        drafts = DraftStore(application),
        interviewDrafts = InterviewDraftStore(application),
        operationStore = SecureOperationStore(application),
        uriReader = { uri ->
            application.contentResolver.openInputStream(uri)?.use { it.readBytes() }
        },
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
    )

    val state = androidx.compose.runtime.mutableStateOf(AppState())
    private var loadToken = 0
    private val contextGeneration = ContextGeneration()
    private val interviewRecoveryLocks = java.util.concurrent.ConcurrentHashMap<String, Mutex>()
    private val confirmedInterviewTurns = java.util.concurrent.ConcurrentHashMap<String, Boolean>()
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

    private fun bumpNavigation() {
        contextGeneration.bumpNavigation()
        // 页面/账号切换后旧订阅立即停止；回到目标页时由恢复路径重新建立唯一订阅。
        jobWatch.cancelAll()
    }

    fun setAppVisible(visible: Boolean) {
        appVisible = visible
        if (visible) {
            resumeInterviewAfterForeground()
            restoreActiveJobs()
        } else {
            jobWatch.cancelAll()
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
        restoreOperations(user.id)
    }

    fun logout() {
        val old = session.current ?: return
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
        if (!isAccountCurrent(ctx)) return
        update {
            it.copy(
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
        val ctx = currentRequestContext() ?: return
        val entry = scopeForJob("extract", null, materialId) ?: return
        update { it.copy(loading = true, busyLabel = "提取文件文本…") }
        viewModelScope.launch {
            val job = performWrite(
                ctx,
                "extractMaterial",
                ExtractMaterialPayload(material.revision),
                targetId = materialId,
                revision = material.revision,
            ) { key -> repository.extractMaterial(materialId, material.revision, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (job) {
                is ApiResult.Err -> showError(job, ctx)
                is ApiResult.Ok -> {
                    applyEffect(restoreCoordinator.project(entry.copy(jobId = job.value.id), job.value))
                }
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
        update { it.copy(material = null, snapshots = emptyList()) }
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
                snapshots = emptyList(),
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
            update {
                it.copy(
                    material = (material as ApiResult.Ok).value,
                    snapshots = okOrNull(snapshots) ?: emptyList(),
                    loading = false,
                    busyLabel = null,
                )
            }
            push(Screen.Review(materialId, preparationId))
            refreshSkillCodes()
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
                    when (extract) {
                        is ApiResult.Err -> showError(extract, screenRequestContext)
                        is ApiResult.Ok -> {
                            applyEffect(
                                restoreCoordinator.project(entry.copy(jobId = extract.value.id), extract.value)
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
        val ctx = currentRequestContext() ?: return
        val entry = scopeForJob("parse", null, materialId) ?: return
        update { it.copy(loading = true, busyLabel = "AI 解析中…") }
        viewModelScope.launch {
            val job = performWrite(
                ctx,
                "parseMaterial",
                ParseMaterialPayload(material.revision, roleId, level),
                targetId = materialId,
                revision = material.revision,
            ) { key -> repository.parseMaterial(materialId, material.revision, roleId, level, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (job) {
                is ApiResult.Err -> showError(job, ctx)
                is ApiResult.Ok -> {
                    applyEffect(restoreCoordinator.project(entry.copy(jobId = job.value.id), job.value))
                }
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
        update { it.copy(job = null) }
        push(Screen.Match(preparationId))
        val ctx = currentRequestContext() ?: return
        val prep = state.value.preparation
        if (prep?.match_report_id != null) {
            viewModelScope.launch {
                when (val result = repository.matchReport(prep.match_report_id)) {
                    is ApiResult.Ok -> if (isCurrent(ctx)) update { it.copy(matchReport = result.value) }
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
        update { it.copy(loading = true, busyLabel = "匹配分析中…", matchReport = null) }
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
        update { it.copy(job = null, planError = null) }
        push(Screen.PlanHub(preparationId))
        val ctx = currentRequestContext() ?: return
        val prep = state.value.preparation
        if (prep == null || prep.id != preparationId) {
            val token = ++loadToken
            viewModelScope.launch {
                val loaded = repository.preparation(preparationId)
                if (loadToken != token || !isCurrent(ctx)) return@launch
                if (loaded is ApiResult.Ok) {
                    update { it.copy(preparation = loaded.value) }
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
                    update { it.copy(plan = result.value) }
                    loadInterviewForPlan(planId)
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
            update { it.copy(preparation = prepValue) }
            val planId = prepValue.current_plan_id
            if (planId == null) {
                update { it.copy(plan = null, message = UiMessage("服务端尚未记录当前计划", isError = true)) }
                return@launch
            }
            when (val plan = repository.plan(planId)) {
                is ApiResult.Err -> showError(plan, ctx)
                is ApiResult.Ok -> if (isCurrent(ctx)) update {
                    it.copy(
                        plan = plan.value,
                        message = UiMessage("计划摘要已刷新（未重新生成）", isError = false),
                    )
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
        update { it.copy(loading = true, busyLabel = "生成计划中…", plan = null, planError = null) }
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
                        update {
                            it.copy(
                                planError = PlanErrorState(
                                    code = job.code,
                                    message = formattedMessage,
                                    details = job.details,
                                    rawServerMessage = job.message,
                                    httpStatus = job.httpStatus,
                                    retryable = job.retryable,
                                    diagnosticContext = "POST /preparations/$preparationId/generate-plan · rev=${prep.revision}",
                                ),
                            )
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
        update { it.copy(loading = true, busyLabel = "重试中…") }
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
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val prep = state.value.preparation
        if (job.kind in setOf("match", "plan") && prep == null) {
            update { it.copy(message = UiMessage("准备尚未加载完成，请返回准备页重试")) }
            return
        }
        val entry = scopeForJob(job.kind, prep?.id, job.target_id, job.id) ?: return
        update { it.copy(loading = true, busyLabel = "重试中…") }
        viewModelScope.launch {
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
                    applyEffect(restoreCoordinator.project(entry, result.value))
                }
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

    fun redoPreparation() {
        val prep = state.value.preparation ?: return
        val resumeId = state.value.resumeSnapshot?.id ?: prep.resume_snapshot_id
        val jdId = state.value.jdSnapshot?.id ?: prep.jd_snapshot_id
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "重新制定…") }
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
                is ApiResult.Err -> showError(result)
                is ApiResult.Ok -> {
                    bumpNavigation()
                    update {
                        it.copy(
                            preparation = result.value,
                            plan = null,
                            matchReport = null,
                            loading = false,
                            busyLabel = null,
                            interviewForPlan = null,
                            stack = listOf(Screen.PreparationList, Screen.Setup(result.value.id)),
                            message = UiMessage("已新建准备，旧计划保留只读", isError = false),
                        )
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
        update {
            it.copy(
                interviewDetail = null,
                interviewReport = null,
                interviewReportError = null,
                pendingInterviewTurnId = null,
                interviewOrphanDrafts = emptyList(),
            )
        }
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
        update {
            if (!isScopeCurrent(page)) {
                it
            } else if (detail.session_id != sessionId ||
                !InterviewUiMerge.shouldApplyDetail(it.interviewDetail, detail)
            ) {
                if (clearLoading) it.copy(loading = false, busyLabel = null) else it
            } else {
                it.copy(
                    interviewDetail = detail,
                    pendingInterviewTurnId = pendingTurnId,
                    interviewOrphanDrafts = collectOrphanDrafts(
                        accountId,
                        sessionId,
                        detail,
                    ),
                    loading = if (clearLoading) false else it.loading,
                    busyLabel = if (clearLoading) null else it.busyLabel,
                )
            }
        }
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

    fun startInterviewForPlan(preparationId: String, plan: PublicPlanDto) {
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        update { it.copy(loading = true, busyLabel = "开始文字面试…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "startInterview",
                StartInterviewPayload(plan.revision),
                targetId = plan.id,
                revision = plan.revision,
                preparationId = preparationId,
            ) { key -> repository.startInterview(plan.id, plan.revision, key) }
            if (!isAccountCurrent(ctx)) return@launch
            // 回执已经落到原账号；只有仍停留在原计划页且目标计划未变时才允许导航。
            if (!isScopeCurrent(page) || state.value.plan?.id != plan.id) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx, page)
                is ApiResult.Ok -> {
                    update { it.copy(loading = false, busyLabel = null) }
                    openInterview(result.value.session_id)
                }
            }
        }
    }

    fun submitInterviewAnswer(sessionId: String, text: String) {
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
            notify("回答不能超过 4000 个字符")
            return
        }
        val ctx = currentRequestContext() ?: return
        val page = pageSnapshot() ?: return
        val jobKind = if (turn.kind == "main") "follow_up" else "interview_report"
        update { it.copy(loading = true, busyLabel = "提交回答…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "interviewAnswer",
                InterviewAnswerPayload(sessionId, detail.revision, turn.turn_id, text),
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
                )
            }
            if (!isAccountCurrent(ctx)) return@launch
            val okReceipt = (result as? ApiResult.Ok<InterviewReceiptDto>)?.value
            if (okReceipt != null && okReceipt.session_id == sessionId) {
                // 已确认的回执只清理匹配回合的草稿；草稿键归原账号原场。
                clearInterviewDraft(sessionId, turn.turn_id)
            }
            if (!isScopeCurrent(page)) return@launch
            when (result) {
                is ApiResult.Err -> {
                    showError(result, ctx, page)
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
                        )
                    }
                    loadInterview(sessionId)
                    result.value.job_id?.let { watchInterviewJob(page, sessionId, it) }
                }
            }
        }
    }

    fun skipInterviewTurn(sessionId: String) {
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
        update { it.copy(loading = true, busyLabel = "重试任务…") }
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

    // ------------------------------------------------------------ 冷启恢复：只读查询 / 原 key 回放

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
        return when (kind) {
            "plan", "match" -> base.copy(
                jobId = jobId,
                preparationId = preparationId ?: base.preparationId,
                materialId = null,
            )
            "follow_up", "interview_report" -> base.copy(
                jobId = jobId,
                preparationId = null,
                materialId = null,
                interviewSessionId = targetId,
            )
            else -> base.copy(
                jobId = jobId,
                preparationId = preparationId ?: base.preparationId,
                materialId = targetId,
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
                    val nextDetail = outcome.interview?.let { incoming ->
                        if (InterviewUiMerge.shouldApplyDetail(it.interviewDetail, incoming)) incoming
                        else it.interviewDetail
                    }
                    val nextJob = if (InterviewUiMerge.shouldApplyJob(it.job, outcome.job)) {
                        outcome.job
                    } else {
                        it.job
                    }
                    it.copy(
                        plan = outcome.plan ?: it.plan,
                        planError = if (outcome.plan != null) null else it.planError,
                        lastGeneratedPlanId = outcome.plan?.id ?: it.lastGeneratedPlanId,
                        preparation = outcome.preparation ?: it.preparation,
                        matchReport = outcome.matchReport ?: it.matchReport,
                        material = outcome.material ?: it.material,
                        snapshots = outcome.snapshots ?: it.snapshots,
                        interviewDetail = nextDetail,
                        interviewReport = outcome.interviewReport ?: it.interviewReport,
                        interviewReportError = null,
                        job = nextJob,
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(message, isError = false),
                    )
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
            }
            is RestoreUiEffect.Inform -> {
                if (!isScopeCurrent(effect.result.scope)) return
                val outcome = effect.result.outcome as RestoreOutcome.Inform
                if (outcome.job.kind in setOf("follow_up", "interview_report")) {
                    if (!InterviewUiMerge.interviewJobUpdateAllowed(
                            state.value.interviewDetail, outcome.job
                        )
                    ) {
                        return
                    }
                    val isError = outcome.job.state != "queued" && outcome.job.state != "running"
                    update {
                        it.copy(
                            loading = false,
                            busyLabel = null,
                            message = UiMessage(
                                outcome.message,
                                isError = isError,
                                code = outcome.job.error?.code,
                                details = outcome.job.error?.details ?: emptyList(),
                                retryable = outcome.job.retryable,
                                diagnosticContext = "jobId=${outcome.job.id} · kind=${outcome.job.kind} · state=${outcome.job.state} · attempt=${outcome.job.attempt}",
                            ),
                        )
                    }
                    if (outcome.job.state == "queued" || outcome.job.state == "running") {
                        jobWatch.ensure(effect.result.scope, outcome.job)
                    } else if (outcome.job.state == "failed" || outcome.job.state == "interrupted") {
                        refreshInterviewInBackground(effect.result.scope.interviewSessionId)
                    }
                    return
                }
                if (!InterviewUiMerge.shouldApplyJob(state.value.job, outcome.job)) return
                val isError = outcome.job.state != "queued" && outcome.job.state != "running"
                val planErr = outcome.job.error
                val formattedOutcomeMsg = if (outcome.job.kind == "plan" && planErr != null) {
                    PlanErrorFormatter.userMessage(planErr.code, outcome.message, planErr.details)
                } else {
                    outcome.message
                }
                val jobCtx = "jobId=${outcome.job.id} · kind=${outcome.job.kind} · state=${outcome.job.state} · attempt=${outcome.job.attempt} · target=${outcome.job.target_id} (rev=${outcome.job.input_revision})"
                update {
                    it.copy(
                        job = outcome.job,
                        planError = if (outcome.job.kind == "plan" && outcome.job.state == "failed" && planErr != null) {
                            PlanErrorState(
                                code = planErr.code,
                                message = formattedOutcomeMsg,
                                details = planErr.details,
                                rawServerMessage = planErr.message,
                                retryable = outcome.job.retryable,
                                diagnosticContext = jobCtx,
                            )
                        } else {
                            it.planError
                        },
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(
                            formattedOutcomeMsg,
                            isError = isError,
                            code = outcome.job.error?.code
                                ?: if (outcome.job.state == "superseded") "SUPERSEDED" else null,
                            details = outcome.job.error?.details ?: emptyList(),
                            retryable = outcome.job.retryable,
                            diagnosticContext = jobCtx,
                        ),
                    )
                }
                if (outcome.job.state == "queued" || outcome.job.state == "running") {
                    // 唯一持续订阅：重复的前台恢复/手动查询不会叠加第二个轮询。
                    jobWatch.ensure(effect.result.scope, outcome.job)
                }
            }
            is RestoreUiEffect.Error -> showError(
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

