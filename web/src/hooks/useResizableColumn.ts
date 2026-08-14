import { useCallback, useEffect, useRef, useState } from "react";

const KEYBOARD_STEP_PX = 20;
// Padding on each side of the visual handle so the touch/pen hit target is at
// least 44px wide without changing the handle's painted width (the matching
// negative margins keep the layout box where it was, and background-clip:
// content-box keeps hover/active backgrounds off the padded area).
const HIT_TARGET_PAD_PX = 22;

export function useResizableColumn(defaultWidth = 176, minWidth = 100, maxWidth = 480) {
  const [width, setWidth] = useState(defaultWidth);
  // Pointer id of the active drag; null when idle. First pointer wins — a
  // second concurrent pointer is ignored until the first drag ends.
  const activePointerId = useRef<number | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  const minRef = useRef(minWidth);
  const maxRef = useRef(maxWidth);
  minRef.current = minWidth;
  maxRef.current = maxWidth;

  const clamp = useCallback(
    (w: number) => Math.max(minRef.current, Math.min(maxRef.current, w)),
    [],
  );

  const endDrag = useCallback(() => {
    if (activePointerId.current === null) return;
    activePointerId.current = null;
    document.body.style.cursor = "";
    document.body.style.userSelect = "";
  }, []);

  const onPointerDown = useCallback((e: React.PointerEvent) => {
    if (activePointerId.current !== null) return;
    e.preventDefault();
    activePointerId.current = e.pointerId;
    // Capture so moves keep arriving when the pointer leaves the handle (or
    // crosses an iframe), and so no other gesture consumer sees the stream.
    e.currentTarget.setPointerCapture(e.pointerId);
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
  }, []);

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current || !containerRef.current) return;
      const left = containerRef.current.getBoundingClientRect().left;
      setWidth(clamp(e.clientX - left));
    },
    [clamp],
  );

  // pointerup ends the drag; pointercancel and capture loss abort it cleanly,
  // keeping the last applied width (never a half-state).
  const onPointerEnd = useCallback(
    (e: React.PointerEvent) => {
      if (e.pointerId !== activePointerId.current) return;
      endDrag();
    },
    [endDrag],
  );

  const onKeyDown = useCallback(
    (e: React.KeyboardEvent) => {
      // Vertical separator between columns: ArrowRight widens the left
      // column, ArrowLeft narrows it, with the same clamps as dragging.
      if (e.key === "ArrowRight") {
        e.preventDefault();
        setWidth((w) => clamp(w + KEYBOARD_STEP_PX));
      } else if (e.key === "ArrowLeft") {
        e.preventDefault();
        setWidth((w) => clamp(w - KEYBOARD_STEP_PX));
      }
    },
    [clamp],
  );

  // Reset body cursor/selection if the component unmounts mid-drag (the
  // element's capture is released implicitly when it leaves the DOM).
  useEffect(() => endDrag, [endDrag]);

  return {
    /** Pixel width for the left column (apply as inline style). */
    width,
    /** Attach to the flex-row container to anchor drag calculations. */
    containerRef,
    /** Spread onto the resize handle element at the right edge of the left column. */
    handleProps: {
      onPointerDown,
      onPointerMove,
      onPointerUp: onPointerEnd,
      onPointerCancel: onPointerEnd,
      onLostPointerCapture: onPointerEnd,
      onKeyDown,
      role: "separator" as const,
      tabIndex: 0,
      "aria-orientation": "vertical" as const,
      "aria-label": "Resize terminal list",
      "aria-valuenow": width,
      "aria-valuemin": minWidth,
      "aria-valuemax": maxWidth,
      style: {
        touchAction: "none",
        boxSizing: "content-box",
        paddingLeft: HIT_TARGET_PAD_PX,
        paddingRight: HIT_TARGET_PAD_PX,
        marginLeft: -HIT_TARGET_PAD_PX,
        marginRight: -HIT_TARGET_PAD_PX,
        backgroundClip: "content-box",
      } as const,
    },
  };
}
