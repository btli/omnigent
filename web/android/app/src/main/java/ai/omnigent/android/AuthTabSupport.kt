package ai.omnigent.android

import android.content.Context
import androidx.browser.customtabs.CustomTabsClient

/**
 * Whether the device's Custom Tabs provider implements the Auth Tab
 * surface (Chrome 132+). Gates the redirect-based login flow: only an
 * Auth Tab verifies the HTTPS callback's Digital Asset Links and returns
 * it through an Activity result. Without support, or when verification
 * fails, the shell keeps the in-WebView login flow instead; it never
 * downgrades to a custom-scheme callback.
 */
object AuthTabSupport {
    fun isSupported(context: Context): Boolean {
        val provider = CustomTabsClient.getPackageName(context, null) ?: return false
        return runCatching { CustomTabsClient.isAuthTabSupported(context, provider) }
            .getOrDefault(false)
    }
}
