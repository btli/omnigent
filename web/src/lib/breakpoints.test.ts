import { describe, expect, it, vi, afterEach } from "vitest";

import {
  MD_BREAKPOINT_PX,
  MD_MAX_WIDTH_QUERY,
  MD_MIN_WIDTH_QUERY,
  isMobileViewport,
} from "./breakpoints";

function stubMatchMedia(matchesFor: (query: string) => boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    configurable: true,
    value: vi.fn((query: string) => ({
      matches: matchesFor(query),
      media: query,
      addEventListener: () => {},
      removeEventListener: () => {},
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
    expect(MD_MIN_WIDTH_QUERY).toBe("(min-width: 768px)");
    // max-md: variant (exclusive upper bound, 768 - 0.02).
    expect(MD_MAX_WIDTH_QUERY).toBe("(max-width: 767.98px)");
  });

  it("isMobileViewport is true below md and false at md+", () => {
    stubMatchMedia((q) => q === MD_MIN_WIDTH_QUERY);
    expect(isMobileViewport()).toBe(false);

    stubMatchMedia(() => false);
    expect(isMobileViewport()).toBe(true);
  });

  it("publishes the signal native shells consume for their back handler", () => {
    // eslint-disable-next-line no-underscore-dangle -- bridge-global naming
    expect(window.__omnigentIsMobileViewport).toBe(isMobileViewport);
  });
});
