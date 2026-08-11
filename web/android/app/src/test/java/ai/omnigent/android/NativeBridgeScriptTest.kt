package ai.omnigent.android

import org.junit.Assert.assertTrue
import org.junit.Test

class NativeBridgeScriptTest {
    @Test
    fun `server picker bridge mirrors the Electron method names and payload shape`() {
        val source =
            NativeBridgeScript.source(
                currentOrigin = "https://current.example.com",
                recentServers =
                    listOf(
                        "https://recent.example.com/path",
                        "https://quote.example.com/\"quoted\"",
                    ),
            )

        assertTrue(source.contains("getServerPicker()"))
        assertTrue(source.contains("switchServer(url)"))
        assertTrue(source.contains("openServerSetup()"))
        assertTrue(source.contains("currentOrigin: pickerCurrentOrigin"))
        assertTrue(source.contains("recentServers: [...pickerRecentServers]"))
        assertTrue(source.contains("return Promise.reject(new Error("))
        assertTrue(source.contains("""post({ method: "switchServer", url });"""))
        assertTrue(source.contains("""post({ method: "openServerSetup" });"""))
        assertTrue(source.contains("\"https://current.example.com\""))
        assertTrue(source.contains("\"https://quote.example.com/\\\"quoted\\\"\""))
    }

    @Test
    fun `server switcher visibility bridge sends the iOS payload shape`() {
        val source =
            NativeBridgeScript.source(
                currentOrigin = "https://current.example.com",
                recentServers = emptyList(),
            )

        assertTrue(source.contains("setServerSwitcherHidden(hidden)"))
        assertTrue(
            source.contains(
                """post({ method: "setServerSwitcherHidden", hidden: hidden === true });""",
            ),
        )
    }
}
