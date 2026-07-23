import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TerminalInfo } from "@/hooks/useTerminals";
import type { ChangedSort } from "./FlatFileList";
import type { RightRailTab } from "./railTabs";
import { makeTerminal } from "./testTerminals";
import { WorkspacePanel } from "./WorkspacePanel";

// The rail's content children are exercised by their own suites; stub them so
// these tests focus on WorkspacePanel's own logic (the open-file tab strip and
// the content branch that swaps FileViewer ↔ FilesPanel). Each stub renders a
// testid (plus, for FileViewer, the path it was asked to show) so we can prove
// which child mounted without dragging in Monaco / hook stacks.
vi.mock("./FileViewer", () => ({
  FileViewer: ({ path }: { path: string }) => <div data-testid="file-viewer-stub">{path}</div>,
}));
vi.mock("./FilesPanel", () => ({
  FilesPanel: () => <div data-testid="files-panel-stub" />,
}));
vi.mock("./InlineTerminalsSection", () => ({
  // Echo the desktop props (inline + readOnly) so the rail's Shells tab
  // can be proven to host the terminal inline, not route to the overlay.
  InlineTerminalsSection: ({ inline, readOnly }: { inline?: boolean; readOnly?: boolean }) => (
    <div
      data-testid="terminals-stub"
      data-inline={String(inline ?? false)}
      data-read-only={String(readOnly ?? false)}
    />
  ),
}));
vi.mock("./SubagentsPanel", () => ({
  SubagentsPanel: () => <div data-testid="subagents-stub" />,
}));
vi.mock("./TodoPanel", () => ({
  TodoPanel: () => <div data-testid="todos-stub" />,
}));
vi.mock("@/components/BrowserPane/BrowserPane", () => ({
  BrowserPane: ({ conversationId }: { conversationId: string }) => (
    <div data-testid="browser-pane-stub">{conversationId}</div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

/**
 * Render WorkspacePanel with a complete prop set, overridable per test. Returns
 * the spied callbacks the tests assert against (openFileViewer / onCloseFile /
 * onRightRailTabChange) alongside the render result.
 */
function renderWorkspace(
  overrides: {
    rightRailTab?: RightRailTab;
    selectedFilePath?: string | null;
    openFiles?: string[];
    showBrowserTab?: boolean;
    showShellsTab?: boolean;
    permissionLevel?: number | null;
    openShells?: TerminalInfo[];
    activeShellKey?: string | null;
    hostsShellsInline?: boolean;
    sessionLabelsReady?: boolean;
  } = {},
) {
  const openFileViewer = vi.fn();
  const onCloseFile = vi.fn();
  const onRightRailTabChange = vi.fn();
  const onOpenShell = vi.fn();
  const onCloseShell = vi.fn();
  const onReturnToShellList = vi.fn();
  render(
    <WorkspacePanel
      conversationId="conv_ws"
      width={360}
      handleProps={{ tabIndex: 0 }}
      rightRailTab={overrides.rightRailTab ?? "files"}
      onRightRailTabChange={onRightRailTabChange}
      showFilesPanel
      showBrowserTab={overrides.showBrowserTab ?? false}
      changedCount={0}
      showShellsTab={overrides.showShellsTab ?? false}
      terminalsLength={0}
      subagentsWorking={0}
      agentCount={1}
      todosSupported={false}
      todosCompleted={0}
      todosTotal={0}
      rootSessionId={null}
      selectedFilePath={overrides.selectedFilePath ?? null}
      openFiles={overrides.openFiles ?? []}
      openFileViewer={openFileViewer}
      onCloseFile={onCloseFile}
      onShowScopeView={vi.fn()}
      onCommentsOpenChange={vi.fn()}
      openTerminalsPanel={vi.fn()}
      openShells={overrides.openShells ?? []}
      activeShellKey={overrides.activeShellKey ?? null}
      onOpenShell={onOpenShell}
      onCloseShell={onCloseShell}
      onReturnToShellList={onReturnToShellList}
      hostsShellsInline={overrides.hostsShellsInline ?? true}
      sessionLabelsReady={overrides.sessionLabelsReady ?? true}
      permissionLevel={overrides.permissionLevel ?? null}
      filesPanelSort={"recent" as ChangedSort}
      onSortChange={vi.fn()}
      filesPanelFlatView={false}
      onFlatViewChange={vi.fn()}
      filesPanelShowHidden={false}
      onShowHiddenChange={vi.fn()}
    />,
  );
  return { openFileViewer, onCloseFile, onRightRailTabChange, onOpenShell, onCloseShell };
}

describe("WorkspacePanel open-file tabs", () => {
  it("renders a tab per open file labeled by basename, next to the fixed Files tab", () => {
    renderWorkspace({ openFiles: ["src/App.tsx", "docs/README.md"] });

    // The fixed Files tab and one file tab per open file (by basename, not the
    // full path). A failure means the strip didn't iterate openFiles or used
    // the full path instead of the basename.
    expect(screen.getByRole("tab", { name: /files/i })).toBeInTheDocument();
    expect(screen.getByText("App.tsx")).toBeInTheDocument();
    expect(screen.getByText("README.md")).toBeInTheDocument();
  });

  it("renders no file tabs when none are open", () => {
    renderWorkspace({ openFiles: [] });

    // No open files → no per-tab close buttons. A failure means the strip
    // rendered for an empty list.
    expect(screen.queryByRole("button", { name: /^Close / })).toBeNull();
  });

  it("marks the active file tab and leaves the Files tab inactive", () => {
    renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
      selectedFilePath: "docs/README.md",
    });

    // The active file's tab carries aria-current; the other does not. Located
    // via the uniquely-labeled close button since the basename text also
    // appears in the FileViewer stub.
    const readmeTab = screen
      .getByRole("button", { name: "Close README.md" })
      .closest("[role='button']");
    const appTab = screen.getByRole("button", { name: "Close App.tsx" }).closest("[role='button']");
    expect(readmeTab).toHaveAttribute("aria-current", "true");
    expect(appTab).toHaveAttribute("aria-current", "false");

    // With a file active the radix value is a sentinel, so the fixed Files tab
    // must read inactive — otherwise both "Files" and the file tab would look
    // selected at once (the bug the sentinel prevents).
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "inactive");
  });

  it("shows the Files tab as active when no file is selected", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });

    // No file selected on the Files tab → the fixed Files trigger is the active
    // selection. A failure means the sentinel leaked into the no-file case.
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "active");
  });

  it("activates a file via openFileViewer when its tab body is clicked", () => {
    const { openFileViewer } = renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
    });

    fireEvent.click(screen.getByText("README.md"));

    // Clicking the tab body opens that file. A failure means the row's onClick
    // isn't wired to openFileViewer with the tab's full path.
    expect(openFileViewer).toHaveBeenCalledWith("docs/README.md");
  });

  it("closes a file via onCloseFile (and does not also open it) when the x is clicked", () => {
    const { openFileViewer, onCloseFile } = renderWorkspace({
      openFiles: ["src/App.tsx", "docs/README.md"],
    });

    fireEvent.click(screen.getByRole("button", { name: "Close App.tsx" }));

    // The x closes exactly that file and must not also activate it
    // (stopPropagation), or closing would race with a selection.
    expect(onCloseFile).toHaveBeenCalledWith("src/App.tsx");
    expect(openFileViewer).not.toHaveBeenCalled();
  });
});

describe("WorkspacePanel content area", () => {
  it("renders the FileViewer for the active path (not the scope panel)", () => {
    renderWorkspace({
      openFiles: ["src/App.tsx"],
      selectedFilePath: "src/App.tsx",
    });

    // A selected file shows its viewer in the content slot; the scope panel
    // must not also mount. The stub echoes the path it received.
    expect(screen.getByTestId("file-viewer-stub")).toHaveTextContent("src/App.tsx");
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });

  it("renders the FilesPanel scope view when no file is active on the Files tab", () => {
    renderWorkspace({ rightRailTab: "files", selectedFilePath: null });

    // No active file → the scope view (Changed/All list/tree) owns the content
    // slot and the viewer is unmounted.
    expect(screen.getByTestId("files-panel-stub")).toBeInTheDocument();
    expect(screen.queryByTestId("file-viewer-stub")).toBeNull();
  });
});

describe("WorkspacePanel shells tab", () => {
  it("hosts the shells section inline (desktop) when the Shells tab is active", () => {
    renderWorkspace({ rightRailTab: "terminals", showShellsTab: true });

    // The rail hosts the terminal inline (mirroring the Files tab's
    // FileViewer), not via the full-screen overlay. `inline` proves the
    // desktop path; a false value would regress to the overlay.
    const stub = screen.getByTestId("terminals-stub");
    expect(stub).toBeInTheDocument();
    expect(stub).toHaveAttribute("data-inline", "true");
    // Owner (null level) attaches read-write.
    expect(stub).toHaveAttribute("data-read-only", "false");
  });

  it("attaches the inline shell read-only for non-owners", () => {
    renderWorkspace({ rightRailTab: "terminals", showShellsTab: true, permissionLevel: 2 });

    // A non-owner (EDIT level) watches but can't type — a shared PTY
    // can't attribute keystrokes per-user.
    expect(screen.getByTestId("terminals-stub")).toHaveAttribute("data-read-only", "true");
  });

  it("hides the Shells tab when showShellsTab is false", () => {
    renderWorkspace({ showShellsTab: false });
    expect(screen.queryByRole("tab", { name: /shells/i })).toBeNull();
  });
});

describe("WorkspacePanel shell identity tabs (header parity with Files)", () => {
  it("renders a top-strip tab per open shell showing its name · session identity", () => {
    renderWorkspace({
      rightRailTab: "terminals",
      showShellsTab: true,
      openShells: [makeTerminal("terminal_bash_s1", "bash", "s1")],
      activeShellKey: "terminal:terminal_bash_s1",
    });

    // The shell surfaces as a tab in the shared top strip (mirroring file
    // tabs) with its identity — name and session — not just the rail content.
    expect(screen.getByText("bash · s1")).toBeInTheDocument();
    // And it's the active tab; the fixed Shells trigger reads inactive so both
    // aren't highlighted at once (the sentinel that prevents that).
    const tab = screen.getByRole("button", { name: /close bash · s1/i }).closest("[role='button']");
    expect(tab).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("tab", { name: /shells/i })).toHaveAttribute("data-state", "inactive");
  });

  it("selects a shell tab via onOpenShell and closes it via onCloseShell", () => {
    const { onOpenShell, onCloseShell } = renderWorkspace({
      rightRailTab: "terminals",
      showShellsTab: true,
      openShells: [
        makeTerminal("terminal_bash_s1", "bash", "s1"),
        makeTerminal("terminal_worker_s2", "worker", "s2"),
      ],
      activeShellKey: "terminal:terminal_bash_s1",
    });

    fireEvent.click(screen.getByText("worker · s2"));
    // Strip-tab clicks carry the current conversation as the mutation source
    // and pendingInventory=false (the shell is already open, not a create).
    expect(onOpenShell).toHaveBeenCalledWith("terminal:terminal_worker_s2", "conv_ws", false);

    fireEvent.click(screen.getByRole("button", { name: /close worker · s2/i }));
    expect(onCloseShell).toHaveBeenCalledWith("terminal:terminal_worker_s2");
    // The x must not also activate the tab (stopPropagation).
    expect(onOpenShell).toHaveBeenCalledTimes(1);
  });

  it("shows no shell tabs when none are open", () => {
    renderWorkspace({ rightRailTab: "terminals", showShellsTab: true, openShells: [] });
    expect(screen.queryByRole("button", { name: /^Close .* · / })).toBeNull();
  });

  it("shows open shell tabs even while a NON-shell rail tab is active (symmetric with files)", () => {
    // D2: shell tabs are peers of file tabs in the shared strip — visible and
    // switchable regardless of the selected rail tab, so one click reaches any
    // open surface. Here the Files tab is active but the shell tab still shows.
    const { onOpenShell } = renderWorkspace({
      rightRailTab: "files",
      showShellsTab: true,
      openShells: [makeTerminal("terminal_bash_s1", "bash", "s1")],
      activeShellKey: null,
    });

    const shellTab = screen.getByText("bash · s1");
    expect(shellTab).toBeInTheDocument();
    // Clicking it activates the shell inline (AppShell pulls the rail to
    // Shells) — the same one-click switch a file tab gives.
    fireEvent.click(shellTab);
    expect(onOpenShell).toHaveBeenCalledWith("terminal:terminal_bash_s1", "conv_ws", false);
  });

  it("does not highlight the shell tab when another rail tab is displayed (highlight-desync)", () => {
    // Regression: ``activeShellKey`` stays set so returning to the Shells tab
    // resumes the same shell, but the tab highlight must track the DISPLAYED
    // surface. With the Files (Agents/etc.) tab selected, the shell tab must
    // read inactive and the fixed Files trigger active — not a stale shell tab
    // left aria-current while other content shows.
    renderWorkspace({
      rightRailTab: "files",
      showShellsTab: true,
      openShells: [makeTerminal("terminal_bash_s1", "bash", "s1")],
      activeShellKey: "terminal:terminal_bash_s1",
    });

    const shellTab = screen
      .getByRole("button", { name: /close bash · s1/i })
      .closest("[role='button']");
    expect(shellTab).toHaveAttribute("aria-current", "false");
    expect(screen.getByRole("tab", { name: /files/i })).toHaveAttribute("data-state", "active");
  });

  it("does not highlight the shell tab when a file is open (highlight-desync)", () => {
    // Even on the Shells rail tab, an open file wins the content slot (FileViewer
    // renders), so the file tab is current and the shell tab must not be.
    renderWorkspace({
      rightRailTab: "terminals",
      showShellsTab: true,
      selectedFilePath: "README.md",
      openFiles: ["README.md"],
      openShells: [makeTerminal("terminal_bash_s1", "bash", "s1")],
      activeShellKey: "terminal:terminal_bash_s1",
    });

    const shellTab = screen
      .getByRole("button", { name: /close bash · s1/i })
      .closest("[role='button']");
    expect(shellTab).toHaveAttribute("aria-current", "false");
    // The open file's tab carries the highlight instead.
    const fileTab = screen
      .getByRole("button", { name: /close readme\.md/i })
      .closest("[role='button']");
    expect(fileTab).toHaveAttribute("aria-current", "true");
  });

  it("renders the in-tab status as a DOT ONLY, without the status word (A2)", () => {
    renderWorkspace({
      rightRailTab: "terminals",
      showShellsTab: true,
      openShells: [makeTerminal("terminal_bash_s1", "bash", "s1")],
      activeShellKey: "terminal:terminal_bash_s1",
    });

    // The status dot carries its label for a11y/tooltip, but the word must not
    // render as visible text in the tab (keeps shell tabs as light as file tabs).
    expect(screen.getByLabelText("Idle")).toBeInTheDocument();
    expect(screen.queryByText("Idle")).toBeNull();
  });
});

describe("WorkspacePanel browser tab", () => {
  it("renders the Browser tab only when showBrowserTab is set", () => {
    renderWorkspace({ showBrowserTab: true });
    expect(screen.getByRole("tab", { name: /browser/i })).toBeInTheDocument();
  });

  it("omits the Browser tab when showBrowserTab is false", () => {
    renderWorkspace({ showBrowserTab: false });
    expect(screen.queryByRole("tab", { name: /browser/i })).toBeNull();
  });

  it("mounts the browser pane when the browser tab is selected", () => {
    renderWorkspace({ showBrowserTab: true, rightRailTab: "browser" });
    // The content slot swaps to the embedded browser pane (stubbed here).
    expect(screen.getByTestId("browser-pane-stub")).toBeInTheDocument();
    // And the file scope views are not mounted in that branch.
    expect(screen.queryByTestId("files-panel-stub")).toBeNull();
  });
});
