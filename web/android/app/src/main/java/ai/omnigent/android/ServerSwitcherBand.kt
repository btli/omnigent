package ai.omnigent.android

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

/** Centre within [band], clamping the recovery control fully on-screen. */
fun serverSwitcherLeftMargin(
    containerWidth: Int,
    switcherWidth: Int,
    band: ServerSwitcherBand,
): Int {
    val bandLeft = containerWidth * band.left
    val bandRight = containerWidth * band.right
    val centered = (bandLeft + bandRight) / 2.0 - switcherWidth / 2.0
    return centered
        .toInt()
        .coerceAtMost((containerWidth - switcherWidth).coerceAtLeast(0))
        .coerceAtLeast(0)
}
