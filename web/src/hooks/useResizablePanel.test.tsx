import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import { resetSharedWidthStoreForTesting, useResizablePanel } from "./useResizablePanel";

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

function createPointerHandle() {
  const element = document.createElement("div");
  const capturedPointers = new Set<number>();
  const setPointerCapture = vi.fn((pointerId: number) => capturedPointers.add(pointerId));
  const releasePointerCapture = vi.fn((pointerId: number) => capturedPointers.delete(pointerId));
  const hasPointerCapture = vi.fn((pointerId: number) => capturedPointers.has(pointerId));
  Object.assign(element, { setPointerCapture, releasePointerCapture, hasPointerCapture });
  return { element, setPointerCapture, releasePointerCapture };
}

function dispatchPointer(
  element: HTMLElement,
  type: string,
  { pointerId, clientX = 0 }: { pointerId: number; clientX?: number },
): void {
  const event = new Event(type, { bubbles: true });
  Object.defineProperties(event, {
    pointerId: { value: pointerId },
    clientX: { value: clientX },
  });
  element.dispatchEvent(event);
}

function startPointerDrag(
  onPointerDown: React.PointerEventHandler<HTMLElement>,
  element: HTMLElement,
  pointerId: number,
): void {
  onPointerDown({
    currentTarget: element,
    pointerId,
    preventDefault: () => {},
  } as React.PointerEvent<HTMLElement>);
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetSharedWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizablePanel persistence", () => {
  it("persists explicit keyboard resize and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizablePanel(true));

    // Default at 2000px viewport is 50vw = 1000. ArrowRight narrows by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.panelWidth).toBe(980);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    unmount();
    resetSharedWidthStoreForTesting();
    const restored = renderHook(() => useResizablePanel(true));

    // A fresh module-level store hydrates from localStorage instead of falling
    // back to 50vw, which is the refresh behavior this hook must preserve.
    expect(restored.result.current.panelWidth).toBe(980);
    restored.unmount();
  });

  it("clamps live on shrink without persisting, then restores the preference on widen", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    setInnerWidth(1000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });

    // The live width clamps to the new 80vw ceiling, but the saved user
    // preference remains 980 so a later wider viewport can restore it.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(980);

    // Widening the viewport again re-derives from the persisted preference,
    // so the panel springs back to 980 within the same session (no reload).
    setInnerWidth(2000);
    act(() => {
      window.dispatchEvent(new Event("resize"));
    });
    expect(result.current.panelWidth).toBe(980);
  });

  it("captures the pointer and persists the final width on release", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const handle = createPointerHandle();

    act(() => {
      startPointerDrag(result.current.handleProps.onPointerDown, handle.element, 7);
    });
    expect(handle.setPointerCapture).toHaveBeenCalledWith(7);

    act(() => {
      // 2000px viewport, cursor at 1200 → width = innerWidth - clientX = 800.
      dispatchPointer(handle.element, "pointermove", { pointerId: 7, clientX: 1200 });
    });

    // Live width tracks the drag, but nothing is written to storage mid-drag —
    // persisting per pointermove would fire a synchronous setItem on every frame.
    expect(result.current.panelWidth).toBe(800);
    expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();

    act(() => {
      dispatchPointer(handle.element, "pointerup", { pointerId: 7 });
    });

    // Release snapshots the final width exactly once.
    expect(readPanelSizePreference("pushPanelWidthPx")).toBe(800);
    expect(handle.releasePointerCapture).toHaveBeenCalledWith(7);
  });

  it.each(["pointercancel", "lostpointercapture"])(
    "aborts cleanly on %s without persisting",
    (abortEvent) => {
      const { result } = renderHook(() => useResizablePanel(true));
      const handle = createPointerHandle();

      act(() => {
        startPointerDrag(result.current.handleProps.onPointerDown, handle.element, 11);
        dispatchPointer(handle.element, "pointermove", { pointerId: 11, clientX: 1200 });
      });
      expect(result.current.panelWidth).toBe(800);

      act(() => {
        dispatchPointer(handle.element, abortEvent, { pointerId: 11 });
        dispatchPointer(handle.element, "pointermove", { pointerId: 11, clientX: 1400 });
      });

      expect(result.current.panelWidth).toBe(800);
      expect(readPanelSizePreference("pushPanelWidthPx")).toBeNull();
      expect(document.body.style.cursor).toBe("");
      expect(document.body.style.userSelect).toBe("");
    },
  );

  it("ignores additional pointers until the active drag ends", () => {
    const { result } = renderHook(() => useResizablePanel(true));
    const firstHandle = createPointerHandle();
    const secondHandle = createPointerHandle();

    act(() => {
      startPointerDrag(result.current.handleProps.onPointerDown, firstHandle.element, 1);
      startPointerDrag(result.current.handleProps.onPointerDown, secondHandle.element, 2);
      dispatchPointer(secondHandle.element, "pointermove", { pointerId: 2, clientX: 1400 });
    });

    expect(firstHandle.setPointerCapture).toHaveBeenCalledWith(1);
    expect(secondHandle.setPointerCapture).not.toHaveBeenCalled();
    expect(result.current.panelWidth).toBe(1000);

    act(() => {
      dispatchPointer(firstHandle.element, "pointermove", { pointerId: 1, clientX: 1200 });
    });
    expect(result.current.panelWidth).toBe(800);
  });

  it("returns touch-action and a 44px cross-axis hit target", () => {
    const { result } = renderHook(() => useResizablePanel(true));

    expect(result.current.handleProps.style).toMatchObject({
      touchAction: "none",
      boxSizing: "content-box",
      paddingInline: 20,
      marginInline: -20,
      backgroundClip: "content-box",
    });
  });

  it("notifies multiple mounted subscribers from the shared width store", () => {
    const first = renderHook(() => useResizablePanel(true));
    const second = renderHook(() => useResizablePanel(true));

    act(() => {
      first.result.current.handleProps.onKeyDown({
        key: "ArrowRight",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    // Both hook instances read the same module-level store. If subscription
    // fan-out breaks, only the initiating hook would observe the new width.
    expect(first.result.current.panelWidth).toBe(980);
    expect(second.result.current.panelWidth).toBe(980);

    first.unmount();
    second.unmount();
  });
});
