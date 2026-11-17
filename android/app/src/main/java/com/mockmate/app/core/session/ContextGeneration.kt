package com.mockmate.app.core.session

/** 异步请求必须携带的账号／会话／导航代数快照。 */
data class RequestContext(
    val accountId: String,
    val sessionGeneration: Int,
    val navigationEpoch: Int,
)

/**
 * 账号与会话代数 + 导航代数。旧账号或旧页面的迟到回包不得写入新账号/新页面。
 */
class ContextGeneration {
    @Volatile
    var navigationEpoch: Int = 0
        private set

    fun bumpNavigation() {
        navigationEpoch += 1
    }

    fun current(accountId: String?, sessionGeneration: Int): RequestContext? =
        accountId?.let { RequestContext(it, sessionGeneration, navigationEpoch) }

    fun isAccountCurrent(
        context: RequestContext?,
        accountId: String?,
        sessionGeneration: Int,
    ): Boolean =
        context != null &&
            context.accountId == accountId &&
            context.sessionGeneration == sessionGeneration

    fun isCurrent(
        context: RequestContext?,
        accountId: String?,
        sessionGeneration: Int,
    ): Boolean =
        isAccountCurrent(context, accountId, sessionGeneration) &&
            context?.navigationEpoch == navigationEpoch
}
