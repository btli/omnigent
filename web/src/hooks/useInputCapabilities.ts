// Single source of truth for pointer/input capability.
//
// Capability gates AFFORDANCES and defaults only (hit-target sizing,
// persistent vs hover-revealed controls, swipe hints) — never per-event
// handling. Gesture recognition must instead branch on the active sequence's
// `PointerEvent.pointerType`, so a touch on a fine-primary laptop still gets
// gesture semantics regardless of what these queries report. Viewport width
// is an independent LAYOUT axis — see `@/lib/breakpoints`.

import { useSyncExternalStore } from "react";

import { subscribeMatchMedia } from "@/lib/breakpoints";

export interface InputCapabilities {
  /** ANY attached pointer is coarse — `(any-pointer: coarse)`. */
  anyCoarse: boolean;
}

const CAPABILITY_QUERIES = ["(any-pointer: coarse)"] as const;

// SSR / no-matchMedia fallback: assume a hovering fine pointer (mouse
// desktop), matching the shell's historical desktop-first defaults.
const SERVER_SNAPSHOT: InputCapabilities = {
  anyCoarse: false,
};

function read(): InputCapabilities {
  if (typeof window === "undefined" || !window.matchMedia) return SERVER_SNAPSHOT;
  return {
    anyCoarse: window.matchMedia(CAPABILITY_QUERIES[0]).matches,
  };
}

// useSyncExternalStore requires a referentially stable snapshot while values
// are unchanged; re-reading is cheap, so compare and keep the old object.
let cached: InputCapabilities = SERVER_SNAPSHOT;

function getSnapshot(): InputCapabilities {
  const next = read();
  if (next.anyCoarse !== cached.anyCoarse) {
    cached = next;
  }
  return cached;
}

function subscribe(callback: () => void): () => void {
  return subscribeMatchMedia(CAPABILITY_QUERIES, callback);
}

/**
 * Reactive coarse-pointer snapshot. Updates live when a touch-capable pointer
 * attaches or detaches. SSR-safe: the server snapshot assumes a mouse desktop.
 */
export function useInputCapabilities(): InputCapabilities {
  return useSyncExternalStore(subscribe, getSnapshot, () => SERVER_SNAPSHOT);
}
