package ai.omnigent.android

import android.app.Activity
import android.os.Looper
import android.webkit.CookieManager
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLooper
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class OidcLoginManagerTest {
    private lateinit var server: HttpServer
    private lateinit var origin: String
    private val managers = mutableListOf<OidcLoginManager>()

    @Before
    fun setUp() {
        ShadowCookieManager.resetCookies()
        server = HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0)
        origin = "http://127.0.0.1:${server.address.port}"
        server.start()
    }

    @After
    fun tearDown() {
        managers.forEach(OidcLoginManager::shutdown)
        server.stop(0)
        ShadowCookieManager.resetCookies()
    }

    @Test
    fun `cli-login redirect is rejected without requesting its target`() {
        val redirectHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(302, location = "$origin/redirect-target")
        }
        server.createContext("/redirect-target") { exchange ->
            redirectHits.incrementAndGet()
            exchange.respond(200, ticketBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Rejected, result)
        assertEquals(0, redirectHits.get())
    }

    @Test
    fun `cli-poll redirect is rejected without requesting valid token target`() {
        val redirectHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(302, location = "$origin/redirect-target")
        }
        server.createContext("/redirect-target") { exchange ->
            redirectHits.incrementAndGet()
            exchange.respond(200, tokenBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Rejected, result)
        assertEquals(0, redirectHits.get())
    }

    @Test
    fun `WebView cookies are attached to login and poll endpoints`() {
        val handlerFailure = AtomicReference<Throwable?>()
        val expectedCookie = "front_door=session"
        CookieManager.getInstance().setCookie(origin, expectedCookie)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.assertCookie(expectedCookie, handlerFailure)
            exchange.respond(200, tokenBody())
        }

        val result = runLogin(manager())

        assertEquals(LoginResult.Success(TOKEN), result)
        handlerFailure.get()?.let { throw it }
    }

    @Test
    fun `absolute and scheme-relative login urls are rejected`() {
        val loginUrls =
            ConcurrentLinkedQueue(
                listOf(
                    "https://other.example/browser-login",
                    "//other.example/browser-login",
                ),
            )
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody(checkNotNull(loginUrls.poll())))
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(200, tokenBody())
        }

        repeat(2) {
            assertEquals(LoginResult.Rejected, runLogin(manager()))
        }
        assertEquals(0, pollHits.get())
    }

    @Test
    fun `deadline expiry is timed out rather than rejected`() {
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(202)
        }

        val result = runLogin(manager(pollTimeoutMs = 50))

        assertEquals(LoginResult.TimedOut, result)
    }

    @Test
    fun `malformed poll payload is rejected without another poll`() {
        val pollHits = AtomicInteger()
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            pollHits.incrementAndGet()
            exchange.respond(200, "not-json")
        }

        val result = runLogin(manager(pollTimeoutMs = 1_000))

        assertEquals(LoginResult.Rejected, result)
        assertEquals(1, pollHits.get())
    }

    @Test
    fun `start after shutdown does not throw or leave inFlight set`() {
        val manager = manager()
        manager.shutdown()

        val started =
            manager.start(activity(), origin) {
                throw AssertionError("shutdown manager delivered a callback")
            }

        assertFalse(started)
        assertFalse(manager.isInFlightForTest())
    }

    @Test
    fun `results are delivered on the main thread`() {
        val deliveredOnMain = AtomicBoolean(false)
        server.createContext("/auth/cli-login") { exchange ->
            exchange.respond(200, ticketBody())
        }
        server.createContext("/auth/cli-poll") { exchange ->
            exchange.respond(200, tokenBody())
        }

        val result =
            runLogin(manager()) {
                deliveredOnMain.set(Looper.myLooper() == Looper.getMainLooper())
            }

        assertEquals(LoginResult.Success(TOKEN), result)
        assertTrue(deliveredOnMain.get())
    }

    private fun manager(
        pollIntervalMs: Long = 1,
        pollTimeoutMs: Long = 1_000,
    ): OidcLoginManager =
        OidcLoginManager(
            pollIntervalMs = pollIntervalMs,
            pollTimeoutMs = pollTimeoutMs,
        ).also(managers::add)

    private fun runLogin(
        manager: OidcLoginManager,
        onResult: (LoginResult) -> Unit = {},
    ): LoginResult {
        val result = AtomicReference<LoginResult?>()
        val delivered = CountDownLatch(1)
        assertTrue(
            manager.start(activity(), origin) { loginResult ->
                result.set(loginResult)
                onResult(loginResult)
                delivered.countDown()
            },
        )
        awaitCallback(manager, delivered)
        return checkNotNull(result.get())
    }

    private fun awaitCallback(
        manager: OidcLoginManager,
        delivered: CountDownLatch,
    ) {
        val deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5)
        while (delivered.count > 0 && System.nanoTime() < deadline) {
            if (!manager.isInFlightForTest()) ShadowLooper.idleMainLooper()
            Thread.yield()
        }
        ShadowLooper.idleMainLooper()
        assertTrue("login callback was not delivered", delivered.await(0, TimeUnit.MILLISECONDS))
    }

    private fun activity(): Activity =
        Robolectric
            .buildActivity(Activity::class.java)
            .setup()
            .get()

    private fun ticketBody(loginUrl: String = "/browser-login"): String =
        """{"ticket":"ticket-1","login_url":"$loginUrl"}"""

    private fun tokenBody(): String = """{"token":"$TOKEN"}"""

    private fun HttpExchange.assertCookie(
        expected: String,
        failure: AtomicReference<Throwable?>,
    ) {
        runCatching {
            assertEquals(expected, requestHeaders.getFirst("Cookie"))
        }.exceptionOrNull()?.let { failure.compareAndSet(null, it) }
    }

    private fun HttpExchange.respond(
        status: Int,
        body: String = "",
        location: String? = null,
    ) {
        if (location != null) responseHeaders.add("Location", location)
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        sendResponseHeaders(status, bytes.size.toLong())
        responseBody.use { it.write(bytes) }
        close()
    }

    private companion object {
        const val TOKEN = "header.payload.signature"
    }
}
