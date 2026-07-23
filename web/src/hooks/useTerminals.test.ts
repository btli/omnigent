// Unit tests for the terminal resource mappers and the HTTP seed fetch.
//
// `terminalInfoFromResource` maps a wire ``session.resource`` dict onto
// the UI-facing TerminalInfo (used by both the chatStore SSE handler and
// the seed fetch). `fetchTerminals` is the authoritative HTTP seed that
// `useTerminals` runs on mount so the Terminal pill reflects an
// already-running terminal on load / after a missed SSE event.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createTerminal,
  fetchTerminals,
  inventoryTerminals,
  isAgentTerminalKey,
  PENDING_RECONCILE_INTERVAL_MS,
  reconcileTerminal,
  resetTerminalReconciliationForTests,
  terminalInfoFromResource,
  terminalReconciliationStats,
  terminalsReconcileInterval,
  terminalsQueryKey,
  terminalTabKey,
  useCreateTerminal,
  useTerminals,
  type TerminalInfo,
} from "./useTerminals";
import { resetShellAttemptsForTests } from "@/lib/shellAttempt";

// useTerminals reads runner liveness to request an authoritative correction
// on state edges. Mock it so we can drive that signal directly; it defaults
// to `undefined` (the no-provider value), which leaves other tests unchanged.
vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(() => undefined),
}));
import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
const runnerOnlineMock = vi.mocked(useSessionRunnerOnline);

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  resetTerminalReconciliationForTests();
  resetShellAttemptsForTests();
});

describe("reconcileTerminal", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    resetTerminalReconciliationForTests();
  });

  const terminal = (id = "terminal_zsh_u-one"): TerminalInfo => ({
    id,
    name: "zsh",
    session: id.replace("terminal_zsh_", ""),
    running: true,
  });

  it("keeps a delete that crosses a deferred older full snapshot", () => {
    const info = terminal();
    reconcileTerminal(client, "conv_race", {
      kind: "snapshot",
      terminals: [info],
      resourceRevision: 10,
      fullSnapshot: true,
    });
    reconcileTerminal(client, "conv_race", { kind: "deleted", id: info.id, sequence: 12 });

    reconcileTerminal(client, "conv_race", {
      kind: "snapshot",
      terminals: [info],
      resourceRevision: 11,
      fullSnapshot: true,
    });

    expect(client.getQueryData(terminalsQueryKey("conv_race"))).toEqual([]);
  });

  it("allows a higher-sequence create to retire a same-id delete", () => {
    const info = terminal();
    reconcileTerminal(client, "conv_recreate", { kind: "deleted", id: info.id, sequence: 20 });
    reconcileTerminal(client, "conv_recreate", { kind: "created", info, sequence: 21 });
    expect(client.getQueryData(terminalsQueryKey("conv_recreate"))).toEqual([info]);
  });

  it("fails closed and logs an equal-sequence conflict", () => {
    const info = terminal();
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    reconcileTerminal(client, "conv_conflict", { kind: "created", info, sequence: 30 });
    reconcileTerminal(client, "conv_conflict", { kind: "deleted", id: info.id, sequence: 30 });
    expect(client.getQueryData(terminalsQueryKey("conv_conflict"))).toEqual([info]);
    expect(error).toHaveBeenCalledWith(expect.stringContaining("equal-sequence conflict"));
    error.mockRestore();
  });

  it("fails closed on an equal-floor conflict after snapshot compaction", () => {
    const info = terminal();
    const error = vi.spyOn(console, "error").mockImplementation(() => {});
    reconcileTerminal(client, "conv_floor_conflict", {
      kind: "snapshot",
      terminals: [info],
      resourceRevision: 35,
      fullSnapshot: true,
    });
    reconcileTerminal(client, "conv_floor_conflict", {
      kind: "deleted",
      id: info.id,
      sequence: 35,
    });
    expect(client.getQueryData(terminalsQueryKey("conv_floor_conflict"))).toEqual([info]);
    expect(error).toHaveBeenCalledWith(expect.stringContaining("equal-sequence conflict"));
    error.mockRestore();
  });

  it("requests a snapshot at sustained growth and compacts only at its watermark", () => {
    const invalidate = vi.spyOn(client, "invalidateQueries");
    for (let sequence = 1; sequence <= 4_097; sequence += 1) {
      reconcileTerminal(client, "conv_growth", {
        kind: "deleted",
        id: `terminal_zsh_u-${sequence}`,
        sequence,
      });
    }
    expect(invalidate).toHaveBeenCalledWith({ queryKey: terminalsQueryKey("conv_growth") });
    expect(terminalReconciliationStats("conv_growth").entryCount).toBe(4_097);

    reconcileTerminal(client, "conv_growth", {
      kind: "snapshot",
      terminals: [],
      resourceRevision: 4_097,
      fullSnapshot: true,
    });
    expect(terminalReconciliationStats("conv_growth")).toEqual({
      authoritativeFloor: 4_097,
      entryCount: 0,
      snapshotRequested: false,
    });
  });

  it("leaves canonical state intact on reset and rejects a crossed late response", () => {
    const info = terminal();
    reconcileTerminal(client, "conv_reset", { kind: "created", info, sequence: 41 });
    reconcileTerminal(client, "conv_reset", { kind: "reset" });
    reconcileTerminal(client, "conv_reset", {
      kind: "created",
      info: { ...info, name: "stale" },
      sequence: 40,
    });
    expect(client.getQueryData(terminalsQueryKey("conv_reset"))).toEqual([info]);
  });

  it("does not mutate ordered state from an unsequenced legacy delta", () => {
    const info = terminal();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    reconcileTerminal(client, "conv_legacy", {
      kind: "snapshot",
      terminals: [info],
      resourceRevision: 60,
      fullSnapshot: true,
    });
    reconcileTerminal(client, "conv_legacy", {
      kind: "created",
      info: { ...info, name: "legacy" },
    });
    expect(client.getQueryData(terminalsQueryKey("conv_legacy"))).toEqual([info]);
    expect(invalidate).toHaveBeenCalledWith({ queryKey: terminalsQueryKey("conv_legacy") });
  });

  it("re-arms snapshot invalidation after a non-authoritative response", () => {
    const info = terminal();
    const invalidate = vi.spyOn(client, "invalidateQueries");
    reconcileTerminal(client, "conv_soft", { kind: "created", info });
    reconcileTerminal(client, "conv_soft", {
      kind: "snapshot",
      terminals: [],
      fullSnapshot: false,
    });
    reconcileTerminal(client, "conv_soft", { kind: "deleted", id: info.id });
    expect(invalidate).toHaveBeenCalledTimes(2);
  });
});

describe("terminalInfoFromResource", () => {
  it("lifts id, metadata.terminal_name, metadata.session_key, metadata.running", () => {
    const info = terminalInfoFromResource({
      id: "terminal_bash_s1",
      object: "session.resource",
      type: "terminal",
      session_id: "conv_abc",
      name: "bash:s1",
      environment: "env_terminal_bash_s1",
      metadata: { terminal_name: "bash", session_key: "s1", running: true },
    });
    expect(info).not.toBeNull();
    const t = info as TerminalInfo;
    expect(t.id).toBe("terminal_bash_s1");
    expect(t.name).toBe("bash");
    expect(t.session).toBe("s1");
    expect(t.running).toBe(true);
  });

  it("falls back to resource.name when metadata.terminal_name is absent", () => {
    // The server should always supply metadata.terminal_name, but a
    // thin event from a legacy producer must not break the rail.
    const t = terminalInfoFromResource({
      id: "terminal_x",
      type: "terminal",
      session_id: "conv_abc",
      name: "fallback-name",
      metadata: {},
    });
    expect(t?.name).toBe("fallback-name");
    expect(t?.session).toBe("");
    expect(t?.running).toBe(false);
  });

  it("returns null when the resource has no id", () => {
    // applyTerminalCreated drops a resource that can't be addressed
    // by id rather than inserting an unkeyable entry.
    const info = terminalInfoFromResource({
      type: "terminal",
      name: "bash:s1",
      metadata: {},
    });
    expect(info).toBeNull();
  });

  it("lifts metadata.terminal_transport, defaulting to undefined for unknown values", () => {
    const control = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "control" },
    });
    expect(control?.transport).toBe("control");

    const pty = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "pty" },
    });
    expect(pty?.transport).toBe("pty");

    // Absent or unrecognized → undefined (frontend treats as legacy PTY).
    const legacy = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: {},
    });
    expect(legacy?.transport).toBeUndefined();
    const junk = terminalInfoFromResource({
      id: "terminal_bash_s1",
      type: "terminal",
      name: "bash:s1",
      metadata: { terminal_transport: "garbage" },
    });
    expect(junk?.transport).toBeUndefined();
  });
});

describe("fetchTerminals", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("GETs the terminals list and maps each row to TerminalInfo", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        object: "list",
        resource_revision: 7,
        full_snapshot: true,
        data: [
          {
            id: "terminal_codex_main",
            type: "terminal",
            session_id: "conv_abc",
            name: "codex:main",
            metadata: { terminal_name: "codex", session_key: "main", running: true },
          },
        ],
      }),
    );
    const out = await fetchTerminals("conv_abc");
    // Proves the seed both hits the right endpoint and lands a usable
    // entry — len-only would pass even if mapping produced empty fields.
    // `order=asc` is load-bearing: the endpoint defaults to desc
    // (newest first), which on refresh would bump the session's own
    // terminal (created first) out of the first tab slot; asc keeps
    // the seed consistent with SSE created-deltas appending at the end.
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/conv_abc/resources/terminals?order=asc&limit=1000",
      expect.anything(),
    );
    expect(out).toEqual({
      terminals: [{ id: "terminal_codex_main", name: "codex", session: "main", running: true }],
      resourceRevision: 7,
      fullSnapshot: true,
    });
  });

  it("returns [] for a not-yet-reachable runner (404/409/502/503)", async () => {
    // These are "no terminal yet", not errors: the live SSE event fills
    // the rail once the runner binds, so the seed must not throw.
    for (const status of [404, 409, 502, 503]) {
      fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status }));
      expect(await fetchTerminals("conv_abc")).toEqual({ terminals: [], fullSnapshot: false });
    }
  });

  it("throws on a hard error status so React Query can retry", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(null, { ok: false, status: 500 }));
    await expect(fetchTerminals("conv_abc")).rejects.toThrow(/500/);
  });
});

describe("createTerminal", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs the declared name with the canonical attempt id and maps the sequenced resource", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        terminal: {
          id: "terminal_shell_u-abc123",
          object: "session.resource",
          type: "terminal",
          session_id: "conv_abc",
          name: "shell:u-abc123",
          metadata: { terminal_name: "shell", session_key: "u-abc123", running: true },
        },
        sequence: 8,
      }),
    );

    const attemptId = "123e4567-e89b-42d3-a456-426614174000";
    const out = await createTerminal("conv_abc", "shell", attemptId);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/conv_abc/resources/terminals");
    expect(init.method).toBe("POST");
    const body = JSON.parse(init.body as string) as Record<string, unknown>;
    expect(body).toEqual({ terminal: "shell", attempt_id: attemptId });
    // Mapped through terminalInfoFromResource — proves the POST
    // response shape lands as a usable TerminalInfo, not raw wire.
    expect(out).toEqual({
      terminal: {
        id: "terminal_shell_u-abc123",
        name: "shell",
        session: "u-abc123",
        running: true,
      },
      sequence: 8,
    });
  });

  it("surfaces the server gate's message on a 400 rejection", async () => {
    // The server's iff gate (agent has no terminals: block / name not
    // declared) returns a structured error; the UI must surface that
    // message, not a generic status line.
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "invalid_input", message: "Terminal 'zsh' is not declared" } },
        { ok: false, status: 400 },
      ),
    );
    await expect(
      createTerminal("conv_abc", "zsh", "123e4567-e89b-42d3-a456-426614174000"),
    ).rejects.toThrow(/not declared/);
  });
});

describe("useCreateTerminal attempt lifecycle", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("reuses the direct-create attempt id after response loss", async () => {
    const client = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    fetchMock.mockRejectedValueOnce(new TypeError("create response lost"));
    const { result } = renderHook(() => useCreateTerminal("conv_direct"), { wrapper });

    await expect(result.current.mutateAsync("zsh")).rejects.toThrow("create response lost");
    const firstBody = JSON.parse(
      String((fetchMock.mock.calls[0]![1] as RequestInit).body),
    ) as Record<string, unknown>;
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        terminal: {
          id: "terminal_zsh_u-direct",
          name: "zsh:u-direct",
          metadata: { terminal_name: "zsh", session_key: "u-direct", running: true },
        },
        sequence: 15,
      }),
    );
    await result.current.mutateAsync("zsh");
    const secondBody = JSON.parse(
      String((fetchMock.mock.calls[1]![1] as RequestInit).body),
    ) as Record<string, unknown>;
    expect(secondBody.attempt_id).toBe(firstBody.attempt_id);
  });
});

describe("terminalsReconcileInterval", () => {
  // The spinner is `terminalPending && !terminalsAvailable`. This decides
  // when the terminals query re-polls to recover a missed
  // `session.resource.created` (the dbx-apps stuck-spinner bug) — and,
  // critically, when it STOPS so there's no steady-state polling.
  it("polls only while pending AND no terminal is visible yet", () => {
    expect(terminalsReconcileInterval(true, 0)).toBe(PENDING_RECONCILE_INTERVAL_MS);
  });

  it("stops the instant a terminal lands (clears the spinner via AND)", () => {
    expect(terminalsReconcileInterval(true, 1)).toBe(false);
  });

  it("never polls when the runner is not spinning up a terminal", () => {
    expect(terminalsReconcileInterval(false, 0)).toBe(false);
    expect(terminalsReconcileInterval(false, 2)).toBe(false);
  });
});

describe("useTerminals ordered fetch races", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("keeps a delete SSE that crosses a deferred older snapshot fetch", async () => {
    let resolveFetch: (response: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    const info: TerminalInfo = {
      id: "terminal_zsh_u-race",
      name: "zsh",
      session: "u-race",
      running: true,
    };
    const { result } = renderHook(() => useTerminals("conv_fetch_race"), { wrapper });
    reconcileTerminal(client, "conv_fetch_race", { kind: "created", info, sequence: 10 });
    reconcileTerminal(client, "conv_fetch_race", { kind: "deleted", id: info.id, sequence: 12 });

    resolveFetch(
      mockResponse({
        data: [
          {
            id: info.id,
            name: "zsh:u-race",
            metadata: { terminal_name: "zsh", session_key: "u-race", running: true },
          },
        ],
        resource_revision: 11,
        full_snapshot: true,
      }),
    );
    await waitFor(() => expect(result.current.isLoading).toBe(false));
    expect(result.current.terminals).toEqual([]);
  });
});

describe("useTerminals reconcile poll (stuck-spinner self-heal)", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  function makeWrapper() {
    // A fresh client per render so query cache never leaks between tests.
    // (The hook hard-codes retry:1, which this default does not override; the
    // mocks below always resolve, so no retry/backoff fires under fake timers.)
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
  }

  const emptyList = () => mockResponse({ object: "list", data: [] });
  const oneTerminal = () =>
    mockResponse({
      object: "list",
      data: [
        {
          id: "terminal_claude_main",
          type: "terminal",
          session_id: "conv_abc",
          name: "claude:main",
          metadata: { terminal_name: "claude", session_key: "main", running: true },
        },
      ],
    });

  it("re-polls while pending until the terminal lands, then stops", async () => {
    // Mount + first reconcile poll see no terminal; the auto-created
    // terminal lands on the third fetch — exactly the missed-delta case
    // a page refresh used to be the only recovery for.
    fetchMock
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValue(oneTerminal());

    const { result } = renderHook(() => useTerminals("conv_abc", { reconcileWhilePending: true }), {
      wrapper: makeWrapper(),
    });

    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(result.current.terminals).toEqual([]);
    const seedCalls = fetchMock.mock.calls.length;

    // First reconcile poll fires while still empty: the interval scheduled a
    // refetch, so the fetch count must climb past the seed. (Asserting `>`
    // rather than an exact `+1` because React Query anchors the next interval
    // off each fetch's settle time, so the precise count under fake timers is
    // a scheduling detail; a count still == seedCalls would mean no poll fired
    // — the bug we're guarding against.)
    await act(async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS)));
    expect(result.current.terminals).toEqual([]);
    expect(fetchMock.mock.calls.length).toBeGreaterThan(seedCalls);

    // Further reconcile polls -> terminal lands. Assert the full mapped row
    // (not just id) so a broken resource→TerminalInfo mapping can't pass.
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 2)),
    );
    expect(result.current.terminals).toEqual([
      { id: "terminal_claude_main", name: "claude", session: "main", running: true },
    ]);

    // Polling must stop now that a terminal is visible: the refetchInterval
    // returns false once terminalCount > 0. Assert ZERO further fetches over
    // 4 would-be intervals — a higher count would mean the interval was never
    // disabled and the poll runs forever.
    const callsAtLand = fetchMock.mock.calls.length;
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 4)),
    );
    expect(fetchMock.mock.calls.length).toBe(callsAtLand);
  });

  it("does not poll when reconcileWhilePending is false (no terminal yet)", async () => {
    fetchMock.mockResolvedValue(emptyList());

    renderHook(() => useTerminals("conv_abc", { reconcileWhilePending: false }), {
      wrapper: makeWrapper(),
    });

    // Exactly one fetch (the mount seed) and never more: with pending false,
    // refetchInterval returns false so no interval is ever scheduled. A 2nd
    // fetch here would mean an orchestrator/non-terminal session (no terminal
    // expected) was being polled — the regression this guards against.
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(
      async () => void (await vi.advanceTimersByTimeAsync(PENDING_RECONCILE_INTERVAL_MS * 5)),
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("useTerminals — SSE-primary list, poll corrects on edges", () => {
  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
    vi.useFakeTimers();
    runnerOnlineMock.mockReturnValue(undefined);
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    runnerOnlineMock.mockReturnValue(undefined);
  });

  function makeClientWrapper() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(QueryClientProvider, { client }, children);
    return { client, wrapper };
  }

  const TERMINAL = { id: "terminal_claude_main", name: "claude", session: "main", running: true };
  const emptyList = () => mockResponse({ object: "list", data: [] });
  const oneTerminal = () =>
    mockResponse({
      object: "list",
      data: [
        {
          id: "terminal_claude_main",
          type: "terminal",
          session_id: "conv_abc",
          name: "claude:main",
          metadata: { terminal_name: "claude", session_key: "main", running: true },
        },
      ],
    });

  it("shows a cached terminal immediately even while the poll still reads offline (boot lag)", async () => {
    // SSE-primary: a terminal in the cache (here via the mount seed; in
    // production also via a live `session.resource.created`) is openable right
    // away even though runner liveness still reads `false` during the cold-boot
    // poll lag. A continuous offline mask would hide it until the next poll —
    // the "terminal never clickable" bug. `undefined → false` is NOT a
    // was-online edge, so no correction clears it either.
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(false);

    const { result } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));

    expect(result.current.terminals).toEqual([TERMINAL]);
  });

  it("keeps canonical terminals on an unsequenced confirmed-stop reset", async () => {
    // The poll's subtractive correction: a running runner's terminal is shown,
    // then the poll confirms the runner went offline. Its PTYs are gone but a
    // stop emits no `session.resource.deleted`, so the SSE list would keep
    // showing dead terminals — the hook must drop them. This is what greys the
    // pill after a Stop. Gated on the was-online edge so it never fires during
    // the cold-boot `undefined → false` window (covered by the test above).
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(true);

    const { result, rerender } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    // Online: the seeded terminal is shown.
    expect(result.current.terminals).toEqual([TERMINAL]);

    // Runner stops: request reconciliation, but do not synthesize an
    // unordered delete that a crossed late response could overwrite.
    runnerOnlineMock.mockReturnValue(false);
    await act(async () => {
      rerender();
      await vi.runAllTimersAsync();
    });
    await act(async () => void rerender());
    expect(result.current.terminals).toEqual([TERMINAL]);
  });

  it("re-reads the endpoint on the → online edge to recover a missed created event", async () => {
    // SSE-primary, but if the one-shot `resource.created` was dropped the cache
    // stays empty. The poll's additive correction: on the `→ true` edge the
    // hook re-reads the authoritative endpoint and unions in the terminal it
    // now reports. Start empty (endpoint has nothing, runner unseen), then the
    // runner comes online with a terminal present.
    fetchMock.mockResolvedValue(emptyList());
    runnerOnlineMock.mockReturnValue(undefined);

    const { result, rerender } = renderHook(() => useTerminals("conv_abc"), {
      wrapper: makeClientWrapper().wrapper,
    });
    await act(async () => void (await vi.advanceTimersByTimeAsync(0)));
    // Nothing yet: empty seed, runner not observed.
    expect(result.current.terminals).toEqual([]);

    // Runner comes online AND the endpoint now reports the terminal: the
    // → true edge re-reads and surfaces it. Without that edge re-read the
    // dropped `created` would leave the pill grey (staleTime: Infinity).
    fetchMock.mockResolvedValue(oneTerminal());
    runnerOnlineMock.mockReturnValue(true);
    await act(async () => {
      rerender();
      await vi.runAllTimersAsync();
    });
    await act(async () => void rerender());
    expect(result.current.terminals).toEqual([TERMINAL]);
  });
});

describe("terminalTabKey", () => {
  it("keys off the opaque resource id, not display fields", () => {
    const t: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };
    expect(terminalTabKey(t)).toBe("terminal:terminal_bash_s1");
  });

  it("is stable across display-field changes", () => {
    // The tab key must survive a rename / metadata churn so the
    // active tab doesn't jump when the server updates display fields.
    const before: TerminalInfo = {
      id: "terminal_bash_s1",
      name: "bash",
      session: "s1",
      running: true,
    };
    const after: TerminalInfo = { ...before, name: "bash-renamed", session: "s2" };
    expect(terminalTabKey(before)).toBe(terminalTabKey(after));
  });
});

describe("inventoryTerminals", () => {
  const repl: TerminalInfo = {
    id: "terminal_tui_main",
    name: "tui",
    session: "main",
    running: true,
  };
  const claudePane: TerminalInfo = {
    id: "terminal_claude_main",
    name: "claude",
    session: "main",
    running: true,
  };
  const piPane: TerminalInfo = {
    id: "terminal_pi_main",
    name: "pi",
    session: "main",
    running: true,
  };
  const cursorPane: TerminalInfo = {
    id: "terminal_cursor_main",
    name: "cursor",
    session: "main",
    running: true,
  };
  const kiroPane: TerminalInfo = {
    id: "terminal_kiro_main",
    name: "kiro",
    session: "main",
    running: true,
  };
  const goosePane: TerminalInfo = {
    id: "terminal_goose_main",
    name: "goose",
    session: "main",
    running: true,
  };
  const qwenPane: TerminalInfo = {
    id: "terminal_qwen_main",
    name: "qwen",
    session: "main",
    running: true,
  };
  const kimiPane: TerminalInfo = {
    id: "terminal_kimi_main",
    name: "kimi",
    session: "main",
    running: true,
  };
  const hermesPane: TerminalInfo = {
    id: "terminal_hermes_main",
    name: "hermes",
    session: "main",
    running: true,
  };
  const antigravityPane: TerminalInfo = {
    id: "terminal_antigravity_main",
    name: "antigravity",
    session: "main",
    running: true,
  };
  const bash: TerminalInfo = {
    id: "terminal_bash_s1",
    name: "bash",
    session: "s1",
    running: true,
  };

  it("drops the pi vendor pane for native Pi sessions", () => {
    // Regression: terminal_pi_main was missing from AGENT_TERMINAL_IDS, so
    // the pi pane leaked into the Shells inventory and (via isShellView) hid
    // the Chat/Terminal pill in Terminal view — stranding the user.
    expect(inventoryTerminals([piPane, bash], true)).toEqual([bash]);
  });

  it("drops the cursor vendor pane for native Cursor sessions", () => {
    // Regression: terminal_cursor_main was missing from AGENT_TERMINAL_IDS,
    // same failure mode as the pi pane above — leaked into Shells and hid
    // the Chat/Terminal pill in Terminal view.
    expect(inventoryTerminals([cursorPane, bash], true)).toEqual([bash]);
  });

  it("drops the kiro vendor pane for native Kiro sessions", () => {
    // Regression: terminal_kiro_main was missing from AGENT_TERMINAL_IDS,
    // so Kiro's Terminal pill opened a shell view with an X close button
    // instead of preserving the Chat/Terminal toggle.
    expect(inventoryTerminals([kiroPane, bash], true)).toEqual([bash]);
  });

  it("drops the goose vendor pane for native Goose sessions", () => {
    // Regression: terminal_goose_main was missing from AGENT_TERMINAL_IDS, so
    // the goose TUI pane leaked into the Shells inventory and (via isShellView)
    // opened as a plain shell while hiding the Chat/Terminal pill — same failure
    // mode as the pi/cursor panes above.
    expect(inventoryTerminals([goosePane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_goose_main")).toBe(true);
  });

  it("drops the qwen vendor pane for native Qwen sessions", () => {
    // Regression: terminal_qwen_main was missing from AGENT_TERMINAL_IDS, so
    // clicking Terminal opened the qwen TUI pane as a plain shell (shell-header
    // chrome) while hiding the Chat/Terminal pill via isShellView — same failure
    // mode as the pi/cursor/goose panes above.
    expect(inventoryTerminals([qwenPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_qwen_main")).toBe(true);
  });

  it("drops the kimi vendor pane for native Kimi sessions", () => {
    // Regression: terminal_kimi_main was missing from AGENT_TERMINAL_IDS,
    // same failure mode as the pi/cursor panes above — leaked into Shells
    // and hid the Chat/Terminal pill in Terminal view.
    expect(inventoryTerminals([kimiPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_kimi_main")).toBe(true);
  });

  it("drops the hermes vendor pane for native Hermes sessions", () => {
    // Regression: terminal_hermes_main was missing from AGENT_TERMINAL_IDS, so
    // the hermes TUI pane leaked into the Shells inventory and (via isShellView)
    // opened as a plain shell while hiding the Chat/Terminal pill — same failure
    // mode as the pi/cursor/goose/qwen panes above.
    expect(inventoryTerminals([hermesPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_hermes_main")).toBe(true);
  });

  it("drops the antigravity vendor pane for native Antigravity sessions", () => {
    // Regression (#1157): terminal_antigravity_main was missing from
    // AGENT_TERMINAL_IDS, so the agy TUI pane leaked into the Shells inventory
    // and (via isShellView) hid the Chat/Terminal pill in Terminal view —
    // stranding the user with no way back to Chat. Same failure mode as the
    // pi/cursor/goose/qwen panes above.
    expect(inventoryTerminals([antigravityPane, bash], true)).toEqual([bash]);
    expect(isAgentTerminalKey("terminal:terminal_antigravity_main")).toBe(true);
  });

  it("drops the embedded REPL terminal for terminal-first SDK sessions", () => {
    // The REPL terminal backs the pill's Terminal view; listing it in
    // the rail reads as a phantom "main" terminal on agents that don't
    // run a TUI. Agent-launched terminals must survive the filter — an
    // empty result here would hide the agent's real terminals too.
    expect(inventoryTerminals([repl, bash], true)).toEqual([bash]);
  });

  it("drops the vendor pane for native-wrapper sessions", () => {
    // Native sessions share the SDK rule: the claude/codex pane IS the
    // pill's Terminal view, so the Shells rail must list only user
    // shells. The pane surviving the filter re-creates the phantom
    // "main" entry on claude code / codex sessions.
    expect(inventoryTerminals([claudePane, bash], true)).toEqual([bash]);
  });

  it("hides the shell list entirely when the agent terminal is the only one", () => {
    // [] (not [repl]) is what makes the rail show the virtual
    // new-shell row alone for sessions with no user shells.
    expect(inventoryTerminals([repl], true)).toEqual([]);
  });

  it("keeps the full list for non-terminal-first sessions", () => {
    // A regular session never hosts an agent terminal; an id collision
    // (agent-declared "tui"/"main") must not be hidden.
    expect(inventoryTerminals([repl, bash], false)).toEqual([repl, bash]);
  });
});

describe("isAgentTerminalKey", () => {
  it("recognizes the agent's own terminal for every session shape", () => {
    expect(isAgentTerminalKey("terminal:terminal_tui_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_claude_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_codex_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_opencode_main")).toBe(true);
    // pi-native: missing here is what hid the Chat/Terminal pill in
    // Terminal view (isShellView wrongly true) for Pi sessions.
    expect(isAgentTerminalKey("terminal:terminal_pi_main")).toBe(true);
    // cursor-native: same regression class as pi above.
    expect(isAgentTerminalKey("terminal:terminal_cursor_main")).toBe(true);
    // kiro-/goose-/qwen-native: same regression class as cursor/pi above.
    expect(isAgentTerminalKey("terminal:terminal_kiro_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_goose_main")).toBe(true);
    expect(isAgentTerminalKey("terminal:terminal_qwen_main")).toBe(true);
  });

  it("treats a user shell as not-the-agent-terminal", () => {
    expect(isAgentTerminalKey("terminal:terminal_bash_s1")).toBe(false);
  });
});
