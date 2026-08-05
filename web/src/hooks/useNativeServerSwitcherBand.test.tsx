import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useNativeServerSwitcherBand } from "./useNativeServerSwitcherBand";

const VIEWPORT_WIDTH = 1000;

function domRect(left: number, right: number): DOMRect {
  return {
    x: left,
    y: 0,
    left,
    right,
    top: 0,
    bottom: 600,
    width: right - left,
    height: 600,
    toJSON: () => ({}),
  } as DOMRect;
}

function makeColumn(left: number, right: number): HTMLElement {
  const column = document.createElement("main");
  column.getBoundingClientRect = () => domRect(left, right);
  return column;
}

function installAndroidBridge() {
  const setServerSwitcherBand = vi.fn();
  const setServerSwitcherHidden = vi.fn();
  (window as unknown as Record<string, unknown>).omnigentNative = {
    kind: "android",
    setBadgeCount: vi.fn(),
    notify: vi.fn().mockResolvedValue(true),
    setServerSwitcherBand,
    setServerSwitcherHidden,
  };
  return { setServerSwitcherBand, setServerSwitcherHidden };
}

beforeEach(() => {
  Object.defineProperty(window, "innerWidth", { configurable: true, value: VIEWPORT_WIDTH });
  vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
    callback(0);
    return 1;
  });
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  delete (window as unknown as Record<string, unknown>).omnigentNative;
  document.documentElement.removeAttribute("dir");
  document.documentElement.style.removeProperty("--omnigent-top-bar-visible");
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("useNativeServerSwitcherBand", () => {
  it("clamps transformed bounds to the viewport", () => {
    const { setServerSwitcherBand } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(makeColumn(-0.5, 1000.5)));

    expect(setServerSwitcherBand).toHaveBeenCalledWith(0, 1);
  });

  it("publishes physical bounds unchanged in RTL", () => {
    document.documentElement.dir = "rtl";
    const { setServerSwitcherBand } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(makeColumn(100, 700)));

    expect(setServerSwitcherBand).toHaveBeenCalledWith(0.1, 0.7);
  });

  it("hides instead of publishing the content beneath a full-screen overlay", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(makeColumn(0, 1000), true));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });

  it("hides instead of borrowing an adjacent region for a collapsed chat column", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(makeColumn(320, 383)));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });

  it("clears the native placement when the tracked UI unmounts", () => {
    const { setServerSwitcherHidden } = installAndroidBridge();
    const { unmount } = renderHook(() => useNativeServerSwitcherBand(makeColumn(100, 700)));
    setServerSwitcherHidden.mockClear();

    unmount();

    expect(setServerSwitcherHidden).toHaveBeenCalledOnce();
    expect(setServerSwitcherHidden).toHaveBeenCalledWith(true);
  });

  it("publishes a neutral hidden state when no tracked UI is mounted", () => {
    const { setServerSwitcherBand, setServerSwitcherHidden } = installAndroidBridge();

    renderHook(() => useNativeServerSwitcherBand(null));

    expect(setServerSwitcherBand).not.toHaveBeenCalled();
    expect(setServerSwitcherHidden).toHaveBeenLastCalledWith(true);
  });
});
