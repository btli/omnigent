// Single source of truth for pointer/input capability.
//
// Capability gates AFFORDANCES and defaults only (hit-target sizing,
// persistent vs hover-revealed controls, swipe hints) — never per-event
// handling. Gesture recognition must instead branch on the active sequence's
// `PointerEvent.pointerType`, so a touch on a fine-primary laptop still gets
// gesture semantics regardless of what these queries report. Viewport width
// is an independent LAYOUT axis — see `@/lib/breakpoints`.

import { useSyncExternalStore } from "react";

export interface InputCapabilities {
  /** Primary pointer is coarse — `(pointer: coarse)`. */
  coarsePrimary: boolean;
  /** ANY attached pointer is coarse — `(any-pointer: coarse)`. */
  anyCoarse: boolean;
  /** Primary pointer can hover — `(hover: hover)`. */
  hoverPrimary: boolean;
  /** A touch digitizer is present — `navigator.maxTouchPoints > 0`. */
  hasTouch: boolean;
}

const CAPABILITY_QUERIES = [
  "(pointer: coarse)",
  "(any-pointer: coarse)",
  "(hover: hover)",
] as const;

// SSR / no-matchMedia fallback: assume a hovering fine pointer (mouse
// desktop), matching the shell's historical desktop-first defaults.
const SERVER_SNAPSHOT: InputCapabilities = {
  coarsePrimary: false,
  anyCoarse: false,
  hoverPrimary: true,
  hasTouch: false,
};

function read(): InputCapabilities {
  if (typeof window === "undefined" || !window.matchMedia) return SERVER_SNAPSHOT;
  const [coarse, anyCoarse, hover] = CAPABILITY_QUERIES.map((q) => window.matchMedia(q).matches);
  return {
    coarsePrimary: coarse,
    anyCoarse,
    hoverPrimary: hover,
    hasTouch: typeof navigator !== "undefined" && navigator.maxTouchPoints > 0,
  };
}

// useSyncExternalStore requires a referentially stable snapshot while values
// are unchanged; re-reading is cheap, so compare and keep the old object.
let cached: InputCapabilities = SERVER_SNAPSHOT;

function getSnapshot(): InputCapabilities {
  const next = read();
  if (
    next.coarsePrimary !== cached.coarsePrimary ||
    next.anyCoarse !== cached.anyCoarse ||
    next.hoverPrimary !== cached.hoverPrimary ||
    next.hasTouch !== cached.hasTouch
  ) {
    cached = next;
  }
  return cached;
}

function subscribe(callback: () => void): () => void {
  if (typeof window === "undefined" || !window.matchMedia) return () => {};
  const lists = CAPABILITY_QUERIES.map((q) => window.matchMedia(q));
  for (const mql of lists) mql.addEventListener("change", callback);
  return () => {
    for (const mql of lists) mql.removeEventListener("change", callback);
  };
}

/**
 * Reactive input-capability snapshot. Updates live when a convertible flips
 * modes or a mouse attaches/detaches (matchMedia change events). SSR-safe:
 * the server snapshot assumes a mouse desktop.
 */
export function useInputCapabilities(): InputCapabilities {
  return useSyncExternalStore(subscribe, getSnapshot, () => SERVER_SNAPSHOT);
}
