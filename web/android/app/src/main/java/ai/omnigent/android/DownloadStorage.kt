package ai.omnigent.android

import android.content.ContentValues
import android.content.Context
import android.os.Build
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.provider.MediaStore
import android.widget.Toast
import androidx.annotation.RequiresApi
import java.io.File
import java.io.OutputStream

internal data class DownloadSaveResult(
    val name: String,
    val saved: Boolean,
)

/** Shared destination, filename, and user-notification handling for downloaded files. */
internal class DownloadStorage(context: Context) {
    private val context = context.applicationContext ?: context
    private val main = Handler(Looper.getMainLooper())

    fun save(
        suggestedName: String,
        mimeType: String,
        write: (OutputStream) -> Unit,
    ): DownloadSaveResult {
        val name = safeFileName(suggestedName)
        val saved =
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                saveViaMediaStore(name, mimeType, write)
            } else {
                saveToAppDownloads(name, write)
            }
        return DownloadSaveResult(name, saved)
    }

    fun failed(suggestedName: String): DownloadSaveResult =
        DownloadSaveResult(safeFileName(suggestedName), false)

    fun report(result: DownloadSaveResult) {
        main.post {
            val message =
                if (result.saved) {
                    "Saved ${result.name} to Downloads"
                } else {
                    "Couldn't save ${result.name}"
                }
            Toast.makeText(context, message, Toast.LENGTH_SHORT).show()
        }
    }

    @RequiresApi(Build.VERSION_CODES.Q)
    private fun saveViaMediaStore(
        name: String,
        mimeType: String,
        write: (OutputStream) -> Unit,
    ): Boolean {
        val resolver = context.contentResolver
        val pendingValues =
            ContentValues().apply {
                put(MediaStore.Downloads.DISPLAY_NAME, name)
                put(MediaStore.Downloads.MIME_TYPE, mimeType)
                put(MediaStore.Downloads.IS_PENDING, 1)
            }
        val uri =
            runCatching {
                resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, pendingValues)
            }.getOrNull() ?: return false

        val wrote =
            runCatching {
                val output = resolver.openOutputStream(uri) ?: error("No output stream")
                output.use(write)
            }.isSuccess
        if (!wrote) {
            runCatching { resolver.delete(uri, null, null) }
            return false
        }

        val publishValues = ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }
        val published =
            runCatching {
                resolver.update(uri, publishValues, null, null) > 0
            }.getOrDefault(false)
        if (!published) runCatching { resolver.delete(uri, null, null) }
        return published
    }

    private fun saveToAppDownloads(
        name: String,
        write: (OutputStream) -> Unit,
    ): Boolean =
        runCatching {
            val dir =
                context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)
                    ?: context.filesDir
            File(dir, name).outputStream().use(write)
            true
        }.getOrDefault(false)

    private fun safeFileName(suggested: String): String {
        // Treat both separator styles as paths before replacing unsafe characters.
        val cleaned =
            suggested
                .substringAfterLast('/')
                .substringAfterLast('\\')
                .replace(Regex("[^A-Za-z0-9._-]"), "_")
        // Dot paths resolve to directories on the pre-Q filesystem destination.
        return if (cleaned.isBlank() || cleaned == "." || cleaned == "..") {
            "omnigent-${System.currentTimeMillis()}"
        } else {
            cleaned
        }
    }
}
