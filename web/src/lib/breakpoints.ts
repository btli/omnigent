// Canonical encoding of the shell's `md` layout breakpoint.
//
// Tailwind's `md` breakpoint (768px) is the shell's single mobile/desktop
// layout pivot, used both by CSS (`md:` / `max-md:` classes) and by JS call
// sites. Every JS encoding of that line derives from this module so the
// variants (767px / 767.98px / 768) can't drift apart. Viewport width is a
// LAYOUT concern only — input capability (touch, hover) is a separate axis,
// exposed by `useInputCapabilities`.

export const MD_BREAKPOINT_PX = 768;

/** `(min-width: …)` media query — matches Tailwind's `md:` variant. */
export function minWidthQuery(px: number = MD_BREAKPOINT_PX): string {
  return `(min-width: ${px}px)`;
}

/**
 * `(max-width: …)` media query — matches Tailwind's `max-md:` variant, whose
 * upper bound is exclusive (768 - 0.02 = 767.98px).
 */
export function maxWidthQuery(px: number = MD_BREAKPOINT_PX): string {
  return `(max-width: ${px - 0.02}px)`;
}

/**
 * True on mobile viewports (below the `md` breakpoint). Non-reactive
 * point-in-time check for event handlers; components that must re-render on
 * breakpoint crossings use `useIsMobileViewport` instead. SSR-safe (returns
 * false when window is undefined).
 */
export function isMobileViewport(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return !window.matchMedia(minWidthQuery()).matches;
}

declare global {
  interface Window {
    /** Layout signal consumed by native shells (see publication below). */
    __omnigentIsMobileViewport?: () => boolean;
  }
}

// Native shells (e.g. the Android back handler in NativeBridgeScript.kt)
// consume the web layer's breakpoint signal instead of re-deriving it from
// their own copy of the literal.
if (typeof window !== "undefined") {
  // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
  window.__omnigentIsMobileViewport = isMobileViewport;
}
