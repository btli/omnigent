package ai.omnigent.android

// Pure snapshot-diff logic for the background session poller, mirroring the web
// SPA's `idleTransitions.ts`. Kept free of Android imports so the "which
// sessions newly need attention" decision is unit-testable without a device,
// exactly as the web module is testable without React.
//
// SessionPollWorker owns the I/O (HTTP fetch, snapshot persistence); this
// module only diffs two snapshots.

// Statuses that mean "the agent stopped and is waiting on the user".
private val TERMINAL_STATUSES = setOf("idle", "failed")

/** One session as seen in a `GET /v1/sessions` list item — the fields we diff. */
data class SessionState(
    val id: String,
    val status: String,
    val pendingElicitations: Int,
    // Best-effort display name; the list carries `title` and a fallback name.
    val title: String?,
    // The HTTP list omits `runner_online`, so this is usually null. Null (or
    // true) is treated as online — we never over-suppress a genuine finish.
    val runnerOnline: Boolean?,
)

/** Prior-run snapshot of a single session, keyed by id in the persisted map. */
data class SessionSnapshot(
    val status: String,
    val pendingElicitations: Int,
)

/** Snapshot the current list into the map persisted between poll runs. */
fun buildSnapshot(sessions: List<SessionState>): Map<String, SessionSnapshot> =
    sessions.associate { it.id to SessionSnapshot(it.status, it.pendingElicitations) }

/**
 * Sessions whose status went `running` → `idle`/`failed` between the previous
 * snapshot and the current list.
 *
 * Requiring the *previous* status to be exactly `running` means a first run
 * (empty [previous]) fires nothing, and steady-state idle rows never re-notify
 * on a later poll — only a genuine finish does. This is what dedups a
 * transition across runs: once notified, the persisted status is the terminal
 * one, so the next poll's `previous` is no longer `running`.
 */
fun detectIdleTransitions(
    previous: Map<String, SessionSnapshot>,
    sessions: List<SessionState>,
): List<SessionState> =
    sessions.filter { session ->
        session.status in TERMINAL_STATUSES && previous[session.id]?.status == "running"
    }

/**
 * Sessions whose pending-elicitation count *increased* between the previous
 * snapshot and the current list — the agent just raised a new prompt.
 *
 * Requiring a previous entry means a first run with already-pending
 * elicitations fires nothing; only an increase this client observed does. A
 * 0 → 1 change fires; a steady count or a decrease (the user answered) does
 * not. Persisting the new count dedups the increase across runs.
 */
fun detectNewElicitations(
    previous: Map<String, SessionSnapshot>,
    sessions: List<SessionState>,
): List<SessionState> =
    sessions.filter { session ->
        val prior = previous[session.id] ?: return@filter false
        session.pendingElicitations > prior.pendingElicitations
    }

/**
 * The session-cookie name the server issues, matching MainActivity's injection
 * and the server's `session_cookie_name`: the `__Host-` prefix on HTTPS.
 */
fun sessionCookieName(secure: Boolean): String = if (secure) "__Host-ap_session" else "ap_session"

/**
 * Pull one cookie's value out of a `CookieManager.getCookie` string
 * ("a=1; b=2; …"), or null if [name] isn't present. Returned trimmed; a
 * present-but-empty value is treated as absent (null).
 */
fun extractCookieValue(
    cookieHeader: String?,
    name: String,
): String? {
    if (cookieHeader.isNullOrBlank()) return null
    for (part in cookieHeader.split(';')) {
        val eq = part.indexOf('=')
        if (eq <= 0) continue
        if (part.substring(0, eq).trim() != name) continue
        return part.substring(eq + 1).trim().ifEmpty { null }
    }
    return null
}

/**
 * Display label for a session notification, mirroring the web
 * `conversationDisplayLabel` fallback chain (title, then a generic label). The
 * background poll has no access to the wrapper-label lookup the SPA uses for
 * native coding-agent names, so it falls back straight to the generic label.
 */
fun sessionDisplayLabel(title: String?): String = title?.takeIf { it.isNotBlank() } ?: "New session"

/** The list query the poller hits — the existing endpoint, no new params. */
const val SESSIONS_LIST_PATH =
    "/v1/sessions?order=desc&sort_by=updated_at&limit=20&include_archived=true"
