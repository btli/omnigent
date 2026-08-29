package ai.omnigent.android

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import androidx.annotation.VisibleForTesting
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors
import java.util.concurrent.atomic.AtomicBoolean

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
class OidcLoginManager {
    private val main = Handler(Looper.getMainLooper())

    // One executor per login flow, so cancel() can shut a flow down (interrupting
    // its polling sleep) without a stale poll blocking or outliving the next one.
    // Only touched on the main thread.
    private var flow: Flow? = null

    // A cancelled flow never delivers, so a poll that finishes after the host is
    // destroyed — or after a switch to another server — can't invoke into it.
    private class Flow(
        val executor: ExecutorService,
    ) {
        val cancelled = AtomicBoolean(false)
    }

    // The in-flight HTTP request, so cancel() can force blocking I/O to abort:
    // shutdownNow() only interrupts the polling sleeps, not a thread blocked
    // in HttpURLConnection connect/read (or a slow-trickle response body).
    @Volatile private var activeConnection: HttpURLConnection? = null

    // Monotonic clock for login deadlines — unlike wall-clock
    // System.currentTimeMillis(), it can't jump with NTP/user adjustments and
    // silently expire (or extend) a login window. Substitutable because
    // Robolectric's SystemClock is simulated and does not advance with real
    // background-thread sleeps.
    @VisibleForTesting
    internal var monotonicNowMs: () -> Long = { SystemClock.elapsedRealtime() }

    // The flow's two network steps, substitutable so tests can drive a token
    // through the completion path without a server. Deadlines are
    // [monotonicNowMs] values.
    @VisibleForTesting
    internal var requestTicket: (origin: String, deadlineMs: Long) -> Ticket? =
        { origin, deadlineMs -> httpRequestTicket(origin, deadlineMs) }

    @VisibleForTesting
    internal var pollForToken: (origin: String, ticket: String, deadlineMs: Long) -> String? =
        { origin, ticket, deadlineMs -> httpPollForToken(origin, ticket, deadlineMs) }

    /**
     * Begin a login against [origin] (the pinned server). Opens the browser and
     * polls in the background; [onSession] is invoked on the main thread with the
     * origin the flow was started for and the session JWT once the browser flow
     * completes.
     *
     * Returns true if this call started a flow, or false if one was already in
     * flight (a second concurrent call is ignored). The caller uses the result so
     * a no-op call isn't counted against a retry budget.
     */
    fun start(
        activity: Activity,
        origin: String,
        onSession: (origin: String, token: String) -> Unit,
    ): Boolean {
        if (flow != null) return false
        val current = Flow(Executors.newSingleThreadExecutor())
        flow = current
        // One monotonic deadline bounds ticket creation AND polling, mirroring
        // the desktop shell's single 5-minute login window.
        val deadlineMs = monotonicNowMs() + LOGIN_TIMEOUT_MS
        current.executor.execute {
            var token: String? = null
            try {
                val ticket = requestTicket(origin, deadlineMs)
                authLog("cli-login -> ${if (ticket != null) "ticket ok" else "FAILED"}")
                if (ticket != null) {
                    main.post {
                        // cancel() may have run after this launch was queued —
                        // never open the browser for an abandoned flow's origin.
                        if (!current.cancelled.get()) launchTab(activity, origin + ticket.loginUrl)
                    }
                    token = pollForToken(origin, ticket.id, deadlineMs)
                    authLog(
                        "poll -> ${if (token != null) "token (len=${token.length})" else "no token"}",
                    )
                }
            } catch (_: InterruptedException) {
                // cancel() interrupted the poll — this flow is abandoned; drop.
            } catch (t: Throwable) {
                authLog("login flow error: ${t.javaClass.simpleName}")
            } finally {
                current.executor.shutdown()
            }
            val result = token
            main.post {
                // Deliver only for a flow that wasn't cancelled — a cancelled flow's
                // token belongs to a server the host has switched away from.
                if (result != null && !current.cancelled.get()) {
                    onSession(origin, result)
                }
                // Free the slot for the next login — unless onSession already
                // started one (a re-login) or cancel() moved on to another flow.
                if (flow === current) flow = null
            }
        }
        return true
    }

    /**
     * Abandon any in-flight login: no callback will fire, and a new [start] is
     * immediately possible. Safe to call with no flow in flight.
     */
    fun cancel() {
        flow?.let {
            it.cancelled.set(true)
            it.executor.shutdownNow() // interrupts the polling sleep so the task exits promptly
        }
        // shutdownNow() cannot interrupt blocking HttpURLConnection I/O —
        // tear the in-flight connection down so the flow thread exits promptly
        // instead of waiting out a read timeout (or a slow-trickle body).
        runCatching { activeConnection?.disconnect() }
        flow = null
    }

    /** Release the host entirely. Call from onDestroy. */
    fun shutdown() = cancel()

    internal data class Ticket(
        val id: String,
        val loginUrl: String,
    )

    private fun httpRequestTicket(
        origin: String,
        deadlineMs: Long,
    ): Ticket? {
        while (true) {
            val conn = (URL("$origin/auth/cli-login").openConnection() as HttpURLConnection)
            conn.requestMethod = "POST"
            // Bodyless POST — set Content-Length explicitly; some servers/WAFs reject
            // a POST without it (411 Length Required).
            conn.setRequestProperty("Content-Length", "0")
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            activeConnection = conn
            try {
                val status = conn.responseCode
                if (status !in TRANSIENT_STATUSES) {
                    if (status != 200) return null
                    val json = JSONObject(conn.inputStream.bufferedReader().use { it.readText() })
                    val id = json.optString("ticket").ifEmpty { return null }
                    val loginUrl = json.optString("login_url").ifEmpty { return null }
                    // The browser hand-off must stay on the pinned origin: [start]
                    // concatenates this onto it, so only a relative path may pass — an
                    // absolute URL or a scheme-relative `//host` would send the one-time
                    // ticket flow to a server-chosen destination instead.
                    if (!loginUrl.startsWith("/") || loginUrl.startsWith("//")) return null
                    return Ticket(id, loginUrl)
                }
            } finally {
                activeConnection = null
                conn.disconnect()
            }
            // A transient gate/proxy hiccup (same status set as the desktop
            // shell) — wait out one interval and retry until the login deadline.
            if (monotonicNowMs() >= deadlineMs) return null
            Thread.sleep(POLL_INTERVAL_MS) // throws InterruptedException on shutdownNow()
            if (monotonicNowMs() >= deadlineMs) return null
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

    private fun httpPollForToken(
        origin: String,
        ticket: String,
        deadlineMs: Long,
    ): String? {
        val encoded = Uri.encode(ticket)
        while (monotonicNowMs() < deadlineMs) {
            Thread.sleep(POLL_INTERVAL_MS) // throws InterruptedException on shutdownNow()
            // The sleep itself can cross the deadline — never issue a request
            // past it.
            if (monotonicNowMs() >= deadlineMs) return null
            val conn = (
                URL(
                    "$origin/auth/cli-poll?ticket=$encoded",
                ).openConnection() as HttpURLConnection
            )
            conn.requestMethod = "GET"
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            activeConnection = conn
            try {
                when (conn.responseCode) {
                    202 -> {
                        continue
                    }

                    // still pending
                    200 -> {
                        val body = conn.inputStream.bufferedReader().use { it.readText() }
                        // A token trickling in past the deadline belongs to an
                        // expired login window — reject it.
                        if (monotonicNowMs() >= deadlineMs) return null
                        return JSONObject(body).optString("token").ifEmpty { null }
                    }

                    // Transient gate/proxy hiccup (same status set as the
                    // desktop shell) — keep polling until the deadline.
                    in TRANSIENT_STATUSES -> {
                        continue
                    }

                    else -> {
                        return null
                    } // 410 expired/rejected, or other
                }
            } catch (_: Throwable) {
                if (Thread.currentThread().isInterrupted) return null // shutdown mid-request
                continue // transient network error — keep polling until the deadline
            } finally {
                activeConnection = null
                conn.disconnect()
            }
        }
        return null
    }

    private companion object {
        const val POLL_INTERVAL_MS = 2_000L
        const val LOGIN_TIMEOUT_MS = 5 * 60 * 1_000L // mirrors the CLI's 5-minute window
        const val HTTP_TIMEOUT_MS = 10_000 // connect + read timeout for the login endpoints

        // Retryable statuses — must match the desktop shell's
        // TRANSIENT_AUTH_STATUSES (oidc_auth.js) so a single 503 from a gate
        // or proxy doesn't abort the whole login. 410 (expired) and auth
        // failures stay fatal per the shared contract.
        val TRANSIENT_STATUSES = setOf(429, 502, 503, 504)
    }
}
