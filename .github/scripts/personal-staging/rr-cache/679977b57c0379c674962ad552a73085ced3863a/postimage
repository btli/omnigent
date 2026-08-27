package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import androidx.browser.auth.AuthTabIntent
import androidx.core.graphics.Insets
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadow.api.Shadow
import org.robolectric.shadows.ShadowRestrictionsManager

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class MainActivityTest {
    @Test
    fun `cutout-only safe area is published on every edge`() {
        val cutout = Insets.of(11, 23, 31, 0)
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
                .build()

        val safeArea = systemSafeAreaInsets(insets)
        assertEquals(cutout, safeArea)

        val script = androidSafeAreaScript(safeArea, 1f)
        assertTrue(script.contains("const top = '23.0px'"))
        assertTrue(script.contains("const left = '11.0px'"))
        assertTrue(script.contains("const right = '31.0px'"))
        assertTrue(script.contains("setProperty('--omnigent-safe-left', left)"))
        assertTrue(script.contains("setProperty('--omnigent-safe-right', right)"))
    }

    @Test
    fun `landscape cutout unions with the system bars per edge`() {
        // Landscape phone: the gesture nav bar keeps the bottom inset while the
        // camera cutout eats the left edge — a systemBars()-only source would
        // report left as 0 and let the rail/drawers slide under the cutout.
        val bars = Insets.of(0, 24, 0, 16)
        val cutout = Insets.of(31, 0, 0, 0)
        val insets =
            WindowInsetsCompat
                .Builder()
                .setInsets(WindowInsetsCompat.Type.systemBars(), bars)
                .setInsets(WindowInsetsCompat.Type.displayCutout(), cutout)
                .build()

        val safeArea = systemSafeAreaInsets(insets)
        assertEquals(Insets.of(31, 24, 0, 16), safeArea)

        val script = androidSafeAreaScript(safeArea, 1f)
        assertTrue(script.contains("const top = '24.0px'"))
        assertTrue(script.contains("const bottom = '16.0px'"))
        assertTrue(script.contains("const left = '31.0px'"))
    }

    @Test
    fun `connect activity marks its handoff as an explicit server change`() {
        val activity = Robolectric.buildActivity(ConnectActivity::class.java).setup().get()
        activity.findViewById<EditText>(R.id.server_url).setText("https://new.example")

        activity.findViewById<Button>(R.id.connect).performClick()

        val intent = shadowOf(activity).nextStartedActivity
        assertTrue(intent.getBooleanExtra(ConnectActivity.EXTRA_SERVER_CHANGED, false))
    }

    @Test
    fun `webview leaves algorithmic darkening disabled`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertFalse(activity.testWebView().settings.isAlgorithmicDarkeningAllowed)
    }

    @Test
    @Config(qualifiers = "notnight")
    fun `light configuration uses dark status bar icons`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertTrue(insetsController.isAppearanceLightStatusBars)
        assertTrue(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    @Config(qualifiers = "night")
    fun `dark configuration uses light status bar icons`() {
        testStore().connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val insetsController =
            WindowInsetsControllerCompat(activity.window, activity.window.decorView)

        assertFalse(insetsController.isAppearanceLightStatusBars)
        assertFalse(insetsController.isAppearanceLightNavigationBars)
    }

    @Test
    fun `configuration change updates system bar icon polarity`() {
        testStore().connect("https://example.com")
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
    fun `a managed preset never overrides the server the user picked`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect("https://example.com")
        val manager = context.getSystemService(RestrictionsManager::class.java)
        Shadow
            .extract<ShadowRestrictionsManager>(manager)
            .setApplicationRestrictions(
                Bundle().apply {
                    putString(ManagedConfig.KEY_SERVER_URLS, "https://managed.example.com")
                },
            )

        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()

        assertEquals("https://example.com", shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `dismissed auth tab falls back to the inline login`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)

        activity.onAuthTabOutcome(AuthTabIntent.RESULT_CANCELED, null)

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `failed app link verification falls back to inline login`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)

        activity.onAuthTabOutcome(AuthTabIntent.RESULT_VERIFICATION_FAILED, null)

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `unmatched auth callback abandons the flow and falls back`() {
        // Regression: a mismatched or malformed callback used to leave the
        // pending flow armed, so the in-flight check short-circuited every
        // later login attempt — a permanent wedge until process death.
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)

        activity.onAuthTabOutcome(
            AuthTabIntent.RESULT_OK,
            Uri.parse(
                "$DATABRICKS_ORIGIN${NativeAuth.CALLBACK_PATH}" +
                    "?state=not-the-flow-state&code=c0de&exchange=tab",
            ),
        )

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
    }

    @Test
    fun `initial auth tab launch falls back when provider resolution is null`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabProviderPackageForTest = { null }

        activity.startProxyLogin()

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `missing auth tab provider falls back without reloading a custom origin`() {
        val context = ApplicationProvider.getApplicationContext<Context>()
        ServerStore(context).connect(CUSTOM_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = shadowOf(activity.testWebView())
        val loadedBeforeFallback = webView.lastLoadedUrl
        activity.authTabProviderPackageForTest = { null }

        activity.startProxyLogin()

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(loadedBeforeFallback, webView.lastLoadedUrl)
    }

    @Test
    fun `exchange auth tab launch falls back when provider resolution is null`() {
        val origin = DATABRICKS_ORIGIN
        ServerStore(ApplicationProvider.getApplicationContext()).connect(origin)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabProviderPackageForTest = { null }
        val completion = activity.authTabFlow.begin(origin, activity.packageName)!!
        val state = completion.getQueryParameter("state")!!

        activity.onAuthTabOutcome(
            AuthTabIntent.RESULT_OK,
            Uri.parse(
                "$origin${NativeAuth.CALLBACK_PATH}" +
                    "?state=$state&code=one-time-code-1234&exchange=tab",
            ),
        )

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(origin, shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `stale exchange result for a switched-away origin leaves the current flow alone`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = shadowOf(activity.testWebView())
        // The current origin's own login is in flight when the exchange an old
        // origin started (before the server switch) finally reports back.
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)
        val loadedBefore = webView.lastLoadedUrl
        val stale =
            AuthTabFlow.Outcome.ExchangePost(
                origin = "https://previous.example.net",
                code = "one-time-code-1234",
                state = "stale-state",
                verifier = "stale-verifier",
            )

        activity.onExchangeResult(stale, null)

        assertTrue(activity.authTabFlow.inFlight)
        assertFalse(activity.authTabFellBack)
        assertEquals(loadedBefore, webView.lastLoadedUrl)
    }

    @Test
    fun `failed exchange for the current origin abandons the flow and falls back`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)
        val failed =
            AuthTabFlow.Outcome.ExchangePost(
                origin = DATABRICKS_ORIGIN,
                code = "one-time-code-1234",
                state = "flow-state",
                verifier = "flow-verifier",
            )

        activity.onExchangeResult(failed, null)

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.testWebView()).lastLoadedUrl)
    }

    @Test
    fun `cookie write landing after a server switch does not reload the old origin`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(CUSTOM_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = shadowOf(activity.testWebView())
        val loadedBefore = webView.lastLoadedUrl

        // The async setCookie callback carries the origin captured when the
        // write started; the pinned origin has since moved on.
        activity.onSessionCookieWritten(
            "https://previous.example.net",
            "ap_session",
            accepted = true,
        )

        assertEquals(loadedBefore, webView.lastLoadedUrl)
    }

    @Test
    fun `cookie write for the pinned origin reloads it`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("$CUSTOM_ORIGIN/workspace")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val webView = shadowOf(activity.testWebView())
        assertEquals("$CUSTOM_ORIGIN/workspace", webView.lastLoadedUrl)

        activity.onSessionCookieWritten(CUSTOM_ORIGIN, "ap_session", accepted = true)

        assertEquals(CUSTOM_ORIGIN, webView.lastLoadedUrl)
    }

    private companion object {
        const val CUSTOM_ORIGIN = "https://example.com"
        const val DATABRICKS_ORIGIN = "https://example.databricksapps.com"
    }
}
