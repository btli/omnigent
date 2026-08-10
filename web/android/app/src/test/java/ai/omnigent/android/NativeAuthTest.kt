package ai.omnigent.android

import android.content.Context
import android.content.Intent
import android.net.Uri
import androidx.test.core.app.ApplicationProvider
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
    fun `valid code callback parses`() {
        val grant =
            NativeAuth.parseCodeCallback(
                Uri.parse(
                    "omnigent://auth-callback?state=abc12345&code=one-time_Code&exchange=tab",
                ),
            )

        assertNotNull(grant)
        assertEquals("abc12345", grant!!.state)
        assertEquals("one-time_Code", grant.code)
        assertEquals(NativeAuth.EXCHANGE_TAB, grant.exchange)
    }

    @Test
    fun `code callback with unknown exchange transport is rejected`() {
        assertNull(
            NativeAuth.parseCodeCallback(
                Uri.parse(
                    "omnigent://auth-callback?state=abc12345&code=c0de&exchange=carrier-pigeon",
                ),
            ),
        )
    }

    @Test
    fun `code callback never parses as a token callback and vice versa`() {
        val codeUri = Uri.parse("omnigent://auth-callback?state=abc12345&code=c0de&exchange=post")
        val tokenUri =
            Uri.parse(
                "omnigent://auth-callback?state=abc12345&token_type=session&token=$JWT",
            )

        assertNull(NativeAuth.parseTokenCallback(codeUri))
        assertNull(NativeAuth.parseCodeCallback(tokenUri))
    }

    @Test
    fun `valid session and bearer token callbacks parse`() {
        val session =
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=session&token=$JWT"),
            )
        val bearer =
            NativeAuth.parseTokenCallback(
                Uri.parse(
                    "omnigent://auth-callback?state=abc12345&token_type=bearer&token=tok-123",
                ),
            )

        assertEquals(NativeAuth.TOKEN_TYPE_SESSION, session!!.tokenType)
        assertEquals(JWT, session.token)
        assertEquals(NativeAuth.TOKEN_TYPE_BEARER, bearer!!.tokenType)
    }

    @Test
    fun `wrong scheme or host is rejected`() {
        val query = "state=abc12345&token_type=session&token=$JWT"
        assertNull(NativeAuth.parseTokenCallback(Uri.parse("https://auth-callback?$query")))
        assertNull(NativeAuth.parseTokenCallback(Uri.parse("omnigent://evil-host?$query")))
        // A deep link on the shared scheme must never parse as a login.
        assertNull(NativeAuth.parseTokenCallback(Uri.parse("omnigent://example.com/c/abc?$query")))
        assertNull(NativeAuth.parseTokenCallback(null))
        assertNull(NativeAuth.parseCodeCallback(null))
    }

    @Test
    fun `missing or empty fields are rejected`() {
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?token_type=session&token=$JWT"),
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?state=&token_type=session&token=$JWT"),
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token=$JWT"),
            ),
        )
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=session&token="),
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&exchange=tab"),
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&code=&exchange=tab"),
            ),
        )
        assertNull(
            NativeAuth.parseCodeCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&code=c0de"),
            ),
        )
    }

    @Test
    fun `server error report parses as neither shape`() {
        // error=no_token / error=exchange_failed carry no grant; callers fall back.
        val uri = Uri.parse("omnigent://auth-callback?state=abc12345&error=no_token")
        assertNull(NativeAuth.parseCodeCallback(uri))
        assertNull(NativeAuth.parseTokenCallback(uri))
    }

    @Test
    fun `unknown token type is rejected`() {
        assertNull(
            NativeAuth.parseTokenCallback(
                Uri.parse("omnigent://auth-callback?state=abc12345&token_type=refresh&token=$JWT"),
            ),
        )
    }

    @Test
    fun `token with header-breaking characters is rejected`() {
        for (token in listOf("a b", "a;b", "a\"b", "a\nb", "a,b", "日本")) {
            assertNull(
                NativeAuth.parseTokenCallback(
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
    fun `code challenge derivation matches the RFC 7636 vector`() {
        // RFC 7636 Appendix B.
        assertEquals(
            "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
            NativeAuth.deriveCodeChallenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"),
        )
    }

    @Test
    fun `completion and exchange urls target the pinned origin`() {
        val completion =
            NativeAuth.completionUrl("https://x.databricksapps.com", "st4te-abc", "chall")
        assertEquals(
            "https://x.databricksapps.com/auth/native-complete?state=st4te-abc&code_challenge=chall",
            completion.toString(),
        )

        val exchange =
            NativeAuth.exchangeUrl(
                "https://x.databricksapps.com",
                NativeAuth.CodeGrant("st4te-abc", "c0de", NativeAuth.EXCHANGE_TAB),
                "v3rifier",
            )
        assertEquals(
            "https://x.databricksapps.com/auth/native-exchange" +
                "?code=c0de&state=st4te-abc&code_verifier=v3rifier",
            exchange.toString(),
        )
    }

    @Test
    fun `no activity claims the callback as a VIEW intent`() {
        // Callbacks must only ever arrive through the Auth Tab's
        // Activity-result channel; a manifest VIEW handler would be a
        // squattable delivery path (and once wedged a login permanently
        // when fed a mismatched state).
        val context = ApplicationProvider.getApplicationContext<Context>()
        val intent =
            Intent(
                Intent.ACTION_VIEW,
                Uri.parse("omnigent://auth-callback?state=x&code=c&exchange=tab"),
            )

        val handlers =
            context.packageManager
                .queryIntentActivities(intent, 0)
                .filter { it.activityInfo?.packageName == context.packageName }

        assertTrue(handlers.isEmpty())
    }

    private companion object {
        // Any three base64url segments — the parser checks shape-safety, not validity.
        const val JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhIn0.c2ln"
    }
}
