package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for the pure snapshot-diff logic, mirroring the web SPA's
 * `idleTransitions.test.ts`. No Android/Robolectric — this logic is a plain
 * Kotlin function set, testable without a device.
 */
class SessionPollDiffTest {
    private fun session(
        id: String,
        status: String,
        pending: Int = 0,
        title: String? = null,
        runnerOnline: Boolean? = null,
    ) = SessionState(id, status, pending, title, runnerOnline)

    // --- idle transitions (running -> terminal) ---------------------------

    @Test
    fun `running to idle is an idle transition`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertEquals(listOf("a"), transitions.map { it.id })
    }

    @Test
    fun `running to failed is an idle transition`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "failed")))
        assertEquals(listOf("a"), transitions.map { it.id })
    }

    @Test
    fun `no prior snapshot fires no idle transition`() {
        // First run: empty previous — a session already idle must not notify.
        val transitions = detectIdleTransitions(emptyMap(), listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    @Test
    fun `steady idle does not re-notify on a later poll`() {
        // Previous already terminal (as persisted after the first notify) — the
        // dedup: the same idle session must not fire again next run.
        val previous = mapOf("a" to SessionSnapshot("idle", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    @Test
    fun `waiting to idle is not an idle transition`() {
        // Only an exact running -> terminal edge counts.
        val previous = mapOf("a" to SessionSnapshot("waiting", 0))
        val transitions = detectIdleTransitions(previous, listOf(session("a", "idle")))
        assertTrue(transitions.isEmpty())
    }

    // --- new elicitations (pending count increased) -----------------------

    @Test
    fun `elicitation count increase fires`() {
        val previous = mapOf("a" to SessionSnapshot("running", 0))
        val fired = detectNewElicitations(previous, listOf(session("a", "running", pending = 1)))
        assertEquals(listOf("a"), fired.map { it.id })
    }

    @Test
    fun `no prior snapshot fires no elicitation`() {
        // Already-pending on the first observation must not notify.
        val fired = detectNewElicitations(emptyMap(), listOf(session("a", "waiting", pending = 2)))
        assertTrue(fired.isEmpty())
    }

    @Test
    fun `steady elicitation count does not fire`() {
        val previous = mapOf("a" to SessionSnapshot("waiting", 2))
        val fired = detectNewElicitations(previous, listOf(session("a", "waiting", pending = 2)))
        assertTrue(fired.isEmpty())
    }

    @Test
    fun `answered elicitation decrease does not fire`() {
        val previous = mapOf("a" to SessionSnapshot("waiting", 2))
        val fired = detectNewElicitations(previous, listOf(session("a", "waiting", pending = 1)))
        assertTrue(fired.isEmpty())
    }

    // --- snapshot round-trip (the dedup key) ------------------------------

    @Test
    fun `buildSnapshot captures status and pending count per id`() {
        val snapshot =
            buildSnapshot(listOf(session("a", "running", pending = 1), session("b", "idle")))
        assertEquals(SessionSnapshot("running", 1), snapshot["a"])
        assertEquals(SessionSnapshot("idle", 0), snapshot["b"])
    }

    @Test
    fun `re-running the diff against the saved snapshot yields nothing`() {
        // Simulates run N notifying, persisting, then run N+1 diffing the same
        // list against that persisted snapshot — the transition must not repeat.
        val current = listOf(session("a", "idle"), session("b", "waiting", pending = 1))
        val afterNotify = buildSnapshot(current)
        assertTrue(detectIdleTransitions(afterNotify, current).isEmpty())
        assertTrue(detectNewElicitations(afterNotify, current).isEmpty())
    }

    // --- cookie extraction ------------------------------------------------

    @Test
    fun `extractCookieValue pulls the named cookie`() {
        val header = "__Host-ap_session=abc.def.ghi; other=1"
        assertEquals("abc.def.ghi", extractCookieValue(header, "__Host-ap_session"))
    }

    @Test
    fun `extractCookieValue returns null when absent or empty`() {
        assertNull(extractCookieValue("other=1", "__Host-ap_session"))
        assertNull(extractCookieValue(null, "ap_session"))
        assertNull(extractCookieValue("ap_session=", "ap_session"))
    }

    @Test
    fun `sessionCookieName uses the Host prefix on https only`() {
        assertEquals("__Host-ap_session", sessionCookieName(secure = true))
        assertEquals("ap_session", sessionCookieName(secure = false))
    }

    @Test
    fun `sessionDisplayLabel falls back to a generic label`() {
        assertEquals("My session", sessionDisplayLabel("My session"))
        assertEquals("New session", sessionDisplayLabel(null))
        assertEquals("New session", sessionDisplayLabel("   "))
    }
}
