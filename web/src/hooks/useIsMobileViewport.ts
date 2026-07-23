// Reactive "is this a mobile-width viewport?" hook.
//
// The shell's responsive layout pivots on Tailwind's `md` breakpoint
// (`min-width: 768px`), used both as CSS classes (`md:` / `max-md:`) and as
// the JS threshold in AppShell's `initialSidebarOpen`. This hook exposes the
// `max-md` side of that line to component logic that can't be expressed in
// CSS alone (e.g. swapping a hover flyout for an in-place page on touch).

import { useSyncExternalStore } from "react";

// The EXACT logical complement of Tailwind's `md` (desktop) boundary
// `(min-width: 768px)` — one source of truth for both sides of the line, so
// CSS visibility (`md:` classes flip at >= 768px) and this JS gate flip at the
// identical point. The prior `(max-width: 767.98px)` left a gap in the open
// interval (767.98, 768): fractional widths reachable under browser zoom /
// display scaling where the rail was CSS-hidden (< 768) but the hook still read
// desktop (> 767.98), so a hidden rail could keep a live PTY while the mobile
// surface opened another. `not all and (min-width: 768px)` is true precisely
// when `(min-width: 768px)` is false, closing the gap with no rounding slack.
const MOBILE_QUERY = "not all and (min-width: 768px)";

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const mql = window.matchMedia(MOBILE_QUERY);
  mql.addEventListener("change", callback);
  return () => mql.removeEventListener("change", callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MOBILE_QUERY).matches;
}

/**
 * True when the viewport is narrower than Tailwind's `md` breakpoint (768px)
 * — i.e. the "mobile" layout the shell's `max-md:` classes target. Reactive:
 * components re-render when the viewport crosses the breakpoint. SSR-safe
 * (returns `false` on the server, matching `initialSidebarOpen`).
 */
export function useIsMobileViewport(): boolean {
  return useSyncExternalStore(subscribe, getSnapshot, () => false);
}
