package ai.omnigent.android

import android.content.res.Configuration
import android.text.TextUtils
import android.view.Gravity
import android.view.View
import android.webkit.WebView
import android.widget.FrameLayout
import android.widget.TextView
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
import org.robolectric.annotation.Config

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
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)

        val layout = button.layoutParams as FrameLayout.LayoutParams
        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band)
        assertEquals(Gravity.TOP or Gravity.LEFT, layout.gravity)
        assertEquals(expectedLeft, layout.leftMargin)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    fun `server switcher remains fully visible beside a narrow right edge band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.99, 1.0)
        layout(parent, width = 1000, height = 600)
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)

        val visibleWidth = minOf(button.right, parent.width) - maxOf(button.left, 0)
        assertTrue(button.width > serverSwitcherBandWidth(parent.width, band))
        assertTrue(button.left >= 0)
        assertTrue(button.right <= parent.width)
        assertEquals(button.width, visibleWidth)
    }

    @Test
    fun `server switcher absolute left margin does not mirror in RTL`() {
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://example.com")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val band = ServerSwitcherBand(0.3, 0.8)
        parent.layoutDirection = View.LAYOUT_DIRECTION_RTL
        activity.setSwitcherBand(band)

        layout(parent, width = 1000, height = 600)
        layout(parent, width = 1000, height = 600)

        val expectedLeft = serverSwitcherLeftMargin(1000, button.width, band)
        assertEquals(expectedLeft, button.left)
        assertEquals(expectedLeft + button.width, button.right)
    }

    @Test
    fun `server switcher width bounds follow the published band`() {
        val host = "a-very-long-server-hostname-that-needs-truncation.example.com"
        ServerStore(ApplicationProvider.getApplicationContext()).connect("https://$host")
        val activity = Robolectric.buildActivity(MainActivity::class.java).setup().get()
        val button = activity.switchButton()
        val parent = button.parent as View
        val density = activity.resources.displayMetrics.density
        val defaultMax = (172 * density).toInt()
        val recoveryFloor = (48 * density).toInt()
        layout(parent, width = 1000, height = 600)

        activity.setSwitcherBand(ServerSwitcherBand(0.45, 0.55))
        val expectedWideBandWidth = maxOf(recoveryFloor, minOf(defaultMax, 100))
        assertEquals(expectedWideBandWidth, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        // The pill may measure narrower than the cap; what matters is that it
        // stays inside the band it was given.
        assertTrue(button.width <= expectedWideBandWidth)
        assertTrue(button.left >= 450)
        assertTrue(button.right <= 550)

        activity.setSwitcherBand(ServerSwitcherBand(0.49, 0.51))
        assertEquals(recoveryFloor, button.maxWidth)
        assertEquals(recoveryFloor, button.minWidth)
        layout(parent, width = 1000, height = 600)
        // Band narrower than the recovery floor: keep the pill tappable and
        // anchored at the band's left edge, accepting bounded overflow.
        assertEquals(recoveryFloor, button.width)
        assertEquals(490, button.left)
        assertEquals(490 + button.width, button.right)
    }

    private fun layout(
        view: View,
        width: Int,
        height: Int,
    ) {
        // Force the traversal: an unchanged frame otherwise skips onLayout, so
        // children keep their stale positions.
        view.requestLayout()
        view.measure(
            View.MeasureSpec.makeMeasureSpec(width, View.MeasureSpec.EXACTLY),
            View.MeasureSpec.makeMeasureSpec(height, View.MeasureSpec.EXACTLY),
        )
        view.layout(0, 0, width, height)
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
