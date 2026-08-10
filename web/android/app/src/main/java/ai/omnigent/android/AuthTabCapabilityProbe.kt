package ai.omnigent.android

import android.os.Handler
import android.os.Looper
import android.util.JsonReader
import java.io.InputStreamReader
import java.io.Reader
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Probes whether an origin anonymously serves a non-empty Digital Asset Links
 * array. Results are cached per origin for the lifetime of this probe.
 */
internal class AuthTabCapabilityProbe(
    private val fetch: (String) -> Boolean = ::fetchAssetLinks,
    private val execute: ((() -> Unit) -> Unit) = { task -> IO.execute(task) },
    private val post: ((() -> Unit) -> Unit) = { task -> MAIN.post(task) },
) {
    private val lock = Any()
    private val results = mutableMapOf<String, Boolean>()
    private val pending = mutableMapOf<String, MutableList<(Boolean) -> Unit>>()

    fun probe(
        origin: String?,
        onResult: (Boolean) -> Unit,
    ) {
        val normalized = originOf(origin)
        if (normalized == null || URL(normalized).protocol != "https") {
            post { onResult(false) }
            return
        }

        synchronized(lock) {
            results[normalized]?.let { cached ->
                post { onResult(cached) }
                return
            }
            pending[normalized]?.let { callbacks ->
                callbacks += onResult
                return
            }
            pending[normalized] = mutableListOf(onResult)
        }

        execute {
            val supported = runCatching { fetch(normalized) }.getOrDefault(false)
            val callbacks =
                synchronized(lock) {
                    results[normalized] = supported
                    pending.remove(normalized).orEmpty()
                }
            post { callbacks.forEach { callback -> callback(supported) } }
        }
    }

    private companion object {
        val IO: Executor by lazy { Executors.newCachedThreadPool() }
        val MAIN: Handler by lazy { Handler(Looper.getMainLooper()) }
        const val TIMEOUT_MS = 3_000

        fun fetchAssetLinks(origin: String): Boolean {
            val connection =
                URL("$origin/.well-known/assetlinks.json").openConnection() as HttpURLConnection
            connection.requestMethod = "GET"
            connection.instanceFollowRedirects = false
            connection.useCaches = false
            connection.connectTimeout = TIMEOUT_MS
            connection.readTimeout = TIMEOUT_MS
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("Cookie", "")
            return try {
                if (connection.responseCode != HttpURLConnection.HTTP_OK) return false
                hasNonEmptyAssetLinks(
                    InputStreamReader(connection.inputStream, Charsets.UTF_8),
                )
            } finally {
                connection.disconnect()
            }
        }
    }
}

internal fun hasNonEmptyAssetLinks(source: Reader): Boolean =
    JsonReader(source).use { reader ->
        reader.beginArray()
        reader.hasNext()
    }
