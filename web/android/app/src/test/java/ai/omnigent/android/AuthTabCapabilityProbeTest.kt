package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.io.StringReader

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AuthTabCapabilityProbeTest {
    @Test
    fun `non-empty asset links array advertises capability`() {
        assertTrue(hasNonEmptyAssetLinks(StringReader("[{\"relation\": []}]")))
    }

    @Test
    fun `empty asset links array does not advertise capability`() {
        assertFalse(hasNonEmptyAssetLinks(StringReader("[]")))
    }

    @Test
    fun `probe caches the anonymous asset links result per origin`() {
        var fetches = 0
        val results = mutableListOf<Boolean>()
        val probe =
            AuthTabCapabilityProbe(
                fetch = {
                    fetches++
                    true
                },
                execute = { task -> task() },
                post = { task -> task() },
            )

        probe.probe("https://front-door.example.com", results::add)
        probe.probe("https://front-door.example.com/", results::add)

        assertEquals(1, fetches)
        assertEquals(listOf(true, true), results)
    }

    @Test
    fun `probe coalesces concurrent requests for one origin`() {
        var fetches = 0
        var queued: (() -> Unit)? = null
        val results = mutableListOf<Boolean>()
        val probe =
            AuthTabCapabilityProbe(
                fetch = {
                    fetches++
                    false
                },
                execute = { task -> queued = task },
                post = { task -> task() },
            )

        probe.probe("https://front-door.example.com", results::add)
        probe.probe("https://front-door.example.com", results::add)
        assertTrue(results.isEmpty())

        queued!!()

        assertEquals(1, fetches)
        assertEquals(listOf(false, false), results)
    }

    @Test
    fun `probe rejects non-https origins without fetching`() {
        var fetched = false
        var result = true
        val probe =
            AuthTabCapabilityProbe(
                fetch = {
                    fetched = true
                    true
                },
                execute = { task -> task() },
                post = { task -> task() },
            )

        probe.probe("http://front-door.example.com") { result = it }

        assertFalse(fetched)
        assertFalse(result)
    }
}
