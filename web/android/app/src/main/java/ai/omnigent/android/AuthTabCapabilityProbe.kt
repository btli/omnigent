package ai.omnigent.android

import android.content.Context
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.util.JsonReader
import android.util.JsonToken
import java.io.InputStreamReader
import java.io.Reader
import java.net.HttpURLConnection
import java.net.URL
import java.security.MessageDigest
import java.util.Locale
import java.util.concurrent.Executor
import java.util.concurrent.Executors

/**
 * Probes whether an origin anonymously serves Digital Asset Links matching the
 * running app. Results are cached per origin until explicitly forgotten.
 */
internal class AuthTabCapabilityProbe(
    context: Context,
    private val signingFingerprint: () -> String? = { signingCertificateSha256(context) },
    private val fetch: (String, String, String) -> Boolean = ::fetchAssetLinks,
    private val execute: ((() -> Unit) -> Unit) = { task -> IO.execute(task) },
    private val post: ((() -> Unit) -> Unit) = { task -> MAIN.post(task) },
) {
    private class PendingProbe(
        val callbacks: MutableList<(Boolean) -> Unit>,
    )

    private val packageName = context.packageName
    private val lock = Any()
    private val results = mutableMapOf<String, Boolean>()
    private val pending = mutableMapOf<String, PendingProbe>()

    fun probe(
        origin: String?,
        onResult: (Boolean) -> Unit,
    ) {
        val normalized = originOf(origin)
        if (normalized == null || URL(normalized).protocol != "https") {
            post { onResult(false) }
            return
        }

        val pendingProbe =
            synchronized(lock) {
                results[normalized]?.let { cached ->
                    post { onResult(cached) }
                    return
                }
                pending[normalized]?.let { probe ->
                    probe.callbacks += onResult
                    return
                }
                PendingProbe(mutableListOf(onResult)).also { pending[normalized] = it }
            }

        execute {
            // This is only a launch hint; browser DAL verification remains authoritative.
            val supported =
                signingFingerprint()?.let { fingerprint ->
                    runCatching { fetch(normalized, packageName, fingerprint) }.getOrDefault(false)
                } ?: false
            val callbacks =
                synchronized(lock) {
                    if (pending[normalized] !== pendingProbe) {
                        emptyList()
                    } else {
                        results[normalized] = supported
                        pending.remove(normalized)
                        pendingProbe.callbacks.toList()
                    }
                }
            post { callbacks.forEach { callback -> callback(supported) } }
        }
    }

    fun forget(origin: String?) {
        val normalized = originOf(origin) ?: return
        synchronized(lock) {
            results.remove(normalized)
            pending.remove(normalized)
        }
    }

    private companion object {
        val IO: Executor by lazy { Executors.newCachedThreadPool() }
        val MAIN: Handler by lazy { Handler(Looper.getMainLooper()) }
        const val TIMEOUT_MS = 3_000

        fun fetchAssetLinks(
            origin: String,
            packageName: String,
            signingFingerprint: String,
        ): Boolean {
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
                hasMatchingAssetLinks(
                    InputStreamReader(connection.inputStream, Charsets.UTF_8),
                    packageName,
                    signingFingerprint,
                )
            } finally {
                connection.disconnect()
            }
        }

        fun signingCertificateSha256(context: Context): String? =
            runCatching {
                @Suppress("DEPRECATION")
                val packageInfo =
                    context.packageManager.getPackageInfo(
                        context.packageName,
                        PackageManager.GET_SIGNING_CERTIFICATES,
                    )
                val certificate =
                    packageInfo.signingInfo?.apkContentsSigners?.firstOrNull()
                        ?: return@runCatching null
                MessageDigest
                    .getInstance("SHA-256")
                    .digest(certificate.toByteArray())
                    .joinToString(":") { byte ->
                        String.format(Locale.US, "%02X", byte.toInt() and 0xff)
                    }
            }.getOrNull()
    }
}

internal fun hasMatchingAssetLinks(
    source: Reader,
    packageName: String,
    signingFingerprint: String,
): Boolean =
    runCatching {
        JsonReader(source).use { reader ->
            if (reader.peek() != JsonToken.BEGIN_ARRAY) return@use false
            reader.beginArray()
            var matches = false
            while (reader.hasNext()) {
                matches = readAssetLink(reader, packageName, signingFingerprint) || matches
            }
            reader.endArray()
            matches && reader.peek() == JsonToken.END_DOCUMENT
        }
    }.getOrDefault(false)

private fun readAssetLink(
    reader: JsonReader,
    packageName: String,
    signingFingerprint: String,
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_OBJECT) {
        reader.skipValue()
        return false
    }
    reader.beginObject()
    var relationMatches = false
    var targetMatches = false
    while (reader.hasNext()) {
        when (reader.nextName()) {
            "relation" -> {
                relationMatches =
                    readStringArrayContains(
                        reader,
                        "delegate_permission/common.handle_all_urls",
                    )
            }

            "target" -> {
                targetMatches = readTarget(reader, packageName, signingFingerprint)
            }

            else -> {
                reader.skipValue()
            }
        }
    }
    reader.endObject()
    return relationMatches && targetMatches
}

private fun readTarget(
    reader: JsonReader,
    packageName: String,
    signingFingerprint: String,
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_OBJECT) {
        reader.skipValue()
        return false
    }
    reader.beginObject()
    var namespace: String? = null
    var targetPackage: String? = null
    var fingerprintMatches = false
    while (reader.hasNext()) {
        when (reader.nextName()) {
            "namespace" -> {
                namespace = readString(reader)
            }

            "package_name" -> {
                targetPackage = readString(reader)
            }

            "sha256_cert_fingerprints" -> {
                fingerprintMatches = readStringArrayContains(reader, signingFingerprint)
            }

            else -> {
                reader.skipValue()
            }
        }
    }
    reader.endObject()
    return namespace == "android_app" &&
        targetPackage == packageName &&
        fingerprintMatches
}

private fun readString(reader: JsonReader): String? {
    if (reader.peek() != JsonToken.STRING) {
        reader.skipValue()
        return null
    }
    return reader.nextString()
}

private fun readStringArrayContains(
    reader: JsonReader,
    expected: String,
): Boolean {
    if (reader.peek() != JsonToken.BEGIN_ARRAY) {
        reader.skipValue()
        return false
    }
    reader.beginArray()
    var matches = false
    while (reader.hasNext()) {
        if (reader.peek() == JsonToken.STRING) {
            matches = reader.nextString() == expected || matches
        } else {
            reader.skipValue()
        }
    }
    reader.endArray()
    return matches
}
