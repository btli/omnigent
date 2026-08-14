import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useResizableColumn } from "./useResizableColumn";

type Handle = ReturnType<typeof useResizableColumn>["handleProps"];

function pointerEvent(
  pointerId: number,
  clientX = 0,
  setPointerCapture = vi.fn(),
): React.PointerEvent & { setPointerCapture: ReturnType<typeof vi.fn> } {
  return {
    pointerId,
    clientX,
    preventDefault: vi.fn(),
    currentTarget: { setPointerCapture },
    setPointerCapture,
  } as unknown as React.PointerEvent & { setPointerCapture: ReturnType<typeof vi.fn> };
}

function keyEvent(key: string) {
  const preventDefault = vi.fn();
  return { event: { key, preventDefault } as unknown as React.KeyboardEvent, preventDefault };
}

/** Render the hook with a container anchored at the given viewport left edge. */
function renderColumn(containerLeft = 0) {
  const rendered = renderHook(() => useResizableColumn());
  rendered.result.current.containerRef.current = {
    getBoundingClientRect: () => ({ left: containerLeft }),
  } as HTMLElement;
  return rendered;
}

afterEach(() => {
  document.body.style.cursor = "";
  document.body.style.userSelect = "";
});

describe("useResizableColumn pointer dragging", () => {
  it("captures the pointer on pointerdown and tracks moves on the captured element", () => {
    const { result } = renderColumn(100);
    const down = pointerEvent(7);

    act(() => result.current.handleProps.onPointerDown(down));
    expect(down.setPointerCapture).toHaveBeenCalledWith(7);
    expect(document.body.style.cursor).toBe("col-resize");
    expect(document.body.style.userSelect).toBe("none");

    // Width follows the pointer, measured from the container's left edge.
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 400)));
    expect(result.current.width).toBe(300);

    // Drag clamps to [minWidth, maxWidth] (defaults 100..480).
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 100)));
    expect(result.current.width).toBe(100);
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 5000)));
    expect(result.current.width).toBe(480);

    act(() => result.current.handleProps.onPointerUp(pointerEvent(7)));
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // Moves after release no longer resize.
    act(() => result.current.handleProps.onPointerMove(pointerEvent(7, 350)));
    expect(result.current.width).toBe(480);
  });

  it("ignores a second concurrent pointer (first pointer wins)", () => {
    const { result } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(1)));

    // A second finger going down mid-drag must not capture or steal the drag.
    const second = pointerEvent(2);
    act(() => result.current.handleProps.onPointerDown(second));
    expect(second.setPointerCapture).not.toHaveBeenCalled();

    act(() => result.current.handleProps.onPointerMove(pointerEvent(2, 999)));
    expect(result.current.width).toBe(176);

    // The second pointer lifting must not end the first pointer's drag.
    act(() => result.current.handleProps.onPointerUp(pointerEvent(2)));
    act(() => result.current.handleProps.onPointerMove(pointerEvent(1, 300)));
    expect(result.current.width).toBe(300);
  });

  it.each([
    ["onPointerCancel", (h: Handle) => h.onPointerCancel],
    ["onLostPointerCapture", (h: Handle) => h.onLostPointerCapture],
  ])("aborts cleanly on %s, keeping the last applied width", (_name, pick) => {
    const { result } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(3)));
    act(() => result.current.handleProps.onPointerMove(pointerEvent(3, 250)));
    expect(result.current.width).toBe(250);

    act(() => pick(result.current.handleProps)(pointerEvent(3)));
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");

    // The aborted pointer is dead: further moves must not resize.
    act(() => result.current.handleProps.onPointerMove(pointerEvent(3, 400)));
    expect(result.current.width).toBe(250);
  });

  it("resets body cursor/selection when unmounted mid-drag", () => {
    const { result, unmount } = renderColumn(0);

    act(() => result.current.handleProps.onPointerDown(pointerEvent(4)));
    expect(document.body.style.cursor).toBe("col-resize");

    unmount();
    expect(document.body.style.cursor).toBe("");
    expect(document.body.style.userSelect).toBe("");
  });
});

describe("useResizableColumn keyboard resizing", () => {
  it("resizes with arrow keys using the same clamps as dragging", () => {
    const { result } = renderColumn(0);

    const right = keyEvent("ArrowRight");
    act(() => result.current.handleProps.onKeyDown(right.event));
    expect(result.current.width).toBe(196);
    expect(right.preventDefault).toHaveBeenCalled();

    act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowLeft").event));
    expect(result.current.width).toBe(176);

    // Repeated ArrowLeft stops at minWidth (100).
    for (let i = 0; i < 10; i++) {
      act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowLeft").event));
    }
    expect(result.current.width).toBe(100);

    // Repeated ArrowRight stops at maxWidth (480).
    for (let i = 0; i < 30; i++) {
      act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowRight").event));
    }
    expect(result.current.width).toBe(480);

    // Unrelated keys neither resize nor swallow the event.
    const other = keyEvent("Enter");
    act(() => result.current.handleProps.onKeyDown(other.event));
    expect(result.current.width).toBe(480);
    expect(other.preventDefault).not.toHaveBeenCalled();
  });

  it("exposes a focusable separator with value semantics that track the width", () => {
    const { result } = renderColumn(0);
    const props = result.current.handleProps;

    expect(props.role).toBe("separator");
    expect(props.tabIndex).toBe(0);
    expect(props["aria-orientation"]).toBe("vertical");
    expect(props["aria-valuenow"]).toBe(176);
    expect(props["aria-valuemin"]).toBe(100);
    expect(props["aria-valuemax"]).toBe(480);

    act(() => result.current.handleProps.onKeyDown(keyEvent("ArrowRight").event));
    expect(result.current.handleProps["aria-valuenow"]).toBe(196);
  });
});

describe("useResizableColumn touch affordances", () => {
  it("disables touch-action and widens the hit target to >=44px without repainting it", () => {
    const { result } = renderColumn(0);
    const style = result.current.handleProps.style;

    expect(style.touchAction).toBe("none");

    // Symmetric padding gives a >=44px hit box; matching negative margins keep
    // the layout box in place and background-clip keeps the padding unpainted.
    expect(style.paddingLeft + style.paddingRight).toBeGreaterThanOrEqual(44);
    expect(style.marginLeft).toBe(-style.paddingLeft);
    expect(style.marginRight).toBe(-style.paddingRight);
    expect(style.backgroundClip).toBe("content-box");
    expect(style.boxSizing).toBe("content-box");
  });
});
