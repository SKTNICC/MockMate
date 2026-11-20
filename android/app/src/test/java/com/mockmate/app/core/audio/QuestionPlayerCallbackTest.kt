package com.mockmate.app.core.audio

import android.media.AudioManager
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * T03 动态回调顺序回归：构造 A→B 后 A 的迟到完成/错误/焦点回调，
 * 断言旧回调只释放自身句柄，不触碰 B 的播放器、焦点和 ViewModel 回调。
 */
class QuestionPlayerCallbackTest {
    @get:Rule
    val temporary = TemporaryFolder()

    private class FakeHandle(
        private val onCompletion: () -> Unit,
        private val onError: (Int, Int) -> Unit,
    ) : PlaybackHandle {
        var started = false
        var stopped = false
        var released = false
        var lastVolume = 1f

        override fun start() {
            started = true
        }

        override fun setVolume(volume: Float) {
            this.lastVolume = volume
        }

        override fun stop() {
            stopped = true
        }

        override fun release() {
            released = true
        }

        fun complete() = onCompletion()
        fun fail() = onError(-1, -2)
    }

    private class FakeEngine : PlaybackEngine {
        val handles = mutableListOf<FakeHandle>()
        override fun create(
            file: File,
            onCompletion: () -> Unit,
            onError: (Int, Int) -> Unit,
        ): PlaybackHandle = FakeHandle(onCompletion, onError).also { handles += it }
    }

    private class FakeFocus : FocusController {
        val listeners = mutableListOf<(Int) -> Unit>()
        var abandonCount = 0

        override fun request(onChange: (Int) -> Unit): Boolean {
            listeners += onChange
            return true
        }

        override fun abandon() {
            abandonCount += 1
        }
    }

    private fun file(name: String): File =
        temporary.newFile(name).apply { writeBytes(ByteArray(128) { 1 }) }

    private fun player(engine: FakeEngine, focus: FakeFocus) =
        QuestionPlayer(engine, focus)

    @Test
    fun lateCompletionOfPreviousPlaybackDoesNotReleaseCurrentPlayer() {
        val engine = FakeEngine()
        val focus = FakeFocus()
        val player = player(engine, focus)
        val finished = mutableListOf<String>()
        assertTrue(player.play(file("a.wav"), "A", { finished += "A-$it" }, { _, _ -> }))
        val a = engine.handles[0]
        assertTrue(a.started)

        assertTrue(player.play(file("b.wav"), "B", { finished += "B-$it" }, { _, _ -> }))
        val b = engine.handles[1]
        assertTrue("切换播放应释放 A", a.released)

        // A 的完成回调在 B 开始后才到达：只能清理自身，不能碰 B。
        a.complete()
        assertFalse("旧完成回调不得释放当前播放器", b.released)
        assertFalse("旧完成回调不得停止当前播放器", b.stopped)
        assertTrue(finished.isEmpty())
        assertTrue(player.isPlaying)

        b.complete()
        assertTrue("当前播放器完成时应释放自身", b.released)
        assertEquals(listOf("B-B"), finished)
        assertFalse(finished.contains("A-A"))
    }

    @Test
    fun lateErrorOfPreviousPlaybackDoesNotReleaseOrReportCurrentPlayer() {
        val engine = FakeEngine()
        val focus = FakeFocus()
        val player = player(engine, focus)
        val errors = mutableListOf<String>()
        assertTrue(player.play(file("a.wav"), "A", { }, { _, message -> errors += "A:$message" }))
        val a = engine.handles[0]
        assertTrue(player.play(file("b.wav"), "B", { }, { _, message -> errors += "B:$message" }))
        val b = engine.handles[1]

        a.fail()
        assertFalse("旧错误回调不得释放当前播放器", b.released)
        assertTrue("旧错误回调不得通知当前播放器", errors.isEmpty())
        assertTrue(player.isPlaying)

        b.fail()
        assertTrue(b.released)
        assertEquals(1, errors.size)
        assertTrue(errors.single().startsWith("B:"))
    }

    @Test
    fun lateFocusLossFromPreviousPlaybackDoesNotStopCurrentPlayer() {
        val engine = FakeEngine()
        val focus = FakeFocus()
        val player = player(engine, focus)
        val errors = mutableListOf<String>()
        assertTrue(player.play(file("a.wav"), "A", { }, { _, message -> errors += "A:$message" }))
        val a = engine.handles[0]
        assertEquals(1, focus.listeners.size)

        assertTrue(player.play(file("b.wav"), "B", { }, { _, message -> errors += "B:$message" }))
        val b = engine.handles[1]
        assertEquals(2, focus.listeners.size)

        // A 的旧焦点监听器在 B 播放时迟到触发永久丢失。
        focus.listeners[0](AudioManager.AUDIOFOCUS_LOSS)
        assertFalse("旧焦点回调不得释放 B", b.released)
        assertFalse("旧焦点回调不得停止 B", b.stopped)
        assertTrue(errors.isEmpty())
        assertTrue(player.isPlaying)
        assertEquals(1f, b.lastVolume)

        // B 自己的永久焦点丢失仍应停止并通知。
        focus.listeners[1](AudioManager.AUDIOFOCUS_LOSS)
        assertTrue(b.released)
        assertEquals(1, errors.size)
        assertTrue(errors.single().startsWith("B:"))
        assertFalse(player.isPlaying)
    }

    @Test
    fun duckAndRestoreFromOldPlaybackDoNotChangeCurrentVolume() {
        val engine = FakeEngine()
        val focus = FakeFocus()
        val player = player(engine, focus)
        assertTrue(player.play(file("a.wav"), "A", { }, { _, _ -> }))
        assertTrue(player.play(file("b.wav"), "B", { }, { _, _ -> }))
        val b = engine.handles[1]

        focus.listeners[0](AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK)
        assertEquals(1f, b.lastVolume)
        focus.listeners[1](AudioManager.AUDIOFOCUS_LOSS_TRANSIENT_CAN_DUCK)
        assertEquals(0.2f, b.lastVolume)
        focus.listeners[1](AudioManager.AUDIOFOCUS_GAIN)
        assertEquals(1f, b.lastVolume)
        assertFalse(b.released)
    }

    @Test
    fun explicitStopMakesLateCallbacksInertAndReleasesOnlyOnce() {
        val engine = FakeEngine()
        val focus = FakeFocus()
        val player = player(engine, focus)
        val finished = mutableListOf<String>()
        val errors = mutableListOf<String>()
        assertTrue(player.play(file("a.wav"), "A", { finished += it }, { _, message -> errors += message }))
        val a = engine.handles[0]

        player.stop()
        assertTrue(a.released)
        assertFalse(player.isPlaying)
        a.complete()
        a.fail()
        assertTrue(finished.isEmpty())
        assertTrue(errors.isEmpty())
    }
}
