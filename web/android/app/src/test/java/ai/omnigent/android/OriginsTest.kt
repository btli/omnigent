package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class OriginsTest {
    @Test
    fun `origin strips credentials and canonicalizes case and ports`() {
        val cases =
            mapOf(
                "https://pin@evil.com/x" to "https://evil.com",
                "https://good.com:443/x" to "https://good.com",
                "http://good.com:80/x" to "http://good.com",
                "HTTPS://GOOD.com/x" to "https://good.com",
                "https://a:b@good.com:8443" to "https://good.com:8443",
                "https://good.com./x" to "https://good.com.",
            )

        cases.forEach { (url, expected) -> assertEquals(url, expected, originOf(url)) }
        assertNull(originOf("https:foo"))
        // WHATWG: a non-numeric port is a parse failure, not a strippable extra.
        assertNull(originOf("https://good.com:notaport"))
    }

    @Test
    fun `unicode and punycode hosts have the same origin`() {
        assertEquals(
            originOf("https://xn--r8jz45g.jp"),
            originOf("https://例え.jp"),
        )
    }

    @Test
    fun `server input is normalized or rejected`() {
        assertEquals("https://example.com", normalizeServerUrl("  example.com/  "))
        assertEquals("HTTP://GOOD.com/path", normalizeServerUrl("HTTP://GOOD.com/path/"))
        assertNull(normalizeServerUrl(""))
        assertNull(normalizeServerUrl("ftp://example.com"))
        assertNull(normalizeServerUrl("https://good.com/bad path"))
        assertNull(normalizeServerUrl("https:///missing-host"))
    }

    @Test
    fun `http scheme matching is case insensitive and exact`() {
        assertTrue(isHttpScheme("HTTP"))
        assertTrue(isHttpScheme("Https"))
        assertFalse(isHttpScheme("httpfoo"))
        assertFalse(isHttpScheme("mailto"))
        assertFalse(isHttpScheme(null))
    }

    @Test
    fun `front-door proxy authorize url is detected`() {
        // Databricks-Apps-style: every path is intercepted and bounced to the
        // host's IdP with a redirect_uri returning to the pinned origin on the
        // proxy's own callback path.
        assertTrue(
            isProxyAuthUrl(
                "https://idp.example.com/oidc/oauth2/v2.0/authorize" +
                    "?client_id=abc&response_type=code" +
                    "&redirect_uri=https%3A%2F%2Fapp.example.com%2F.auth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `own oidc bounce is not a proxy url`() {
        // The app's own IdP bounce uses the server's /auth/callback — that flow
        // must keep going through the system browser, not load inline.
        assertFalse(
            isProxyAuthUrl(
                "https://accounts.google.com/o/oauth2/v2/auth" +
                    "?client_id=abc&redirect_uri=https%3A%2F%2Fapp.example.com%2Fauth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `own oidc bounce with trailing slash is not a proxy url`() {
        assertFalse(
            isProxyAuthUrl(
                "https://accounts.google.com/o/oauth2/v2/auth" +
                    "?client_id=abc&redirect_uri=https%3A%2F%2Fapp.example.com%2Fauth%2Fcallback%2F",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `redirect_uri to a foreign origin is not a proxy url`() {
        assertFalse(
            isProxyAuthUrl(
                "https://idp.example.com/authorize" +
                    "?redirect_uri=https%3A%2F%2Fother.example.com%2F.auth%2Fcallback",
                "https://app.example.com",
            ),
        )
    }

    @Test
    fun `url without redirect_uri is not a proxy url`() {
        assertFalse(isProxyAuthUrl("https://idp.example.com/login", "https://app.example.com"))
    }

    @Test
    fun `null inputs are not a proxy url`() {
        assertFalse(isProxyAuthUrl(null, "https://app.example.com"))
        assertFalse(
            isProxyAuthUrl(
                "https://idp.example.com/authorize" +
                    "?redirect_uri=https%3A%2F%2Fapp.example.com%2F.auth%2Fcallback",
                null,
            ),
        )
    }

    @Test
    fun `opaque uri does not crash the classifier`() {
        // Uri.getQueryParameter throws on opaque (non-hierarchical) URIs.
        assertFalse(isProxyAuthUrl("mailto:someone@example.com", "https://app.example.com"))
    }
}
