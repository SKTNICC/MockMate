package com.mockmate.app

import com.mockmate.app.core.audio.AudioFocusPolicy
import org.junit.Assert.assertEquals
import org.junit.Test

class AudioFocusPolicyTest {
    @Test
    fun permanentAndTransientLossBothStopPlayback() {
        assertEquals(AudioFocusPolicy.Action.STOP, AudioFocusPolicy.actionFor(-1))
        assertEquals(AudioFocusPolicy.Action.STOP, AudioFocusPolicy.actionFor(-2))
    }

    @Test
    fun duckAndGainDoNotStop() {
        assertEquals(AudioFocusPolicy.Action.DUCK, AudioFocusPolicy.actionFor(-3))
        assertEquals(AudioFocusPolicy.Action.RESTORE, AudioFocusPolicy.actionFor(1))
        assertEquals(AudioFocusPolicy.Action.IGNORE, AudioFocusPolicy.actionFor(0))
    }
}
