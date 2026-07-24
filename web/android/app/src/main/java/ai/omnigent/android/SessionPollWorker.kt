package ai.omnigent.android

import android.content.Context
import android.webkit.CookieManager
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.net.HttpURLConnection
import java.net.URL

/**
 * Background session poller. Fires local notifications when a session finishes
 * (`running` → `idle`/`failed`) or gains a new pending elicitation, without the
 * app being foregrounded — an interim, OS-scheduled mirror of the web SPA's
 * `useIdleNotifications` hook, driven by WorkManager instead of a live poll.
 *
 * Client-only: it reuses the JWT the shell already injected into the WebView's
 * [CookieManager] (see MainActivity.onSessionToken) rather than duplicating
 * credential storage, and hits the existing `GET /v1/sessions` endpoint. No
 * server change.
 *
 * The 15-minute WorkManager floor doubles as the SPA's 10s idle-settle window:
 * a session seen `running` a poll ago and terminal now is genuinely finished,
 * so unlike the live hook this worker needs no in-worker deferral.
 *
 * Graceful no-ops (always [Result.success], never a crash or retry storm):
 *   * not logged in / cookie expired → no session cookie → nothing to poll
 *   * no pinned server → nothing to poll
 *   * network/HTTP error → skip this run, persist nothing, try again next tick
 */
class SessionPollWorker(
    context: Context,
    params: WorkerParameters,
) : CoroutineWorker(context, params) {
    override suspend fun doWork(): Result =
        withContext(Dispatchers.IO) {
            val serverUrl =
                ServerStore(applicationContext).let {
                    if (it.hasServer()) it.currentServerUrl() else null
                }
            val origin = originOf(serverUrl) ?: return@withContext Result.success()

            val secure = origin.startsWith("https://")
            val jwt =
                extractCookieValue(
                    CookieManager.getInstance().getCookie(origin),
                    sessionCookieName(secure),
                )
                    ?: return@withContext Result.success() // not logged in / expired — no-op

            val body = fetchSessions(origin, jwt, secure) ?: return@withContext Result.success()
            val sessions = parseSessionList(body)
            // An empty parse means either no sessions or a transient bad body —
            // either way there's nothing to diff. Don't overwrite the prior
            // snapshot with empty: a `running` session that briefly drops off a
            // poll would otherwise lose its state and never notify on finish.
            // Mirrors the SPA hook's empty-list early return.
            if (sessions.isEmpty()) return@withContext Result.success()

            val store = SessionSnapshotStore(applicationContext)
            val previous = store.load()

            // Persist the new snapshot BEFORE posting: the snapshot is the dedup
            // key, so even if a notify throws, the next run won't re-fire the
            // same edge. A first run (empty `previous`) yields no transitions.
            store.save(buildSnapshot(sessions))
            if (previous.isEmpty()) return@withContext Result.success()

            val notifications = NativeNotificationManager(applicationContext)
            for (session in detectIdleTransitions(previous, sessions)) {
                if (session.runnerOnline == false) continue // stale dead-runner reconciliation
                notifications.notify(
                    title = sessionDisplayLabel(session.title),
                    body = IDLE_BODY,
                    navigatePath = "/c/${session.id}",
                )
            }
            for (session in detectNewElicitations(previous, sessions)) {
                if (session.runnerOnline == false) continue
                notifications.notify(
                    title = sessionDisplayLabel(session.title),
                    body = ELICITATION_BODY,
                    navigatePath = "/c/${session.id}",
                )
            }
            Result.success()
        }

    /**
     * GET the sessions list, authenticating with the reused JWT as both a
     * `Cookie` (matching the WebView) and `Authorization: Bearer` (the server
     * accepts either). Returns the body on 200, or null on any non-200 /
     * network error so the run no-ops and retries next tick.
     */
    private fun fetchSessions(
        origin: String,
        jwt: String,
        secure: Boolean,
    ): String? {
        val conn = (URL(origin + SESSIONS_LIST_PATH).openConnection() as HttpURLConnection)
        return try {
            conn.requestMethod = "GET"
            conn.setRequestProperty("Cookie", "${sessionCookieName(secure)}=$jwt")
            conn.setRequestProperty("Authorization", "Bearer $jwt")
            conn.setRequestProperty("Accept", "application/json")
            conn.connectTimeout = HTTP_TIMEOUT_MS
            conn.readTimeout = HTTP_TIMEOUT_MS
            if (conn.responseCode != 200) return null
            conn.inputStream.bufferedReader().use { it.readText() }
        } catch (_: Throwable) {
            null
        } finally {
            conn.disconnect()
        }
    }

    companion object {
        // Mirrors useIdleNotifications' IDLE_BODY / ELICITATION_BODY.
        const val IDLE_BODY = "Agent finished and is ready for your input."
        const val ELICITATION_BODY = "Agent is asking for your input."
        private const val HTTP_TIMEOUT_MS = 15_000
    }
}
