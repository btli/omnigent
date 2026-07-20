import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { type TerminalInfo, useTerminals } from "@/hooks/useTerminals";
import type { TerminalFirstContextValue } from "./TerminalFirstContext";
import { TerminalFirstContextProvider } from "./TerminalFirstContext";
import { InlineTerminalsSection } from "./InlineTerminalsSection";

vi.mock("@/hooks/useTerminals", async (importOriginal) => ({
  // Keep the real module (inventoryTerminals etc.) — only the
  // network-backed hook is replaced.
  ...(await importOriginal<typeof import("@/hooks/useTerminals")>()),
  useTerminals: vi.fn(),
}));

// Stub the xterm view so inline-mode tests can assert which terminal is
// hosted (and its read-only flag) without dragging in the WebSocket stack.
vi.mock("@/components/blocks/TerminalView", () => ({
  TerminalView: ({
    sessionId,
    terminalId,
    readOnly,
  }: {
    sessionId: string;
    terminalId: string;
    readOnly?: boolean;
  }) => (
    <div
      data-testid="terminal-view"
      data-session-id={sessionId}
      data-terminal-id={terminalId}
      data-read-only={String(readOnly ?? false)}
    />
  ),
}));

// These tests cover row navigation, not shell creation. The button
// needs a QueryClient (it reads the session agent for its access
// gate); its behavior is covered by NewTerminalButton.test.tsx. The
// marker keeps the variant visible so the virtual-row placement is
// assertable.
vi.mock("./NewTerminalButton", () => ({
  NewTerminalButton: ({ variant }: { variant?: string }) => (
    <div data-testid="new-shell-button" data-variant={variant} />
  ),
}));

const useTerminalsMock = vi.mocked(useTerminals);

function makeTerminal(id: string, name: string, session: string): TerminalInfo {
  return {
    id,
    name,
    session,
    running: true,
  };
}

/**
 * Minimal TerminalFirst context for a terminal-first SDK session, so
 * the section's inventory filter (REPL excluded) is exercised.
 */
const TERMINAL_FIRST_SDK_CTX = {
  isClaudeNative: false,
  isNativeWrapper: false,
  isTerminalFirst: true,
  isShellView: false,
  view: "chat",
  terminalViewKey: null,
  setView: () => {},
  terminalsAvailable: true,
  terminalStartingUp: false,
} as TerminalFirstContextValue;

// A regular (non-terminal-first) session: the rail hosts shells inline
// rather than replacing the main column. This is the desktop-fix path.
const REGULAR_CTX = {
  ...TERMINAL_FIRST_SDK_CTX,
  isTerminalFirst: false,
} as TerminalFirstContextValue;

function renderInlineSection(
  terminals: TerminalInfo[],
  onExpand: (key: string) => void = vi.fn(),
  opts: { inline?: boolean; readOnly?: boolean; ctx?: TerminalFirstContextValue } = {},
) {
  useTerminalsMock.mockReturnValue({
    terminals,
    isLoading: false,
    error: null,
  });
  return render(
    <TerminalFirstContextProvider value={opts.ctx ?? TERMINAL_FIRST_SDK_CTX}>
      <InlineTerminalsSection
        conversationId="conv_terminal"
        onExpand={onExpand}
        inline={opts.inline}
        readOnly={opts.readOnly}
      />
    </TerminalFirstContextProvider>,
  );
}

beforeEach(() => {
  useTerminalsMock.mockReset();
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("InlineTerminalsSection rows open shells in the main view", () => {
  it("clicking a shell row hands its key to onExpand instead of mounting an in-rail xterm", () => {
    const onExpand = vi.fn();
    renderInlineSection(
      [
        makeTerminal("terminal_bash_s1", "bash", "s1"),
        makeTerminal("terminal_worker_s2", "worker", "s2"),
      ],
      onExpand,
    );

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));

    // The shell must take over the MAIN view via onExpand — the rail
    // never mounts an xterm of its own. A missing call means rows went
    // back to in-rail selection; a wrong key opens the wrong shell.
    expect(onExpand).toHaveBeenCalledWith("terminal:terminal_bash_s1");
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("lists every shell with no selection state", () => {
    renderInlineSection([
      makeTerminal("terminal_bash_s1", "bash", "s1"),
      makeTerminal("terminal_worker_s2", "worker", "s2"),
    ]);

    expect(screen.getByRole("button", { name: /s1/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /s2/ })).toBeInTheDocument();
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });

  it("excludes the embedded REPL terminal from the shell list", () => {
    renderInlineSection([
      makeTerminal("terminal_tui_main", "tui", "main"),
      makeTerminal("terminal_bash_s1", "bash", "s1"),
    ]);

    // The REPL backs the Chat/Terminal pill — a row for it here is the
    // phantom "main" terminal regression.
    expect(screen.queryByText("main")).toBeNull();
    expect(screen.getByRole("button", { name: /s1/ })).toBeInTheDocument();
  });

  it("renders only the virtual new-shell row when the only terminal is the embedded REPL", () => {
    renderInlineSection([makeTerminal("terminal_tui_main", "tui", "main")]);

    // No centered empty-state copy — the list IS the surface, and with
    // zero shells it consists of just the virtual "+ New shell" row.
    const row = screen.getByTestId("new-shell-button");
    expect(row).toHaveAttribute("data-variant", "row");
    expect(screen.queryByText("No shells running.")).toBeNull();
  });

  it("keeps the virtual new-shell row above the shell rows", () => {
    renderInlineSection([makeTerminal("terminal_bash_s1", "bash", "s1")]);

    const row = screen.getByTestId("new-shell-button");
    expect(row).toHaveAttribute("data-variant", "row");
    // Leading keeps the affordance at a fixed spot — trailing would
    // drift down as shells accumulate.
    const shellRow = screen.getByRole("button", { name: /s1/ });
    expect(row.compareDocumentPosition(shellRow) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });
});

describe("InlineTerminalsSection inline mode hosts the shell in the rail", () => {
  it("clicking a shell row mounts its TerminalView in-rail instead of calling onExpand", () => {
    const onExpand = vi.fn();
    renderInlineSection(
      [
        makeTerminal("terminal_bash_s1", "bash", "s1"),
        makeTerminal("terminal_worker_s2", "worker", "s2"),
      ],
      onExpand,
      { inline: true, ctx: REGULAR_CTX },
    );

    // Baseline: the list is showing, no xterm yet.
    expect(screen.queryByTestId("terminal-view")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));

    // The shell now renders INSIDE the rail (the FileViewer-in-rail
    // mirror) and onExpand — the full-screen overlay path — is never
    // called on desktop.
    const view = screen.getByTestId("terminal-view");
    expect(view).toHaveAttribute("data-terminal-id", "terminal_bash_s1");
    expect(onExpand).not.toHaveBeenCalled();
  });

  it("returns focus to the shell list without announcing an intentional back action", () => {
    renderInlineSection([makeTerminal("terminal_bash_s1", "bash", "s1")], vi.fn(), {
      inline: true,
      ctx: REGULAR_CTX,
    });

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));
    expect(screen.getByTestId("terminal-view")).toBeInTheDocument();

    // Back returns keyboard users to the list without presenting an
    // intentional navigation action as a shell failure.
    fireEvent.click(screen.getByRole("button", { name: /back to shells/i }));
    expect(screen.queryByTestId("terminal-view")).toBeNull();
    expect(screen.getByTestId("new-shell-button")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /shell list/i })).toHaveFocus();
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("attaches the hosted shell read-only for non-owners", () => {
    renderInlineSection([makeTerminal("terminal_bash_s1", "bash", "s1")], vi.fn(), {
      inline: true,
      readOnly: true,
      ctx: REGULAR_CTX,
    });

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));
    expect(screen.getByTestId("terminal-view")).toHaveAttribute("data-read-only", "true");
  });

  it("focuses the list and announces when the hosted shell disappears", () => {
    const { rerender } = renderInlineSection(
      [makeTerminal("terminal_bash_s1", "bash", "s1")],
      vi.fn(),
      { inline: true, ctx: REGULAR_CTX },
    );

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));
    expect(screen.getByTestId("terminal-view")).toBeInTheDocument();

    // The shell closes out from under the rail — the view drops back to
    // the list, restores focus, and tells assistive tech what happened.
    useTerminalsMock.mockReturnValue({ terminals: [], isLoading: false, error: null });
    rerender(
      <TerminalFirstContextProvider value={REGULAR_CTX}>
        <InlineTerminalsSection conversationId="conv_terminal" onExpand={vi.fn()} inline />
      </TerminalFirstContextProvider>,
    );

    expect(screen.queryByTestId("terminal-view")).toBeNull();
    expect(screen.getByTestId("new-shell-button")).toBeInTheDocument();
    expect(screen.getByRole("region", { name: /shell list/i })).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent(
      "The active shell closed unexpectedly. Focus returned to the shell list.",
    );
  });

  it("routes terminal-first shells to onExpand even with inline set (main-column takeover)", () => {
    // Terminal-first sessions keep their chat-replacing UX: the shell
    // opens in the main column via MainTerminalView, not in the rail.
    const onExpand = vi.fn();
    renderInlineSection([makeTerminal("terminal_bash_s1", "bash", "s1")], onExpand, {
      inline: true,
      ctx: TERMINAL_FIRST_SDK_CTX,
    });

    fireEvent.click(screen.getByRole("button", { name: /s1/ }));

    expect(onExpand).toHaveBeenCalledWith("terminal:terminal_bash_s1");
    expect(screen.queryByTestId("terminal-view")).toBeNull();
  });
});
