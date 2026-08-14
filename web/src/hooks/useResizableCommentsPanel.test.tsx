import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { readPanelSizePreference } from "@/lib/panelSizePreferences";
import {
  resetCommentsWidthStoreForTesting,
  useResizableCommentsPanel,
} from "./useResizableCommentsPanel";

const originalInnerWidth = window.innerWidth;

function setInnerWidth(px: number): void {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: px });
}

// jsdom has no pointer capture, so tests drive the returned handlers directly
// with a stub handle element that tracks capture state.
function makeHandleTarget() {
  const captured = new Set<number>();
  return {
    setPointerCapture: vi.fn((id: number) => captured.add(id)),
    releasePointerCapture: vi.fn((id: number) => captured.delete(id)),
    hasPointerCapture: (id: number) => captured.has(id),
  };
}

type HandleTarget = ReturnType<typeof makeHandleTarget>;

function pointerEvent(
  target: HandleTarget,
  overrides: Partial<{ pointerId: number; pointerType: string; button: number; clientX: number }>,
): React.PointerEvent {
  return {
    pointerId: 1,
    pointerType: "touch",
    button: 0,
    clientX: 0,
    preventDefault: () => {},
    currentTarget: target,
    ...overrides,
  } as unknown as React.PointerEvent;
}

/** Panel root whose right edge sits at x=1000 inside a 2000px-wide row. */
function attachContainer(ref: React.MutableRefObject<HTMLDivElement | null>): void {
  const parent = document.createElement("div");
  const panel = document.createElement("div");
  parent.appendChild(panel);
  vi.spyOn(parent, "getBoundingClientRect").mockReturnValue({ width: 2000 } as DOMRect);
  vi.spyOn(panel, "getBoundingClientRect").mockReturnValue({ right: 1000 } as DOMRect);
  ref.current = panel;
}

beforeEach(() => {
  setInnerWidth(2000);
});

afterEach(() => {
  localStorage.clear();
  resetCommentsWidthStoreForTesting();
  setInnerWidth(originalInnerWidth);
});

describe("useResizableCommentsPanel persistence", () => {
  it("persists explicit keyboard resize and restores it after store reset", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    // Default comments width is 240. ArrowLeft widens by 20px.
    act(() => {
      result.current.handleProps.onKeyDown({
        key: "ArrowLeft",
        preventDefault: () => {},
      } as React.KeyboardEvent);
    });

    expect(result.current.width).toBe(260);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(260);

    unmount();
    resetCommentsWidthStoreForTesting();
    const restored = renderHook(() => useResizableCommentsPanel());

    // The restored hook must use the saved comments width instead of the fixed
    // 240px default, matching a browser refresh while comments are open.
    expect(restored.result.current.width).toBe(260);
    restored.unmount();
  });
});

describe("useResizableCommentsPanel pointer drag", () => {
  it("captures the pointer on pointerdown and resizes from pointermove", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 7 })));
    // Capture keeps the drag alive when the pointer leaves the 1px handle.
    expect(target.setPointerCapture).toHaveBeenCalledWith(7);

    // Panel right edge is at 1000, so a move to x=700 means a 300px width.
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 7, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(300);

    // Live moves must not persist; only the release does.
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBeNull();
    act(() =>
      result.current.handleProps.onPointerUp(pointerEvent(target, { pointerId: 7, clientX: 700 })),
    );
    expect(target.releasePointerCapture).toHaveBeenCalledWith(7);
    expect(readPanelSizePreference("commentsPanelWidthPx")).toBe(300);
    unmount();
  });

  it("ignores a second concurrent pointer — first pointer wins", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 1 })));
    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 2 })));
    // A second finger neither captures nor steals the drag.
    expect(target.setPointerCapture).toHaveBeenCalledTimes(1);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 2, clientX: 500 }),
      ),
    );
    expect(result.current.width).toBe(240);

    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 1, clientX: 700 }),
      ),
    );
    expect(result.current.width).toBe(300);
    unmount();
  });

  it("does not start a drag from a secondary mouse button", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() =>
      result.current.handleProps.onPointerDown(
        pointerEvent(target, { pointerType: "mouse", button: 2 }),
      ),
    );
    expect(target.setPointerCapture).not.toHaveBeenCalled();

    act(() => result.current.handleProps.onPointerMove(pointerEvent(target, { clientX: 700 })));
    expect(result.current.width).toBe(240);
    unmount();
  });

  it.each([
    ["pointercancel", "onPointerCancel"],
    ["lostpointercapture", "onLostPointerCapture"],
  ] as const)("aborts cleanly at the last applied width on %s", (_name, handler) => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    attachContainer(result.current.containerRef);
    const target = makeHandleTarget();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, { pointerId: 3 })));
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 3, clientX: 700 }),
      ),
    );
    act(() => result.current.handleProps[handler](pointerEvent(target, { pointerId: 3 })));

    // Never a half-state: width settles, body styles restore, drag is over so
    // later moves from the same pointer are inert.
    expect(result.current.width).toBe(300);
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
    act(() =>
      result.current.handleProps.onPointerMove(
        pointerEvent(target, { pointerId: 3, clientX: 500 }),
      ),
    );
    expect(result.current.width).toBe(300);
    unmount();
  });
});

describe("useResizableCommentsPanel touch affordances", () => {
  it("declares touch-action none and a >=44px invisible hit target", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());

    // No scroll/swipe may start from the handle during a potential drag.
    expect(result.current.handleProps.style.touchAction).toBe("none");

    // The 1px visual handle carries an invisible child widened for touch/pen.
    const hit = result.current.handleProps.children;
    expect(hit.props["aria-hidden"]).toBe(true);
    expect(hit.props.style.width).toBeGreaterThanOrEqual(44);
    expect(hit.props.style.touchAction).toBe("none");
    unmount();
  });

  it("keeps the separator contract the consumer's markup relies on", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    expect(result.current.handleProps.role).toBe("separator");
    expect(result.current.handleProps["aria-label"]).toBe("Resize comments panel");
    expect(result.current.handleProps.tabIndex).toBe(0);
    unmount();
  });
});

describe("useResizableCommentsPanel drag overlay", () => {
  const overlaySelector = () =>
    [...document.body.children].find(
      (c): c is HTMLElement =>
        c instanceof HTMLElement && c.style.position === "fixed" && c.style.zIndex === "2147483647",
    ) ?? null;

  it("shields iframes with a full-window overlay for the duration of the drag", () => {
    // The divider sits beside the HTML-preview iframe. If capture is lost,
    // the overlay keeps the pointer stream in the parent document so the
    // release is never swallowed by the frame.
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    const target = makeHandleTarget();
    expect(overlaySelector()).toBeNull();

    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, {})));
    const overlay = overlaySelector();
    expect(overlay).not.toBeNull();
    expect(overlay?.style.cursor).toBe("col-resize");

    act(() => result.current.handleProps.onPointerUp(pointerEvent(target, {})));
    expect(overlaySelector()).toBeNull();
    unmount();
  });

  it("removes the overlay and restores body styles if unmounted mid-drag", () => {
    const { result, unmount } = renderHook(() => useResizableCommentsPanel());
    const target = makeHandleTarget();
    act(() => result.current.handleProps.onPointerDown(pointerEvent(target, {})));
    expect(overlaySelector()).not.toBeNull();
    expect(document.body.style.cursor).toBe("col-resize");

    unmount();
    expect(overlaySelector()).toBeNull();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});
