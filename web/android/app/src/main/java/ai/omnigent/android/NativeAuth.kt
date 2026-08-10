package ai.omnigent.android

import android.net.Uri

/**
 * The native-login completion contract shared with the server's
 * `GET /auth/native-complete`: the shell opens that endpoint in an Auth Tab,
 * the server (once the request arrives authenticated — after any front-door
 * proxy and IdP hops ran in the real browser) 302s to
 * `omnigent://auth-callback` carrying the app's `state` nonce plus a
 * credential, and the Auth Tab returns that URI to the app.
 */
object NativeAuth {
    /** Redirect scheme the Auth Tab intercepts. */
    const val SCHEME = "omnigent"

    /** Redirect host — narrower than the scheme so the manifest filter for
     * the Custom-Tab fallback claims only auth callbacks, not every
     * `omnigent://` link. */
    const val HOST = "auth-callback"

    private const val COMPLETE_PATH = "/auth/native-complete"

    /** Session JWT to install as the WebView session cookie (oidc/accounts). */
    const val TOKEN_TYPE_SESSION = "session"

    /** Proxy-forwarded access token to present as a Bearer (header mode). */
    const val TOKEN_TYPE_BEARER = "bearer"

    /** A parsed, validated completion callback. */
    data class Result(
        val state: String,
        val tokenType: String,
        val token: String,
    )

    /**
     * The URL to open in the Auth Tab for [origin]. [state] is the flow
     * nonce — generated from the base64url alphabet, so it needs no
     * encoding.
     */
    fun completionUrl(
        origin: String,
        state: String,
    ): Uri = Uri.parse("$origin$COMPLETE_PATH?state=$state")

    /**
     * Parse a completion callback URI, or null when it isn't one: wrong
     * scheme/host, missing state/token, an unknown token type, an error
     * report (`error=no_token`), or a token carrying characters that could
     * break out of a header or cookie value. The caller still has to match
     * [Result.state] against its in-flight flow.
     */
    fun parseCallback(uri: Uri?): Result? {
        if (uri == null) return null
        if (uri.scheme?.lowercase() != SCHEME) return null
        if (uri.host?.lowercase() != HOST) return null
        val state = uri.getQueryParameter("state") ?: return null
        val tokenType = uri.getQueryParameter("token_type") ?: return null
        val token = uri.getQueryParameter("token") ?: return null
        if (state.isEmpty()) return null
        if (tokenType != TOKEN_TYPE_SESSION && tokenType != TOKEN_TYPE_BEARER) return null
        if (!isTokenSafe(token)) return null
        return Result(state, tokenType, token)
    }

    /**
     * True when [token] is non-empty and uses only characters that are safe
     * to interpolate into an `Authorization` header or a cookie value —
     * the token68 alphabet (RFC 7235), which covers JWTs and OAuth access
     * tokens. Rejects `;`, whitespace, and control chars outright.
     */
    fun isTokenSafe(token: String): Boolean =
        token.isNotEmpty() &&
            token.all { c ->
                c in 'A'..'Z' || c in 'a'..'z' || c in '0'..'9' || c in "-._~+/="
            }
}
