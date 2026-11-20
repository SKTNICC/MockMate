package com.mockmate.app

import com.mockmate.app.core.audio.InMemoryVoiceTakeStorage
import com.mockmate.app.core.audio.VoiceTakeMeta
import com.mockmate.app.core.audio.VoiceTakeRead
import java.io.File
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class VoiceTakeStorageTest {
    @get:Rule
    val temporary = TemporaryFolder()

    private fun meta(takeId: String, bytes: Int) = VoiceTakeMeta(
        takeId = takeId,
        operationKey = "op-$takeId",
        sha256 = "a".repeat(64),
        byteLength = bytes,
        durationMs = bytes / 32,
        interrupted = false,
        reachedLimit = false,
    )

    @Test
    fun presentMissingAndLengthCorruptAreDistinct() {
        val root = temporary.newFolder("voice-root")
        val store = InMemoryVoiceTakeStorage(root)
        val file = store.file("account", "session", "turn", "take-1")
        file.parentFile?.mkdirs()
        file.writeBytes(ByteArray(320))
        store.write("account", "session", "turn", meta("take-1", 320))

        val present = store.read("account", "session", "turn")
        assertTrue(present is VoiceTakeRead.Present)
        assertEquals(320, (present as VoiceTakeRead.Present).meta.byteLength)

        file.writeBytes(ByteArray(319))
        val corrupt = store.read("account", "session", "turn")
        assertTrue(corrupt is VoiceTakeRead.Corrupt)

        file.delete()
        val missing = store.read("account", "session", "turn")
        assertTrue(missing is VoiceTakeRead.MissingFile)
    }

    @Test
    fun clearOnlyMatchingTakeAndAccountIsolation() {
        val store = InMemoryVoiceTakeStorage(temporary.newFolder("voice-root-2"))
        val first = store.file("a", "s", "t", "take-1")
        val second = store.file("b", "s", "t", "take-1")
        first.parentFile?.mkdirs()
        second.parentFile?.mkdirs()
        first.writeBytes(ByteArray(64))
        second.writeBytes(ByteArray(64))
        store.write("a", "s", "t", meta("take-1", 64))
        store.write("b", "s", "t", meta("take-1", 64))

        assertFalse(store.clear("a", "s", "t", "other-take"))
        assertTrue(first.exists())
        assertTrue(store.clear("a", "s", "t", "take-1"))
        assertFalse(first.exists())
        assertTrue(second.exists())
        assertTrue(store.read("a", "s", "t") is VoiceTakeRead.Absent)
        assertTrue(store.read("b", "s", "t") is VoiceTakeRead.Present)
    }

    @Test
    fun clearingKnownTakeDoesNotDeleteNewUnsealedPart() {
        val store = InMemoryVoiceTakeStorage(temporary.newFolder("voice-root-part"))
        val first = store.file("a", "s", "t", "take-1")
        first.parentFile?.mkdirs()
        first.writeBytes(ByteArray(64))
        store.write("a", "s", "t", meta("take-1", 64))
        val newPart = store.partFile("a", "s", "t", "take-2")
        newPart.parentFile?.mkdirs()
        newPart.writeBytes(ByteArray(128) { 3 })

        assertTrue(store.clear("a", "s", "t", "take-1"))
        assertFalse(first.exists())
        assertTrue("旧 take 清理不得删除刚开始的新录音 part", newPart.exists())
        assertTrue(store.read("a", "s", "t") is VoiceTakeRead.UnsealedPart)
    }

    @Test
    fun inMemoryPathsDoNotMixAccounts() {
        val store = InMemoryVoiceTakeStorage(File(temporary.root, "voice-root-3"))
        assertTrue(store.file("a", "s", "t", "k") != store.file("b", "s", "t", "k"))
        assertTrue(store.file("a", "s1", "t", "k") != store.file("a", "s2", "t", "k"))
        assertTrue(store.file("a", "s", "t1", "k") != store.file("a", "s", "t2", "k"))
    }
}
