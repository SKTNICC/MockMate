package com.mockmate.app.core.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import java.io.File
import java.io.FileOutputStream
import java.io.OutputStream

data class RecorderStart(val ok: Boolean, val message: String = "")

data class RecorderSnapshot(
    val elapsedMs: Long,
    val samples: Long,
    val reachedLimit: Boolean,
    val error: String? = null,
)

data class RecorderStop(
    val file: File?,
    val byteLength: Int,
    val sampleCount: Long,
    val durationMs: Long,
    val reachedLimit: Boolean,
    val interrupted: Boolean,
    val error: String? = null,
)

/**
 * 固定 16kHz/单声道/PCM16LE 的 AudioRecord 采集器。
 * 计时只在设备真正开始返回样本后启动；同时以样本数与单调时钟限制 120 秒。
 *
 * 写入先落到 `.part` 临时文件，并经 EncryptedFile 流式加密；只有正常停止且样本完整时
 * 才原子改名封存。进程被杀留下的 `.part` 由 VoiceTakeStore 识别为未封存片段，不能冒充完整回答。
 */
class VoiceRecorder(private val cipher: VoiceFileCipher) {
    private val lock = Any()
    private var record: AudioRecord? = null
    private var thread: Thread? = null
    private var output: OutputStream? = null
    private var target: File? = null
    private var part: File? = null

    @Volatile private var stopping = false
    @Volatile private var startedAtNanos = 0L
    @Volatile private var totalSamples = 0L
    @Volatile private var reachedLimit = false
    @Volatile private var readError: String? = null
    @Volatile private var maxSamples = 1_920_000L
    @Volatile private var interrupted = false

    val isRecording: Boolean
        get() = thread?.isAlive == true

    /**
     * EncryptedFile 把文件名作为 AEAD associated data，因此临时文件必须与最终文件同名；
     * 用独立的 `.part-<takeId>` 目录做原子封存，避免改名破坏认证。
     */
    private fun partOf(file: File): File =
        File(File(file.parentFile, ".part-${file.nameWithoutExtension}"), file.name)

    /**
     * 仅用于 debug instrumentation 的 fixture 注入：把已知 PCM 当成已采集音频，
     * 用于验证上传/转写/计时/文件恢复；不能替代真实麦克风验收。
     */
    fun startFromFixture(file: File, fixture: File, sampleLimit: Long = 1_920_000L): RecorderStart {
        synchronized(lock) {
            if (isRecording) return RecorderStart(false, "已有录音正在进行")
            if (!fixture.exists() || fixture.length() <= 0) {
                return RecorderStart(false, "测试音频 fixture 不存在")
            }
            val limit = sampleLimit.coerceAtMost(1_920_000L)
            val maxBytes = (limit * 2).toInt()
            val bytes = fixture.readBytes()
            val usable = if (bytes.size % 2 == 0) bytes.copyOf(minOf(bytes.size, maxBytes))
            else bytes.copyOf(minOf(bytes.size - 1, maxBytes))
            if (usable.isEmpty()) return RecorderStart(false, "测试音频 fixture 为空")
            file.parentFile?.mkdirs()
            file.delete()
            val temporary = partOf(file)
            temporary.parentFile?.mkdirs()
            temporary.delete()
            return try {
                cipher.openEncryptedOutput(temporary).use { it.write(usable) }
                if (!temporary.renameTo(file)) {
                    temporary.delete()
                    return RecorderStart(false, "测试音频封存失败")
                }
                temporary.parentFile?.delete()
                target = file
                part = null
                totalSamples = usable.size / 2L
                reachedLimit = totalSamples >= limit
                readError = null
                interrupted = false
                startedAtNanos = System.nanoTime() - (totalSamples * 1_000_000_000L / SAMPLE_RATE)
                RecorderStart(true)
            } catch (e: Exception) {
                temporary.delete()
                RecorderStart(false, "测试音频加密写入失败：${e.javaClass.simpleName}")
            }
        }
    }

    @SuppressLint("MissingPermission")
    fun start(file: File, sampleLimit: Long = 1_920_000L): RecorderStart {
        synchronized(lock) {
            if (isRecording) return RecorderStart(false, "已有录音正在进行")
            file.parentFile?.mkdirs()
            file.delete()
            val temporary = partOf(file)
            temporary.parentFile?.mkdirs()
            temporary.delete()
            val minBuffer = AudioRecord.getMinBufferSize(
                SAMPLE_RATE,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
            )
            if (minBuffer <= 0) {
                return RecorderStart(false, "设备不支持 16kHz 单声道 PCM 采集（$minBuffer）")
            }
            val bufferSize = maxOf(minBuffer * 2, SAMPLE_RATE / 5 * 2)
            val recorder = try {
                AudioRecord(
                    MediaRecorder.AudioSource.MIC,
                    SAMPLE_RATE,
                    AudioFormat.CHANNEL_IN_MONO,
                    AudioFormat.ENCODING_PCM_16BIT,
                    bufferSize,
                )
            } catch (e: Exception) {
                return RecorderStart(false, "录音设备初始化失败：${e.javaClass.simpleName}")
            }
            if (recorder.state != AudioRecord.STATE_INITIALIZED) {
                runCatching { recorder.release() }
                return RecorderStart(false, "录音设备初始化失败（状态未就绪）")
            }
            try {
                recorder.startRecording()
            } catch (e: Exception) {
                runCatching { recorder.release() }
                return RecorderStart(false, "无法开始录音：${e.javaClass.simpleName}")
            }
            if (recorder.recordingState != AudioRecord.RECORDSTATE_RECORDING) {
                runCatching { recorder.release() }
                return RecorderStart(false, "录音设备未进入采集状态")
            }
            stopping = false
            startedAtNanos = 0L
            totalSamples = 0L
            reachedLimit = false
            readError = null
            maxSamples = sampleLimit.coerceAtMost(1_920_000L)
            interrupted = false
            target = file
            part = temporary
            output = try {
                cipher.openEncryptedOutput(temporary)
            } catch (e: Exception) {
                runCatching { recorder.stop(); recorder.release() }
                return RecorderStart(false, "无法创建本地录音文件：${e.javaClass.simpleName}")
            }
            record = recorder
            thread = Thread({ readLoop(recorder, bufferSize) }, "MockMateVoiceRecorder").apply {
                isDaemon = true
                start()
            }
            return RecorderStart(true)
        }
    }

    private fun readLoop(recorder: AudioRecord, bufferSize: Int) {
        val buffer = ByteArray(bufferSize)
        try {
            while (!stopping) {
                val read = recorder.read(buffer, 0, buffer.size)
                if (read > 0) {
                    if (startedAtNanos == 0L) startedAtNanos = System.nanoTime()
                    var offset = 0
                    var remaining = read
                    while (remaining > 0 && !stopping) {
                        val allowedSamples = maxSamples - totalSamples
                        if (allowedSamples <= 0) {
                            reachedLimit = true
                            stopping = true
                            break
                        }
                        val allowedBytes = (allowedSamples * 2).coerceAtMost(remaining.toLong()).toInt()
                        val evenBytes = allowedBytes - (allowedBytes % 2)
                        if (evenBytes <= 0) break
                        output?.write(buffer, offset, evenBytes)
                        totalSamples += evenBytes / 2
                        offset += evenBytes
                        remaining -= evenBytes
                    }
                    if (totalSamples >= maxSamples) {
                        reachedLimit = true
                        stopping = true
                    }
                } else if (read < 0) {
                    readError = "读取录音失败（$read）"
                    stopping = true
                }
            }
        } catch (e: Exception) {
            readError = "录音写入失败：${e.javaClass.simpleName}"
        } finally {
            runCatching { output?.flush() }
            runCatching { output?.close() }
            output = null
            runCatching { recorder.stop() }
            runCatching { recorder.release() }
        }
    }

    fun snapshot(): RecorderSnapshot {
        val started = startedAtNanos
        val elapsed = if (started == 0L) 0L else (System.nanoTime() - started) / 1_000_000
        return RecorderSnapshot(elapsed, totalSamples, reachedLimit, readError)
    }

    fun stop(interrupted: Boolean = false): RecorderStop {
        synchronized(lock) {
            if (interrupted) this.interrupted = true
            stopping = true
            val worker = thread
            worker?.join(3_000)
            if (worker != null && worker.isAlive) {
                runCatching { record?.stop() }
                worker.join(1_000)
            }
            runCatching { record?.release() }
            record = null
            thread = null
            val samples = totalSamples
            val duration = (samples * 1000) / SAMPLE_RATE
            val bytes = (samples * 2).coerceAtMost(Int.MAX_VALUE.toLong()).toInt()
            var error = readError
            val finalFile = target
            val temporary = part
            var sealed: File? = null
            if (finalFile != null && temporary != null && temporary.exists()) {
                if (error == null && samples > 0) {
                    try {
                        FileOutputStream(temporary, true).use { it.fd.sync() }
                        finalFile.delete()
                        if (temporary.renameTo(finalFile)) {
                            sealed = finalFile
                            temporary.parentFile?.delete()
                        } else {
                            error = "录音封存失败：临时文件无法转正"
                        }
                    } catch (e: Exception) {
                        error = "录音封存失败：${e.javaClass.simpleName}"
                    }
                } else if (error == null) {
                    error = "录音未产生有效样本"
                }
            } else if (finalFile != null && finalFile.exists() && error == null) {
                // fixture 已在开始时封存。
                sealed = finalFile
            }
            val wasInterrupted = this.interrupted
            startedAtNanos = 0L
            totalSamples = 0L
            target = null
            part = null
            readError = null
            return RecorderStop(
                file = sealed,
                byteLength = bytes,
                sampleCount = samples,
                durationMs = duration,
                reachedLimit = reachedLimit,
                interrupted = wasInterrupted,
                error = error,
            )
        }
    }

    companion object {
        const val SAMPLE_RATE = 16_000
        const val MAX_SAMPLES = 1_920_000L
    }
}
