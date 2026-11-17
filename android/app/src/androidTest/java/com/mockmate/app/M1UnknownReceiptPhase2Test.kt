package com.mockmate.app

import androidx.compose.ui.test.junit4.createAndroidComposeRule
import androidx.compose.ui.test.onAllNodesWithText
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.mockmate.app.core.operation.OperationLoad
import com.mockmate.app.core.operation.SecureOperationStore
import com.mockmate.app.core.session.SecureSessionStore
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.runner.RunWith

/**
 * D-M06 第二阶段：force-stop 后冷启动，按原 key 重放未知回执并清理 pending。
 * 服务端应只保留一份资源（由宿主核对数据库/访问日志）。
 */
@RunWith(AndroidJUnit4::class)
class M1UnknownReceiptPhase2Test {

    @get:Rule
    val composeRule = createAndroidComposeRule<MainActivity>()

    @Test
    fun coldStartReplaysPendingWithOriginalKeyAndClearsIt() {
        composeRule.waitUntil(timeoutMillis = 40_000) {
            composeRule.onAllNodesWithText("新建面试准备").fetchSemanticsNodes().isNotEmpty()
        }
        val context = InstrumentationRegistry.getInstrumentation().targetContext
        val accountId = SecureSessionStore(context).load()?.accountId
        assertTrue(accountId != null)
        val store = SecureOperationStore(context)
        var pendingSize = -1
        repeat(20) {
            val loaded = runBlocking { store.load(accountId!!) }
            if (loaded is OperationLoad.Loaded) {
                pendingSize = loaded.operations.pending.size
                if (pendingSize == 0) return@repeat
            }
            Thread.sleep(500)
        }
        assertEquals(0, pendingSize)
    }
}
