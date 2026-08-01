package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.webkit.WebResourceRequest
import android.webkit.WebView
import android.webkit.WebViewClient

/**
 * Signals [onPageReady] once a pinned-origin page finishes loading and routes
 * the OIDC login flow to the system browser via [onLoginRequired].
 *
 * The facade is normally registered with `addDocumentStartJavaScript` in
 * `MainActivity`. Older WebViews that support the message listener but not
 * document-start scripts inject it after the pinned page finishes.
 */
class OmnigentWebViewClient(
    private val pinnedOrigin: () -> String?,
    private val shouldInjectBridgeAtPageReady: () -> Boolean,
    private val onPageReady: (url: String?) -> Unit,
    private val onLoginRequired: () -> Unit,
    private val onNavigationStarted: () -> Unit,
) : WebViewClient() {
    // True while the WebView is walking a front-door auth proxy's IdP flow
    // (see [isProxyAuthUrl]): off-origin navigation stays inline until a
    // pinned-origin page loads again (the proxy callback bounced us home).
    private var proxyAuthInFlight = false

    /** Forget an in-flight proxy auth flow — call when the pinned server changes. */
    fun resetProxyAuth() {
        proxyAuthInFlight = false
    }

    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)
        onNavigationStarted()

        val origin = originOf(url)
        val scheme = url?.let { Uri.parse(it).scheme?.lowercase() }

        // A real http(s) navigation to a foreign origin means the server bounced
        // us to the OIDC IdP and shouldOverrideUrlLoading didn't catch the
        // redirect. Stop and run native system-browser login (RFC 8252: never
        // authenticate in an embedded WebView; Google blocks it and passkeys don't
        // work). Idempotent: the login manager ignores a second start while one is
        // in flight.
        //
        // A null / about:blank / chrome-error:// URL is a failed or transitional
        // load of the pinned server (e.g. it's offline), NOT an IdP redirect —
        // don't misread it as a bounce and pop the browser. Mirror the http(s)
        // gate in shouldOverrideUrlLoading.
        if (isHttpScheme(scheme) && origin != pinnedOrigin()) {
            // A hosting front door bouncing to its IdP must complete inline —
            // its session cookie has to land in this WebView's cookie store.
            if (proxyAuthInFlight || isProxyAuthUrl(url, pinnedOrigin())) {
                proxyAuthInFlight = true
                authLog("proxy-auth landing $origin — loading inline")
                return
            }
            // Log origin only, never the full URL (carries OAuth state/PKCE).
            authLog("off-origin landing $origin -> login")
            view.stopLoading()
            onLoginRequired()
            return
        }
        if (origin == pinnedOrigin()) proxyAuthInFlight = false
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)
        if (originOf(url) == pinnedOrigin() && shouldInjectBridgeAtPageReady()) {
            view.evaluateJavascript(NativeBridgeScript.source) { onPageReady(url) }
            return
        }
        onPageReady(url)
    }

    override fun shouldOverrideUrlLoading(
        view: WebView,
        request: WebResourceRequest,
    ): Boolean {
        val url = request.url
        val scheme = url.scheme?.lowercase()

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        // Non-http(s) schemes (mailto:, tel:, intent:, custom links) can't load in
        // the WebView — hand to the system, fail-closed if nothing handles them.
        if (!isHttpScheme(scheme)) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
            return true
        }

        // Same-origin app pages load in the WebView.
        val origin = originOf(url.toString())
        if (origin == pinnedOrigin()) {
            proxyAuthInFlight = false
            return false
        }

        // A front-door proxy's IdP flow loads inline: the initial authorize
        // bounce (redirect_uri back to the pinned origin) enters the flow, and
        // every navigation inside it — SSO hops, form posts, gestures on the
        // login page — stays in the WebView until we return to the pinned
        // origin, so the proxy cookie ends up where the app pages load.
        if (proxyAuthInFlight || isProxyAuthUrl(url.toString(), pinnedOrigin())) {
            proxyAuthInFlight = true
            authLog("proxy-auth nav $origin — loading inline")
            return false
        }

        // Off-origin top-level navigation. A server redirect (no user gesture) is
        // the OIDC flow bouncing to the IdP -> run native system-browser login. A
        // user gesture is an external link -> hand to the system browser. Either
        // way the foreign page never loads in this WebView (which holds the
        // native bridge).
        authLog("off-origin nav $origin gesture=${request.hasGesture()}")
        if (request.hasGesture()) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
        } else {
            onLoginRequired()
        }
        return true
    }
}
