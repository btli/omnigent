package ai.omnigent.android

import android.content.Context
import androidx.browser.customtabs.CustomTabsClient

/**
 * Whether the device's Custom Tabs provider implements the Auth Tab
 * surface (Chrome 132+). Gates the redirect-based login flow: only an
 * Auth Tab returns the completion redirect through an Activity result —
 * a plain Custom Tab would broadcast it as a VIEW intent any app
 * claiming the scheme could receive — so without support the shell keeps
 * the in-WebView login flow instead.
 */
object AuthTabSupport {
    fun isSupported(context: Context): Boolean {
        val provider = CustomTabsClient.getPackageName(context, null) ?: return false
        return runCatching { CustomTabsClient.isAuthTabSupported(context, provider) }
            .getOrDefault(false)
    }
}
