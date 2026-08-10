package ai.omnigent.android

import android.net.Uri
import android.util.Base64
import java.security.MessageDigest

/**
 * The native-login contract shared with the server's
 * `/auth/native-complete` + `/auth/native-exchange` pair — an OAuth-style
 * code flow (PKCE, RFC 7636) so no credential ever rides the completion
 * redirect:
 *
 * 1. The shell opens `native-complete` (with a state nonce and an S256
 *    code challenge) in an Auth Tab; the server's front door / IdP chain
 *    runs in the real browser.
 * 2. The server 302s `omnigent://auth-callback` with the state and an
 *    opaque one-time **code** (never a token), plus which exchange
 *    transport to use.
 * 3. The shell exchanges code + state + verifier for the credential:
 *    a native POST where reachable, or a second silent Auth Tab hop for
 *    servers behind a front-door proxy that 302s native requests.
 *
 * There is deliberately NO manifest handler for `omnigent://auth-callback`:
 * callbacks are only accepted through the Auth Tab's Activity-result
 * channel, which the OS delivers exclusively to this app. A browser
 * without Auth Tab support never gets this flow launched at it, so a
 * VIEW-intent delivery can only be unsolicited — and even a stolen code
 * is useless without the in-memory verifier — but not claiming the
 * scheme at all is simpler and leaves nothing to squat against us.
 */
object NativeAuth {
    /** Redirect scheme the Auth Tab intercepts. */
    const val SCHEME = "omnigent"

    /** Redirect host — narrower than the scheme so callbacks can never be
     * confused with `omnigent://` deep links. */
    const val HOST = "auth-callback"

    private const val COMPLETE_PATH = "/auth/native-complete"
    private const val EXCHANGE_PATH = "/auth/native-exchange"

    /** Session JWT to install as the WebView session cookie (oidc/accounts). */
    const val TOKEN_TYPE_SESSION = "session"

    /** Proxy-forwarded access token to present as a Bearer (header mode). */
    const val TOKEN_TYPE_BEARER = "bearer"

    /** Exchange transports the completion redirect can name. */
    const val EXCHANGE_TAB = "tab"
    const val EXCHANGE_POST = "post"

    /** A parsed leg-1 callback: the one-time code awaiting exchange. */
    data class CodeGrant(
        val state: String,
        val code: String,
        val exchange: String,
    )

    /** A parsed, validated credential (POST body or leg-2 callback). */
    data class Result(
        val state: String,
        val tokenType: String,
        val token: String,
    )

    /**
     * The URL to open in the Auth Tab for [origin]. [state] and
     * [codeChallenge] come from the base64url alphabet, so they need no
     * encoding.
     */
    fun completionUrl(
        origin: String,
        state: String,
        codeChallenge: String,
    ): Uri = Uri.parse("$origin$COMPLETE_PATH?state=$state&code_challenge=$codeChallenge")

    /**
     * The exchange URL for the second Auth Tab hop (front-door servers,
     * where a native POST can't cross the proxy). The code and verifier
     * are single-use — both are dead the moment this request lands.
     */
    fun exchangeUrl(
        origin: String,
        grant: CodeGrant,
        codeVerifier: String,
    ): Uri =
        Uri.parse(
            "$origin$EXCHANGE_PATH?code=${grant.code}&state=${grant.state}" +
                "&code_verifier=$codeVerifier",
        )

    /**
     * Parse a leg-1 completion callback, or null when it isn't one:
     * wrong scheme/host, missing/malformed fields, a server error report
     * (`error=...`), or an unknown exchange transport. The caller still
     * has to match [CodeGrant.state] against its in-flight flow.
     */
    fun parseCodeCallback(uri: Uri?): CodeGrant? {
        if (!isCallback(uri)) return null
        val state = uri!!.getQueryParameter("state") ?: return null
        val code = uri.getQueryParameter("code") ?: return null
        val exchange = uri.getQueryParameter("exchange") ?: return null
        if (state.isEmpty() || !isUrlSafe(code)) return null
        if (exchange != EXCHANGE_TAB && exchange != EXCHANGE_POST) return null
        return CodeGrant(state, code, exchange)
    }

    /**
     * Parse a leg-2 (token) callback, or null when it isn't one: wrong
     * scheme/host, missing state/token, an unknown token type, an error
     * report, or a token carrying characters that could break out of a
     * header or cookie value.
     */
    fun parseTokenCallback(uri: Uri?): Result? {
        if (!isCallback(uri)) return null
        val state = uri!!.getQueryParameter("state") ?: return null
        val tokenType = uri.getQueryParameter("token_type") ?: return null
        val token = uri.getQueryParameter("token") ?: return null
        if (state.isEmpty()) return null
        if (tokenType != TOKEN_TYPE_SESSION && tokenType != TOKEN_TYPE_BEARER) return null
        if (!isTokenSafe(token)) return null
        return Result(state, tokenType, token)
    }

    /** True when [uri] targets the fixed callback scheme + host. */
    fun isCallback(uri: Uri?): Boolean =
        uri != null &&
            uri.scheme?.lowercase() == SCHEME &&
            uri.host?.lowercase() == HOST

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

    /** PKCE S256: base64url (unpadded) SHA-256 of the ASCII verifier. */
    fun deriveCodeChallenge(codeVerifier: String): String {
        val digest =
            MessageDigest
                .getInstance("SHA-256")
                .digest(codeVerifier.toByteArray(Charsets.US_ASCII))
        return Base64.encodeToString(
            digest,
            Base64.URL_SAFE or Base64.NO_WRAP or Base64.NO_PADDING,
        )
    }

    private fun isUrlSafe(value: String): Boolean =
        value.isNotEmpty() &&
            value.all { c ->
                c in 'A'..'Z' || c in 'a'..'z' || c in '0'..'9' || c == '-' || c == '_'
            }
}
