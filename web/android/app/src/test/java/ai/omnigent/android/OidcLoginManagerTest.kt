package ai.omnigent.android

import android.app.Activity
import android.os.Looper
import com.sun.net.httpserver.HttpServer
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import java.net.InetSocketAddress
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    // Port 1 is never listening — requestTicket fails fast, and the flow ends
    // without a token. The start/cancel bookkeeping tests use it so they never
    // touch a real login; the delivery tests stub the two network steps instead.
    private val deadOrigin = "http://127.0.0.1:1"
    private val origin = "https://server.example"

    // Build the host once per test and reuse it: driving an Activity lifecycle
    // idles the main looper, which would run a finished flow's queued completion
    // (the post that frees the slot) in the middle of the assertions below.
    private fun activity(): Activity = Robolectric.buildActivity(Activity::class.java).setup().get()

    // The flow completes on its own thread and posts back; pump the (paused)
    // main looper until the post lands or the window closes.
    private fun drainMain(condition: () -> Boolean = { false }) {
        val deadline = System.currentTimeMillis() + 2_000
        while (System.currentTimeMillis() < deadline) {
            shadowOf(Looper.getMainLooper()).idle()
            if (condition()) return
            Thread.sleep(5)
        }
    }

    @Test
    fun `second start while a flow is in flight is refused`() {
        val activity = activity()
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        assertFalse(manager.start(activity, deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `cancel frees the manager for an immediate new start`() {
        val activity = activity()
        val manager = OidcLoginManager()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.cancel()
        assertTrue(manager.start(activity, deadOrigin) { _, _ -> })
        manager.shutdown()
    }

    @Test
    fun `a completed flow delivers the origin it was started for with the token`() {
        val activity = activity()
        val manager = OidcLoginManager()
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ -> "session-jwt" }

        var delivered: Pair<String, String>? = null
        assertTrue(manager.start(activity, origin) { o, t -> delivered = o to t })
        drainMain { delivered != null }

        assertEquals(origin to "session-jwt", delivered)
        manager.shutdown()
    }

    @Test
    fun `a cancelled flow does not deliver a token that lands after the switch`() {
        val activity = activity()
        val manager = OidcLoginManager()
        val polling = CountDownLatch(1)
        val cancelled = CountDownLatch(1)
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ ->
            // Hold the flow mid-poll until the host has switched servers, then
            // hand back a token anyway — cancel() interrupts this wait.
            polling.countDown()
            runCatching { cancelled.await(2, TimeUnit.SECONDS) }
            "stale-token"
        }

        var delivered: Pair<String, String>? = null
        assertTrue(manager.start(activity, origin) { o, t -> delivered = o to t })
        assertTrue(polling.await(2, TimeUnit.SECONDS))
        manager.cancel()
        cancelled.countDown()
        drainMain { delivered != null }

        assertNull(delivered)
    }

    @Test
    fun `a cancelled flow does not launch the browser for a ticket that lands after cancel`() {
        val activity = activity()
        val manager = OidcLoginManager()
        val polling = CountDownLatch(1)
        val cancelled = CountDownLatch(1)
        // The launchTab post is queued (paused main looper) right before the
        // poll starts; cancelling while the poll holds must drop that launch.
        manager.requestTicket = { _, _ -> OidcLoginManager.Ticket("ticket-1", "/auth/login?t=1") }
        manager.pollForToken = { _, _, _ ->
            polling.countDown()
            runCatching { cancelled.await(2, TimeUnit.SECONDS) }
            null
        }

        assertTrue(manager.start(activity, origin) { _, _ -> })
        assertTrue(polling.await(2, TimeUnit.SECONDS))
        manager.cancel()
        cancelled.countDown()
        drainMain()

        assertNull(shadowOf(activity).nextStartedActivity)
    }

    // A one-shot local server: transient failures for the first [failures]
    // requests to a path, then the real answer — drives the default (HTTP)
    // requestTicket/pollForToken seams end to end on the JVM.
    private fun transientThenOk(
        failures: Int,
        status: Int,
        okBody: String,
    ): Pair<HttpServer, AtomicInteger> {
        val server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        val hits = AtomicInteger(0)
        server.createContext("/") { exchange ->
            if (hits.incrementAndGet() <= failures) {
                exchange.sendResponseHeaders(status, -1)
                exchange.close()
            } else {
                val bytes = okBody.toByteArray()
                exchange.sendResponseHeaders(200, bytes.size.toLong())
                exchange.responseBody.use { it.write(bytes) }
            }
        }
        server.start()
        return server to hits
    }

    @Test
    fun `ticket creation retries a transient 503 and completes on the next 200`() {
        val (server, hits) =
            transientThenOk(1, 503, """{"ticket":"t-1","login_url":"/auth/login?ticket=t-1"}""")
        try {
            val manager = OidcLoginManager()
            val origin = "http://127.0.0.1:${server.address.port}"
            val ticket = manager.requestTicket(origin, System.currentTimeMillis() + 30_000)
            assertEquals(OidcLoginManager.Ticket("t-1", "/auth/login?ticket=t-1"), ticket)
            assertEquals(2, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `ticket creation still fails fast on a non-transient error`() {
        val (server, hits) = transientThenOk(1, 401, """{"ticket":"t","login_url":"/l"}""")
        try {
            val manager = OidcLoginManager()
            val origin = "http://127.0.0.1:${server.address.port}"
            assertNull(manager.requestTicket(origin, System.currentTimeMillis() + 30_000))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `polling rides out a transient 503 and returns the token from the next 200`() {
        val (server, hits) = transientThenOk(1, 503, """{"token":"session-jwt"}""")
        try {
            val manager = OidcLoginManager()
            val origin = "http://127.0.0.1:${server.address.port}"
            val token = manager.pollForToken(origin, "t-1", System.currentTimeMillis() + 30_000)
            assertEquals("session-jwt", token)
            assertEquals(2, hits.get())
        } finally {
            server.stop(0)
        }
    }

    @Test
    fun `polling stays fatal on 410 expired`() {
        val (server, hits) = transientThenOk(1, 410, """{"token":"session-jwt"}""")
        try {
            val manager = OidcLoginManager()
            val origin = "http://127.0.0.1:${server.address.port}"
            assertNull(manager.pollForToken(origin, "t-1", System.currentTimeMillis() + 30_000))
            assertEquals(1, hits.get())
        } finally {
            server.stop(0)
        }
    }
}
