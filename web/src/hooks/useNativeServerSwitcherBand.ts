import { useEffect } from "react";

import {
  isAndroidShell,
  NATIVE_READY_EVENT,
  setNativeServerSwitcherBand,
  setNativeServerSwitcherHidden,
} from "@/lib/nativeBridge";

import { useSurfaceFrontmost } from "./useNativeServerSwitcher";

const MIN_USABLE_BAND_PX = 64;

/**
 * Publish the chat column's horizontal extent so the native server switcher can
 * centre itself there instead of over the whole window.
 *
 * The switcher is a native view stacked above the web view, so wherever it
 * lands it swallows taps. The chat column keeps it off adjacent rails, while
 * native reserves the column's header controls at both edges. Obscured or
 * collapsed columns hide the switcher instead of borrowing another surface.
 *
 * Android-only: the iOS shell has no band setter and owns the switcher's
 * visibility from its own frontmost tracking, which a publish here would
 * clobber. No-op outside the Android shell.
 */
export function useNativeServerSwitcherBand(column: HTMLElement | null): void {
  // Sole Android owner of the switcher's visibility: any overlay covering the
  // column (drawer, sidebar, sheet, maximized rail) drops frontmost and hides
  // the switcher, so a band republish can never re-show it over an overlay.
  const frontmost = useSurfaceFrontmost(column, column !== null, isAndroidShell);
  useEffect(() => {
    if (!column) {
      if (isAndroidShell()) setNativeServerSwitcherHidden(true);
      return;
    }

    // `pending` is deliberately separate from `frame`: it is set before
    // requestAnimationFrame returns, so a callback that runs re-entrantly cannot
    // be overwritten by the handle and wedge scheduling.
    let pending = false;
    let frame = 0;
    const publish = () => {
      pending = false;
      frame = 0;
      if (!isAndroidShell()) return;
      if (!frontmost) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      const viewport = window.innerWidth;
      if (viewport <= 0) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      const columnRect = column.getBoundingClientRect();
      // Adjacent workspace and sidebar surfaces have their own top-row controls.
      if (columnRect.width < MIN_USABLE_BAND_PX) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      const left = Math.max(0, Math.min(1, columnRect.left / viewport));
      const right = Math.max(0, Math.min(1, columnRect.right / viewport));
      if (left >= right) {
        setNativeServerSwitcherHidden(true);
        return;
      }
      setNativeServerSwitcherBand(left, right);
      setNativeServerSwitcherHidden(false);
    };
    // Coalesce to one frame so a drag-resize does not post per pointer event.
    const schedule = () => {
      if (pending) return;
      pending = true;
      frame = requestAnimationFrame(publish);
    };

    schedule();

    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
    observer?.observe(column);
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    window.addEventListener(NATIVE_READY_EVENT, schedule);

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      if (isAndroidShell()) setNativeServerSwitcherHidden(true);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      window.removeEventListener(NATIVE_READY_EVENT, schedule);
    };
  }, [column, frontmost]);
}
