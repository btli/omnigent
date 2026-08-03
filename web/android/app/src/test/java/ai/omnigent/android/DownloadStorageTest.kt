package ai.omnigent.android

import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.MatrixCursor
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.ByteArrayOutputStream
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DownloadStorageTest {
    private lateinit var context: Application
    private lateinit var provider: RecordingMediaProvider
    private lateinit var output: ByteArrayOutputStream

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        context
            .getSharedPreferences(MEDIA_STORE_JOURNAL, Context.MODE_PRIVATE)
            .edit()
            .clear()
            .commit()
        provider = RecordingMediaProvider()
        output = ByteArrayOutputStream()
        ShadowContentResolver.registerProviderInternal("media", provider)
        shadowOf(context.contentResolver).registerOutputStream(provider.insertedUri, output)
    }

    @After
    fun tearDown() {
        ShadowContentResolver.reset()
    }

    @Test
    fun `failed MediaStore publish reports failure and deletes the pending row`() {
        provider.updateResult = 0

        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { output ->
                output.write("report".toByteArray())
            }

        assertFalse(result.saved)
        assertEquals(1, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
    }

    @Test
    fun `failed MediaStore write deletes the pending row without publishing it`() {
        val result =
            DownloadStorage(context).save("report.txt", "text/plain") {
                error("write failed")
            }

        assertFalse(result.saved)
        assertEquals(0, provider.updateCalls)
        assertEquals(1, provider.deleteCalls)
    }

    @Test
    fun `successful MediaStore save inserts metadata writes and publishes the row`() {
        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { stream ->
                stream.write("report".toByteArray())
            }

        assertTrue(result.saved)
        assertEquals(
            "report.txt",
            provider.insertedValues?.getAsString(MediaStore.Downloads.DISPLAY_NAME),
        )
        assertEquals(
            "text/plain",
            provider.insertedValues?.getAsString(MediaStore.Downloads.MIME_TYPE),
        )
        assertEquals(1, provider.insertedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING))
        assertEquals(0, provider.updatedValues?.getAsInteger(MediaStore.Downloads.IS_PENDING))
        assertEquals("report", output.toString(Charsets.UTF_8.name()))
        assertEquals(0, provider.deleteCalls)
    }

    @Test
    fun `rerun of one operation reuses its published MediaStore row`() {
        val storage = DownloadStorage(context)
        var writes = 0

        val first =
            storage.save("report.txt", "text/plain", operationId = "same-work") { stream ->
                writes++
                stream.write("report".toByteArray())
            }
        val rerun =
            storage.save("report.txt", "text/plain", operationId = "same-work") {
                writes++
            }

        assertTrue(first.saved)
        assertTrue(rerun.saved)
        assertEquals(1, provider.insertCalls)
        assertEquals(1, writes)
    }

    @Test
    fun `orphaned pending MediaStore row is swept before the next save`() {
        provider.orphanedPendingIds += 99L

        val result =
            DownloadStorage(context).save("report.txt", "text/plain") { stream ->
                stream.write("report".toByteArray())
            }

        assertTrue(result.saved)
        assertTrue(
            provider.deletedUris.contains(
                Uri.parse("content://media/external/downloads/99"),
            ),
        )
    }

    @Test
    fun `safe file names strip paths replace illegal characters and fall back for dots`() {
        val storage = DownloadStorage(context)

        assertEquals("passwd", storage.failed("../../etc/passwd").name)
        assertEquals("bar.txt", storage.failed("foo\\bar.txt").name)
        assertEquals("bad_name_.txt", storage.failed("bad:name?.txt").name)
        listOf(".", "..", "", "   ").forEach { suggested ->
            val fallback = storage.failed(suggested).name
            assertTrue(fallback.startsWith("omnigent-"))
            assertTrue(fallback != "." && fallback != "..")
        }
    }

    @Test
    @Config(sdk = [28])
    fun `failed app storage write preserves an existing file and deletes its temporary`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val target = File(dir, "existing-download.txt")
        val temporaryFilesBefore =
            dir.listFiles().orEmpty().filter(File::isTemporaryDownload).map(File::getName).toSet()
        target.writeText("complete original")

        try {
            val result =
                DownloadStorage(context).save(target.name, "text/plain") { stream ->
                    stream.write("partial".toByteArray())
                    error("network failed")
                }

            assertFalse(result.saved)
            assertEquals("complete original", target.readText())
            assertEquals(
                temporaryFilesBefore,
                dir
                    .listFiles()
                    .orEmpty()
                    .filter(File::isTemporaryDownload)
                    .map(File::getName)
                    .toSet(),
            )
        } finally {
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `same name saves are serialized across storage instances`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val target = File(dir, "concurrent-download.txt")
        val firstEntered = CountDownLatch(1)
        val releaseFirst = CountDownLatch(1)
        val secondEntered = CountDownLatch(1)
        val executor = Executors.newFixedThreadPool(2)

        try {
            val first =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(target.name, "text/plain") { stream ->
                        stream.write("first ".toByteArray())
                        firstEntered.countDown()
                        check(releaseFirst.await(5, TimeUnit.SECONDS))
                        stream.write("complete".toByteArray())
                    }
                }
            assertTrue(firstEntered.await(5, TimeUnit.SECONDS))
            val second =
                executor.submit<DownloadSaveResult> {
                    DownloadStorage(context).save(target.name, "text/plain") { stream ->
                        secondEntered.countDown()
                        stream.write("second complete".toByteArray())
                    }
                }

            assertFalse(secondEntered.await(200, TimeUnit.MILLISECONDS))
            releaseFirst.countDown()

            assertTrue(first.get(5, TimeUnit.SECONDS).saved)
            assertTrue(second.get(5, TimeUnit.SECONDS).saved)
            assertEquals("second complete", target.readText())
        } finally {
            releaseFirst.countDown()
            executor.shutdownNow()
            target.delete()
        }
    }

    @Test
    @Config(sdk = [28])
    fun `app storage save sweeps a stale temporary from a dead process`() {
        val dir = checkNotNull(context.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS))
        val stale = File(dir, ".omnigent-dead-process.tmp")
        val target = File(dir, "fresh-download.txt")
        stale.writeText("partial")

        try {
            val result =
                DownloadStorage(context).save(target.name, "text/plain") { stream ->
                    stream.write("complete".toByteArray())
                }

            assertTrue(result.saved)
            assertFalse(stale.exists())
            assertEquals("complete", target.readText())
        } finally {
            stale.delete()
            target.delete()
        }
    }

    private companion object {
        const val MEDIA_STORE_JOURNAL = "ai.omnigent.android.download_media_store"
    }
}

private fun File.isTemporaryDownload(): Boolean =
    name.startsWith(".omnigent-") && name.endsWith(".tmp")

private class RecordingMediaProvider : ContentProvider() {
    // Literal rather than MediaStore.Downloads.EXTERNAL_CONTENT_URI: that constant
    // is null below Q, and the pre-Q cases build this provider too.
    val insertedUri: Uri = Uri.parse("content://media/external/downloads/42")
    var insertedValues: ContentValues? = null
    var updatedValues: ContentValues? = null
    var updateResult = 1
    var insertCalls = 0
    var updateCalls = 0
    var deleteCalls = 0
    var rowExists = false
    var pending = 1
    val orphanedPendingIds = mutableListOf<Long>()
    val deletedUris = mutableListOf<Uri>()

    override fun onCreate(): Boolean = true

    override fun insert(
        uri: Uri,
        values: ContentValues?,
    ): Uri {
        insertCalls++
        rowExists = true
        pending = 1
        insertedValues = values?.let { ContentValues(it) }
        return insertedUri
    }

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        updateCalls++
        updatedValues = values?.let { ContentValues(it) }
        if (updateResult > 0) pending = values?.getAsInteger(MediaStore.Downloads.IS_PENDING) ?: 0
        return updateResult
    }

    override fun delete(
        uri: Uri,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        deleteCalls++
        deletedUris += uri
        if (uri == insertedUri) rowExists = false
        orphanedPendingIds.remove(uri.lastPathSegment?.toLongOrNull())
        return 1
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? {
        val columns = projection ?: emptyArray()
        val cursor = MatrixCursor(columns)
        if (uri == insertedUri) {
            if (rowExists) {
                cursor.addRow(
                    columns.map { column ->
                        when (column) {
                            MediaStore.Downloads.IS_PENDING -> pending
                            MediaStore.Downloads._ID -> 42L
                            else -> null
                        }
                    },
                )
            }
            return cursor
        }
        if (uri == MediaStore.Downloads.EXTERNAL_CONTENT_URI) {
            orphanedPendingIds.forEach { id ->
                cursor.addRow(
                    columns.map { column ->
                        if (column == MediaStore.Downloads._ID) id else null
                    },
                )
            }
            return cursor
        }
        return cursor
    }

    override fun getType(uri: Uri): String? = null
}
