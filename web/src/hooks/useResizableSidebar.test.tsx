import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { resetSidebarWidthStoreForTesting, useResizableSidebar } from "./useResizableSidebar";

// useResizableSidebar keeps its width in a module-level store shared across all
// callers. resetSidebarWidthStoreForTesting resets it between tests so cases
// are independent. A 2000px viewport gives a 1000px ceiling (50vw).

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

// Simulate one keyboard step on the public handle. ArrowRight widens by 20px
// (right edge of a left panel), ArrowLeft narrows. Returns the resulting width.
function nudge(
  result: { current: ReturnType<typeof useResizableSidebar> },
  key: "ArrowRight" | "ArrowLeft",
): number {
  act(() =>
    result.current.handleProps.onKeyDown({
      key,
      preventDefault: () => {},
    } as React.KeyboardEvent),
  );
  return result.current.width;
}

function pointerEvent(type: string, pointerId: number, clientX = 0): PointerEvent {
  const event = new Event(type) as PointerEvent;
  Object.defineProperties(event, {
    pointerId: { value: pointerId },
    clientX: { value: clientX },
  });
  return event;
}

function createHandle() {
  const handle = document.createElement("div");
  const capturedPointers = new Set<number>();
  handle.setPointerCapture = vi.fn((pointerId: number) => capturedPointers.add(pointerId));
  handle.hasPointerCapture = vi.fn((pointerId: number) => capturedPointers.has(pointerId));
  handle.releasePointerCapture = vi.fn((pointerId: number) => capturedPointers.delete(pointerId));
  return handle;
}

function startDrag(
  result: { current: ReturnType<typeof useResizableSidebar> },
  handle: HTMLDivElement,
  pointerId = 1,
): void {
  act(() =>
    result.current.handleProps.onPointerDown({
      pointerId,
      currentTarget: handle,
      preventDefault: () => {},
    } as unknown as React.PointerEvent<HTMLElement>),
  );
}

// Simulate a drag: press the handle, move the captured pointer, then release.
// For a left panel the live width tracks the cursor's distance from the
// viewport's left edge (clientX).
function dragTo(
  result: { current: ReturnType<typeof useResizableSidebar> },
  clientX: number,
): void {
  const handle = createHandle();
  startDrag(result, handle);
  act(() => handle.dispatchEvent(pointerEvent("pointermove", 1, clientX)));
  act(() => handle.dispatchEvent(pointerEvent("pointerup", 1)));
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetSidebarWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizableSidebar", () => {
  it("defaults to 320px with no saved preference", () => {
    const { result } = renderHook(() => useResizableSidebar());
    expect(result.current.width).toBe(320);
    // A pristine default is not a user choice, so nothing is persisted.
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
  });

  it("widens on ArrowRight and narrows on ArrowLeft, persisting each step", () => {
    const { result } = renderHook(() => useResizableSidebar());

    expect(nudge(result, "ArrowRight")).toBe(340); // 320 + 20
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(340);

    expect(nudge(result, "ArrowLeft")).toBe(320); // back down 20
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(320);
  });

  it("clamps between 220px and half the viewport", () => {
    const { result } = renderHook(() => useResizableSidebar());

    // Drag far past the right edge — capped at half of the 2000px viewport.
    dragTo(result, 1500);
    expect(result.current.width).toBe(1000);

    // Drag below the floor — held at 220, not 50.
    dragTo(result, 50);
    expect(result.current.width).toBe(220);
  });

  it("persists a drag and restores it after a store reset (reload)", () => {
    const { result, unmount } = renderHook(() => useResizableSidebar());

    dragTo(result, 400);
    expect(result.current.width).toBe(400);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(400);

    unmount();
    resetSidebarWidthStoreForTesting();
    const restored = renderHook(() => useResizableSidebar());
    expect(restored.result.current.width).toBe(400);
    restored.unmount();
  });

  it("clamps down on viewport shrink and springs back to the saved width on widen", () => {
    const { result } = renderHook(() => useResizableSidebar());

    // Establish a 900px preference (under the 1000px ceiling at 2000px).
    dragTo(result, 900);
    expect(result.current.width).toBe(900);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(900);

    // Shrink the viewport: ceiling = 700*0.5 = 350. Live width clamps down to
    // 350 but the saved 900 preference is untouched.
    setInnerWidth(700);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.width).toBe(350);
    expect(readPanelSizePreference("sidebarWidthPx")).toBe(900);

    // Widen again: re-derives from the preference, restoring 900 in-session.
    setInnerWidth(2000);
    act(() => window.dispatchEvent(new Event("resize")));
    expect(result.current.width).toBe(900);
  });

  it("captures pointer drags on the handle and exposes touch-safe affordances", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, 7);
    expect(handle.setPointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");
    expect(result.current.handleProps.style).toEqual({ touchAction: "none", width: 44 });

    act(() => handle.dispatchEvent(pointerEvent("pointermove", 7, 480)));
    expect(result.current.width).toBe(480);
    act(() => handle.dispatchEvent(pointerEvent("pointerup", 7)));

    expect(readPanelSizePreference("sidebarWidthPx")).toBe(480);
    expect(handle.releasePointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });

  it("ignores concurrent pointers until the captured pointer ends", () => {
    const { result } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, 1);
    startDrag(result, handle, 2);
    expect(handle.setPointerCapture).toHaveBeenCalledTimes(1);

    act(() => handle.dispatchEvent(pointerEvent("pointermove", 2, 700)));
    expect(result.current.width).toBe(320);
    act(() => handle.dispatchEvent(pointerEvent("pointermove", 1, 450)));
    expect(result.current.width).toBe(450);
  });

  it.each(["pointercancel", "lostpointercapture"])(
    "aborts to the pre-drag width on %s",
    (abortEvent) => {
      const { result } = renderHook(() => useResizableSidebar());
      const handle = createHandle();

      startDrag(result, handle, 3);
      act(() => handle.dispatchEvent(pointerEvent("pointermove", 3, 500)));
      expect(result.current.width).toBe(500);

      act(() => handle.dispatchEvent(pointerEvent(abortEvent, 3)));
      expect(result.current.width).toBe(320);
      expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    },
  );

  it("removes captured-element listeners and restores state on unmount", () => {
    const { result, unmount } = renderHook(() => useResizableSidebar());
    const handle = createHandle();

    startDrag(result, handle, 9);
    act(() => handle.dispatchEvent(pointerEvent("pointermove", 9, 560)));
    expect(result.current.width).toBe(560);

    unmount();
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    act(() => handle.dispatchEvent(pointerEvent("pointermove", 9, 700)));
    expect(readPanelSizePreference("sidebarWidthPx")).toBeNull();
  });
});
