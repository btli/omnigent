package ai.omnigent.android

import android.app.Activity
import android.content.Intent
import android.os.Bundle

/**
 * Disposable-task trampoline for `omnigent://auth-callback` VIEW intents.
 *
 * The Auth Tab returns the completion redirect as an Activity result, so
 * this activity is normally never hit — it exists for the degraded path
 * where the launch fell back to a plain Custom Tab (browser without Auth
 * Tab support) and the redirect arrives as an ordinary deep link. It
 * forwards the URI into the existing [MainActivity] task, where the flow's
 * state-nonce check decides whether it belongs to an in-flight login;
 * unsolicited intents are dropped there.
 */
class AuthRedirectActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val uri = intent?.data
        if (uri != null &&
            uri.scheme?.lowercase() == NativeAuth.SCHEME &&
            uri.host?.lowercase() == NativeAuth.HOST
        ) {
            startActivity(
                Intent(this, MainActivity::class.java)
                    .setData(uri)
                    .addFlags(
                        Intent.FLAG_ACTIVITY_NEW_TASK or
                            Intent.FLAG_ACTIVITY_CLEAR_TOP or
                            Intent.FLAG_ACTIVITY_SINGLE_TOP,
                    ),
            )
        }
        finish()
    }
}
