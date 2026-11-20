package com.mockmate.app.core.audio

import android.content.Context
import android.media.AudioAttributes
import android.media.AudioFocusRequest
import android.media.AudioManager
import android.media.MediaPlayer
import java.io.File

/**
 * 播放引擎抽象。设备实现直接包装 MediaPlayer；单元测试用可控假实现构造
 * A→B→迟到 A 完成/错误/焦点回调，验证旧回调不会释放当前播放器。
 */
internal interface PlaybackEngine {
    fun create(
        file: File,
        onCompletion: () -> Unit,
        onError: (Int, Int) -> Unit,
    ): PlaybackHandle
}

internal interface PlaybackHandle {
    fun start()
    fun setVolume(volume: Float)
    fun stop()
    fun release()
}

internal interface FocusController {
    fun request(onChange: (Int) -> Unit): Boolean
    fun abandon()
}

/**
 * 题目播报：下载完整 WAV 后本地播放，申请短暂音频焦点；播放与录音互斥由调用方保证。
 *
 * 每次播放持有自己的 generation 与 handle。完成/错误/焦点回调必须先确认
 * generation 与当前 handle 仍属于自己，才允许释放资源、放弃焦点或通知 ViewModel；
 * 迟到旧回调只清理它自己的 handle，绝不触碰当前播放器/焦点/回调。
 */
class QuestionPlayer internal constructor(
    private val engine: PlaybackEngine,
    private val focus: FocusController,
) {
    constructor(context: Context) : this(
        AndroidPlaybackEngine(),
        AndroidFocusController(
            context.getSystemService(Context.AUDIO_SERVICE) as AudioManager,
        ),
    )

    private class Playback(
        val generation: Long,
        val tag: String,
        val onFinished: (String) -> Unit,
        val onError: (String, String) -> Unit,
        var handle: PlaybackHandle? = null,
        var released: Boolean = false,
        var focusHeld: Boolean = false,
    )

    private var generation: Long = 0L
    private var current: Playback? = null

    val isPlaying: Boolean
        get() = current?.released == false

    fun play(
        file: File,
        tag: String,
        onFinished: (String) -> Unit,
        onError: (String, String) -> Unit,
    ): Boolean {
        stop()
        val playbackGeneration = generation
        if (!file.exists() || file.length() <= 44) {
            onError(tag, "语音文件不存在或不完整")
            return false
        }
        val playback = Playback(playbackGeneration, tag, onFinished, onError)
        val granted = focus.request { change ->
            // 迟到的旧焦点回调不能停止、调音量或报错当前播放。
            if (!isCurrent(playback)) return@request
            when (AudioFocusPolicy.actionFor(change)) {
                AudioFocusPolicy.Action.STOP -> finish(
                    playback,
                    "播放被系统或来电打断，可重新播放题目",
                )
                AudioFocusPolicy.Action.DUCK ->
                    playback.handle?.let { runCatching { it.setVolume(0.2f) } }
                AudioFocusPolicy.Action.RESTORE ->
                    playback.handle?.let { runCatching { it.setVolume(1f) } }
                AudioFocusPolicy.Action.IGNORE -> Unit
            }
        }
        if (!granted) {
            onError(tag, "无法获取音频焦点，请关闭其他正在播放的应用")
            return false
        }
        playback.focusHeld = true
        current = playback
        return try {
            val handle = engine.create(
                file,
                onCompletion = { finish(playback, null) },
                onError = { what, extra -> finish(playback, "播放失败（$what/$extra）") },
            )
            playback.handle = handle
            handle.start()
            true
        } catch (e: Exception) {
            discard(playback)
            onError(tag, "播放失败：${e.javaClass.simpleName}")
            false
        }
    }

    fun stop() {
        generation += 1
        val playback = current ?: return
        current = null
        releaseResources(playback, abandonFocus = true)
    }

    private fun isCurrent(playback: Playback): Boolean =
        playback.generation == generation && current === playback

    /**
     * 完成/错误/焦点停止共用的终态处理。当前播放者在任何副作用前先摘除并推进
     * generation；迟到回调走只释放自身 handle 的分支。
     */
    private fun finish(playback: Playback, error: String?) {
        val wasCurrent = isCurrent(playback)
        if (wasCurrent) {
            generation += 1
            current = null
        }
        releaseResources(playback, abandonFocus = wasCurrent)
        if (!wasCurrent) return
        if (error == null) playback.onFinished(playback.tag)
        else playback.onError(playback.tag, error)
    }

    private fun discard(playback: Playback) {
        if (isCurrent(playback)) {
            generation += 1
            current = null
        }
        releaseResources(playback, abandonFocus = true)
    }

    private fun releaseResources(playback: Playback, abandonFocus: Boolean) {
        if (!playback.released) {
            playback.released = true
            playback.handle?.let { handle ->
                runCatching { handle.stop() }
                runCatching { handle.release() }
            }
            playback.handle = null
        }
        if (abandonFocus && playback.focusHeld) {
            playback.focusHeld = false
            focus.abandon()
        }
    }

    private class AndroidPlaybackEngine : PlaybackEngine {
        override fun create(
            file: File,
            onCompletion: () -> Unit,
            onError: (Int, Int) -> Unit,
        ): PlaybackHandle {
            val media = MediaPlayer()
            media.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            media.setDataSource(file.absolutePath)
            media.setOnCompletionListener { onCompletion() }
            media.setOnErrorListener { _, what, extra ->
                onError(what, extra)
                true
            }
            return object : PlaybackHandle {
                private var released = false

                override fun start() {
                    media.prepare()
                    media.start()
                }

                override fun setVolume(volume: Float) {
                    if (!released) media.setVolume(volume, volume)
                }

                override fun stop() {
                    if (!released) runCatching { media.stop() }
                }

                override fun release() {
                    if (released) return
                    released = true
                    runCatching { media.release() }
                }
            }
        }
    }

    private class AndroidFocusController(
        private val audioManager: AudioManager,
    ) : FocusController {
        private var request: AudioFocusRequest? = null

        override fun request(onChange: (Int) -> Unit): Boolean {
            abandon()
            val next = AudioFocusRequest.Builder(AudioManager.AUDIOFOCUS_GAIN_TRANSIENT_MAY_DUCK)
                .setAudioAttributes(
                    AudioAttributes.Builder()
                        .setUsage(AudioAttributes.USAGE_MEDIA)
                        .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                        .build(),
                )
                .setOnAudioFocusChangeListener { change -> onChange(change) }
                .build()
            val granted = audioManager.requestAudioFocus(next) ==
                AudioManager.AUDIOFOCUS_REQUEST_GRANTED
            if (granted) request = next
            return granted
        }

        override fun abandon() {
            request?.let { runCatching { audioManager.abandonAudioFocusRequest(it) } }
            request = null
        }
    }
}
