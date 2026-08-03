package ai.omnigent.android

import android.app.Application
import android.app.Notification
import android.app.NotificationManager
import android.content.Context
import androidx.test.core.app.ApplicationProvider
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.shadows.ShadowNotificationManager
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
class DownloadNotificationManagerTest {
    private lateinit var context: Application
    private lateinit var shadow: ShadowNotificationManager

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        ShadowNotificationManager.reset()
        context
            .getSharedPreferences(NOTIFICATION_PREFS, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        shadow =
            shadowOf(
                context.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager,
            )
    }

    @Test
    fun `download notifications are isolated from session activation and ids`() {
        val sessions = NativeNotificationManager(context, ORIGIN)
        sessions.notify("Session ready", null, "/c/session")
        val notificationIds =
            context.getSharedPreferences(NOTIFICATION_PREFS, Context.MODE_PRIVATE)
        val nextSessionId = notificationIds.getInt(KEY_NEXT_NOTIFICATION_ID, -1)
        val workId = UUID.randomUUID()

        DownloadNotificationManager(context).succeeded("report.pdf", workId)

        val download =
            shadow.getNotification(
                DownloadNotificationManager.notificationTag(workId),
                DownloadNotificationManager.NOTIFICATION_ID,
            )
        assertNotNull(download)
        assertEquals(DownloadNotificationManager.CHANNEL_ID, download!!.channelId)
        assertEquals(
            "Download complete",
            download.extras.getCharSequence(Notification.EXTRA_TITLE),
        )
        assertNull(download.contentIntent)
        assertEquals(nextSessionId, notificationIds.getInt(KEY_NEXT_NOTIFICATION_ID, -1))
        val session = shadow.getNotification(FIRST_SESSION_NOTIFICATION_ID)
        assertNotNull(session)
        assertNotEquals(session!!.channelId, download.channelId)

        sessions.cancelAll()

        assertNull(shadow.getNotification(FIRST_SESSION_NOTIFICATION_ID))
        assertNotNull(
            shadow.getNotification(
                DownloadNotificationManager.notificationTag(workId),
                DownloadNotificationManager.NOTIFICATION_ID,
            ),
        )
    }

    private companion object {
        const val ORIGIN = "https://example.com"
        const val FIRST_SESSION_NOTIFICATION_ID = 2
        const val NOTIFICATION_PREFS = "ai.omnigent.android.notifications"
        const val KEY_NEXT_NOTIFICATION_ID = "next_notification_id"
    }
}
