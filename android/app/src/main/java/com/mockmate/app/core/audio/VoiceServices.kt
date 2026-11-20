package com.mockmate.app.core.audio

enum class VoicePhase {
    IDLE,
    LOADING,
    TTS_LOADING,
    PLAYING,
    RECORDING,
    LOCAL_READY,
    UPLOADING,
    TRANSCRIBING,
    REVIEW,
    SUBMITTING,
    INTERRUPTED,
    ERROR,
}

data class VoiceServices(
    // 单元测试可只注入存储/网络路径；真实设备由 AppViewModel(application) 注入完整组件。
    val recorder: VoiceRecorder? = null,
    val player: QuestionPlayer? = null,
    val takes: VoiceTakeStorage,
    val reviews: VoiceReviewStorage = InMemoryVoiceReviewStorage(),
    val cacheRoot: java.io.File,
    val fixtureProvider: () -> java.io.File? = { null },
)
