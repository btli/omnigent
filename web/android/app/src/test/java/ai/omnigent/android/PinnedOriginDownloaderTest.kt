@file:Suppress("RestrictedApi")

package ai.omnigent.android

import android.app.Application
import android.app.NotificationManager
import android.content.Context
import android.os.Environment
import android.util.Log
import android.webkit.CookieManager
import androidx.test.core.app.ApplicationProvider
import androidx.work.BackoffPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.ListenableWorker
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.testing.TestListenableWorkerBuilder
import com.sun.net.httpserver.HttpExchange
import com.sun.net.httpserver.HttpServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLog
import org.robolectric.shadows.ShadowNotificationManager
import java.io.File
import java.net.InetSocketAddress
import java.net.URL
import java.nio.charset.StandardCharsets
import java.util.concurrent.ConcurrentLinkedQueue
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
    private val savedFiles = mutableListOf<File>()

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
        ShadowNotificationManager.reset()
        pinnedServer = server()
        otherServer = server()
        pinnedOrigin = originOf(pinnedServer)
        otherOrigin = originOf(otherServer)
    }

    @After
    fun tearDown() {
        pinnedServer.stop(0)
        otherServer.stop(0)
        savedFiles.forEach(File::delete)
        ShadowCookieManager.resetCookies()
        ShadowLog.clear()
        ShadowNotificationManager.reset()
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
        val target = targetFile("redirected.txt")
        val worker = worker("$pinnedOrigin/start", target.name)
        CookieManager.getInstance().setCookie(pinnedOrigin, SESSION_COOKIE)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertEquals(SESSION_COOKIE, firstCookie.get())
        assertEquals(SESSION_COOKIE, sameOriginCookie.get())
        assertNull(otherCookie.get())
        assertNull(returnedCookie.get())
        assertEquals(listOf(USER_AGENT, USER_AGENT, USER_AGENT, USER_AGENT), userAgents.toList())
        assertEquals(DOWNLOAD_BODY, target.readText())
        val notification = notificationFor(worker)
        assertNotNull(notification)
        assertEquals(
            "Saved ${target.name} to app storage",
            notification!!.extras.getCharSequence(android.app.Notification.EXTRA_TEXT),
        )
    }

    @Test
    fun `cookie is dropped when a redirect changes only the host string`() {
        val redirectedCookie = AtomicReference<String?>()
        pinnedServer.createContext("/host-start") { exchange ->
            exchange.redirect("http://localhost:${pinnedServer.address.port}/host-final")
        }
        pinnedServer.createContext("/host-final") { exchange ->
            redirectedCookie.set(exchange.requestHeaders.getFirst("Cookie"))
            exchange.respond(200, DOWNLOAD_BODY)
        }
        CookieManager.getInstance().setCookie(pinnedOrigin, SESSION_COOKIE)
        val target = targetFile("different-host.txt")

        val result = worker("$pinnedOrigin/host-start", target.name).doWork()

        assertEquals(ListenableWorker.Result.success(), result)
        assertNull(redirectedCookie.get())
        assertEquals(DOWNLOAD_BODY, target.readText())
    }

    @Test
    fun `pinned origin rejects a protocol change when host and port match`() {
        val worker = worker("$pinnedOrigin/file", "protocol.txt")

        assertFalse(
            worker.hasPinnedOrigin(
                URL("https://pinned.example:8443/file"),
                "http://pinned.example:8443",
            ),
        )
    }

    @Test
    fun `pinned origin rejects a port change when protocol and host match`() {
        val worker = worker("$pinnedOrigin/file", "port.txt")

        assertFalse(
            worker.hasPinnedOrigin(
                URL("https://pinned.example:8444/file"),
                "https://pinned.example:8443",
            ),
        )
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
        val worker = worker("$pinnedOrigin/loop?step=0", target.name)

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertEquals(11, hits.get())
        assertFalse(target.exists())
        assertNotNull(notificationFor(worker))
        val error = ShadowLog.getLogsForTag("PinnedOriginDownloader").single()
        assertEquals(Log.ERROR, error.type)
        assertEquals("Too many redirects", error.throwable.message)
    }

    @Test
    fun `enqueued work requires connected network and exponential backoff`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)

        downloader.download(
            "$pinnedOrigin/report",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "report.txt",
        )

        val workSpec = calls.single().request.workSpec
        assertEquals(NetworkType.CONNECTED, workSpec.constraints.requiredNetworkType)
        assertEquals(BackoffPolicy.EXPONENTIAL, workSpec.backoffPolicy)
        assertEquals(TimeUnit.SECONDS.toMillis(30), workSpec.backoffDelayDuration)
    }

    @Test
    fun `HTTP 500 is retried before the attempt cap`() {
        pinnedServer.createContext("/unavailable") { exchange ->
            exchange.respond(503, "try later")
        }
        val worker = worker("$pinnedOrigin/unavailable", "retry.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.retry(), result)
        assertNull(notificationFor(worker))
    }

    @Test
    fun `HTTP 500 fails when the bounded attempt cap is reached`() {
        pinnedServer.createContext("/still-unavailable") { exchange ->
            exchange.respond(503, "try later")
        }
        val worker =
            worker(
                "$pinnedOrigin/still-unavailable",
                "retry-exhausted.txt",
                PinnedOriginDownloadWorker.MAX_ATTEMPTS - 1,
            )

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        val notification = notificationFor(worker)
        assertNotNull(notification)
        assertEquals(
            "Download failed",
            notification!!.extras.getCharSequence(android.app.Notification.EXTRA_TITLE),
        )
    }

    @Test
    fun `HTTP 4xx is terminal without a retry`() {
        pinnedServer.createContext("/missing") { exchange ->
            exchange.respond(404, "not found")
        }
        val worker = worker("$pinnedOrigin/missing", "missing.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Download failed with HTTP 404",
            ShadowLog.getLogsForTag("PinnedOriginDownloader").single().throwable.message,
        )
    }

    @Test
    fun `rejected initial origin is terminal without a retry`() {
        val worker =
            TestListenableWorkerBuilder<PinnedOriginDownloadWorker>(
                context = context,
                inputData =
                    PinnedOriginDownloadWorker.inputData(
                        "$otherOrigin/file",
                        pinnedOrigin,
                        USER_AGENT,
                        "text/plain",
                        "rejected.txt",
                    ),
                runAttemptCount = 0,
            ).build()

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Rejected download origin",
            ShadowLog.getLogsForTag("PinnedOriginDownloader").single().throwable.message,
        )
    }

    @Test
    fun `redirect without Location is terminal without a retry`() {
        pinnedServer.createContext("/missing-location") { exchange ->
            exchange.sendResponseHeaders(302, -1)
            exchange.close()
        }
        val worker = worker("$pinnedOrigin/missing-location", "missing-location.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Redirect missing Location",
            ShadowLog.getLogsForTag("PinnedOriginDownloader").single().throwable.message,
        )
    }

    @Test
    fun `non-http redirect is terminal without a retry`() {
        pinnedServer.createContext("/ftp") { exchange ->
            exchange.redirect("ftp://files.example.com/report.txt")
        }
        val worker = worker("$pinnedOrigin/ftp", "ftp.txt")

        val result = worker.doWork()

        assertEquals(ListenableWorker.Result.failure(), result)
        assertNotNull(notificationFor(worker))
        assertEquals(
            "Unsupported redirect scheme",
            ShadowLog.getLogsForTag("PinnedOriginDownloader").single().throwable.message,
        )
    }

    @Test
    fun `download submitted after shutdown is dropped with a warning`() {
        val calls = mutableListOf<EnqueueCall>()
        val downloader = recordingDownloader(calls)
        downloader.shutdown()

        downloader.download(
            "$pinnedOrigin/file",
            pinnedOrigin,
            USER_AGENT,
            "text/plain",
            "after-shutdown.txt",
        )

        assertTrue(calls.isEmpty())
        val warning = ShadowLog.getLogsForTag("PinnedOriginDownloader").single()
        assertEquals(Log.WARN, warning.type)
        assertEquals("Dropping download because the worker is shut down", warning.msg)
    }

    private fun worker(
        url: String,
        suggestedName: String,
        runAttemptCount: Int = 0,
    ): PinnedOriginDownloadWorker =
        TestListenableWorkerBuilder<PinnedOriginDownloadWorker>(
            context = context,
            inputData =
                PinnedOriginDownloadWorker.inputData(
                    url,
                    pinnedOrigin,
                    USER_AGENT,
                    "text/plain",
                    suggestedName,
                ),
            runAttemptCount = runAttemptCount,
        ).build()

    private fun recordingDownloader(calls: MutableList<EnqueueCall>): PinnedOriginDownloader =
        PinnedOriginDownloader(
            context,
            PinnedOriginWorkEnqueuer { uniqueName, policy, request ->
                calls += EnqueueCall(uniqueName, policy, request)
            },
        )

    private fun notificationFor(worker: PinnedOriginDownloadWorker) =
        shadowNotificationManager().getNotification(
            DownloadNotificationManager.notificationTag(worker.id),
            DownloadNotificationManager.NOTIFICATION_ID,
        )

    private fun shadowNotificationManager() =
        shadowOf(
            context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
        )

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

    private data class EnqueueCall(
        val uniqueName: String,
        val policy: ExistingWorkPolicy,
        val request: OneTimeWorkRequest,
    )

    private companion object {
        const val SESSION_COOKIE = "front_door=pinned"
        const val USER_AGENT = "OmnigentTest/1.0"
        const val DOWNLOAD_BODY = "download body"
    }
}
