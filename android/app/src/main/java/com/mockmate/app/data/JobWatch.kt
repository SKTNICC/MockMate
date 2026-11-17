package com.mockmate.app.data

import com.mockmate.app.core.network.JobDto
import java.util.concurrent.ConcurrentHashMap
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.CoroutineStart
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

/**
 * 账号＋任务维度的唯一只读轮询订阅。
 *
 * - 恢复 GET 返回 queued/running 时，由这里继续查询到终态；
 * - App 不可见、账号/页面改变或订阅被取消时立即停止；
 * - 前台恢复通过 ActiveJob 记录重新 ensure，同一 job 不会产生并行重复轮询；
 * - 只 GET /jobs，不重新 POST，也不重发结果未知的 AI 调用。
 */
class JobWatch(
    private val scope: CoroutineScope,
    private val coordinator: RestoreCoordinator,
    private val isVisible: () -> Boolean,
    private val onEffect: suspend (RestoreUiEffect) -> Unit,
    private val intervalMillis: Long = 900,
) {
    private class Entry(
        val key: String,
        val scope: RestoreScope,
        val jobId: String,
    ) {
        @Volatile
        var handle: Job? = null
    }

    private val active = ConcurrentHashMap<String, Entry>()

    /** 返回是否新建订阅；同一账号同一任务已存在订阅时不重复创建。 */
    fun ensure(entry: RestoreScope, job: JobDto): Boolean {
        if (job.state != "queued" && job.state != "running") return false
        val key = "${entry.accountId}/${job.id}"
        if (active.containsKey(key)) return false
        val holder = Entry(key, entry, job.id)
        val handle = scope.launch(start = CoroutineStart.LAZY) { poll(holder) }
        holder.handle = handle
        if (active.putIfAbsent(key, holder) != null) {
            handle.cancel()
            return false
        }
        handle.start()
        return true
    }

    fun isWatching(accountId: String, jobId: String): Boolean = active.containsKey("$accountId/$jobId")

    /** 账号切换、退出或回到后台时取消全部订阅；前后台切换不得叠加并行轮询。 */
    fun cancelAll() {
        val snapshot = active.values.toList()
        active.clear()
        snapshot.forEach { it.handle?.cancel() }
    }

    private fun isTerminal(outcome: RestoreOutcome): Boolean =
        outcome is RestoreOutcome.Project ||
            (outcome is RestoreOutcome.Inform && outcome.job.state in M1Repository.TERMINAL_STATES)

    private suspend fun poll(holder: Entry) {
        try {
            while (true) {
                if (!isVisible() || !coordinator.isCurrent(holder.scope)) return
                delay(intervalMillis)
                if (!isVisible() || !coordinator.isCurrent(holder.scope)) return
                when (val effect = coordinator.query(holder.scope)) {
                    is RestoreUiEffect.Ignore -> return
                    is RestoreUiEffect.Error -> {
                        onEffect(effect)
                        return
                    }
                    is RestoreUiEffect.Project -> {
                        onEffect(effect)
                        return
                    }
                    is RestoreUiEffect.Inform -> {
                        onEffect(effect)
                        if (isTerminal(effect.result.outcome)) return
                    }
                }
            }
        } finally {
            active.remove(holder.key, holder)
        }
    }
}
