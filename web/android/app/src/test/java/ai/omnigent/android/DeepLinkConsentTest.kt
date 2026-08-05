package ai.omnigent.android

import android.content.DialogInterface
import android.content.Intent
import android.net.Uri
import android.os.Looper
import android.webkit.WebView
import androidx.appcompat.app.AlertDialog
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.Robolectric
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowDialog

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DeepLinkConsentTest {
    private val hex = "e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9"

    private fun store() = ServerStore(ApplicationProvider.getApplicationContext())

    private fun viewIntent(link: String) =
        Intent(Intent.ACTION_VIEW, Uri.parse(link)).addCategory(Intent.CATEGORY_BROWSABLE)

    private fun MainActivity.field(name: String): Any? =
        MainActivity::class.java
            .getDeclaredField(name)
            .apply { isAccessible = true }
            .get(this)

    private fun MainActivity.setField(
        name: String,
        value: Any?,
    ) = MainActivity::class.java
        .getDeclaredField(name)
        .apply { isAccessible = true }
        .set(this, value)

    private fun MainActivity.webView(): WebView = field("webView") as WebView

    private fun latestDialog(): AlertDialog = ShadowDialog.getLatestDialog() as AlertDialog

    // AlertDialog's button click listeners post dismiss work to the main
    // looper; Robolectric needs an explicit pump for it (and any deep-link
    // follow-up work it schedules) to actually run before assertions.
    private fun idle() = shadowOf(Looper.getMainLooper()).idle()

    private fun launchWithLink(link: String): MainActivity {
        store().connect("https://current.example")
        return Robolectric
            .buildActivity(MainActivity::class.java, viewIntent(link))
            .setup()
            .get()
    }

    @Test
    fun `unknown server shows consent and does nothing until answered`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        assertTrue(latestDialog().isShowing)
        // Nothing loaded, pinned, or persisted pre-consent.
        assertEquals("https://current.example", activity.field("pinnedOrigin"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `consent open loads the origin but persists only on page ready`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        idle()

        assertEquals("https://new.example", activity.field("pinnedOrigin"))
        assertEquals("https://new.example", shadowOf(activity.webView()).lastLoadedUrl)
        assertEquals("/c/$hex", activity.field("pendingNavigatePath"))
        // Not yet a trusted recent: the load hasn't succeeded.
        assertFalse(store().recentServers().any { it.contains("new.example") })
        assertEquals("https://current.example", store().currentServerUrl())

        // Simulate the first successful pinned-origin load.
        invokeOnPageReady(activity, "https://new.example/")
        assertEquals("https://new.example", store().currentServerUrl())
        assertTrue(store().recentServers().contains("https://new.example"))
    }

    @Test
    fun `consent cancel drops the link`() {
        val activity = launchWithLink("omnigent://new.example/c/$hex")
        latestDialog().getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()

        assertEquals("https://current.example", activity.field("pinnedOrigin"))
        assertNull(activity.field("pendingNavigatePath"))
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    @Test
    fun `cold start with no server and unknown link consents instead of redirecting`() {
        // No store().connect — nothing configured.
        val activity =
            Robolectric
                .buildActivity(
                    MainActivity::class.java,
                    viewIntent("omnigent://new.example/c/$hex"),
                ).setup()
                .get()
        assertFalse(activity.isFinishing)
        assertNotNull(ShadowDialog.getLatestDialog())
    }

    @Test
    fun `second link waits for the first consent (FIFO)`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://first.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val first = latestDialog()
        controller.newIntent(viewIntent("omnigent://second.example/c/$hex"))
        // Still the first dialog; the second link is queued, not racing it.
        assertEquals(first, ShadowDialog.getLatestDialog())

        first.getButton(DialogInterface.BUTTON_NEGATIVE).performClick()
        idle()
        // First resolved -> second dequeues and asks.
        val second = ShadowDialog.getLatestDialog() as AlertDialog
        assertTrue(second !== first && second.isShowing)
    }

    @Test
    fun `an exception mid-accept still resets processingDeepLink so later links aren't wedged`() {
        store().connect("https://current.example")
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val realWebView = activity.webView()
        // Force reloadWithNewServer's webView.loadUrl to throw, simulating an
        // unexpected failure partway through accepting consent.
        val throwingWebView =
            object : WebView(activity) {
                override fun loadUrl(url: String): Unit = throw RuntimeException("boom")
            }
        activity.setField("webView", throwingWebView)

        latestDialog().getButton(DialogInterface.BUTTON_POSITIVE).performClick()
        try {
            // AlertDialog's button handler runs on the next looper pump, so
            // the exception surfaces here rather than from performClick().
            idle()
        } catch (_: RuntimeException) {
            // Expected: propagates from loadUrl through resolve() — the
            // finally must have already run finishDeepLink() by this point.
        }

        assertFalse(activity.field("processingDeepLink") as Boolean)

        // Restore a working WebView before continuing — the forced failure
        // above already proved its point; a real load must not also throw.
        activity.setField("webView", realWebView)

        // Queue isn't wedged: a later link still gets asked, not silently dropped.
        controller.newIntent(viewIntent("omnigent://another.example/c/$hex"))
        idle()
        val dialog = ShadowDialog.getLatestDialog() as AlertDialog
        assertTrue(dialog.isShowing)
    }

    @Test
    fun `destroying the activity dismisses an open consent dialog without side effects`() {
        val controller =
            Robolectric.buildActivity(
                MainActivity::class.java,
                viewIntent("omnigent://new.example/c/$hex"),
            )
        val activity = controller.setup().get()
        val dialog = latestDialog()
        assertTrue(dialog.isShowing)

        controller.destroy()

        assertFalse(dialog.isShowing)
        // Dismissal is not an accept: no persistence, no reload was triggered.
        assertFalse(store().recentServers().any { it.contains("new.example") })
    }

    private fun invokeOnPageReady(
        activity: MainActivity,
        url: String,
    ) {
        MainActivity::class
            .java
            .getDeclaredMethod("onPageReady", String::class.java)
            .apply { isAccessible = true }
            .invoke(activity, url)
    }
}
