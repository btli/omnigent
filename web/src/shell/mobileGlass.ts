// Shared iOS-style "liquid glass" treatment for the mobile chat header.
//
// The header paints no background and chat scrolls underneath it, so its
// controls — and the menu they open — sit on their own translucent, blurred
// surface, the way native iOS floating chrome does. All classes are `max-md:`
// so the desktop header, which sits above the conversation rather than over
// it, is untouched.

/** Translucent blurred surface: controls and the menus they open. */
export const MOBILE_GLASS_SURFACE =
  "max-md:border max-md:border-black/[0.06] max-md:bg-background/70 max-md:shadow-[0_6px_20px_-4px_rgb(0_0_0/0.18)] max-md:backdrop-blur-xl max-md:backdrop-saturate-150 dark:max-md:border-white/10 dark:max-md:bg-background/60";

/**
 * A control cluster's floating pill. No padding of its own so a single
 * ``size="icon"`` child stays exactly 40px — the sidebar toggle and the
 * session kebab must read as the same size.
 *
 * The exception is the Chat/Terminal track (ViewModeToggle): it paints its
 * own background out to its edge, so with no padding its ink runs into the
 * pill's rounded cap while an icon-only neighbour clears it by the slack
 * inside its 40px box. When the track is present, inset BOTH ends by that
 * slack and pin the pill to 40px, so the 28px track sits centered with
 * vertical clearance and its ink stays inside the rounded caps whether it is
 * flanked by a kebab or is the cluster's only control (a terminal-first
 * sub-agent). The symmetric inset means the leading child no longer has to be
 * the track.
 */
export const MOBILE_GLASS_PILL = `${MOBILE_GLASS_SURFACE} max-md:rounded-full max-md:has-data-[slot=view-mode-toggle]:h-10 max-md:has-data-[slot=view-mode-toggle]:px-1.5`;
