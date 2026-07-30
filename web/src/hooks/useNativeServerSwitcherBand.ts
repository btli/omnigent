import { useEffect } from "react";

import { isAndroidShell, isIOSShell, setNativeServerSwitcherBand } from "@/lib/nativeBridge";

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
export function useNativeServerSwitcherBand(column: HTMLElement | null): void {
  useEffect(() => {
    if (!isAndroidShell() && !isIOSShell()) return;
    if (!column) return;

    let pending = false;
    let frame = 0;
    const publish = () => {
      pending = false;
      frame = 0;
      const viewport = window.innerWidth;
      if (viewport <= 0) return;
      const rect = column.getBoundingClientRect();
      // A push panel can take the column's place, leaving it collapsed. Publish
      // nothing and let native keep its last good band.
      if (rect.width <= 0) return;
      setNativeServerSwitcherBand(rect.left / viewport, rect.right / viewport);
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

    return () => {
      if (frame !== 0) cancelAnimationFrame(frame);
      observer?.disconnect();
      window.removeEventListener("resize", schedule);
      window.removeEventListener("orientationchange", schedule);
    };
  }, [column]);
}
