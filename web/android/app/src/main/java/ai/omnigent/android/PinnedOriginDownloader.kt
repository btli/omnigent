package ai.omnigent.android

import android.content.Context
import android.util.Log
import android.webkit.CookieManager
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException

internal interface PinnedOriginDownloadHandler {
    fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    )

    fun shutdown()
}

/** Streams pinned-origin downloads while keeping WebView cookies on their trusted origin. */
internal class PinnedOriginDownloader(
    context: Context,
) : PinnedOriginDownloadHandler {
    private val storage = DownloadStorage(context.applicationContext ?: context)
    private val io: ExecutorService = Executors.newSingleThreadExecutor()

    override fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ) {
        if (originOf(url) != pinnedOrigin) return
        val cookie = CookieManager.getInstance().getCookie(url)
        try {
            io.execute {
                val result =
                    runCatching {
                        downloadFollowingRedirects(
                            url,
                            pinnedOrigin,
                            cookie,
                            userAgent,
                            mimeType,
                            suggestedName,
                        )
                    }.getOrElse { failure ->
                        Log.e(TAG, "Download failed for $suggestedName", failure)
                        storage.failed(suggestedName)
                    }
                storage.report(result)
            }
        } catch (_: RejectedExecutionException) {
            Log.w(TAG, "Dropping download because the worker is shut down")
        }
    }

    override fun shutdown() {
        io.shutdown()
    }

    private fun downloadFollowingRedirects(
        initialUrl: String,
        pinnedOrigin: String,
        cookie: String?,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ): DownloadSaveResult {
        var currentUrl = URL(initialUrl)
        require(hasPinnedOrigin(currentUrl, pinnedOrigin))
        var cookieAllowed = true
        var redirectCount = 0

        while (true) {
            val connection = currentUrl.openConnection() as HttpURLConnection
            try {
                connection.instanceFollowRedirects = false
                connection.connectTimeout = CONNECT_TIMEOUT_MS
                connection.readTimeout = READ_TIMEOUT_MS
                connection.requestMethod = "GET"
                if (userAgent.isNotBlank()) connection.setRequestProperty("User-Agent", userAgent)
                if (cookieAllowed && cookie != null) {
                    connection.setRequestProperty("Cookie", cookie)
                }

                val status = connection.responseCode
                if (status in REDIRECT_STATUS_CODES) {
                    if (redirectCount >= MAX_REDIRECTS) error("Too many redirects")
                    val location = connection.getHeaderField("Location")
                        ?: error("Redirect missing Location")
                    val nextUrl = URL(currentUrl, location)
                    if (!isHttpScheme(nextUrl.protocol)) error("Unsupported redirect scheme")
                    cookieAllowed = cookieAllowed && hasPinnedOrigin(nextUrl, pinnedOrigin)
                    currentUrl = nextUrl
                    redirectCount++
                    continue
                }
                if (status !in 200..299) error("Download failed with HTTP $status")

                val resolvedMimeType =
                    mimeType?.takeIf(String::isNotBlank)
                        ?: connection.contentType
                            ?.substringBefore(';')
                            ?.trim()
                            ?.takeIf(String::isNotBlank)
                        ?: DEFAULT_MIME_TYPE
                return connection.inputStream.use { input ->
                    var streamFailure: Throwable? = null
                    val result =
                        storage.save(suggestedName, resolvedMimeType) { output ->
                            try {
                                input.copyTo(output)
                            } catch (failure: Throwable) {
                                streamFailure = failure
                                throw failure
                            }
                        }
                    streamFailure?.let { throw it }
                    result
                }
            } finally {
                connection.disconnect()
            }
        }
    }

    internal fun hasPinnedOrigin(
        url: URL,
        pinnedOrigin: String,
    ): Boolean {
        val pinnedUrl = runCatching { URL(pinnedOrigin) }.getOrNull() ?: return false
        return url.protocol.equals(pinnedUrl.protocol, ignoreCase = true) &&
            url.host.equals(pinnedUrl.host, ignoreCase = true) &&
            effectivePort(url) == effectivePort(pinnedUrl)
    }

    private fun effectivePort(url: URL): Int =
        if (url.port == -1) url.defaultPort else url.port

    private companion object {
        const val TAG = "PinnedOriginDownloader"
        const val MAX_REDIRECTS = 10
        const val CONNECT_TIMEOUT_MS = 15_000
        const val READ_TIMEOUT_MS = 30_000
        const val DEFAULT_MIME_TYPE = "application/octet-stream"
        val REDIRECT_STATUS_CODES = setOf(301, 302, 303, 307, 308)
    }
}
