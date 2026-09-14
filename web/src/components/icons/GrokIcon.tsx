// Import the Mono glyph directly, not the package index. The barrel default
// attaches `.Color`/`.Avatar`/`.Combine` statics that pull in antd; we only
// render the monochrome `currentColor` glyph, so the subpath keeps antd out of
// the bundle. Same rationale as KimiIcon.
import Grok from "@lobehub/icons/es/Grok/components/Mono";

export const GrokIcon = Grok;
