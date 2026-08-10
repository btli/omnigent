package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class AuthTabSupportTest {
    @Test
    fun `launch intent pins the resolved provider package`() {
        val intent = AuthTabSupport.launchIntent(PROVIDER_PACKAGE).intent

        assertEquals(PROVIDER_PACKAGE, intent.`package`)
    }

    @Test
    fun `a null provider disables auth tab without probing support`() {
        var supportChecked = false

        val provider =
            AuthTabSupport.supportedProviderPackage(null) {
                supportChecked = true
                true
            }

        assertNull(provider)
        assertFalse(supportChecked)
    }

    private companion object {
        const val PROVIDER_PACKAGE = "com.android.chrome"
    }
}
