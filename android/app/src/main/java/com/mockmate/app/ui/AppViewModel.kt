package com.mockmate.app.ui

import android.app.Application
import android.net.Uri
import androidx.lifecycle.AndroidViewModel
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
import com.mockmate.app.core.network.SnapshotDto
import com.mockmate.app.core.network.UserDto
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
import com.mockmate.app.core.operation.MatchPayload
import com.mockmate.app.core.operation.OperationCoordinator
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.OperationPayload
import com.mockmate.app.core.operation.OperationSave
import com.mockmate.app.core.operation.ParseMaterialPayload
import com.mockmate.app.core.operation.PendingOperationReplayer
import com.mockmate.app.core.operation.RetryJobPayload
import com.mockmate.app.core.operation.SecureOperationStore
import com.mockmate.app.core.operation.UploadFilePayload
import com.mockmate.app.core.operation.WritePipeline
import com.mockmate.app.core.session.ContextGeneration
import com.mockmate.app.core.session.DraftStore
import com.mockmate.app.core.session.RequestContext
import com.mockmate.app.core.session.SecureSessionStore
import com.mockmate.app.core.session.SessionManager
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
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.cancelChildren
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.JsonObject

data class Account(val id: String, val username: String)

data class UiMessage(
    val text: String,
    val isError: Boolean = true,
    val code: String? = null,
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
}

data class AppState(
    val booting: Boolean = true,
    val account: Account? = null,
    val stack: List<Screen> = listOf(Screen.Auth),
    val loading: Boolean = false,
    val busyLabel: String? = null,
    val message: UiMessage? = null,
    val roles: List<RoleDto> = emptyList(),
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
    val job: JobDto? = null,
    val lastGeneratedPlanId: String? = null,
)

class AppViewModel(application: Application) : AndroidViewModel(application) {
    private val sessionStore = SecureSessionStore(application)
    private val drafts = DraftStore(application)
    private val operations = OperationCoordinator(SecureOperationStore(application))
    private val session = SessionManager(sessionStore)
    private val api = MockMateApi(session, BuildConfig.API_BASE_URL)
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

    private fun bumpNavigation() {
        contextGeneration.bumpNavigation()
        // 页面/账号切换后旧订阅立即停止；回到目标页时由恢复路径重新建立唯一订阅。
        jobWatch.cancelAll()
    }

    fun setAppVisible(visible: Boolean) {
        appVisible = visible
        if (visible) {
            restoreActiveJobs()
        } else {
            jobWatch.cancelAll()
        }
    }

    init {
        viewModelScope.launch { bootstrap() }
    }

    @Suppress("UNCHECKED_CAST")
    private fun <T> okOrNull(result: ApiResult<T>): T? = (result as? ApiResult.Ok<T>)?.value

    private fun update(block: (AppState) -> AppState) {
        state.value = block(state.value)
    }

    private fun showError(result: ApiResult.Err, ctx: RequestContext? = null) {
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
                        message = UiMessage(result.message, isError = true, code = result.code),
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
                    message = UiMessage("登录已过期，请重新登录", isError = true, code = result.code),
                )
            }
            return
        }
        if (ctx != null && !isAccountCurrent(ctx)) return
        update {
            it.copy(
                loading = false,
                busyLabel = null,
                message = UiMessage(result.message, isError = true, code = result.code),
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
        fileName: String? = null,
        fileSha256: String? = null,
        fileMime: String? = null,
        call: suspend (String) -> ApiResult<T>,
    ): ApiResult<T> = writePipeline.perform(
        accountId = ctx.accountId,
        type = type,
        payload = payload,
        targetId = targetId,
        revision = revision,
        preparationId = preparationId,
        fileName = fileName,
        fileSha256 = fileSha256,
        fileMime = fileMime,
        isContextCurrent = { isAccountCurrent(ctx) },
        onJob = { job -> recordJobResult(ctx.accountId, job, preparationId) },
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
        update { it.copy(stack = stack.dropLast(1), message = null) }
        return true
    }

    private fun push(screen: Screen) {
        bumpNavigation()
        update { it.copy(stack = it.stack + screen, message = null) }
    }

    private fun replaceTop(screen: Screen) {
        bumpNavigation()
        update { it.copy(stack = it.stack.dropLast(1) + screen, message = null) }
    }

    private fun popTo(predicate: (Screen) -> Boolean) {
        bumpNavigation()
        update { current ->
            val index = current.stack.indexOfLast(predicate)
            if (index < 0) current else current.copy(stack = current.stack.take(index + 1))
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
                        )
                    }
                    loadSnapshotLabels(result.value, ctx)
                    if (isCurrent(ctx)) push(Screen.Setup(preparationId))
                }
            }
        }
    }

    fun startNewPreparation() {
        update { it.copy(preparation = null, plan = null, matchReport = null, job = null) }
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
        update { it.copy(material = null, snapshots = emptyList()) }
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
                    update { it.copy(loading = false, busyLabel = null) }
                    val saved = result.value
                    replaceTop(Screen.Review(saved.id, preparationId))
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
            val bytes = withContext(Dispatchers.IO) {
                runCatching {
                    getApplication<Application>().contentResolver.openInputStream(uri)?.use { it.readBytes() }
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
                            message = UiMessage("文件已上传，正在提取文本（不调用 AI）", isError = false),
                        )
                    }
                    replaceTop(Screen.Review(material.id, preparationId))
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
                    update { it.copy(loading = false, busyLabel = null) }
                    replaceTop(Screen.Review(material.id, preparationId))
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
        val ctx = currentRequestContext() ?: return
        update { it.copy(loading = true, busyLabel = "保存核对结果…") }
        viewModelScope.launch {
            val result = performWrite(
                ctx,
                "confirmMaterial",
                ConfirmMaterialPayload(material.revision, profile),
                targetId = materialId,
                revision = material.revision,
            ) { key -> repository.confirmMaterial(materialId, material.revision, profile, key) }
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
                is ApiResult.Ok -> {
                    val snapshots = repository.snapshots(materialId)
                    val refreshed = repository.material(materialId)
                    if (!isAccountCurrent(ctx)) return@launch
                    update {
                        it.copy(
                            snapshots = okOrNull(snapshots) ?: it.snapshots,
                            material = okOrNull(refreshed) ?: it.material,
                            loading = false,
                            busyLabel = null,
                            message = UiMessage("已保存为不可变版本（快照）", isError = false),
                        )
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
        update { it.copy(job = null) }
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
                is ApiResult.Ok -> if (isCurrent(ctx)) update { it.copy(plan = result.value) }
                is ApiResult.Err -> showError(result, ctx)
            }
        }
    }

    /** 只读刷新计划摘要：重新读取准备与当前计划，不新开页面、不重新生成。 */
    fun refreshPlan(preparationId: String) {
        val token = ++loadToken
        val ctx = currentRequestContext() ?: return
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
        }
    }

    fun generatePlan(preparationId: String) {
        val prep = state.value.preparation ?: return
        if (prep.id != preparationId) return
        val ctx = currentRequestContext() ?: return
        val entry = scopeForJob("plan", preparationId, preparationId) ?: return
        update { it.copy(loading = true, busyLabel = "生成计划中…", plan = null) }
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
                is ApiResult.Err -> showError(job, ctx)
                is ApiResult.Ok -> {
                    // POST 只负责创建任务；终态统一由恢复归并口读取，running 由 JobWatch 持续订阅。
                    applyEffect(restoreCoordinator.project(entry.copy(jobId = job.value.id), job.value))
                }
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
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
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
            if (!isAccountCurrent(ctx)) return@launch
            when (result) {
                is ApiResult.Err -> showError(result, ctx)
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
                            stack = listOf(Screen.PreparationList, Screen.Setup(result.value.id)),
                            message = UiMessage("已新建准备，旧计划保留只读", isError = false),
                        )
                    }
                    refreshPreparations()
                }
            }
        }
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
        val report = replayer.replay(accountId) { isAccountCurrent(ctx) }
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
        }
        val preparationId = when (screen) {
            is Screen.Setup -> screen.preparationId
            is Screen.Library -> screen.preparationId
            is Screen.Editor -> screen.preparationId
            is Screen.Review -> screen.preparationId
            is Screen.Templates -> screen.preparationId
            is Screen.Match -> screen.preparationId
            is Screen.PlanHub -> screen.preparationId
            else -> state.value.preparation?.id
        }
        val materialId = when (screen) {
            is Screen.Review -> screen.materialId
            is Screen.Editor -> screen.materialId
            else -> null
        }
        return RestoreScope(
            accountId = ctx.accountId,
            sessionGeneration = ctx.sessionGeneration,
            navigationEpoch = ctx.navigationEpoch,
            screen = screenName,
            preparationId = preparationId,
            materialId = materialId,
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
            else -> base.screen == RestoreJobService.SCREEN_REVIEW && base.materialId == active.targetId
        }

    private fun scopeForActiveJob(base: RestoreScope, active: ActiveJob): RestoreScope =
        when (active.kind) {
            "plan", "match" -> base.copy(
                jobId = active.jobId,
                preparationId = active.preparationId ?: base.preparationId,
                materialId = null,
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
                val outcome = effect.result.outcome as RestoreOutcome.Project
                val message = when {
                    outcome.plan != null -> "计划已生成，请确认保存"
                    outcome.matchReport != null -> "匹配分析完成"
                    outcome.job.kind == "parse" -> "解析完成，请核对并确认结构；解析结果本身不是已确认事实"
                    outcome.job.kind == "extract" -> "文本提取完成，请核对结构后确认"
                    else -> outcome.message
                }
                update {
                    it.copy(
                        plan = outcome.plan ?: it.plan,
                        lastGeneratedPlanId = outcome.plan?.id ?: it.lastGeneratedPlanId,
                        preparation = outcome.preparation ?: it.preparation,
                        matchReport = outcome.matchReport ?: it.matchReport,
                        material = outcome.material ?: it.material,
                        snapshots = outcome.snapshots ?: it.snapshots,
                        job = outcome.job,
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(message, isError = false),
                    )
                }
            }
            is RestoreUiEffect.Inform -> {
                val outcome = effect.result.outcome as RestoreOutcome.Inform
                val isError = outcome.job.state != "queued" && outcome.job.state != "running"
                update {
                    it.copy(
                        job = outcome.job,
                        loading = false,
                        busyLabel = null,
                        message = UiMessage(
                            outcome.message,
                            isError = isError,
                            code = outcome.job.error?.code
                                ?: if (outcome.job.state == "superseded") "SUPERSEDED" else null,
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
            )
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

    fun dismissMessage() = update { it.copy(message = null) }

    override fun onCleared() {
        api.close()
        super.onCleared()
    }
}

