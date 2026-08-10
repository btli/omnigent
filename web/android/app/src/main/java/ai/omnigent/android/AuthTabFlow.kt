package ai.omnigent.android

import android.net.Uri
import java.security.SecureRandom

/**
 * Tracks the single in-flight Auth Tab login and binds its completion to
 * the flow that started it: a callback is accepted only when its `state`
 * nonce matches the pending flow AND the pinned origin is still the one
 * the flow was launched for. A result from a previous server (switched
 * away mid-login) or an unsolicited `omnigent://auth-callback` intent
 * from another app can therefore never install a credential.
 *
 * Main-thread confined, like the rest of the login state in
 * [MainActivity].
 */
class AuthTabFlow {
    private data class Pending(
        val state: String,
        val origin: String,
    )

    private val random = SecureRandom()
    private var pending: Pending? = null

    /** True while a flow is awaiting its Auth Tab result. */
    val inFlight: Boolean get() = pending != null

    /**
     * Start a flow against [origin] (the pinned server). Returns the URL
     * to open in the Auth Tab, or null when a flow is already in flight
     * (a redirect storm must not stack tabs).
     */
    fun begin(origin: String): Uri? {
        if (pending != null) return null
        val state = newState()
        pending = Pending(state, origin)
        return NativeAuth.completionUrl(origin, state)
    }

    /**
     * Try to complete the pending flow with a callback [uri]. Returns the
     * validated result and clears the flow when everything binds; returns
     * null — leaving any pending flow armed — otherwise. [currentOrigin]
     * is the origin pinned *now*; it must equal the flow's launch origin.
     */
    fun complete(
        uri: Uri?,
        currentOrigin: String?,
    ): NativeAuth.Result? {
        val flow = pending ?: return null
        val result = NativeAuth.parseCallback(uri) ?: return null
        if (result.state != flow.state) return null
        if (currentOrigin == null || currentOrigin != flow.origin) return null
        pending = null
        return result
    }

    /** Abandon the pending flow (tab dismissed, launch failed, server switch). */
    fun cancel() {
        pending = null
    }

    /** A fresh 128-bit URL-safe nonce (base64url alphabet, no padding). */
    private fun newState(): String {
        val bytes = ByteArray(16)
        random.nextBytes(bytes)
        return android.util.Base64.encodeToString(
            bytes,
            android.util.Base64.URL_SAFE or android.util.Base64.NO_WRAP or android.util.Base64.NO_PADDING,
        )
    }
}
