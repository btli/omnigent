package ai.omnigent.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.webkit.CookieManager
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.Executors
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.atomic.AtomicBoolean

sealed interface LoginResult {
    data class Success(val token: String) : LoginResult

    data object Rejected : LoginResult

    data object TimedOut : LoginResult
}

/**
 * Drives the RFC 8252 login flow for the shell: authenticate in the system
 * browser — a real browser, so Google sign-in (which blocks embedded WebViews
 * with `disallowed_useragent`) and passkeys (which need the browser / a password
 * manager) both work — then bridge the resulting session into the WebView, whose
 * cookie store is isolated from the browser's.
 *
 * Reuses the server's existing browser-login endpoints (the same ones the
 * `omnigent login` CLI uses, no server change):
 *   1. `POST /auth/cli-login` -> `{ticket, login_url}`
 *   2. open `login_url` in the browser; the user authenticates; the OIDC
 *      callback fulfills the ticket server-side
 *   3. `GET /auth/cli-poll?ticket=...` -> `{token}` once fulfilled
 *
 * That `token` is exactly the session-cookie JWT (the server validates the same
 * HS256 JWT as either the session cookie or a `Bearer`), so [MainActivity]
 * injects it into the WebView's CookieManager and reloads — authenticated.
 */
class OidcLoginManager(
    private val pollIntervalMs: Long = DEFAULT_POLL_INTERVAL_MS,
    private val pollTimeoutMs: Long = DEFAULT_POLL_TIMEOUT_MS,
) {
    private val io = Executors.newSingleThreadExecutor()
    private val main = Handler(Looper.getMainLooper())
    private val inFlight = AtomicBoolean(false)

    // Held only for the duration of a login; nulled by [shutdown] so a poll that
    // finishes after the host is destroyed can neither invoke into a dead
    // Activity nor pin it (and its View tree) for the poll's lifetime.
    @Volatile private var resultCallback: ((LoginResult) -> Unit)? = null

    /**
     * Begin a login against [origin] (the pinned server). Opens the browser and
     * polls in the background; [onResult] is invoked on the main thread when the
     * flow succeeds, is rejected, or reaches its deadline.
     *
     * Returns true if this call started a flow, or false if one was already in
     * flight (a second concurrent call is ignored). The caller uses the result so
     * a no-op call isn't counted against a retry budget.
     */
    fun start(
        activity: Activity,
        origin: String,
        onResult: (LoginResult) -> Unit,
    ): Boolean {
        if (!inFlight.compareAndSet(false, true)) return false
        resultCallback = onResult
        try {
            io.execute {
                val result =
                    try {
                        val ticket = requestTicket(origin)
                        authLog("cli-login -> ${if (ticket != null) "ticket ok" else "FAILED"}")
                        if (ticket == null) {
                            LoginResult.Rejected
                        } else {
                            main.post { launchTab(activity, origin + ticket.loginUrl) }
                            pollForToken(origin, ticket.id).also { pollResult ->
                                authLog(
                                    "poll -> " +
                                        when (pollResult) {
                                            is LoginResult.Success ->
                                                "token (len=${pollResult.token.length})"

                                            LoginResult.Rejected -> "rejected"
                                            LoginResult.TimedOut -> "timed out"
                                        },
                                )
                            }
                        }
                    } catch (_: InterruptedException) {
                        // shutdown() interrupted the poll — the host is going away; drop.
                        null
                    } catch (t: Throwable) {
                        authLog("login flow error: ${t.javaClass.simpleName}")
                        LoginResult.Rejected
                    } finally {
                        inFlight.set(false)
                    }
                // resultCallback is null once shutdown() ran — never invoke into a
                // destroyed host.
                if (result != null) main.post { resultCallback?.invoke(result) }
            }
        } catch (_: RejectedExecutionException) {
            resultCallback = null
            inFlight.set(false)
            return false
        }
        return true
    }

    internal fun isInFlightForTest(): Boolean = inFlight.get()

    /** Cancel an in-flight login and release the host. Call from onDestroy. */
    fun shutdown() {
        resultCallback = null
        io.shutdownNow() // interrupts the polling sleep so the task exits promptly
    }

    private data class Ticket(
        val id: String,
        val loginUrl: String,
    )

    /**
     * Carry the WebView's cookies for [origin] on a login-endpoint request.
     * HttpURLConnection has its own (empty) cookie store, so without this a
     * server behind a front-door auth proxy would 302 these endpoints to its
     * IdP even after the WebView already holds the proxy's session.
     */
    private fun attachWebViewCookies(
        conn: HttpURLConnection,
        origin: String,
    ) {
        val cookies = runCatching { CookieManager.getInstance().getCookie(origin) }.getOrNull()
        if (!cookies.isNullOrBlank()) conn.setRequestProperty("Cookie", cookies)
    }

    private fun requestTicket(origin: String): Ticket? {
        val conn = (URL("$origin/auth/cli-login").openConnection() as HttpURLConnection)
        conn.requestMethod = "POST"
        // Bodyless POST — set Content-Length explicitly; some servers/WAFs reject
        // a POST without it (411 Length Required).
        conn.setRequestProperty("Content-Length", "0")
        // A front-door auth proxy 302s this endpoint to its IdP's HTML login
        // page; following it would "succeed" with unparseable HTML. Fail fast.
        conn.instanceFollowRedirects = false
        attachWebViewCookies(conn, origin)
        conn.connectTimeout = HTTP_TIMEOUT_MS
        conn.readTimeout = HTTP_TIMEOUT_MS
        return try {
            if (conn.responseCode != 200) return null
            val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
            val id = json.optString("ticket").ifEmpty { return null }
            val loginUrl = json.optString("login_url").ifEmpty { return null }
            // The browser hand-off must stay on the pinned origin: [start]
            // concatenates this onto it, so only a relative path may pass — an
            // absolute URL or a scheme-relative `//host` would send the one-time
            // ticket flow to a server-chosen destination instead.
            if (!loginUrl.startsWith("/") || loginUrl.startsWith("//")) return null
            Ticket(id, loginUrl)
        } finally {
            conn.disconnect()
        }
    }

    private fun launchTab(
        activity: Activity,
        url: String,
    ) {
        // Full system browser (not a Custom Tab): the IdP flow page renders blank
        // in an in-app Custom Tab on some setups but works in the browser. Still
        // RFC 8252 — the system browser is the canonical external user-agent.
        authLog("opening login in browser") // URL carries the one-time ticket — not logged
        val intent =
            Intent(
                Intent.ACTION_VIEW,
                Uri.parse(url),
            ).addCategory(Intent.CATEGORY_BROWSABLE)
        runCatching { activity.startActivity(intent) }
    }

    private fun pollForToken(
        origin: String,
        ticket: String,
    ): LoginResult {
        val deadline = System.currentTimeMillis() + pollTimeoutMs
        val encoded = Uri.encode(ticket)
        while (System.currentTimeMillis() < deadline) {
            Thread.sleep(pollIntervalMs) // throws InterruptedException on shutdownNow()
            if (System.currentTimeMillis() >= deadline) break

            var conn: HttpURLConnection? = null
            val body =
                try {
                    conn = (
                        URL(
                            "$origin/auth/cli-poll?ticket=$encoded",
                        ).openConnection() as HttpURLConnection
                    )
                    conn.requestMethod = "GET"
                    // A proxied 302 is a terminal rejection, never redirect HTML.
                    conn.instanceFollowRedirects = false
                    attachWebViewCookies(conn, origin)
                    conn.connectTimeout = HTTP_TIMEOUT_MS
                    conn.readTimeout = HTTP_TIMEOUT_MS
                    when (conn.responseCode) {
                        202 -> {
                            null
                        }

                        200 -> {
                            conn.inputStream.bufferedReader().use { it.readText() }
                        }

                        else -> {
                            return LoginResult.Rejected
                        }
                    }
                } catch (e: InterruptedException) {
                    throw e
                } catch (_: Throwable) {
                    if (Thread.currentThread().isInterrupted) throw InterruptedException()
                    continue // transient network error — keep polling until the deadline
                } finally {
                    conn?.disconnect()
                }
            if (body == null) continue

            // Parsing is outside the transient-error catch: a 200 with a bad
            // payload is terminal and must not be retried to the deadline.
            return try {
                JSONObject(body)
                    .optString("token")
                    .takeIf { it.isNotEmpty() }
                    ?.let { LoginResult.Success(it) }
                    ?: LoginResult.Rejected
            } catch (_: Throwable) {
                LoginResult.Rejected
            }
        }
        return LoginResult.TimedOut
    }

    private companion object {
        const val DEFAULT_POLL_INTERVAL_MS = 2_000L
        const val DEFAULT_POLL_TIMEOUT_MS = 5 * 60 * 1_000L // mirrors the CLI's window
        const val HTTP_TIMEOUT_MS = 10_000 // connect + read timeout for the login endpoints
    }
}
