package ai.omnigent.android

import android.app.AlertDialog
import android.content.Intent
import android.content.pm.ActivityInfo
import android.content.pm.ResolveInfo
import android.content.res.Configuration
import android.net.Uri
import android.view.View
import android.webkit.CookieManager
import android.webkit.RenderProcessGoneDetail
import android.webkit.WebResourceRequest
import android.webkit.WebView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.annotation.Implementation
import org.robolectric.annotation.Implements
import org.robolectric.annotation.RealObject
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowAlertDialog
import org.robolectric.shadows.ShadowCookieManager
import org.robolectric.shadows.ShadowLooper.idleMainLooper
import org.robolectric.shadows.ShadowPopupMenu
import org.robolectric.shadows.ShadowToast
import org.robolectric.shadows.ShadowWebView
import org.robolectric.util.ReflectionHelpers
import org.robolectric.util.ReflectionHelpers.ClassParameter
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutorService
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35], shadows = [CountingOmnigentWebViewClientShadow::class])
class MainActivityTest {
    @Test
    fun `renderer crash recreates the activity and WebView`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val controller = Robolectric.buildActivity(MainActivity::class.java).setup()
        val activity = controller.get()
        val oldWebView = activity.webView()

        val handled =
            activity.shellWebViewClient().onRenderProcessGone(
                oldWebView,
                renderProcessGoneDetail(),
            )
        idleMainLooper()

        val recreatedActivity = controller.get()
        assertTrue(handled)
        assertTrue(activity.isDestroyed)
        assertNotSame(activity, recreatedActivity)
        assertNotSame(oldWebView, recreatedActivity.webView())
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.webView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        val darkConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_YES
            }
        activity.onConfigurationChanged(darkConfiguration)
        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)

        val lightConfiguration =
            Configuration(activity.resources.configuration).apply {
                uiMode =
                    (uiMode and Configuration.UI_MODE_NIGHT_MASK.inv()) or
                    Configuration.UI_MODE_NIGHT_NO
            }
        activity.onConfigurationChanged(lightConfiguration)
        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `refusal shows the browser-required dialog`() {
        val activity = activity()

        val dialog = activity.refuseEmbeddedSignIn()
        val shadowDialog = shadowOf(dialog)

        assertEquals(activity.getString(R.string.proxy_auth_refused_title), shadowDialog.title)
        assertEquals(
            activity.getString(R.string.proxy_auth_refused_body, "example.com"),
            shadowDialog.message,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_open_browser),
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_cancel),
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).text,
        )
        assertTrue(dialog.isShowing)
        assertEquals(ProxyAuthState.REFUSED, activity.shellWebViewClient().proxyAuthState())
    }

    @Test
    fun `only this app resolving reports no browser and keeps the dialog open`() {
        val activity = activity()
        activity.addBrowser(activity.packageName, "OwnBrowserActivity")
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(0, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `one external browser launches as an explicit component`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
        idleMainLooper()

        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(Intent.ACTION_VIEW, started.action)
        assertEquals(PINNED_ORIGIN, started.dataString)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
        assertTrue(started.component?.packageName != activity.packageName)
        assertFalse(dialog.isShowing)
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `self and external browsers produce a chooser whose every intent excludes self`() {
        val activity = activity()
        activity.addBrowser(activity.packageName, "OwnBrowserActivity")
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        activity.addBrowser(SECOND_BROWSER_PACKAGE, SECOND_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        val chooser = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(Intent.ACTION_CHOOSER, chooser.action)
        val target =
            checkNotNull(
                chooser.getParcelableExtra(Intent.EXTRA_INTENT, Intent::class.java),
            )
        val initial =
            checkNotNull(chooser.getParcelableArrayExtra(Intent.EXTRA_INITIAL_INTENTS))
                .map { it as Intent }
        val browserIntents = listOf(target) + initial
        assertEquals(
            setOf(EXTERNAL_BROWSER_PACKAGE, SECOND_BROWSER_PACKAGE),
            browserIntents.map { it.component?.packageName }.toSet(),
        )
        assertTrue(initial.isNotEmpty())
        assertTrue(
            browserIntents.all { intent ->
                intent.component != null &&
                    intent.component?.packageName != activity.packageName &&
                    intent.dataString == PINNED_ORIGIN
            },
        )
    }

    @Test
    fun `no resolvers reports no browser and keeps the dialog open`() {
        val activity = activity()
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(0, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `every dialog dismissal route resets proxy auth exactly once`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dismissals =
            listOf<(AlertDialog) -> Unit>(
                { dialog -> dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick() },
                { dialog -> dialog.getButton(AlertDialog.BUTTON_NEGATIVE).performClick() },
                { dialog -> dialog.onBackPressed() },
                { dialog ->
                    assertTrue(shadowOf(dialog).isCancelableOnTouchOutside)
                    dialog.cancel()
                },
            )

        dismissals.forEachIndexed { index, dismiss ->
            val dialog = activity.refuseEmbeddedSignIn()

            dismiss(dialog)
            idleMainLooper()

            assertFalse(dialog.isShowing)
            assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
            assertEquals(index + 1, ActivityCallLog.endProxyAuthCalls)

            dialog.dismiss()
            idleMainLooper()
            assertEquals(index + 1, ActivityCallLog.endProxyAuthCalls)
        }
    }

    @Test
    fun `server reload dismisses the refusal dialog without double reset`() {
        val activity = activity()
        val dialog = activity.refuseEmbeddedSignIn()
        ActivityCallLog.clear()

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertFalse(dialog.isShowing)
        assertNull(activity.embeddedSignInDialog())
        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `refusal while finishing resets to idle without showing a dialog`() {
        val activity = activity()
        activity.finish()

        activity.enterProxyAuth()
        activity.shellWebViewClient().onPageStarted(activity.webView(), REFUSAL_URL, null)

        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `menu escape hatch ends an in-flight flow and opens externally`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        activity.enterProxyAuth()
        assertEquals(ProxyAuthState.IN_FLIGHT, activity.shellWebViewClient().proxyAuthState())

        activity.selectOpenInBrowserMenuItem()

        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    @Test
    fun `menu escape hatch dismisses refusal and resets exactly once`() {
        val activity = activity()
        activity.addBrowser(EXTERNAL_BROWSER_PACKAGE, EXTERNAL_BROWSER_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()
        ActivityCallLog.clear()

        activity.selectOpenInBrowserMenuItem()
        idleMainLooper()

        assertFalse(dialog.isShowing)
        assertNull(activity.embeddedSignInDialog())
        assertEquals(ProxyAuthState.IDLE, activity.shellWebViewClient().proxyAuthState())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
        val started = checkNotNull(shadowOf(activity).nextStartedActivity)
        assertEquals(EXTERNAL_BROWSER_PACKAGE, started.component?.packageName)
    }

    @Test
    fun `menu escape hatch with no browsers shows a toast without a dialog`() {
        val activity = activity()

        activity.selectOpenInBrowserMenuItem()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_toast),
            ShadowToast.getTextOfLatestToast(),
        )
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
        assertNull(shadowOf(activity).peekNextStartedActivity())
        assertEquals(1, ActivityCallLog.endProxyAuthCalls)
    }

    @Test
    fun `rejected login shows generic retry dialog and never the refusal dialog`() {
        val activity = activity()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
        val shadowDialog = shadowOf(dialog)

        assertEquals(
            activity.getString(R.string.login_failed_title),
            shadowDialog.title,
        )
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowDialog.message,
        )
        assertTrue(shadowDialog.title != activity.getString(R.string.proxy_auth_refused_title))
        assertEquals(
            activity.getString(R.string.login_failed_retry),
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).text,
        )
        assertEquals(
            activity.getString(R.string.proxy_auth_cancel),
            dialog.getButton(AlertDialog.BUTTON_NEGATIVE).text,
        )
    }

    @Test
    fun `timed out login shows the generic retry dialog`() {
        val activity = activity()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)
        val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())

        assertEquals(activity.getString(R.string.login_failed_title), shadowOf(dialog).title)
        assertEquals(
            activity.getString(R.string.login_failed_body, "example.com"),
            shadowOf(dialog).message,
        )
    }

    @Test
    fun `repeated login failure reuses the generic dialog`() {
        val activity = activity()
        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
        val firstDialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.TimedOut)

        assertSame(firstDialog, ShadowAlertDialog.getLatestAlertDialog())
        assertSame(firstDialog, activity.loginFailedDialog())
        assertTrue(firstDialog.isShowing)
    }

    @Test
    fun `retry resets the login budget and starts a fresh attempt`() {
        val activity = activity()
        val loginManager = activity.loginManager()
        val executor: ExecutorService = ReflectionHelpers.getField(loginManager, "io")
        val workerStarted = CountDownLatch(1)
        val holdWorker = CountDownLatch(1)
        executor.execute {
            workerStarted.countDown()
            runCatching { holdWorker.await() }
        }
        assertTrue(workerStarted.await(5, TimeUnit.SECONDS))

        try {
            ReflectionHelpers.setField(activity, "loginAttempts", 3)
            activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)
            val dialog = checkNotNull(ShadowAlertDialog.getLatestAlertDialog())

            dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()
            idleMainLooper()

            assertEquals(1, activity.loginAttempts())
        } finally {
            loginManager.shutdown()
            holdWorker.countDown()
        }
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `success from the previous origin sets no cookie and loads nothing`() {
        ShadowCookieManager.resetCookies()
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
        ActivityCallLog.clear()

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Success(SESSION_TOKEN))

        assertNull(CookieManager.getInstance().getCookie(PINNED_ORIGIN))
        assertNull(CookieManager.getInstance().getCookie(NEW_ORIGIN))
        assertTrue(ActivityCallLog.entries.isEmpty())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `a server switch before the cookie callback cancels the reload`() {
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)
        ActivityCallLog.clear()

        // setCookie acknowledges asynchronously: a switch landing between the
        // write and its callback must not reload the previous server.
        activity.onSessionCookieSet(PINNED_ORIGIN, accepted = true)

        assertTrue(ActivityCallLog.entries.none { it.startsWith("loadUrl:") })
    }

    @Test
    fun `rejection from the previous origin shows no failure surface`() {
        val activity = activity()
        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        activity.onLoginResult(PINNED_ORIGIN, LoginResult.Rejected)

        assertNull(activity.loginFailedDialog())
        assertNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    @Test
    fun `browsable host deep-link handler is not treated as a browser`() {
        val activity = activity()
        val deepLinkIntent =
            Intent(Intent.ACTION_VIEW, Uri.parse(PINNED_ORIGIN))
                .addCategory(Intent.CATEGORY_BROWSABLE)
        activity.addResolver(deepLinkIntent, DEEP_LINK_PACKAGE, DEEP_LINK_ACTIVITY)
        val dialog = activity.refuseEmbeddedSignIn()

        dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick()

        assertEquals(
            activity.getString(R.string.proxy_auth_no_browser_body),
            shadowOf(dialog).message,
        )
        assertTrue(dialog.isShowing)
        assertNull(shadowOf(activity).peekNextStartedActivity())
    }

    @Test
    @Config(
        sdk = [35],
        shadows = [CountingOmnigentWebViewClientShadow::class, RecordingWebViewShadow::class],
    )
    fun `server reload calls reset before ledgered stop before loadUrl`() {
        val activity = activity()
        val client = activity.shellWebViewClient()
        val webView = activity.webView()
        client.onPageStarted(webView, OLD_LOADING_URL, null)

        activity.reloadWithNewServer(NEW_SERVER_URL, NEW_ORIGIN)

        assertEquals(
            listOf(
                "endProxyAuth",
                "stopLoading",
                "loadUrl:$NEW_SERVER_URL",
            ),
            ActivityCallLog.entries,
        )
        assertEquals(OLD_LOADING_URL, client.lastSelfStoppedUrl())

        activity.enterProxyAuth(NEW_ORIGIN)
        client.onPageStarted(webView, PLAIN_IDP_URL, null)
        client.onPageFinished(webView, OLD_LOADING_URL)

        assertEquals(ProxyAuthState.IN_FLIGHT, client.proxyAuthState())
        assertNull(client.lastSelfStoppedUrl())
    }

    private fun activity(): MainActivity {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(PINNED_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        while (shadowOf(activity).nextStartedActivity != null) {
            // Drain setup-only launches before testing browser dispatch.
        }
        ActivityCallLog.clear()
        return activity
    }

    private fun MainActivity.refuseEmbeddedSignIn(): AlertDialog {
        enterProxyAuth()
        shellWebViewClient().onPageStarted(webView(), REFUSAL_URL, null)
        return checkNotNull(ShadowAlertDialog.getLatestAlertDialog())
    }

    private fun MainActivity.enterProxyAuth(origin: String = PINNED_ORIGIN) {
        assertFalse(
            shellWebViewClient().shouldOverrideUrlLoading(
                webView(),
                request(proxyAuthUrl(origin), redirect = true),
            ),
        )
    }

    private fun MainActivity.addBrowser(
        resolverPackage: String,
        resolverActivity: String,
    ) {
        addResolver(browserProbe(), resolverPackage, resolverActivity)
    }

    private fun MainActivity.addResolver(
        intent: Intent,
        resolverPackage: String,
        resolverActivity: String,
    ) {
        val resolveInfo =
            ResolveInfo().apply {
                activityInfo =
                    ActivityInfo().apply {
                        packageName = resolverPackage
                        name = resolverActivity
                        exported = true
                    }
                isDefault = true
            }
        shadowOf(packageManager).addResolveInfoForIntent(intent, resolveInfo)
    }

    private fun MainActivity.selectOpenInBrowserMenuItem() {
        switchButton().performClick()
        val popup = checkNotNull(ShadowPopupMenu.getLatestPopupMenu())
        val expectedTitle = getString(R.string.menu_open_in_browser)
        val item =
            (0 until popup.menu.size())
                .map(popup.menu::getItem)
                .single { it.title.toString() == expectedTitle }
        assertTrue(checkNotNull(shadowOf(popup).onMenuItemClickListener).onMenuItemClick(item))
    }

    private fun MainActivity.reloadWithNewServer(
        serverUrl: String,
        newOrigin: String,
    ) {
        ReflectionHelpers.callInstanceMethod<Unit>(
            this,
            "reloadWithNewServer",
            ClassParameter.from(String::class.java, serverUrl),
            ClassParameter.from(String::class.java, newOrigin),
        )
    }

    private fun MainActivity.shellWebViewClient(): OmnigentWebViewClient =
        ReflectionHelpers.getField(this, "shellWebViewClient")

    private fun MainActivity.embeddedSignInDialog(): AlertDialog? =
        ReflectionHelpers.getField(this, "embeddedSignInDialog")

    private fun MainActivity.loginFailedDialog(): AlertDialog? =
        ReflectionHelpers.getField(this, "loginFailedDialog")

    private fun MainActivity.loginAttempts(): Int =
        ReflectionHelpers.getField(this, "loginAttempts")

    private fun MainActivity.loginManager(): OidcLoginManager =
        ReflectionHelpers.getField(this, "loginManager")

    private fun MainActivity.switchButton(): View = ReflectionHelpers.getField(this, "switchButton")

    private fun MainActivity.webView(): WebView = ReflectionHelpers.getField(this, "webView")

    private fun OmnigentWebViewClient.proxyAuthState(): ProxyAuthState =
        ReflectionHelpers.getField(this, "proxyAuthState")

    private fun OmnigentWebViewClient.lastSelfStoppedUrl(): String? =
        ReflectionHelpers.getField(this, "lastSelfStoppedUrl")

    private fun request(
        url: String,
        redirect: Boolean = false,
    ): WebResourceRequest =
        object : WebResourceRequest {
            override fun getUrl(): Uri = Uri.parse(url)

            override fun isForMainFrame(): Boolean = true

            override fun isRedirect(): Boolean = redirect

            override fun hasGesture(): Boolean = false

            override fun getMethod(): String = "GET"

            override fun getRequestHeaders(): Map<String, String> = emptyMap()
        }

    private fun renderProcessGoneDetail() =
        object : RenderProcessGoneDetail() {
            override fun didCrash(): Boolean = true

            override fun rendererPriorityAtExit(): Int = 0
        }

    private fun browserProbe(): Intent =
        Intent(Intent.ACTION_VIEW, Uri.parse("http:"))
            .addCategory(Intent.CATEGORY_BROWSABLE)

    private fun proxyAuthUrl(origin: String): String =
        "https://idp.example.com/oidc/oauth2/v2.0/authorize?response_type=code" +
            "&redirect_uri=" + Uri.encode("$origin/.auth/callback")

    private companion object {
        const val PINNED_ORIGIN = "https://example.com"
        const val NEW_ORIGIN = "https://new.example.com"
        const val NEW_SERVER_URL = "$NEW_ORIGIN/app"
        const val OLD_LOADING_URL = "$PINNED_ORIGIN/old-loading"
        const val PLAIN_IDP_URL = "https://idp.example.com/login/sso"
        const val REFUSAL_URL =
            "https://accounts.google.com/v3/signin/rejected?error=disallowed_useragent"
        const val SESSION_TOKEN = "header.payload.signature"

        const val EXTERNAL_BROWSER_PACKAGE = "com.example.browser"
        const val EXTERNAL_BROWSER_ACTIVITY = "com.example.browser.MainActivity"
        const val SECOND_BROWSER_PACKAGE = "org.example.browser"
        const val SECOND_BROWSER_ACTIVITY = "org.example.browser.BrowserActivity"
        const val DEEP_LINK_PACKAGE = "com.example.deep-link"
        const val DEEP_LINK_ACTIVITY = "com.example.deep-link.DeepLinkActivity"
    }
}

/** Ordered record of the shell calls the Activity makes on its collaborators. */
private object ActivityCallLog {
    val entries = mutableListOf<String>()

    val endProxyAuthCalls: Int get() = entries.count { it == "endProxyAuth" }

    fun clear() = entries.clear()
}

@Implements(OmnigentWebViewClient::class, isInAndroidSdk = false)
class CountingOmnigentWebViewClientShadow {
    @RealObject
    private lateinit var realClient: OmnigentWebViewClient

    @Implementation
    fun endProxyAuth() {
        ActivityCallLog.entries += "endProxyAuth"
        Shadow.directlyOn<Any?>(
            realClient,
            OmnigentWebViewClient::class.java.name,
            "endProxyAuth",
        )
    }
}

@Implements(WebView::class)
class RecordingWebViewShadow : ShadowWebView() {
    @Implementation
    override fun loadUrl(url: String) {
        ActivityCallLog.entries += "loadUrl:$url"
        super.loadUrl(url)
    }

    @Implementation
    fun stopLoading() {
        ActivityCallLog.entries += "stopLoading"
    }
}
