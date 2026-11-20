package com.mockmate.app.core.audio

/**
 * Android 音频焦点常量在部分 ROM/文档中容易混淆：永久 LOSS 为 -1，临时 LOSS 为 -2，
 * 可 duck 的临时丢失为 -3。这里显式映射，避免用大小比较漏掉永久丢失。
 */
object AudioFocusPolicy {
    const val LOSS = -1
    const val LOSS_TRANSIENT = -2
    const val LOSS_TRANSIENT_CAN_DUCK = -3
    const val GAIN = 1

    enum class Action { STOP, DUCK, RESTORE, IGNORE }

    fun actionFor(change: Int): Action = when (change) {
        LOSS, LOSS_TRANSIENT -> Action.STOP
        LOSS_TRANSIENT_CAN_DUCK -> Action.DUCK
        GAIN -> Action.RESTORE
        else -> Action.IGNORE
    }
}
