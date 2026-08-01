package ai.omnigent.android

import android.content.Context
import android.net.Uri
import android.webkit.ValueCallback
import android.webkit.WebResourceRequest
import android.webkit.WebView
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OmnigentWebViewClientTest {
    @Test
    fun `page start does not inject into the outgoing document`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        val client = client(shouldInjectBridgeAtPageReady = false)

        client.onPageStarted(webView, PINNED_URL, null)

        assertNull(webView.evaluatedScript)
    }

    @Test
    fun `fallback injects the facade before declaring the page ready`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var readyUrl: String? = null
        val client =
            client(shouldInjectBridgeAtPageReady = true) { url ->
                readyUrl = url
            }

        client.onPageFinished(webView, PINNED_URL)

        assertEquals(NativeBridgeScript.source, webView.evaluatedScript)
        assertNull(readyUrl)

        webView.completeEvaluation()
        assertEquals(PINNED_URL, readyUrl)
    }

    @Test
    fun `off-origin idp bounce stops the load and starts native login`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        client.onPageStarted(webView, OWN_IDP_URL, null)

        assertTrue(webView.stoppedLoading)
        assertTrue(loginRequired)
    }

    @Test
    fun `front-door proxy authorize page loads inline`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        client.onPageStarted(webView, PROXY_AUTH_URL, null)

        assertFalse(webView.stoppedLoading)
        assertFalse(loginRequired)
    }

    @Test
    fun `proxy authorize navigation is not overridden`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        assertFalse(loginRequired)
    }

    @Test
    fun `off-origin navigation stays inline while a proxy auth flow is in flight`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        // Enter the proxy IdP flow, then navigate within the IdP (no
        // redirect_uri on intermediate pages, with or without a gesture).
        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        assertFalse(client.shouldOverrideUrlLoading(webView, request("https://idp.example.com/login/sso")))
        assertFalse(
            client.shouldOverrideUrlLoading(
                webView,
                request("https://idp.example.com/login/password", gesture = true),
            ),
        )
        assertFalse(loginRequired)
    }

    @Test
    fun `returning to the pinned origin ends the proxy auth flow`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        client.onPageStarted(webView, PINNED_URL, null)

        // Flow over: a fresh off-origin server bounce routes to native login again.
        assertTrue(client.shouldOverrideUrlLoading(webView, request(OWN_IDP_URL)))
        assertTrue(loginRequired)
    }

    @Test
    fun `proxy auth flow masks the webview user agent and restores it after`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        webView.settings.userAgentString =
            "Mozilla/5.0 (Linux; Android 14; Pixel Build/X; wv) AppleWebKit/537.36 " +
            "(KHTML, like Gecko) Version/4.0 Chrome/126.0.0.0 Mobile Safari/537.36"
        val client = client(shouldInjectBridgeAtPageReady = false)

        client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL))
        val masked = webView.settings.userAgentString
        assertFalse(masked.contains("; wv"))
        assertFalse(masked.contains("Version/4.0"))

        client.onPageStarted(webView, PINNED_URL, null)
        assertTrue(webView.settings.userAgentString.contains("; wv"))
        assertTrue(webView.settings.userAgentString.contains("Version/4.0"))
    }

    @Test
    fun `resetProxyAuth ends the flow without a pinned page load`() {
        val webView = RecordingWebView(ApplicationProvider.getApplicationContext())
        var loginRequired = false
        val client = client(shouldInjectBridgeAtPageReady = false, onLoginRequired = { loginRequired = true })

        assertFalse(client.shouldOverrideUrlLoading(webView, request(PROXY_AUTH_URL)))
        client.resetProxyAuth()

        assertTrue(client.shouldOverrideUrlLoading(webView, request(OWN_IDP_URL)))
        assertTrue(loginRequired)
    }

    private fun request(
        url: String,
        gesture: Boolean = false,
        mainFrame: Boolean = true,
    ): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = mainFrame

            override fun isRedirect(): Boolean = false

            override fun hasGesture(): Boolean = gesture

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun client(
        shouldInjectBridgeAtPageReady: Boolean,
        onLoginRequired: () -> Unit = {},
        onPageReady: (String?) -> Unit = {},
    ) = OmnigentWebViewClient(
        pinnedOrigin = { PINNED_ORIGIN },
        shouldInjectBridgeAtPageReady = { shouldInjectBridgeAtPageReady },
        onPageReady = onPageReady,
        onLoginRequired = onLoginRequired,
    )

    private class RecordingWebView(
        context: Context,
    ) : WebView(context) {
        var evaluatedScript: String? = null
        var stoppedLoading = false
        private var callback: ValueCallback<String>? = null

        override fun evaluateJavascript(
            script: String,
            resultCallback: ValueCallback<String>?,
        ) {
            evaluatedScript = script
            callback = resultCallback
        }

        override fun stopLoading() {
            stoppedLoading = true
        }

        fun completeEvaluation() {
            callback?.onReceiveValue("null")
        }
    }

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val PINNED_URL = "$PINNED_ORIGIN/app"

        // A hosting front door (e.g. Databricks Apps) bouncing to its IdP with
        // a redirect_uri returning to the pinned origin's proxy callback.
        const val PROXY_AUTH_URL =
            "https://idp.example.com/oidc/oauth2/v2.0/authorize?response_type=code" +
                "&redirect_uri=https%3A%2F%2Fexample.com%2F.auth%2Fcallback"

        // The app's own OIDC bounce: redirect_uri is the server's /auth/callback.
        const val OWN_IDP_URL =
            "https://accounts.example.org/authorize?response_type=code" +
                "&redirect_uri=https%3A%2F%2Fexample.com%2Fauth%2Fcallback"
    }
}
