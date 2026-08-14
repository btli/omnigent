// Reactive "is this a mobile-width viewport?" hook.
//
// The shell's responsive layout pivots on Tailwind's `md` breakpoint
// (`min-width: 768px`), used both as CSS classes (`md:` / `max-md:`) and as
// the JS threshold in AppShell's `initialSidebarOpen`. This hook exposes the
// `max-md` side of that line to component logic that can't be expressed in
// CSS alone (e.g. swapping a hover flyout for an in-place page on touch).

import { useSyncExternalStore } from "react";

import { MD_MAX_WIDTH_QUERY, subscribeMatchMedia } from "@/lib/breakpoints";

function subscribe(callback: () => void): () => void {
  return subscribeMatchMedia([MD_MAX_WIDTH_QUERY], callback);
}

function getSnapshot(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia(MD_MAX_WIDTH_QUERY).matches;
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
