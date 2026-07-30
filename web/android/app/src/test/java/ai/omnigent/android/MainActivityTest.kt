package ai.omnigent.android

import android.content.res.Configuration
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
import androidx.core.view.WindowInsetsControllerCompat
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

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
    fun `server switcher starts centered with a capped accessible label`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val density = activity.resources.displayMetrics.density
        val layout = button.layoutParams as FrameLayout.LayoutParams

        assertEquals(Gravity.TOP or Gravity.CENTER_HORIZONTAL, layout.gravity)
        assertEquals((172 * density).toInt(), button.maxWidth)
        assertEquals((48 * density).toInt(), button.minWidth)
        assertEquals(TextUtils.TruncateAt.MIDDLE, button.ellipsize)
        assertTrue(button.isSingleLine)
        assertEquals(host, button.contentDescription)
    }

    @Test
    fun `server switcher band uses an absolute left margin`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        activity.setSwitcherBand(ServerSwitcherBand(0.3, 0.8))
        (button.parent as View).layout(0, 0, 1000, 600)
        // Sizing the pill last fires the layout listener, which is the path
        // that repositions it in production.
        button.layout(0, 0, 160, 48)

        val layout = button.layoutParams as FrameLayout.LayoutParams
        assertEquals(Gravity.TOP or Gravity.LEFT, layout.gravity)
        assertEquals(470, layout.leftMargin)
    }

    @Test
    fun `server switcher width bounds follow the published band`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val density = activity.resources.displayMetrics.density
        (button.parent as View).layout(0, 0, 1000, 600)
        button.layout(0, 0, 160, 48)

        activity.setSwitcherBand(ServerSwitcherBand(0.45, 0.55))
        assertEquals(100, button.maxWidth)
        assertEquals((48 * density).toInt(), button.minWidth)

        activity.setSwitcherBand(ServerSwitcherBand(0.49, 0.51))
        assertEquals(20, button.maxWidth)
        assertEquals(20, button.minWidth)
    }

    private fun MainActivity.webView(): WebView =
        MainActivity::class
            .java
            .getDeclaredField("webView")
            .apply { isAccessible = true }
            .get(this) as WebView

    private fun MainActivity.switchButton(): TextView =
        MainActivity::class
            .java
            .getDeclaredField("switchButton")
            .apply { isAccessible = true }
            .get(this) as TextView

    private fun MainActivity.setSwitcherBand(band: ServerSwitcherBand) {
        MainActivity::class
            .java
            .getDeclaredField("switcherBand")
            .apply { isAccessible = true }
            .set(this, band)
        MainActivity::class
            .java
            .getDeclaredMethod("positionServerSwitcher")
            .apply { isAccessible = true }
            .invoke(this)
    }
}
