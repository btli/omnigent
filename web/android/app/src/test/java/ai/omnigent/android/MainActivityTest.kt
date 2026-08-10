package ai.omnigent.android

import android.content.Context
import android.content.RestrictionsManager
import android.content.res.Configuration
import android.net.Uri
import android.os.Bundle
import android.webkit.WebView
import androidx.browser.auth.AuthTabIntent
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

        assertEquals("https://example.com", shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `dismissed auth tab falls back to the inline login`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)

        activity.onAuthTabOutcome(AuthTabIntent.RESULT_CANCELED, null)

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.webView()).lastLoadedUrl)
    }

    @Test
    fun `failed app link verification falls back to inline login`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect(DATABRICKS_ORIGIN)
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        activity.authTabFlow.begin(DATABRICKS_ORIGIN, activity.packageName)

        activity.onAuthTabOutcome(AuthTabIntent.RESULT_VERIFICATION_FAILED, null)

        assertFalse(activity.authTabFlow.inFlight)
        assertTrue(activity.authTabFellBack)
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.webView()).lastLoadedUrl)
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
        assertEquals(DATABRICKS_ORIGIN, shadowOf(activity.webView()).lastLoadedUrl)
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
        assertEquals(origin, shadowOf(activity.webView()).lastLoadedUrl)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private companion object {
        const val DATABRICKS_ORIGIN = "https://example.databricksapps.com"
    }
}
