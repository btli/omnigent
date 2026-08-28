import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  STREAM_HIDDEN_RECONNECT_MAX_MS,
  STREAM_RECONNECT_MAX_MS,
  awaitReconnectDelay,
  nextReconnectDelay,
} from "./streamReconnect";

/** Shadow `document.hidden` (jsdom defines it on the prototype). */
function setDocumentHidden(value: boolean): void {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => value });
}

describe("streamReconnect pacing", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    // Pin jitter to its ceiling so each delay equals its base exactly.
    vi.spyOn(Math, "random").mockReturnValue(1);
  });

  afterEach(() => {
    delete (document as { hidden?: boolean }).hidden;
    vi.clearAllTimers();
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it("caps the saturated delay at the hidden ceiling only while hidden", () => {
    setDocumentHidden(true);
    expect(nextReconnectDelay(20)).toBe(STREAM_HIDDEN_RECONNECT_MAX_MS);
    setDocumentHidden(false);
    expect(nextReconnectDelay(20)).toBe(STREAM_RECONNECT_MAX_MS);
  });

  it("waits out a stretched hidden delay to its exact deadline", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(STREAM_HIDDEN_RECONNECT_MAX_MS - 1);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("wakes immediately on becoming visible when no jitter is requested", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(5_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });

  it("staggers a background conversation's visible wake by the provided jitter", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal, () => 2_000).then(
      () => {
        resolved = true;
      },
    );

    await vi.advanceTimersByTimeAsync(1_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    await vi.advanceTimersByTimeAsync(1_999);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("resumes the stretched wait when the page re-hides within the jitter window", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal, () => 2_000).then(
      () => {
        resolved = true;
      },
    );

    // Brief foreground visit at t=10s arms the jittered wake for t=12s...
    await vi.advanceTimersByTimeAsync(10_000);
    setDocumentHidden(false);
    document.dispatchEvent(new Event("visibilitychange"));

    // ...but the page re-hides at t=11s, before the wake fires.
    await vi.advanceTimersByTimeAsync(1_000);
    setDocumentHidden(true);
    document.dispatchEvent(new Event("visibilitychange"));

    // Jitter expiry while hidden must NOT reconnect; the remaining stretched
    // wait resumes and resolves only at the original 60 s deadline.
    await vi.advanceTimersByTimeAsync(1_000);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(STREAM_HIDDEN_RECONNECT_MAX_MS - 12_000 - 1);
    expect(resolved).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(resolved).toBe(true);
  });

  it("resolves immediately when the signal aborts mid-wait", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(1_000);
    controller.abort();
    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });

  it("resolves immediately for an already-aborted signal", async () => {
    setDocumentHidden(true);
    const controller = new AbortController();
    controller.abort();
    let resolved = false;
    void awaitReconnectDelay(STREAM_HIDDEN_RECONNECT_MAX_MS, controller.signal).then(() => {
      resolved = true;
    });

    await vi.advanceTimersByTimeAsync(0);
    expect(resolved).toBe(true);
  });
});
