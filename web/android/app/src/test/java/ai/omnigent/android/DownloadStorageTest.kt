package ai.omnigent.android

import android.app.Application
import android.content.ContentProvider
import android.content.ContentValues
import android.database.Cursor
import android.net.Uri
import android.provider.MediaStore
import androidx.test.core.app.ApplicationProvider
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.Shadows.shadowOf
import org.robolectric.annotation.Config
import org.robolectric.shadows.ShadowContentResolver
import java.io.ByteArrayOutputStream

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [35])
class DownloadStorageTest {
    private lateinit var context: Application
    private lateinit var provider: RecordingMediaProvider

    @Before
    fun setUp() {
        context = ApplicationProvider.getApplicationContext()
        provider = RecordingMediaProvider()
        ShadowContentResolver.registerProviderInternal("media", provider)
        shadowOf(context.contentResolver)
            .registerOutputStream(provider.insertedUri, ByteArrayOutputStream())
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
}

private class RecordingMediaProvider : ContentProvider() {
    val insertedUri: Uri = Uri.withAppendedPath(MediaStore.Downloads.EXTERNAL_CONTENT_URI, "42")
    var updateResult = 1
    var updateCalls = 0
    var deleteCalls = 0

    override fun onCreate(): Boolean = true

    override fun insert(
        uri: Uri,
        values: ContentValues?,
    ): Uri = insertedUri

    override fun update(
        uri: Uri,
        values: ContentValues?,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        updateCalls++
        return updateResult
    }

    override fun delete(
        uri: Uri,
        selection: String?,
        selectionArgs: Array<out String>?,
    ): Int {
        deleteCalls++
        return 1
    }

    override fun query(
        uri: Uri,
        projection: Array<out String>?,
        selection: String?,
        selectionArgs: Array<out String>?,
        sortOrder: String?,
    ): Cursor? = null

    override fun getType(uri: Uri): String? = null
}
