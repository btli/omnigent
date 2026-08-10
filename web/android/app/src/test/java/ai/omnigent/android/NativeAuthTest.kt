package ai.omnigent.android

import android.net.Uri
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner

@RunWith(RobolectricTestRunner::class)
class NativeAuthTest {
    @Test
    fun `valid session callback parses`() {
        val result =
            NativeAuth.parseCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=session&token=$JWT"),
            )

        assertNotNull(result)
        assertEquals("abc12345", result!!.state)
        assertEquals(NativeAuth.TOKEN_TYPE_SESSION, result.tokenType)
        assertEquals(JWT, result.token)
    }

    @Test
    fun `valid bearer callback parses`() {
        val result =
            NativeAuth.parseCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=bearer&token=tok-123"),
            )

        assertNotNull(result)
        assertEquals(NativeAuth.TOKEN_TYPE_BEARER, result!!.tokenType)
        assertEquals("tok-123", result.token)
    }

    @Test
    fun `wrong scheme or host is rejected`() {
        val query = "state=abc12345&token_type=session&token=$JWT"
        assertNull(NativeAuth.parseCallback(Uri.parse("https://auth-callback?$query")))
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://evil-host?$query")))
        // A deep link on the shared scheme must never parse as a login.
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://example.com/c/abc?$query")))
        assertNull(NativeAuth.parseCallback(null))
    }

    @Test
    fun `scheme and host match case-insensitively`() {
        val result =
            NativeAuth.parseCallback(
                Uri.parse("OMNIGENT://AUTH-CALLBACK?state=abc12345&token_type=bearer&token=tok"),
            )

        assertNotNull(result)
    }

    @Test
    fun `missing or empty fields are rejected`() {
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?token_type=session&token=$JWT")))
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?state=&token_type=session&token=$JWT")))
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?state=abc12345&token=$JWT")))
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?state=abc12345&token_type=session")))
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?state=abc12345&token_type=session&token=")))
    }

    @Test
    fun `server error report parses as null`() {
        // error=no_token carries no credential; the caller falls back.
        assertNull(NativeAuth.parseCallback(Uri.parse("omnigent://auth-callback?state=abc12345&error=no_token")))
    }

    @Test
    fun `unknown token type is rejected`() {
        assertNull(
            NativeAuth.parseCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=refresh&token=$JWT"),
            ),
        )
    }

    @Test
    fun `token with header-breaking characters is rejected`() {
        for (token in listOf("a b", "a;b", "a\"b", "a\nb", "a,b", "日本")) {
            assertNull(
                NativeAuth.parseCallback(
                    Uri
                        .parse("omnigent://auth-callback")
                        .buildUpon()
                        .appendQueryParameter("state", "abc12345")
                        .appendQueryParameter("token_type", "bearer")
                        .appendQueryParameter("token", token)
                        .build(),
                ),
            )
        }
    }

    @Test
    fun `token68 alphabet is accepted`() {
        assertTrue(NativeAuth.isTokenSafe(JWT))
        assertTrue(NativeAuth.isTokenSafe("dapi+abc/def=="))
        assertFalse(NativeAuth.isTokenSafe(""))
    }

    @Test
    fun `completion url targets the pinned origin`() {
        val url = NativeAuth.completionUrl("https://x.databricksapps.com", "st4te-abc")

        assertEquals(
            "https://x.databricksapps.com/auth/native-complete?state=st4te-abc",
            url.toString(),
        )
    }

    private companion object {
        // Any three base64url segments — the parser checks shape-safety, not validity.
        const val JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.c2ln"
    }
}
