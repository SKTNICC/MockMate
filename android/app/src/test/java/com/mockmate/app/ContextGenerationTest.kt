package com.mockmate.app

import com.mockmate.app.core.session.ContextGeneration
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class ContextGenerationTest {
    @Test
    fun lateResponseFromOldAccountCannotTargetNewAccount() {
        val generation = ContextGeneration()
        val old = generation.current("account-a", 3)!!
        assertTrue(generation.isCurrent(old, "account-a", 3))

        // 退出 A 并登录 B：账号和会话代数都变化，导航代数也递增。
        generation.bumpNavigation()
        assertFalse(generation.isCurrent(old, "account-b", 4))
        assertFalse(generation.isAccountCurrent(old, "account-b", 4))
    }

    @Test
    fun lateResponseFromOldPageCannotOverwriteNewPage() {
        val generation = ContextGeneration()
        val preparationA = generation.current("account-a", 3)!!
        assertTrue(generation.isCurrent(preparationA, "account-a", 3))

        generation.bumpNavigation()
        assertFalse(generation.isCurrent(preparationA, "account-a", 3))
        assertTrue(generation.isAccountCurrent(preparationA, "account-a", 3))
    }

    @Test
    fun oldLogoutOr401CannotInvalidateNewLogin() {
        val generation = ContextGeneration()
        val old = generation.current("account-a", 1)!!
        generation.bumpNavigation()
        // 新账号 B 已登录；旧 A 的 401 只能被丢弃。
        assertFalse(generation.isAccountCurrent(old, "account-b", 2))
    }
}
