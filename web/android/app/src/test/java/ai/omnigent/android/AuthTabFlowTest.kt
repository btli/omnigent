package ai.omnigent.android

import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class AuthTabFlowTest {
    private val flow = AuthTabFlow()

    @Test
    fun `begin produces a completion url carrying a fresh state`() {
        val url = flow.begin(ORIGIN)

        assertNotNull(url)
        assertTrue(flow.inFlight)
        assertEquals(ORIGIN, "${url!!.scheme}://${url.host}")
        assertEquals("/auth/native-complete", url.path)
        val state = url.getQueryParameter("state")!!
        assertTrue(state.length >= 16)
        assertTrue(state.all { it.isLetterOrDigit() || it == '-' || it == '_' })
    }

    @Test
    fun `states are unique per flow`() {
        val first = flow.begin(ORIGIN)!!.getQueryParameter("state")
        flow.cancel()
        val second = flow.begin(ORIGIN)!!.getQueryParameter("state")

        assertTrue(first != second)
    }

    @Test
    fun `only one flow can be in flight`() {
        assertNotNull(flow.begin(ORIGIN))
        assertNull(flow.begin(ORIGIN)) // a redirect storm must not stack tabs
    }

    @Test
    fun `matching callback completes and clears the flow`() {
        val state = flow.begin(ORIGIN)!!.getQueryParameter("state")

        val result = flow.complete(callback(state!!), ORIGIN)

        assertNotNull(result)
        assertEquals("tok", result!!.token)
        assertFalse(flow.inFlight)
    }

    @Test
    fun `state mismatch is rejected and keeps the flow armed`() {
        flow.begin(ORIGIN)

        assertNull(flow.complete(callback("attacker-state"), ORIGIN))
        assertTrue(flow.inFlight) // the real result may still arrive
    }

    @Test
    fun `a result for a previous server never lands on the new one`() {
        val state = flow.begin(ORIGIN)!!.getQueryParameter("state")!!

        // The user switched servers while the tab was open.
        assertNull(flow.complete(callback(state), "https://other.example.com"))
        assertNull(flow.complete(callback(state), null))
    }

    @Test
    fun `unsolicited callback with no pending flow is dropped`() {
        assertNull(flow.complete(callback("any-state-1234"), ORIGIN))
    }

    @Test
    fun `cancel abandons the pending flow`() {
        val state = flow.begin(ORIGIN)!!.getQueryParameter("state")!!
        flow.cancel()

        assertFalse(flow.inFlight)
        assertNull(flow.complete(callback(state), ORIGIN))
    }

    private fun callback(state: String): Uri =
        Uri.parse("omnigent://auth-callback?state=$state&token_type=bearer&token=tok")

    private companion object {
        const val ORIGIN = "https://myapp.aws.databricksapps.com"
    }
}
