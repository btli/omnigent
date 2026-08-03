package ai.omnigent.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.os.Build
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import java.util.UUID

/** Background-safe completion notifications for durable downloads. */
internal class DownloadNotificationManager(
    private val context: Context,
) {
    private val manager = NotificationManagerCompat.from(context)

    init {
        manager.createNotificationChannel(
            NotificationChannel(
                CHANNEL_ID,
                context.getString(R.string.download_notification_channel_name),
                NotificationManager.IMPORTANCE_DEFAULT,
            ),
        )
    }

    fun succeeded(
        name: String,
        workId: UUID,
    ) {
        val body =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                context.getString(R.string.download_complete_body_downloads, name)
            } else {
                context.getString(R.string.download_complete_body_app_storage, name)
            }
        post(
            workId,
            context.getString(R.string.download_complete_title),
            body,
        )
    }

    fun failed(
        name: String,
        workId: UUID,
    ) {
        post(
            workId,
            context.getString(R.string.download_failed_title),
            context.getString(R.string.download_failed_body, name),
        )
    }

    private fun post(
        workId: UUID,
        title: String,
        body: String,
    ) {
        if (!manager.areNotificationsEnabled()) return
        val notification =
            NotificationCompat
                .Builder(context, CHANNEL_ID)
                .setSmallIcon(R.drawable.ic_notification)
                .setContentTitle(title)
                .setContentText(body)
                .setCategory(Notification.CATEGORY_STATUS)
                .setAutoCancel(true)
                .build()
        try {
            manager.notify(notificationTag(workId), NOTIFICATION_ID, notification)
        } catch (_: SecurityException) {
            // POST_NOTIFICATIONS can be revoked while background work is running.
        }
    }

    companion object {
        internal const val CHANNEL_ID = "omnigent.downloads"
        internal const val NOTIFICATION_ID = 0
        private const val NOTIFICATION_TAG_PREFIX = "ai.omnigent.android.download."

        internal fun notificationTag(workId: UUID): String = NOTIFICATION_TAG_PREFIX + workId
    }
}
