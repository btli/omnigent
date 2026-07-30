package ai.omnigent.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test

class ServerSwitcherPositionTest {
    @Test
    fun `valid band retains normalized fractions`() {
        assertEquals(
            ServerSwitcherBand(0.3, 0.8),
            ServerSwitcherBand.from(0.3, 0.8),
        )
    }

    @Test
    fun `invalid bands are rejected`() {
        assertNull(ServerSwitcherBand.from(Double.NaN, 0.8))
        assertNull(ServerSwitcherBand.from(0.2, Double.POSITIVE_INFINITY))
        assertNull(ServerSwitcherBand.from(-0.1, 0.8))
        assertNull(ServerSwitcherBand.from(0.2, 1.1))
        assertNull(ServerSwitcherBand.from(0.5, 0.5))
        assertNull(ServerSwitcherBand.from(0.8, 0.2))
    }

    @Test
    fun `pill is centered within the published band`() {
        assertEquals(
            470,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.3, 0.8),
            ),
        )
    }

    @Test
    fun `pill is clamped fully on screen at either edge`() {
        assertEquals(
            0,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.0, 0.05),
            ),
        )
        assertEquals(
            840,
            serverSwitcherLeftMargin(
                containerWidth = 1000,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.95, 1.0),
            ),
        )
    }

    @Test
    fun `pill wider than the container remains anchored on screen`() {
        assertEquals(
            0,
            serverSwitcherLeftMargin(
                containerWidth = 100,
                switcherWidth = 160,
                band = ServerSwitcherBand(0.2, 0.8),
            ),
        )
    }
}
