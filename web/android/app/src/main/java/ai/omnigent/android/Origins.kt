package ai.omnigent.android

import android.net.Uri

/**
 * Normalizes a URL to its origin (`scheme://host[:port]`), the unit of trust
 * the bridge and navigation gating compare on. Returns null for anything
 * without both a scheme and host.
 */
fun originOf(url: String?): String? {
    val uri = url?.let(Uri::parse) ?: return null
    val scheme = uri.scheme?.lowercase() ?: return null
    val host = uri.host?.lowercase() ?: return null
    // Canonicalize like a browser origin (WHATWG): lowercase scheme + host and
    // omit the default port — so an explicit `https://host:443` (or odd casing)
    // the user typed compares equal to the WebView's normalized `https://host`.
    // The pinned origin and every page URL both flow through here, so they
    // canonicalize identically.
    val port = uri.port
    val hasExplicitPort =
        port != -1 &&
            !(scheme == "https" && port == 443) &&
            !(scheme == "http" && port == 80)
    return if (hasExplicitPort) "$scheme://$host:$port" else "$scheme://$host"
}

/**
 * True for the only two schemes the WebView loads inline (http/https). This
 * gates a security boundary (which navigations load in the bridged WebView vs.
 * trigger login / hand off to the system), so it lowercases internally rather
 * than trust callers to pre-normalize — `"HTTPS"` counts. Everything else
 * (mailto:, intent:, about:, chrome-error://, null) is handed off or ignored.
 */
fun isHttpScheme(scheme: String?): Boolean {
    val normalized = scheme?.lowercase() ?: return false
    return normalized == "http" || normalized == "https"
}

/**
 * True when [url] is a front-door auth-proxy authorize page for [pinnedOrigin]:
 * it carries a `redirect_uri` query parameter that returns to the pinned origin
 * on a path other than the server's own OIDC callback (`/auth/callback`).
 *
 * Deployments fronted by a hosting proxy (e.g. Databricks Apps) intercept every
 * request — including the login endpoints — and bounce unauthenticated visitors
 * to the host's IdP. That flow must complete inside the WebView so the proxy's
 * session cookie lands in the WebView's cookie store; the server's own IdP
 * bounce (redirect_uri path `/auth/callback`) still runs in the system browser
 * (RFC 8252 — Google blocks WebView sign-in).
 */
fun isProxyAuthUrl(
    url: String?,
    pinnedOrigin: String?,
): Boolean {
    if (pinnedOrigin == null) return false
    val uri = url?.let(Uri::parse) ?: return false
    // getQueryParameter throws on opaque (non-hierarchical) URIs like mailto:.
    if (uri.isOpaque) return false
    val redirect = uri.getQueryParameter("redirect_uri") ?: return false
    if (originOf(redirect) != pinnedOrigin) return false
    // Trailing-slash-insensitive: `/auth/callback/` is still the server's own.
    return Uri.parse(redirect).path?.trimEnd('/') != "/auth/callback"
}

/**
 * Normalize user-entered server text into a loadable URL, or null if it isn't a
 * usable http(s) address. Adds a default `https://` scheme when omitted and
 * trims a trailing slash.
 */
fun normalizeServerUrl(input: String): String? {
    val trimmed = input.trim().ifBlank { return null }
    // No internal whitespace — a stray newline would otherwise split the
    // newline-delimited recents store into bogus entries.
    if (trimmed.any { it.isWhitespace() }) return null
    val withScheme = if (trimmed.contains("://")) trimmed else "https://$trimmed"
    val uri = Uri.parse(withScheme)
    val scheme = uri.scheme?.lowercase() ?: return null
    if (!isHttpScheme(scheme)) return null
    if (uri.host.isNullOrBlank()) return null
    return withScheme.trimEnd('/')
}
