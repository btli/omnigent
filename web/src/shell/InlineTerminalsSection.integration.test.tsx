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
});
