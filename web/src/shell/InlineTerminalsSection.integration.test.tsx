import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import { InlineTerminalsSection } from "./InlineTerminalsSection";
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useTerminals")>()),
  useTerminals: vi.fn(),
}));

vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({ terminalId }: { terminalId: string }) => (
    <div data-testid="terminal-view" data-terminal-id={terminalId} />
  ),
}));

const useTerminalsMock = vi.mocked(useTerminals);
const fetchMock = vi.fn();

const REGULAR_CTX = {
  isClaudeNative: false,
  isNativeWrapper: false,
  isTerminalFirst: false,
  isShellView: false,
  view: "chat",
  terminalViewKey: null,
  setView: () => {},
  terminalsAvailable: true,
  terminalStartingUp: false,
} as TerminalFirstContextValue;

function mockResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

function makeTerminal(id: string, name: string, session: string): TerminalInfo {
  return { id, name, session, running: true };
}

beforeEach(() => {
  useTerminalsMock.mockReset();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("InlineTerminalsSection new-shell integration", () => {
  it("shows the shell selected by NewTerminalButton.onCreated after inventory catches up", async () => {
    let terminals: TerminalInfo[] = [];
    useTerminalsMock.mockImplementation(() => ({ terminals, isLoading: false, error: null }));

    let finishCreate: (response: Response) => void = () => {};
    const createResponse = new Promise<Response>((resolve) => {
      finishCreate = resolve;
    });
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return createResponse;
      return mockResponse({
        id: "ag_1",
        object: "agent",
        name: "test-agent",
        terminals: ["shell"],
      });
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const onExpand = vi.fn();
    const section = () => (
      <QueryClientProvider client={queryClient}>
        <TerminalFirstContextProvider value={REGULAR_CTX}>
          <InlineTerminalsSection conversationId="conv_terminal" onExpand={onExpand} inline />
        </TerminalFirstContextProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(section());

    const newShellButton = await screen.findByRole("button", { name: /new shell/i });
    fireEvent.click(newShellButton);
    await waitFor(() => expect(newShellButton).toBeDisabled());

    finishCreate(
      mockResponse({
        id: "terminal_shell_new",
        object: "session.resource",
        type: "terminal",
        session_id: "conv_terminal",
        name: "shell:new",
        metadata: { terminal_name: "shell", session_key: "new", running: true },
      }),
    );
    await waitFor(() => expect(newShellButton).toBeEnabled());

    // onCreated can run before the inventory hook's next render. That gap
    // must not be mistaken for an unexpected close and clear the selection.
    expect(screen.queryByTestId("terminal-view")).toBeNull();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();

    terminals = [makeTerminal("terminal_shell_new", "shell", "new")];
    rerender(section());

    expect(screen.getByTestId("terminal-view")).toHaveAttribute(
      "data-terminal-id",
      "terminal_shell_new",
    );
    expect(onExpand).not.toHaveBeenCalled();
  });

  it("M4: re-checks the latest routing verdict when a create finishes after native labels arrive", async () => {
    // Cold-load a native-wrapper session while labels are pending: the
    // routing verdict defaults inline (hostsShellsInline=true), so "New
    // shell" captures an inline target. If the POST completes AFTER the
    // native verdict lands (hostsShellsInline=false), the fix re-reads the
    // LATEST verdict in the create callback and routes full-screen via
    // onExpand instead of hosting an inline-owned tab that renders no
    // terminal. NOTE: this suite stubs TerminalView, so we assert the
    // observable routing outcome (onExpand vs in-rail mount), not a real
    // PTY attach.
    let terminals: TerminalInfo[] = [];
    useTerminalsMock.mockImplementation(() => ({ terminals, isLoading: false, error: null }));

    let finishCreate: (response: Response) => void = () => {};
    const createResponse = new Promise<Response>((resolve) => {
      finishCreate = resolve;
    });
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return createResponse;
      return mockResponse({
        id: "ag_1",
        object: "agent",
        name: "test-agent",
        terminals: ["shell"],
      });
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const onExpand = vi.fn();
    // ``hostsShellsInline`` is AppShell's single verdict; start true (labels
    // pending → inline default), flip false once native labels resolve.
    const section = (hostsShellsInline: boolean) => (
      <QueryClientProvider client={queryClient}>
        <TerminalFirstContextProvider value={REGULAR_CTX}>
          <InlineTerminalsSection
            conversationId="conv_native"
            onExpand={onExpand}
            inline
            hostsShellsInline={hostsShellsInline}
          />
        </TerminalFirstContextProvider>
      </QueryClientProvider>
    );
    const { rerender } = render(section(true));

    const newShellButton = await screen.findByRole("button", { name: /new shell/i });
    fireEvent.click(newShellButton);
    await waitFor(() => expect(newShellButton).toBeDisabled());

    // Native labels resolve mid-flight: the verdict flips to full-screen.
    rerender(section(false));

    finishCreate(
      mockResponse({
        id: "terminal_shell_new",
        object: "session.resource",
        type: "terminal",
        session_id: "conv_native",
        name: "shell:new",
        metadata: { terminal_name: "shell", session_key: "new", running: true },
      }),
    );
    await waitFor(() => expect(newShellButton).toBeEnabled());

    // The create callback re-read the CURRENT verdict (native) and routed
    // full-screen — no inline xterm was hosted.
    expect(onExpand).toHaveBeenCalledWith("terminal:terminal_shell_new");
    terminals = [makeTerminal("terminal_shell_new", "shell", "new")];
    rerender(section(false));
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("M4: parks shell-create until the routing verdict is authoritative (labels not ready)", async () => {
    // While sessionLabelsReady is false the routing target is ambiguous, so
    // the "New shell" affordance is disabled — a shell can't be created
    // against a mislabeled default and stranded.
    useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });
    fetchMock.mockImplementation(async () =>
      mockResponse({ id: "ag_1", object: "agent", name: "test-agent", terminals: ["shell"] }),
    );

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const { rerender } = render(
      <QueryClientProvider client={queryClient}>
        <TerminalFirstContextProvider value={REGULAR_CTX}>
          <InlineTerminalsSection
            conversationId="conv_native"
            onExpand={vi.fn()}
            inline
            hostsShellsInline
            sessionLabelsReady={false}
          />
        </TerminalFirstContextProvider>
      </QueryClientProvider>,
    );

    const newShellButton = await screen.findByRole("button", { name: /new shell/i });
    expect(newShellButton).toBeDisabled();

    // Once the labels settle the affordance re-enables.
    rerender(
      <QueryClientProvider client={queryClient}>
        <TerminalFirstContextProvider value={REGULAR_CTX}>
          <InlineTerminalsSection
            conversationId="conv_native"
            onExpand={vi.fn()}
            inline
            hostsShellsInline
            sessionLabelsReady
          />
        </TerminalFirstContextProvider>
      </QueryClientProvider>,
    );
    expect(await screen.findByRole("button", { name: /new shell/i })).toBeEnabled();
  });

  it("M5: a create started in A carries A as its source, not the navigated-to B", async () => {
    // Start "New shell" in conversation A, then navigate to B (the keyed
    // remount unmounts A's section). If A's POST resolved against a
    // still-mounted A instance, the onOpenShell callback must carry A as the
    // source so AppShell can reject the mutation for B. Here we assert the
    // SOURCE tag is A's id — the observable half of the owner gate (the
    // suite stubs the transport, so we don't assert a real B attach).
    let terminals: TerminalInfo[] = [];
    useTerminalsMock.mockImplementation(() => ({ terminals, isLoading: false, error: null }));

    let finishCreate: (response: Response) => void = () => {};
    const createResponse = new Promise<Response>((resolve) => {
      finishCreate = resolve;
    });
    fetchMock.mockImplementation(async (_url: string, init?: RequestInit) => {
      if (init?.method === "POST") return createResponse;
      return mockResponse({
        id: "ag_1",
        object: "agent",
        name: "test-agent",
        terminals: ["shell"],
      });
    });

    const queryClient = new QueryClient({
      defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
    });
    const onOpenShell = vi.fn();
    render(
      <QueryClientProvider client={queryClient}>
        <TerminalFirstContextProvider value={REGULAR_CTX}>
          <InlineTerminalsSection
            conversationId="conv_a"
            onExpand={vi.fn()}
            inline
            hostsShellsInline
            activeKey={null}
            onOpenShell={onOpenShell}
            onReturnToList={vi.fn()}
          />
        </TerminalFirstContextProvider>
      </QueryClientProvider>,
    );

    const newShellButton = await screen.findByRole("button", { name: /new shell/i });
    fireEvent.click(newShellButton);
    await waitFor(() => expect(newShellButton).toBeDisabled());

    finishCreate(
      mockResponse({
        id: "terminal_shell_new",
        object: "session.resource",
        type: "terminal",
        session_id: "conv_a",
        name: "shell:new",
        metadata: { terminal_name: "shell", session_key: "new", running: true },
      }),
    );
    await waitFor(() => expect(newShellButton).toBeEnabled());

    // The selection callback tags conversation A as the source — AppShell's
    // openShellTab rejects it whenever the current conversation is no longer A.
    expect(onOpenShell).toHaveBeenCalledWith("terminal:terminal_shell_new", "conv_a");
  });
});
