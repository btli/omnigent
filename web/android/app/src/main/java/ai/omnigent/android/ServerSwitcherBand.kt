package ai.omnigent.android

import kotlin.math.ceil
import kotlin.math.floor

/** Validated horizontal bounds, normalized to the current WebView width. */
data class ServerSwitcherBand(
    val left: Double,
    val right: Double,
) {
    companion object {
        internal fun from(
            left: Double,
            right: Double,
        ): ServerSwitcherBand? {
            if (!left.isFinite() || !right.isFinite()) return null
            if (left < 0.0 || right > 1.0) return null
            if (left >= right) return null
            return ServerSwitcherBand(left, right)
        }
    }
}

private fun ServerSwitcherBand.pixelBounds(containerWidth: Int): Pair<Int, Int> {
    val width = containerWidth.coerceAtLeast(0)
    val leftPx = ceil(width * left).toInt().coerceIn(0, width)
    val rightPx = floor(width * right).toInt().coerceIn(leftPx, width)
    return leftPx to rightPx
}

fun serverSwitcherBandWidth(
    containerWidth: Int,
    band: ServerSwitcherBand,
): Int {
    val (bandLeft, bandRight) = band.pixelBounds(containerWidth)
    return bandRight - bandLeft
}

/** Centre within [band], preferring its edge when the supplied pill cannot fit. */
fun serverSwitcherLeftMargin(
    containerWidth: Int,
    switcherWidth: Int,
    band: ServerSwitcherBand,
): Int {
    val (bandLeft, bandRight) = band.pixelBounds(containerWidth)
    val centered = (bandLeft + bandRight) / 2.0 - switcherWidth / 2.0
    val maxLeft = bandRight - switcherWidth
    val bandAnchoredLeft =
        if (bandLeft > maxLeft) bandLeft else centered.toInt().coerceIn(bandLeft, maxLeft)
    // A narrow right-edge band should overflow to the left, not beyond the parent.
    return bandAnchoredLeft
        .coerceAtMost((containerWidth - switcherWidth).coerceAtLeast(0))
        .coerceAtLeast(0)
}
