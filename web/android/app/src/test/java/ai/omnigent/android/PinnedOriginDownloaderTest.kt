package ai.omnigent.android

import android.app.Application
import android.os.Environment
import android.util.Log
import android.webkit.CookieManager
import androidx.test.core.app.ApplicationProvider
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLog
import org.robolectric.shadows.ShadowLooper.idleMainLooper
import org.robolectric.shadows.ShadowToast
import org.robolectric.util.ReflectionHelpers
import java.io.File
import java.net.InetSocketAddress
import java.nio.charset.StandardCharsets
import java.util.concurrent.ConcurrentLinkedQueue
import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger
import java.util.concurrent.atomic.AtomicReference

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [28])
class PinnedOriginDownloaderTest {
    private lateinit var context: Application
    private lateinit var pinnedServer: HttpServer
    private lateinit var otherServer: HttpServer
    private lateinit var pinnedOrigin: String
    private lateinit var otherOrigin: String
    private val downloaders = mutableListOf<PinnedOriginDownloader>()
    private val savedFiles = mutableListOf<File>()

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
        pinnedServer = server()
        otherServer = server()
        pinnedOrigin = originOf(pinnedServer)
        otherOrigin = originOf(otherServer)
    }

    @After
    fun tearDown() {
        downloaders.forEach(PinnedOriginDownloader::shutdown)
        pinnedServer.stop(0)
        otherServer.stop(0)
        savedFiles.forEach(File::delete)
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
    }

    @Test
    fun `cookie is dropped permanently after a redirect leaves the pinned origin`() {
        val firstCookie = AtomicReference<String?>()
        val sameOriginCookie = AtomicReference<String?>()
        val otherCookie = AtomicReference<String?>()
        val returnedCookie = AtomicReference<String?>()
        val userAgents = ConcurrentLinkedQueue<String>()
        pinnedServer.createContext("/start") { exchange ->
            firstCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("/same-origin")
        }
        pinnedServer.createContext("/same-origin") { exchange ->
            sameOriginCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("$otherOrigin/middle")
        }
        otherServer.createContext("/middle") { exchange ->
            otherCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.redirect("$pinnedOrigin/final")
        }
        pinnedServer.createContext("/final") { exchange ->
            returnedCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            userAgents += checkNotNull(exchange.requestHeaders.getFirst("User-Agent"))
            exchange.respond(200, DOWNLOAD_BODY)
        }
        CookieManager.getInstance().setCookie(pinnedOrigin, SESSION_COOKIE)
        val target = targetFile("redirected.txt")
        val downloader = downloader()

        downloader.download(
            "$pinnedOrigin/start",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            target.name,
        )
        await(downloader)

        assertEquals(SESSION_COOKIE, firstCookie.get())
        assertEquals(SESSION_COOKIE, sameOriginCookie.get())
        assertNull(otherCookie.get())
        assertNull(returnedCookie.get())
        assertEquals(listOf(USER_AGENT, USER_AGENT, USER_AGENT, USER_AGENT), userAgents.toList())
        assertEquals(DOWNLOAD_BODY, target.readText())
        assertEquals("Saved ${target.name} to Downloads", ShadowToast.getTextOfLatestToast())
    }

    @Test
    fun `redirect chains are capped`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/loop") { exchange ->
            hits.incrementAndGet()
            val step = exchange.requestURI.query?.substringAfter("step=")?.toInt() ?: 0
            if (step < 11) {
                exchange.redirect("$pinnedOrigin/loop?step=${step + 1}")
            } else {
                exchange.respond(200, DOWNLOAD_BODY)
            }
        }
        val target = targetFile("too-many-redirects.txt")
        val downloader = downloader()

        downloader.download(
            "$pinnedOrigin/loop?step=0",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            target.name,
        )
        await(downloader)

        assertEquals(11, hits.get())
        assertFalse(target.exists())
        assertEquals("Couldn't save ${target.name}", ShadowToast.getTextOfLatestToast())
    }

    @Test
    fun `download submitted after shutdown is dropped with a warning`() {
        val hits = AtomicInteger()
        pinnedServer.createContext("/file") { exchange ->
            hits.incrementAndGet()
            exchange.respond(200, DOWNLOAD_BODY)
        }
        val downloader = downloader()
        downloader.shutdown()

        downloader.download(
            "$pinnedOrigin/file",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "after-shutdown.txt",
        )

        assertEquals(0, hits.get())
        val warning = ShadowLog.getLogsForTag("PinnedOriginDownloader").single()
        assertEquals(Log.WARN, warning.type)
        assertEquals("Dropping download because the worker is shut down", warning.msg)
    }

    private fun downloader(): PinnedOriginDownloader =
        PinnedOriginDownloader(context).also(downloaders::add)

    private fun await(downloader: PinnedOriginDownloader) {
        downloader.shutdown()
        val executor: ExecutorService = ReflectionHelpers.getField(downloader, "io")
        assertTrue(executor.awaitTermination(5, TimeUnit.SECONDS))
        idleMainLooper()
    }

    private fun targetFile(name: String): File {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        return File(dir, name).also {
            it.delete()
            savedFiles += it
        }
    }

    private fun server(): HttpServer =
        HttpServer.create(InetSocketAddress("127.0.0.1", 0), 0).apply { start() }

    private fun originOf(server: HttpServer): String =
        "http://127.0.0.1:${server.address.port}"

    private fun HttpExchange.redirect(location: String) {
        responseHeaders.add("Location", location)
        sendResponseHeaders(302, -1)
        close()
    }

    private fun HttpExchange.respond(
        status: Int,
        body: String,
    ) {
        val bytes = body.toByteArray(StandardCharsets.UTF_8)
        sendResponseHeaders(status, bytes.size.toLong())
        responseBody.use { it.write(bytes) }
    }

    private companion object {
        const val SESSION_COOKIE = "front_door=pinned"
        const val USER_AGENT = "OmnigentTest/1.0"
        const val DOWNLOAD_BODY = "download body"
    }
}
