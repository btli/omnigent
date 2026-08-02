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
    private val onProxyAuthFlowEnded: () -> Unit = {},
    private val clock: () -> Long = { SystemClock.elapsedRealtime() },
    private val onEmbeddedSignInUnsupported: () -> Unit,
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

    /** Forget an in-flight proxy auth flow when the pinned server changes. */
    fun resetProxyAuth(view: WebView) {
        endProxyAuth()
    }

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

    private fun endProxyAuth() {
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
        expireProxyAuthIfNeeded()

        activeMainFrameUrl = url
        isLoading = true

        if (proxyAuthState == ProxyAuthState.REFUSED) return

        val origin = originOf(url)
        val scheme = url?.let { Uri.parse(it).scheme?.lowercase() }

        if (proxyAuthState == ProxyAuthState.IDLE &&
            isHttpScheme(scheme) &&
            origin != pinnedOrigin() &&
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

        if (isHttpScheme(scheme) && origin != pinnedOrigin()) {
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

        val consumesSelfStoppedFinish = lastSelfStoppedUrl != null && lastSelfStoppedUrl == url
        if (consumesSelfStoppedFinish) {
            lastSelfStoppedUrl = null
        } else {
            if (lastSelfStoppedUrl != null) lastSelfStoppedUrl = null
            if (proxyAuthState == ProxyAuthState.IN_FLIGHT && originOf(url) == pinnedOrigin()) {
                endProxyAuth()
                onProxyAuthFlowEnded()
            }
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
        val scheme = url.scheme?.lowercase()

        // Subframes (cross-origin iframes: web previews, embeds) load inline.
        if (!request.isForMainFrame) return false

        expireProxyAuthIfNeeded()

        val origin = originOf(url.toString())
        if (proxyAuthState == ProxyAuthState.REFUSED) {
            return origin != pinnedOrigin()
        }

        // Non-http(s) schemes must be handed to an installed system handler.
        if (!isHttpScheme(scheme)) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
            return true
        }

        if (origin == pinnedOrigin()) return false

        if (proxyAuthState == ProxyAuthState.IN_FLIGHT) {
            authLog("proxy-auth nav $origin — loading inline")
            return false
        }

        val isProxyAuth = isProxyAuthUrl(url.toString(), pinnedOrigin())
        if (isProxyAuth) {
            if (request.isRedirect) {
                enterProxyAuth()
                authLog("proxy-auth nav $origin — loading inline")
                return false
            }
            if (!request.hasGesture()) return false

            authLog("proxy-shaped external nav $origin")
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
            return true
        }

        authLog("off-origin nav $origin gesture=${request.hasGesture()}")
        if (request.hasGesture()) {
            runCatching { view.context.startActivity(Intent(Intent.ACTION_VIEW, url)) }
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
        handleUrlBearingTerminalCallback(request)
    }

    override fun onReceivedHttpError(
        view: WebView,
        request: WebResourceRequest,
        errorResponse: WebResourceResponse,
    ) {
        super.onReceivedHttpError(view, request, errorResponse)
        handleUrlBearingTerminalCallback(request)
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
        val handled = super.onRenderProcessGone(view, detail)
        activeMainFrameUrl = null
        isLoading = false
        endProxyAuth()
        return handled
    }

    internal fun handleReceivedError(request: WebResourceRequest) {
        handleUrlBearingTerminalCallback(request)
    }

    private fun handleUrlBearingTerminalCallback(request: WebResourceRequest) {
        if (!request.isForMainFrame) return

        val requestUrl = request.url.toString()
        if (activeMainFrameUrl == requestUrl) isLoading = false
        if (proxyAuthState == ProxyAuthState.IN_FLIGHT && trackedMainFrameUrl == requestUrl) {
            endProxyAuth()
        }
    }

    private fun isEmbeddedSignInUnsupported(url: String?): Boolean {
        val uri = url?.let { runCatching { Uri.parse(it) }.getOrNull() } ?: return false
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
