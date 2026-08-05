import "@testing-library/jest-dom/vitest";
import { vi } from "vitest";

// The @lobehub icon packages have broken nested-module resolution
// under vitest; stub presentational glyphs so component modules that
// import them can still load in tests. (The Antigravity glyph additionally
// drags in @lobehub/fluent-emoji → @emoji-mart/data, whose JSON modules need
// an import attribute Node refuses under vitest — so it must be stubbed too.)
vi.mock("@/components/icons/ClaudeIcon", () => ({
  ClaudeIcon: () => null,
}));
vi.mock("@/components/icons/CodexIcon", () => ({
  CodexIcon: () => null,
}));
vi.mock("@/components/icons/OpenCodeIcon", () => ({
  OpenCodeIcon: () => null,
}));
vi.mock("@/components/icons/CursorIcon", () => ({
  CursorIcon: () => null,
}));
vi.mock("@/components/icons/GooseIcon", () => ({
  GooseIcon: () => null,
}));
vi.mock("@/components/icons/KiroIcon", () => ({
  KiroIcon: () => null,
}));
vi.mock("@/components/icons/AntigravityIcon", () => ({
  AntigravityIcon: () => null,
}));

// Radix UI primitives (DropdownMenu, etc.) call these pointer-capture and
// scroll APIs that jsdom doesn't implement. Stub them so component tests
// that open a Radix menu don't throw. No-ops are sufficient — the tests
// assert on the resulting DOM, not on capture/scroll side effects.
if (!Element.prototype.hasPointerCapture) {
  Element.prototype.hasPointerCapture = () => false;
}
if (!Element.prototype.setPointerCapture) {
  Element.prototype.setPointerCapture = () => {};
}
if (!Element.prototype.releasePointerCapture) {
  Element.prototype.releasePointerCapture = () => {};
}
if (!Element.prototype.scrollIntoView) {
  Element.prototype.scrollIntoView = () => {};
}

// jsdom doesn't implement IntersectionObserver (used by the sidebar's
// infinite-scroll sentinel). A no-op stub is enough — tests that need to drive
// auto-loading can override the global with their own controllable mock.
if (!("IntersectionObserver" in globalThis)) {
  class MockIntersectionObserver {
    observe() {}
    unobserve() {}
    disconnect() {}
    takeRecords() {
      return [];
    }
    root = null;
    rootMargin = "";
    thresholds = [];
  }
  Object.defineProperty(globalThis, "IntersectionObserver", {
    writable: true,
    configurable: true,
    value: MockIntersectionObserver,
  });
}

const resizeObserverInstances = new Set<MockResizeObserver>();

function resizeObserverEntry(target: Element): ResizeObserverEntry {
  const contentRect = target.getBoundingClientRect();
  const boxSize = { inlineSize: contentRect.width, blockSize: contentRect.height };
  return {
    target,
    contentRect,
    borderBoxSize: [boxSize],
    contentBoxSize: [boxSize],
    devicePixelContentBoxSize: [boxSize],
  };
}

class MockResizeObserver implements ResizeObserver {
  private readonly targets = new Set<Element>();
  private readonly callback: ResizeObserverCallback;

  constructor(callback: ResizeObserverCallback) {
    this.callback = callback;
    resizeObserverInstances.add(this);
  }

  observe(target: Element): void {
    resizeObserverInstances.add(this);
    this.targets.add(target);
    this.callback([resizeObserverEntry(target)], this);
  }

  unobserve(target: Element): void {
    this.targets.delete(target);
  }

  disconnect(): void {
    this.targets.clear();
    resizeObserverInstances.delete(this);
  }

  notify(target: Element): void {
    if (this.targets.has(target)) this.callback([resizeObserverEntry(target)], this);
  }
}

/** Deliver a resize notification to observers watching the target. */
export function notifyResizeObservers(target: Element): void {
  for (const observer of resizeObserverInstances) observer.notify(target);
}

Object.defineProperty(globalThis, "ResizeObserver", {
  writable: true,
  configurable: true,
  value: MockResizeObserver,
});
Object.defineProperty(window, "ResizeObserver", {
  writable: true,
  configurable: true,
  value: MockResizeObserver,
});

let mockViewportWidth: number | null = null;

function mediaQueryMatches(query: string): boolean {
  if (mockViewportWidth === null) return false;
  const conditions = [...query.matchAll(/\((min|max)-width:\s*(\d+(?:\.\d+)?)px\)/g)];
  if (conditions.length === 0) return false;
  return conditions.every(([, boundary, rawWidth]) => {
    const width = Number(rawWidth);
    return boundary === "min" ? mockViewportWidth! >= width : mockViewportWidth! <= width;
  });
}

class MockMediaQueryList {
  readonly media: string;
  matches: boolean;
  onchange: ((this: MediaQueryList, event: MediaQueryListEvent) => unknown) | null = null;
  private readonly listeners = new Set<(event: MediaQueryListEvent) => unknown>();

  constructor(query: string) {
    this.media = query;
    this.matches = mediaQueryMatches(query);
  }

  addListener(listener: (event: MediaQueryListEvent) => unknown): void {
    this.listeners.add(listener);
  }

  removeListener(listener: (event: MediaQueryListEvent) => unknown): void {
    this.listeners.delete(listener);
  }

  addEventListener(_type: string, listener: (event: MediaQueryListEvent) => unknown): void {
    this.listeners.add(listener);
  }

  removeEventListener(_type: string, listener: (event: MediaQueryListEvent) => unknown): void {
    this.listeners.delete(listener);
  }

  dispatchEvent(event: Event): boolean {
    for (const listener of this.listeners) listener(event as MediaQueryListEvent);
    return true;
  }

  update(): void {
    const matches = mediaQueryMatches(this.media);
    if (matches === this.matches) return;
    this.matches = matches;
    const event = { matches, media: this.media } as MediaQueryListEvent;
    this.onchange?.call(this as unknown as MediaQueryList, event);
    for (const listener of this.listeners) listener(event);
  }
}

const mediaQueryLists = new Map<string, MockMediaQueryList>();

/** Set the viewport width used by matchMedia for the current test. */
export function setMockViewportWidth(width: number): void {
  mockViewportWidth = width;
  for (const mediaQueryList of mediaQueryLists.values()) mediaQueryList.update();
}

/** Restore matchMedia's default all-false behavior. */
export function resetMockViewportWidth(): void {
  mockViewportWidth = null;
  for (const mediaQueryList of mediaQueryLists.values()) mediaQueryList.update();
}

Object.defineProperty(window, "matchMedia", {
  writable: true,
  configurable: true,
  value: (query: string): MediaQueryList => {
    let mediaQueryList = mediaQueryLists.get(query);
    if (!mediaQueryList) {
      mediaQueryList = new MockMediaQueryList(query);
      mediaQueryLists.set(query, mediaQueryList);
    }
    return mediaQueryList as unknown as MediaQueryList;
  },
});
