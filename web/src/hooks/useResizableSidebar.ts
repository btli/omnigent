// Resize hook for the always-visible left sidebar (the conversations aside in
// AppShell/Sidebar). Mirrors useResizableInlinePanel's persistence + keyboard
// behavior, but for a LEFT-edge panel: the drag handle lives on the sidebar's
// right edge, so the live width tracks the cursor's distance from the
// viewport's left edge (``e.clientX``) and ArrowRight grows / ArrowLeft
// shrinks. It keeps its own module-level store + preference key so resizing the
// sidebar never disturbs the right rail's inline-panel width (and vice versa).
//
// Unlike the inline panel this has no "boost" machinery — nothing auto-widens
// the sidebar — so the store is just a persisted, viewport-clamped width.

import { useCallback, useEffect, useRef, useSyncExternalStore } from "react";
import { readPanelSizePreference, writePanelSizePreference } from "@/lib/panelSizePreferences";

// Default 320px (20rem) — wider than the old fixed ``md:w-64`` (256px) sidebar
// so conversation titles have more room before truncating. The floor keeps the
// search box + "New session" button usable; the viewport-relative ceiling keeps
// the sidebar from consuming more than half of the desktop layout.
const DEFAULT_WIDTH_PX = 320;
const MIN_WIDTH_PX = 220;
const MAX_WIDTH_RATIO = 0.5;

function clamp(w: number): number {
  // No viewport available off the DOM (SSR / node test env) — this runs during
  // render, so guard before reading ``window`` to avoid a hard throw.
  if (typeof window === "undefined") return Math.max(MIN_WIDTH_PX, w);
  const ceiling = window.innerWidth * MAX_WIDTH_RATIO;
  return Math.max(MIN_WIDTH_PX, Math.min(w, ceiling));
}

// ---------------------------------------------------------------------------
// Module-level width store (independent of the inline panel / push-panel stores)
// ---------------------------------------------------------------------------

// ``preferredWidth`` mirrors the persisted user choice; ``storedWidth`` is the
// effective (viewport-clamped) width. Keeping the preference in memory lets a
// viewport-resize re-derive the effective width from it — springing back to the
// larger choice when space returns — without ever touching disk.
let preferredWidth: number | null = readPanelSizePreference("sidebarWidthPx");
let storedWidth: number | null = preferredWidth;
const listeners = new Set<() => void>();

function persistWidth(value: number | null) {
  preferredWidth = value;
  writePanelSizePreference("sidebarWidthPx", value);
}

function setStoredWidthRaw(value: number | null, persist = false) {
  if (value === storedWidth) return;
  storedWidth = value;
  if (persist) persistWidth(value);
  for (const l of listeners) l();
}

function setStoredWidth(next: number | ((prev: number | null) => number), persist = false) {
  setStoredWidthRaw(typeof next === "function" ? next(storedWidth) : next, persist);
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => listeners.delete(cb);
}

function getSnapshot(): number | null {
  return storedWidth;
}

function getServerSnapshot(): number | null {
  return null;
}

/** Reset module-level state. Only for use in tests. */
export function resetSidebarWidthStoreForTesting(): void {
  preferredWidth = readPanelSizePreference("sidebarWidthPx");
  setStoredWidthRaw(preferredWidth);
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Makes the desktop left sidebar resizable via a drag handle on its right
 * edge. Persists the chosen width across reloads and re-clamps on viewport
 * resize so the sidebar can't overflow a shrunken window.
 *
 * Returns the current pixel width and handle props to spread onto the resize
 * handle element. Desktop-only — callers should not render the handle on
 * mobile (where the sidebar is a full-screen overlay).
 */
export function useResizableSidebar() {
  const raw = useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
  const width = clamp(raw ?? DEFAULT_WIDTH_PX);
  const activeDragRef = useRef<{
    element: HTMLElement;
    pointerId: number;
    startWidth: number;
    cleanup: () => void;
  } | null>(null);

  // Re-clamp on viewport resize so a shrunken window pulls the sidebar back
  // under the ceiling; widening re-derives from the persisted preference so the
  // user's chosen width springs back when space returns.
  useEffect(() => {
    function onResize() {
      setStoredWidth((prev) => clamp(preferredWidth ?? prev ?? DEFAULT_WIDTH_PX));
    }
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  const finishDrag = useCallback((persist: boolean, restoreStart: boolean) => {
    const activeDrag = activeDragRef.current;
    if (!activeDrag) return;

    activeDragRef.current = null;
    activeDrag.cleanup();
    if (restoreStart) {
      setStoredWidthRaw(activeDrag.startWidth);
    } else if (persist) {
      persistWidth(storedWidth);
    }
    if (activeDrag.element.hasPointerCapture(activeDrag.pointerId)) {
      activeDrag.element.releasePointerCapture(activeDrag.pointerId);
    }
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<HTMLElement>) => {
      // A resize owns exactly one pointer stream until it ends.
      if (activeDragRef.current) return;

      e.preventDefault();
      const element = e.currentTarget;
      const pointerId = e.pointerId;
      const startWidth = storedWidth ?? width;

      function onPointerMove(event: PointerEvent) {
        if (event.pointerId !== pointerId) return;
        setStoredWidth(clamp(event.clientX));
      }

      function onPointerUp(event: PointerEvent) {
        if (event.pointerId !== pointerId) return;
        finishDrag(true, false);
      }

      function onPointerAbort(event: PointerEvent) {
        if (event.pointerId !== pointerId) return;
        finishDrag(false, true);
      }

      function cleanup() {
        element.removeEventListener("pointermove", onPointerMove);
        element.removeEventListener("pointerup", onPointerUp);
        element.removeEventListener("pointercancel", onPointerAbort);
        element.removeEventListener("lostpointercapture", onPointerAbort);
      }

      activeDragRef.current = { element, pointerId, startWidth, cleanup };
      element.addEventListener("pointermove", onPointerMove);
      element.addEventListener("pointerup", onPointerUp);
      element.addEventListener("pointercancel", onPointerAbort);
      element.addEventListener("lostpointercapture", onPointerAbort);
      element.setPointerCapture(pointerId);
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
    },
    [finishDrag, width],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      const step = 20;
      // Right edge of a left panel: ArrowRight widens, ArrowLeft narrows.
      if (e.key === "ArrowRight") {
        e.preventDefault();
        setStoredWidth((prev) => clamp((prev ?? width) + step), true);
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        setStoredWidth((prev) => clamp((prev ?? width) - step), true);
      }
    },
    [width],
  );

  useEffect(() => () => finishDrag(false, true), [finishDrag]);

  return {
    /** Current sidebar width in px (already viewport-clamped). */
    width,
    handleProps: {
      onPointerDown,
      onKeyDown,
      role: "separator" as const,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize sidebar",
      tabIndex: 0,
      style: {
        touchAction: "none",
        width: 44,
      },
    },
  };
}
