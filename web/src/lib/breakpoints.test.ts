import { describe, expect, it, vi, afterEach } from "vitest";

import { MD_BREAKPOINT_PX, isMobileViewport, maxWidthQuery, minWidthQuery } from "./breakpoints";

function stubMatchMedia(matchesFor: (query: string) => boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: matchesFor(query),
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    })),
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("breakpoints", () => {
  it("encodes Tailwind's md breakpoint exactly once", () => {
    expect(MD_BREAKPOINT_PX).toBe(768);
    // md: variant (inclusive lower bound).
    expect(minWidthQuery()).toBe("(min-width: 768px)");
    // max-md: variant (exclusive upper bound, 768 - 0.02).
    expect(maxWidthQuery()).toBe("(max-width: 767.98px)");
  });

  it("builds queries for arbitrary breakpoints", () => {
    expect(minWidthQuery(1024)).toBe("(min-width: 1024px)");
    expect(maxWidthQuery(1024)).toBe("(max-width: 1023.98px)");
  });

  it("isMobileViewport is true below md and false at md+", () => {
    stubMatchMedia((q) => q === minWidthQuery());
    expect(isMobileViewport()).toBe(false);

    stubMatchMedia(() => false);
    expect(isMobileViewport()).toBe(true);
  });

  it("publishes the signal native shells consume for their back handler", () => {
    // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
    expect(window.__omnigentIsMobileViewport).toBe(isMobileViewport);
  });
});
