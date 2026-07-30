import { useEffect } from "react";

import {
  isAndroidShell,
  isIOSShell,
  onNativeInsets,
  setNativeServerSwitcherBand,
} from "@/lib/nativeBridge";

const NATIVE_READY_EVENT = "omnigent:native-ready";
const MIN_USABLE_BAND_PX = 64;

/**
 * Publish the chat column's horizontal extent so the native server switcher can
 * centre itself there instead of over the whole window.
 *
 * The switcher is a native view stacked above the web view, so wherever it
 * lands it swallows taps. Centred on the window it can cover the conversations
 * rail's header controls on one side or the workspace rail's tabs on the other;
 * the column between them is the only band that is always free, and only the
 * web knows where it is.
 *
 * No-op outside the native shells.
 */
export function useNativeServerSwitcherBand(
  column: HTMLElement | null,
  contentRegion: HTMLElement | null,
): void {
  useEffect(() => {
    if (!column && !contentRegion) return;

    let pending = false;
    let frame = 0;
    let unsubscribeInsets = () => {};
    const publish = () => {
      pending = false;
      frame = 0;
      if (!isAndroidShell() && !isIOSShell()) return;
      const viewport = window.innerWidth;
      if (viewport <= 0) return;
      const columnRect = column?.getBoundingClientRect();
      // Preserve a usable recovery control: the content region is clear of rail controls.
      // This assumes push-panel titles and controls stay at their horizontal edges.
      const rect =
        columnRect && columnRect.width >= MIN_USABLE_BAND_PX
          ? columnRect
          : contentRegion?.getBoundingClientRect();
      if (!rect || rect.width <= 0) return;
      setNativeServerSwitcherBand(rect.left / viewport, rect.right / viewport);
    };
    // Coalesce to one frame so a drag-resize does not post per pointer event.
    const schedule = () => {
      if (pending) return;
      pending = true;
      frame = requestAnimationFrame(publish);
    };
    const handleNativeReady = () => {
      unsubscribeInsets();
      unsubscribeInsets = onNativeInsets(schedule);
      schedule();
    };

    schedule();
    unsubscribeInsets = onNativeInsets(schedule);

    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
    if (column) observer?.observe(column);
    if (contentRegion) observer?.observe(contentRegion);
    window.addEventListener("resize", schedule);
    window.addEventListener("orientationchange", schedule);
    window.addEventListener(NATIVE_READY_EVENT, handleNativeReady);

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      unsubscribeInsets();
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
      window.removeEventListener(NATIVE_READY_EVENT, handleNativeReady);
    };
  }, [column, contentRegion]);
}
