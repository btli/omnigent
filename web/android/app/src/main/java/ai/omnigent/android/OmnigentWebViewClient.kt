package ai.omnigent.android

import android.content.Intent
import android.graphics.Bitmap
import android.net.Uri
import android.net.http.SslError
import android.os.SystemClock
import android.webkit.RenderProcessGoneDetail
import android.webkit.SslErrorHandler
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebView
import android.webkit.WebViewClient

enum class ProxyAuthState {
    IDLE,
    IN_FLIGHT,
    REFUSED,
}

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
    private val onProxyAuthFlowEnded: () -> Unit,
    private val clock: () -> Long = { SystemClock.elapsedRealtime() },
    private val onEmbeddedSignInUnsupported: () -> Unit,
    private val onWebViewUnusable: () -> Unit,
) : WebViewClient() {
    private var proxyAuthState = ProxyAuthState.IDLE
    private var flowStartedAt = 0L

    // Error callbacks may arrive after a server switch, so exits require the
    // URL last started while the current flow was in flight.
    private var trackedMainFrameUrl: String? = null

    // The self-stop ledger describes WebView loading, independently of auth.
    private var activeMainFrameUrl: String? = null
    private var isLoading = false
    private var lastSelfStoppedUrl: String? = null

    /** Stop the current load while owning the compatibility finish it causes. */
    fun stopLoadingAndLedger(view: WebView) {
        if (isLoading) lastSelfStoppedUrl = activeMainFrameUrl
        view.stopLoading()
    }

    private fun enterProxyAuth() {
        if (proxyAuthState != ProxyAuthState.IDLE) return
        proxyAuthState = ProxyAuthState.IN_FLIGHT
        flowStartedAt = clock()
    }

    /** End any flow silently — the reset for server switches, expiry, and teardown. */
    fun endProxyAuth() {
        proxyAuthState = ProxyAuthState.IDLE
        flowStartedAt = 0L
        trackedMainFrameUrl = null
        lastSelfStoppedUrl = null
    }

    private fun expireProxyAuthIfNeeded() {
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT &&
            clock() - flowStartedAt > PROXY_AUTH_DEADLINE_MILLIS
        ) {
            endProxyAuth()
        }
    }

    override fun onPageStarted(
        view: WebView,
        url: String?,
        favicon: Bitmap?,
    ) {
        super.onPageStarted(view, url, favicon)
        onNavigationStarted()
        expireProxyAuthIfNeeded()

        activeMainFrameUrl = url
        isLoading = true

        if (proxyAuthState == ProxyAuthState.REFUSED) return

        val origin = originOf(url)
        val isOffOrigin =
            isHttpScheme(url?.let { Uri.parse(it).scheme }) && origin != pinnedOrigin()

        if (proxyAuthState == ProxyAuthState.IDLE &&
            isOffOrigin &&
            isProxyAuthUrl(url, pinnedOrigin())
        ) {
            enterProxyAuth()
        }

        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
            trackedMainFrameUrl = url
            if (isEmbeddedSignInUnsupported(url)) {
                proxyAuthState = ProxyAuthState.REFUSED
                stopLoadingAndLedger(view)
                onEmbeddedSignInUnsupported()
                return
            }
        }

        if (isOffOrigin) {
            if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
                authLog("proxy-auth landing $origin — loading inline")
                return
            }

            authLog("off-origin landing $origin -> login")
            stopLoadingAndLedger(view)
            onLoginRequired()
        }
    }

    override fun onPageFinished(
        view: WebView,
        url: String?,
    ) {
        super.onPageFinished(view, url)

        if (isLoading && activeMainFrameUrl == url) isLoading = false

        // Any main-frame finish clears the ledger; a matching one is also
        // consumed — it must not end the flow the shell's own stop preceded.
        val consumesSelfStoppedFinish = lastSelfStoppedUrl != null && lastSelfStoppedUrl == url
        lastSelfStoppedUrl = null
        if (!consumesSelfStoppedFinish &&
            proxyAuthState == ProxyAuthState.IN_FLIGHT &&
            originOf(url) == pinnedOrigin()
        ) {
            endProxyAuth()
            onProxyAuthFlowEnded()
        }

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

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        expireProxyAuthIfNeeded()

        val urlString = url.toString()
        val origin = originOf(urlString)

        // Non-http(s) schemes must be handed to an installed system handler.
        if (!isHttpScheme(url.scheme)) {
            openExternally(view, url)
            return true
        }

        if (proxyAuthState == ProxyAuthState.REFUSED) {
            return origin != pinnedOrigin()
        }

        if (origin == pinnedOrigin()) return false

        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
            authLog("proxy-auth nav $origin — loading inline")
            return false
        }

        if (isProxyAuthUrl(urlString, pinnedOrigin())) {
            if (request.isRedirect) {
                enterProxyAuth()
                authLog("proxy-auth nav $origin — loading inline")
                return false
            }
            if (!request.hasGesture()) return false

            authLog("proxy-shaped external nav $origin")
            openExternally(view, url)
            return true
        }

        authLog("off-origin nav $origin gesture=${request.hasGesture()}")
        if (request.hasGesture()) {
            openExternally(view, url)
        } else {
            onLoginRequired()
        }
        return true
    }

    override fun onReceivedError(
        view: WebView,
        request: WebResourceRequest,
        error: WebResourceError,
    ) {
        super.onReceivedError(view, request, error)
        handleReceivedError(request)
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        super.onReceivedHttpError(view, request, errorResponse)
        handleReceivedError(request)
    }

    override fun onReceivedSslError(
        view: WebView,
        handler: SslErrorHandler,
        error: SslError,
    ) {
        super.onReceivedSslError(view, handler, error)
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) endProxyAuth()
    }

    override fun onRenderProcessGone(
        view: WebView,
        detail: RenderProcessGoneDetail,
    ): Boolean {
        activeMainFrameUrl = null
        isLoading = false
        endProxyAuth()
        onWebViewUnusable()
        return true
    }

    /**
     * Settle the trackers for a main-frame load that ended in an error, from
     * either error callback. `internal` because [WebResourceError] has no
     * constructor tests can reach.
     */
    internal fun handleReceivedError(request: WebResourceRequest) {
        if (!request.isForMainFrame) return

        val requestUrl = request.url.toString()
        if (activeMainFrameUrl == requestUrl) isLoading = false
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT && trackedMainFrameUrl == requestUrl) {
            endProxyAuth()
        }
    }

    // Hand a URL to the system, fail-closed if nothing handles it.
    private fun openExternally(
        view: WebView,
        url: Uri,
    ) {
        runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
    }

    private fun isEmbeddedSignInUnsupported(url: String?): Boolean {
        val uri = url?.let(Uri::parse) ?: return false
        return containsEmbeddedRejection(uri.encodedQuery) ||
            containsEmbeddedRejection(uri.encodedFragment)
    }

    private fun containsEmbeddedRejection(component: String?): Boolean {
        if (component == null) return false
        return component.contains(EMBEDDED_REJECTION) ||
            Uri.decode(component).contains(EMBEDDED_REJECTION)
    }

    private companion object {
        const val EMBEDDED_REJECTION = "disallowed_useragent"
        const val PROXY_AUTH_DEADLINE_MILLIS = 6 * 60_000L
    }
}
