// Tests for the bang-command executor: wire payload shapes for spawn and
// send, HTTP-200 `{item_id, item, terminal}` success handling (receipt
// append + terminals-cache merge + rail focus), receipt dedupe in BOTH
// arrival orders (POST-first and SSE-first), the no-command create path
// (plain terminal create, no event), pure focus, session-scoped focus
// across navigation, the delete-before-response tombstone, the same-tick
// re-entrancy mutex, and server-error surfacing (400/403/404/409/502).
// Uses a real QueryClient + real chat store + stubbed global fetch so the
// cache merge and receipt append are exercised end-to-end.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  reconcileTerminal,
  resetTerminalReconciliationForTests,
  terminalsQueryKey,
  type TerminalInfo,
} from "@/hooks/useTerminals";
import { ShellFocusProvider } from "@/shell/ShellFocusContext";
import { useChatStore } from "@/store/chatStore";
import { resetShellAttemptsForTests } from "@/lib/shellAttempt";
import { useRunBangCommand, type RunBangCommand } from "./useRunBangCommand";

const fetchMock = vi.fn();
const UUID_V4_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "STATUS",
    json: async () => body,
  } as unknown as Response;
}

/** Wire-shaped terminal resource (the create route / spawn response shape). */
function terminalResource(id = "terminal_zsh_u-new123"): Record<string, unknown> {
  return {
    id,
    name: "zsh",
    metadata: {
      terminal_name: "zsh",
      session_key: id.replace(/^terminal_zsh_/, ""),
      running: true,
    },
  };
}

/** Wire-shaped `terminal_command` receipt item (api-dict shape). */
function receiptItem(
  itemId = "tc_1",
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    id: itemId,
    type: "terminal_command",
    response_id: "turn_1",
    kind: "input",
    input: "echo hi",
    action: "spawn",
    terminal_id: "terminal_zsh_u-new123",
    terminal_name: "zsh",
    session_key: "u-new123",
    status: "ok",
    ...overrides,
  };
}

/** Success body of the shell_command POST — HTTP 200, no `queued` field. */
function successBody(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    item_id: "tc_1",
    item: receiptItem(),
    terminal: terminalResource(),
    sequence: 10,
    ...overrides,
  };
}

function Probe({ onReady }: { onReady: (cmd: RunBangCommand) => void }) {
  const cmd = useRunBangCommand("conv_1");
  onReady(cmd);
  return <output aria-label="pending">{String(cmd.pending)}</output>;
}

let queryClient: QueryClient;
let latest: RunBangCommand;
const focusShell = vi.fn();

function renderHarness() {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={queryClient}>
      <ShellFocusProvider value={focusShell}>
        <Probe
          onReady={(cmd) => {
            latest = cmd;
          }}
        />
      </ShellFocusProvider>
    </QueryClientProvider>,
  );
}

/** Probe whose conversation id is prop-driven, to model a session switch. */
function SwitchProbe({
  conversationId,
  onReady,
}: {
  conversationId: string;
  onReady: (cmd: RunBangCommand) => void;
}) {
  const cmd = useRunBangCommand(conversationId);
  onReady(cmd);
  return <output aria-label="pending">{String(cmd.pending)}</output>;
}

/**
 * Render the switch probe and return a `switchTo` that rerenders the SAME hook
 * instance under a new conversation id — mirroring the real composer, which
 * does not remount on a session switch.
 */
function renderSwitchHarness(initial: string) {
  queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const tree = (conversationId: string) => (
    <QueryClientProvider client={queryClient}>
      <ShellFocusProvider value={focusShell}>
        <SwitchProbe
          conversationId={conversationId}
          onReady={(cmd) => {
            latest = cmd;
          }}
        />
      </ShellFocusProvider>
    </QueryClientProvider>
  );
  const { rerender } = render(tree(initial));
  useChatStore.setState({ conversationId: initial });
  return {
    switchTo: (conversationId: string) => {
      useChatStore.setState({ conversationId });
      rerender(tree(conversationId));
    },
  };
}

function lastRequest(): { url: string; body: Record<string, unknown> } {
  const [url, init] = fetchMock.mock.calls.at(-1)! as [string, RequestInit];
  return { url, body: JSON.parse(String(init.body)) };
}

function terminalReceiptBlocks() {
  return useChatStore.getState().blocks.filter((b) => b.type === "terminal_command");
}

beforeEach(() => {
  fetchMock.mockReset();
  focusShell.mockReset();
  resetTerminalReconciliationForTests();
  resetShellAttemptsForTests();
  vi.stubGlobal("fetch", fetchMock);
  // The composer using this hook is always inside its session — mirror that
  // so the session-scoped focus/append guards see conv_1 as active.
  useChatStore.setState({ conversationId: "conv_1", blocks: [] });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("useRunBangCommand", () => {
  it("posts a spawn shell_command event, appends the receipt, merges + focuses", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(successBody()));
    renderHarness();
    await latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");

    const { url, body } = lastRequest();
    expect(url).toContain("/v1/sessions/conv_1/events");
    expect(body).toEqual({
      type: "shell_command",
      data: {
        action: "spawn",
        terminal: "zsh",
        command: "echo hi",
        attempt_id: expect.stringMatching(UUID_V4_RE),
      },
    });
    // Receipt rendered straight from the POST response (not waiting on SSE).
    expect(terminalReceiptBlocks()).toHaveLength(1);
    const cached = queryClient.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"));
    expect(cached?.map((t) => t.id)).toEqual(["terminal_zsh_u-new123"]);
    expect(focusShell).toHaveBeenCalledWith("terminal:terminal_zsh_u-new123");
  });

  it("renders exactly one receipt when the SSE copy arrives AFTER the POST", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(successBody()));
    renderHarness();
    await latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");
    expect(terminalReceiptBlocks()).toHaveLength(1);
    // The SSE `response.output_item.done` for the same item lands later; the
    // pump would append it via the same item-id-deduped path.
    useChatStore.getState().appendConversationItem("conv_1", receiptItem());
    expect(terminalReceiptBlocks()).toHaveLength(1);
  });

  it("renders exactly one receipt when the SSE copy arrived BEFORE the POST resolves", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(successBody()));
    renderHarness();
    // SSE delivered the receipt first (reconnect replay / fast stream).
    useChatStore.getState().appendConversationItem("conv_1", receiptItem());
    expect(terminalReceiptBlocks()).toHaveLength(1);
    await latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");
    expect(terminalReceiptBlocks()).toHaveLength(1);
  });

  it("dedupes the spawn merge against a terminal the SSE event already cached", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse(successBody()));
    renderHarness();
    queryClient.setQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"), [
      { id: "terminal_zsh_u-new123", name: "zsh", session: "u-new123", running: true },
    ]);
    await latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");
    const cached = queryClient.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"));
    expect(cached).toHaveLength(1);
  });

  it("posts a send shell_command event and focuses the target shell", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_2", { action: "send", terminal_id: "terminal_zsh_u-ab12cd" }),
          terminal: terminalResource("terminal_zsh_u-ab12cd"),
        }),
      ),
    );
    renderHarness();
    await latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "make test" },
      "!u-ab12cd make test",
    );

    expect(lastRequest().body).toEqual({
      type: "shell_command",
      data: {
        action: "send",
        terminal_id: "terminal_zsh_u-ab12cd",
        command: "make test",
        attempt_id: expect.stringMatching(UUID_V4_RE),
      },
    });
    expect(focusShell).toHaveBeenCalledWith("terminal:terminal_zsh_u-ab12cd");
  });

  it("send still focuses when the response carries no terminal resource", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(successBody({ item: receiptItem("tc_2", { action: "send" }), terminal: null })),
    );
    renderHarness();
    await latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" },
      "!u-ab12cd pwd",
    );
    expect(focusShell).toHaveBeenCalledWith("terminal:terminal_zsh_u-ab12cd");
  });

  it("bare create (no command) uses the terminal create route — no event, no receipt", async () => {
    fetchMock.mockResolvedValueOnce(mockResponse({ terminal: terminalResource(), sequence: 10 }));
    renderHarness();
    await latest.run({ kind: "new", type: "zsh", command: null }, "!zsh");

    const { url, body } = lastRequest();
    expect(url).toContain("/v1/sessions/conv_1/resources/terminals");
    expect(url).not.toContain("/events");
    expect(body.terminal).toBe("zsh");
    expect(body.attempt_id).toMatch(UUID_V4_RE);
    expect(body.session_key).toBeUndefined();
    expect(terminalReceiptBlocks()).toHaveLength(0);
    const cached = queryClient.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1"));
    expect(cached?.map((t) => t.id)).toEqual(["terminal_zsh_u-new123"]);
    expect(focusShell).toHaveBeenCalledWith("terminal:terminal_zsh_u-new123");
  });

  it("focus performs no request at all", async () => {
    renderHarness();
    await latest.run({ kind: "focus", terminalId: "terminal_zsh_u-ab12cd" }, "!u-ab12cd");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(focusShell).toHaveBeenCalledWith("terminal:terminal_zsh_u-ab12cd");
  });

  it("does not focus a shell for a session the user has navigated away from", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderHarness();
    const running = latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");
    // Navigate to another session before the POST resolves.
    useChatStore.setState({ conversationId: "conv_other" });
    resolveFetch(mockResponse(successBody()));
    await running;
    expect(focusShell).not.toHaveBeenCalled();
    // The receipt is session-scoped too — it must not leak into conv_other.
    expect(terminalReceiptBlocks()).toHaveLength(0);
  });

  it("does not resurrect a shell deleted before the spawn response lands", async () => {
    const deadId = "terminal_zsh_u-dead99";
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_dead", { terminal_id: deadId }),
          terminal: terminalResource(deadId),
        }),
      ),
    );
    renderHarness();
    // The delete event (e.g. `! exit`) tombstones the id in THIS session
    // before the POST resolves.
    reconcileTerminal(queryClient, "conv_1", { kind: "deleted", id: deadId, sequence: 11 });
    await latest.run({ kind: "new", type: "zsh", command: "exit" }, "!zsh exit");
    const cached = queryClient.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1")) ?? [];
    expect(cached.some((t) => t.id === deadId)).toBe(false);
    expect(focusShell).not.toHaveBeenCalled();
  });

  it("a tombstone in another conversation does NOT suppress a same-id terminal here", async () => {
    // Terminal ids omit the conversation id; a delete in conv_other must not
    // suppress a legitimate same-id terminal (or same-key relaunch) here.
    const sharedId = "terminal_zsh_u-shared1";
    reconcileTerminal(queryClient, "conv_other", { kind: "deleted", id: sharedId, sequence: 11 });
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_s", { terminal_id: sharedId }),
          terminal: terminalResource(sharedId),
        }),
      ),
    );
    renderHarness(); // active session is conv_1
    await latest.run({ kind: "new", type: "zsh", command: "echo hi" }, "!zsh echo hi");
    const cached = queryClient.getQueryData<TerminalInfo[]>(terminalsQueryKey("conv_1")) ?? [];
    expect(cached.some((t) => t.id === sharedId)).toBe(true);
    expect(focusShell).toHaveBeenCalledWith(`terminal:${sharedId}`);
  });

  it("single-flights concurrent duplicate submits into one execution", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderHarness();
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    const first = latest.run(action, "!u-ab12cd pwd");
    const second = latest.run(action, "!u-ab12cd pwd");
    resolveFetch(mockResponse(successBody({ item: receiptItem("tc_2", { action: "send" }) })));
    await Promise.all([first, second]);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("reuses the response-loss attempt id for an unchanged bare-bang create", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("create response lost"));
    renderHarness();
    const action = { kind: "new", type: "zsh", command: null } as const;
    await expect(latest.run(action, "!")).rejects.toThrow("create response lost");
    const firstAttempt = lastRequest().body.attempt_id;

    fetchMock.mockResolvedValueOnce(
      mockResponse({ terminal: terminalResource("terminal_zsh_u-stable"), sequence: 14 }),
    );
    await latest.run(action, "!");
    expect(lastRequest().body.attempt_id).toBe(firstAttempt);
  });

  it("retains the exact attempt id after response loss and reuses it on unchanged resubmit", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("network response lost"));
    renderHarness();
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    await expect(latest.run(action, "!u-ab12cd pwd")).rejects.toThrow("network response lost");
    const firstAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;

    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_retry", { action: "send" }), terminal: null }),
      ),
    );
    await latest.run(action, "!u-ab12cd pwd");
    const secondAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;
    expect(secondAttempt).toBe(firstAttempt);
  });

  it("uses a new attempt id after an edit clears a response-loss attempt", async () => {
    fetchMock.mockRejectedValueOnce(new TypeError("network response lost"));
    renderHarness();
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    await expect(latest.run(action, "!u-ab12cd pwd")).rejects.toThrow("network response lost");
    const lostAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;

    latest.clearAttempt();
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_after_edit", { action: "send" }), terminal: null }),
      ),
    );
    await latest.run(action, "!u-ab12cd pwd");
    expect((lastRequest().body.data as Record<string, unknown>).attempt_id).not.toBe(lostAttempt);
  });

  it("keeps a retained unknown attempt while the draft still matches, reusing its id", async () => {
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_unknown", { status: "unknown", action: "send" }),
          terminal: null,
        }),
      ),
    );
    renderHarness();
    await expect(latest.run(action, "!u-ab12cd pwd")).resolves.toEqual({ status: "unknown" });
    const unknownAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;

    // Retyping the exact same command revises the draft, but passing the
    // still-matching text must NOT discard the retained attempt — an
    // identical resubmit has to reuse its id so the server dedupes rather
    // than double-executing.
    latest.clearAttempt("!u-ab12cd pwd");
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_retry", { action: "send" }), terminal: null }),
      ),
    );
    await latest.run(action, "!u-ab12cd pwd");
    expect((lastRequest().body.data as Record<string, unknown>).attempt_id).toBe(unknownAttempt);
  });

  it("drops a retained unknown attempt once the draft diverges, minting a new id", async () => {
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_unknown", { status: "unknown", action: "send" }),
          terminal: null,
        }),
      ),
    );
    renderHarness();
    await expect(latest.run(action, "!u-ab12cd pwd")).resolves.toEqual({ status: "unknown" });
    const unknownAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;

    // Editing the draft to a different command clears the retained attempt.
    // Even an identical later submit is then a fresh dispatch with a new id.
    latest.clearAttempt("!u-ab12cd ls");
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_new", { action: "send" }), terminal: null }),
      ),
    );
    await latest.run(action, "!u-ab12cd pwd");
    expect((lastRequest().body.data as Record<string, unknown>).attempt_id).not.toBe(
      unknownAttempt,
    );
  });

  it("clears the attempt id after ok and definite-error outcomes", async () => {
    const action = { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" } as const;
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_ok_1", { action: "send" }), terminal: null }),
      ),
    );
    renderHarness();
    await latest.run(action, "!u-ab12cd pwd");
    const okAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;

    fetchMock.mockResolvedValueOnce(
      mockResponse({ error: { message: "terminal moved" } }, { ok: false, status: 409 }),
    );
    await expect(latest.run(action, "!u-ab12cd pwd")).rejects.toThrow("terminal moved");
    const errorAttempt = (lastRequest().body.data as Record<string, unknown>).attempt_id;
    expect(errorAttempt).not.toBe(okAttempt);

    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({ item: receiptItem("tc_ok_2", { action: "send" }), terminal: null }),
      ),
    );
    await latest.run(action, "!u-ab12cd pwd");
    expect((lastRequest().body.data as Record<string, unknown>).attempt_id).not.toBe(errorAttempt);
  });

  it("appends a 200 unknown receipt without restoring or retrying", async () => {
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        successBody({
          item: receiptItem("tc_unknown", { status: "unknown", action: "send" }),
          terminal: null,
        }),
      ),
    );
    renderHarness();
    await expect(
      latest.run(
        { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" },
        "!u-ab12cd pwd",
      ),
    ).resolves.toEqual({ status: "unknown" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(terminalReceiptBlocks()).toHaveLength(1);
    expect(terminalReceiptBlocks()[0]).toMatchObject({ status: "unknown" });
  });

  it.each([
    [400, "Terminal 'nope' is not declared by this agent."],
    [403, "Forbidden"],
    [404, "unknown terminal"],
    [409, "terminal not running"],
    [502, "Runner unreachable while sending terminal input"],
  ])("rejects with the server's message on %i", async (status, message) => {
    fetchMock.mockResolvedValueOnce(mockResponse({ error: { message } }, { ok: false, status }));
    renderHarness();
    await expect(
      latest.run(
        { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" },
        "!u-ab12cd pwd",
      ),
    ).rejects.toThrow(message);
    expect(focusShell).not.toHaveBeenCalled();
    expect(terminalReceiptBlocks()).toHaveLength(0);
  });

  it("falls back to the status line when the error body is not JSON", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 500,
      statusText: "Internal Server Error",
      json: async () => {
        throw new Error("not json");
      },
    } as unknown as Response);
    renderHarness();
    await expect(
      latest.run({ kind: "send", terminalId: "t", command: "pwd" }, "!t pwd"),
    ).rejects.toThrow("shell command failed: 500 Internal Server Error");
  });

  it("rejects an error-kind action without any request", async () => {
    renderHarness();
    await expect(latest.run({ kind: "error", reason: "no shell `x`" }, "!x")).rejects.toThrow(
      "no shell `x`",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("exposes pending=true while the POST is in flight", async () => {
    let resolveFetch: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderHarness();
    const running = latest.run({ kind: "send", terminalId: "t", command: "pwd" }, "!t pwd");
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("true"));
    resolveFetch(
      mockResponse(successBody({ item: receiptItem("tc_2", { action: "send" }), terminal: null })),
    );
    await running;
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("false"));
  });

  it("scopes pending per conversation so a mid-flight switch frees the new session", async () => {
    let resolveA: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveA = resolve;
      }),
    );
    const { switchTo } = renderSwitchHarness("conv_A");
    const aInFlight = latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-a1", command: "sleep" },
      "!u-a1 sleep",
    );
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("true"));

    // Switch to B while A's request is still open — B's composer must be free.
    switchTo("conv_B");
    expect(screen.getByLabelText("pending").textContent).toBe("false");

    // A B submit proceeds instead of being swallowed by A's in-flight mutex.
    fetchMock.mockResolvedValueOnce(
      mockResponse(successBody({ item: receiptItem("tc_b", { action: "send" }), terminal: null })),
    );
    await latest.run({ kind: "send", terminalId: "terminal_zsh_u-b1", command: "ls" }, "!u-b1 ls");
    expect(fetchMock).toHaveBeenCalledTimes(2);

    resolveA(
      mockResponse(successBody({ item: receiptItem("tc_a", { action: "send" }), terminal: null })),
    );
    await aInFlight;
  });

  it("restores pending when switching back to a still-in-flight conversation", async () => {
    let resolveA: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveA = resolve;
      }),
    );
    const { switchTo } = renderSwitchHarness("conv_A");
    const aInFlight = latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-a1", command: "sleep" },
      "!u-a1 sleep",
    );
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("true"));

    switchTo("conv_B");
    expect(screen.getByLabelText("pending").textContent).toBe("false");

    // Returning to A while its request is unsettled shows pending again — the
    // Map-scoped derivation restores it without any resubmit.
    switchTo("conv_A");
    expect(screen.getByLabelText("pending").textContent).toBe("true");

    resolveA(
      mockResponse(successBody({ item: receiptItem("tc_a", { action: "send" }), terminal: null })),
    );
    await aInFlight;
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("false"));
  });

  it("settles only the finishing conversation's slot, leaving the other usable", async () => {
    let resolveA: (r: Response) => void = () => {};
    fetchMock.mockReturnValueOnce(
      new Promise<Response>((resolve) => {
        resolveA = resolve;
      }),
    );
    const { switchTo } = renderSwitchHarness("conv_A");
    const aInFlight = latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-a1", command: "sleep" },
      "!u-a1 sleep",
    );
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("true"));

    // B runs and settles fully while A is still open.
    switchTo("conv_B");
    fetchMock.mockResolvedValueOnce(
      mockResponse(successBody({ item: receiptItem("tc_b", { action: "send" }), terminal: null })),
    );
    await latest.run({ kind: "send", terminalId: "terminal_zsh_u-b1", command: "ls" }, "!u-b1 ls");
    await waitFor(() => expect(screen.getByLabelText("pending").textContent).toBe("false"));

    // A's late resolution clears A's slot without disturbing B's state.
    resolveA(
      mockResponse(successBody({ item: receiptItem("tc_a", { action: "send" }), terminal: null })),
    );
    await aInFlight;
    expect(screen.getByLabelText("pending").textContent).toBe("false");

    // A is settled: back on A, a fresh submit works (a new request goes out).
    switchTo("conv_A");
    expect(screen.getByLabelText("pending").textContent).toBe("false");
    fetchMock.mockResolvedValueOnce(
      mockResponse(successBody({ item: receiptItem("tc_a2", { action: "send" }), terminal: null })),
    );
    await latest.run(
      { kind: "send", terminalId: "terminal_zsh_u-a1", command: "pwd" },
      "!u-a1 pwd",
    );
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });
});
