package ai.omnigent.android

import android.content.Context
import android.util.Log
import android.webkit.CookieManager
import androidx.work.BackoffPolicy
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequest
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.Worker
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import java.io.IOException
import java.net.HttpURLConnection
import java.net.MalformedURLException
import java.net.URL
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

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

internal fun interface PinnedOriginWorkEnqueuer {
    fun enqueue(
        uniqueName: String,
        policy: ExistingWorkPolicy,
        request: OneTimeWorkRequest,
    )
}

/** Schedules durable pinned-origin downloads without persisting session credentials. */
internal class PinnedOriginDownloader(
    context: Context,
    private val workEnqueuer: PinnedOriginWorkEnqueuer = defaultWorkEnqueuer(context),
) : PinnedOriginDownloadHandler {
    private val shutDown = AtomicBoolean()

    override fun download(
        url: String,
        pinnedOrigin: String,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ) {
        if (originOf(url) != pinnedOrigin) return
        if (shutDown.get()) {
            Log.w(TAG, "Dropping download because the worker is shut down")
            return
        }

        val request =
            PinnedOriginDownloadWorker.request(
                url,
                pinnedOrigin,
                userAgent,
                mimeType,
                suggestedName,
            )
        // KEEP lets a pending or running transfer finish instead of restarting it
        // when the user taps the same download again.
        workEnqueuer.enqueue(
            uniqueWorkName(url, pinnedOrigin, suggestedName),
            ExistingWorkPolicy.KEEP,
            request,
        )
    }

    /** Stops this Activity-owned scheduler; already-enqueued work remains durable. */
    override fun shutdown() {
        shutDown.set(true)
    }

    private companion object {
        const val TAG = "PinnedOriginDownloader"
        const val UNIQUE_WORK_PREFIX = "pinned-origin-download:"

        fun defaultWorkEnqueuer(context: Context): PinnedOriginWorkEnqueuer {
            val applicationContext = context.applicationContext ?: context
            return PinnedOriginWorkEnqueuer { uniqueName, policy, request ->
                WorkManager
                    .getInstance(applicationContext)
                    .enqueueUniqueWork(uniqueName, policy, request)
            }
        }

        fun uniqueWorkName(
            url: String,
            pinnedOrigin: String,
            suggestedName: String,
        ): String {
            val identity = "$pinnedOrigin\u0000$url\u0000$suggestedName"
            val digest =
                MessageDigest.getInstance("SHA-256").digest(identity.toByteArray(Charsets.UTF_8))
            val encoded =
                digest.joinToString(separator = "") { byte ->
                    Integer.toHexString(byte.toInt() and 0xff).padStart(2, '0')
                }
            return UNIQUE_WORK_PREFIX + encoded
        }
    }
}

/** Streams a pinned-origin download while keeping WebView cookies on their trusted origin. */
internal class PinnedOriginDownloadWorker(
    context: Context,
    workerParameters: WorkerParameters,
) : Worker(context, workerParameters) {
    private val storage = DownloadStorage(applicationContext)
    private val notifications = DownloadNotificationManager(applicationContext)

    override fun doWork(): Result {
        val input = readInput()
        if (input == null) {
            return terminalFailure(
                inputData.getString(KEY_SUGGESTED_NAME) ?: FALLBACK_NAME,
                TerminalDownloadException("Missing download input"),
            )
        }

        val initialUrl =
            try {
                URL(input.url)
            } catch (failure: MalformedURLException) {
                return terminalFailure(
                    input.suggestedName,
                    TerminalDownloadException("Invalid download URL", failure),
                )
            }
        if (!isHttpScheme(initialUrl.protocol) ||
            !hasPinnedOrigin(initialUrl, input.pinnedOrigin)
        ) {
            return terminalFailure(
                input.suggestedName,
                TerminalDownloadException("Rejected download origin"),
            )
        }

        // WorkManager persists input Data, so fetch the live cookie only when execution starts.
        // This keeps session credentials out of WorkManager's on-disk database.
        val cookie = CookieManager.getInstance().getCookie(input.url)
        return try {
            val saved =
                downloadFollowingRedirects(
                    initialUrl,
                    input.pinnedOrigin,
                    cookie,
                    input.userAgent,
                    input.mimeType,
                    input.suggestedName,
                )
            if (saved.saved) {
                notifications.succeeded(saved.name, id)
                Result.success()
            } else {
                terminalFailure(
                    saved.name,
                    TerminalDownloadException("Couldn't save download"),
                )
            }
        } catch (failure: TransientDownloadException) {
            transientFailure(input.suggestedName, failure)
        } catch (failure: IOException) {
            transientFailure(input.suggestedName, failure)
        } catch (failure: TerminalDownloadException) {
            terminalFailure(input.suggestedName, failure)
        } catch (failure: Throwable) {
            terminalFailure(input.suggestedName, failure)
        }
    }

    private fun readInput(): DownloadInput? {
        val url = inputData.getString(KEY_URL) ?: return null
        val pinnedOrigin = inputData.getString(KEY_PINNED_ORIGIN) ?: return null
        val userAgent = inputData.getString(KEY_USER_AGENT) ?: return null
        val suggestedName = inputData.getString(KEY_SUGGESTED_NAME) ?: return null
        return DownloadInput(
            url,
            pinnedOrigin,
            userAgent,
            inputData.getString(KEY_MIME_TYPE),
            suggestedName,
        )
    }

    private fun transientFailure(
        suggestedName: String,
        failure: Throwable,
    ): Result {
        Log.w(TAG, "Transient download failure for $suggestedName", failure)
        if (runAttemptCount < MAX_ATTEMPTS - 1) {
            // WorkManager retries start at byte zero; this downloader has no partial-file
            // resumption.
            return Result.retry()
        }
        return terminalFailure(suggestedName, failure)
    }

    private fun terminalFailure(
        suggestedName: String,
        failure: Throwable,
    ): Result {
        Log.e(TAG, "Download failed for $suggestedName", failure)
        notifications.failed(suggestedName, id)
        return Result.failure()
    }

    private fun downloadFollowingRedirects(
        initialUrl: URL,
        pinnedOrigin: String,
        cookie: String?,
        userAgent: String,
        mimeType: String?,
        suggestedName: String,
    ): DownloadSaveResult {
        var currentUrl = initialUrl
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
                    if (redirectCount >= MAX_REDIRECTS) {
                        throw TerminalDownloadException("Too many redirects")
                    }
                    val location =
                        connection
                            .getHeaderField("Location")
                            ?.takeIf(String::isNotBlank)
                            ?: throw TerminalDownloadException("Redirect missing Location")
                    val nextUrl =
                        try {
                            URL(currentUrl, location)
                        } catch (failure: MalformedURLException) {
                            throw TerminalDownloadException("Invalid redirect Location", failure)
                        }
                    if (!isHttpScheme(nextUrl.protocol)) {
                        throw TerminalDownloadException("Unsupported redirect scheme")
                    }
                    cookieAllowed = cookieAllowed && hasPinnedOrigin(nextUrl, pinnedOrigin)
                    currentUrl = nextUrl
                    redirectCount++
                    continue
                }
                if (status in 500..599) {
                    throw TransientDownloadException("Download failed with HTTP $status")
                }
                if (status !in 200..299) {
                    throw TerminalDownloadException("Download failed with HTTP $status")
                }

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

    private data class DownloadInput(
        val url: String,
        val pinnedOrigin: String,
        val userAgent: String,
        val mimeType: String?,
        val suggestedName: String,
    )

    private class TransientDownloadException(
        message: String,
    ) : Exception(message)

    private class TerminalDownloadException(
        message: String,
        cause: Throwable? = null,
    ) : Exception(message, cause)

    companion object {
        internal const val TAG = "PinnedOriginDownloader"
        internal const val KEY_URL = "url"
        internal const val KEY_PINNED_ORIGIN = "pinned_origin"
        internal const val KEY_USER_AGENT = "user_agent"
        internal const val KEY_MIME_TYPE = "mime_type"
        internal const val KEY_SUGGESTED_NAME = "suggested_name"
        internal const val MAX_ATTEMPTS = 3
        private const val WORK_TAG = "pinned-origin-download"
        private const val MAX_REDIRECTS = 10
        private const val CONNECT_TIMEOUT_MS = 15_000
        private const val READ_TIMEOUT_MS = 30_000
        private const val DEFAULT_MIME_TYPE = "application/octet-stream"
        private const val FALLBACK_NAME = "download"
        private const val BACKOFF_SECONDS = 30L
        private val REDIRECT_STATUS_CODES = setOf(301, 302, 303, 307, 308)

        internal fun request(
            url: String,
            pinnedOrigin: String,
            userAgent: String,
            mimeType: String?,
            suggestedName: String,
        ): OneTimeWorkRequest {
            val constraints =
                Constraints
                    .Builder()
                    .setRequiredNetworkType(NetworkType.CONNECTED)
                    .build()
            return OneTimeWorkRequestBuilder<PinnedOriginDownloadWorker>()
                .setInputData(inputData(url, pinnedOrigin, userAgent, mimeType, suggestedName))
                .setConstraints(constraints)
                .setBackoffCriteria(
                    BackoffPolicy.EXPONENTIAL,
                    BACKOFF_SECONDS,
                    TimeUnit.SECONDS,
                ).addTag(WORK_TAG)
                .build()
        }

        internal fun inputData(
            url: String,
            pinnedOrigin: String,
            userAgent: String,
            mimeType: String?,
            suggestedName: String,
        ): Data =
            workDataOf(
                KEY_URL to url,
                KEY_PINNED_ORIGIN to pinnedOrigin,
                KEY_USER_AGENT to userAgent,
                KEY_MIME_TYPE to mimeType,
                KEY_SUGGESTED_NAME to suggestedName,
            )
    }
}
