// Composer `!` shell-command behavior (spec §3): menu trigger/suppression,
// keydown priority against the slash menu, token completion, the submit
// interception matrix (new/send/focus/error/escape/attachments/non-owner),
// success-vs-failure input handling, and the highlight-overlay tint.

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStore } from "@/store/chatStore";

// Same provider stubs as ChatPage.composer.test.tsx: the composer touches
// these hooks for mentions / host badge, which these tests don't exercise.
vi.mock("@/hooks/useWorkspaceChangedFiles", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/hooks/useWorkspaceChangedFiles")>();
  return {
    ...actual,
    useWorkspaceAllFiles: () => ({ data: undefined }),
    useWorkspaceDirectory: () => ({ data: undefined }),
  };
});
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useSession")>()),
  useSession: () => ({ session: { hostId: null }, isLoading: false, error: null }),
}));
vi.mock("@/hooks/useHosts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/useHosts")>()),
  useHosts: () => ({ data: [] }),
}));
vi.mock("@/hooks/RunnerHealthProvider", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/hooks/RunnerHealthProvider")>()),
  useSessionHostOnline: () => undefined,
}));
vi.mock("@/lib/agentLabels", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/agentLabels")>()),
  useBrainHarnessLabels: () => ({}),
}));

import type { BangAction } from "@/lib/composerBang";
import type { TerminalInfo } from "@/hooks/useTerminals";
import { Composer, splitBangCommand, type ComposerBangControl } from "./ChatPage";

function shell(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_zsh_u-ab12cd",
    name: "zsh",
    session: "u-ab12cd",
    running: true,
    ...overrides,
  };
}

function bangControl(overrides: Partial<ComposerBangControl> = {}): ComposerBangControl {
  return {
    shells: [shell()],
    declaredTypes: ["zsh", "bash"],
    metadataLoading: false,
    metadataError: false,
    isOwner: true,
    hasDefaultType: true,
    run: vi.fn().mockResolvedValue(undefined),
    clearAttempt: vi.fn(),
    pending: false,
    ...overrides,
  };
}

/** Minimal ComposerProps for an interactive (writable, idle) composer. */
function composerProps(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  return {
    status: "idle" as const,
    isWorking: false,
    disabled: false,
    onSend: vi.fn(),
    onStop: vi.fn(),
    agents: undefined,
    agentsLoading: false,
    selectedAgentId: null,
    onSelectAgent: vi.fn(),
    permissionLevel: null,
    readOnlyReason: null,
    replyQuotes: [],
    onRemoveQuote: vi.fn(),
    onClearAllQuotes: vi.fn(),
    effortLevels: ["low", "medium", "high"] as const,
    showEffort: true,
    showModels: false,
    modelPickerKind: null,
    codexModelOptions: [],
    showCodexPlanMode: false,
    ...overrides,
  };
}

function textarea() {
  return screen.getByLabelText("Message the agent") as HTMLTextAreaElement;
}

function type(value: string) {
  fireEvent.change(textarea(), { target: { value } });
}

function pressEnter() {
  fireEvent.keyDown(textarea(), { key: "Enter" });
}

/** Attach one image file through the composer's hidden file input. */
function attachFile() {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  const file = new File(["x"], "shot.png", { type: "image/png" });
  fireEvent.change(input, { target: { files: [file] } });
}

// Fresh conversation id per test: the composer persists drafts (text AND
// attached files) per session in a module-scope map on unmount, so a
// shared id would leak one test's draft into the next test's mount.
let convSeq = 0;
beforeEach(() => {
  convSeq += 1;
  useChatStore.setState({ conversationId: `conv_bang_${convSeq}`, skills: [] });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("bang menu trigger and suppression", () => {
  it("opens on ! with running shells on top and the types section below", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!");
    const menu = screen.getByTestId("bang-shell-menu");
    const text = menu.textContent ?? "";
    expect(text.indexOf("Running shells")).toBeLessThan(text.indexOf("New shell…"));
    expect(screen.getByTestId("bang-menu-item-u-ab12cd")).toBeDefined();
    expect(screen.getByTestId("bang-menu-item-zsh")).toBeDefined();
  });

  it("hides the types section when only one type is declared", () => {
    render(<Composer {...composerProps({ bang: bangControl({ declaredTypes: ["zsh"] }) })} />);
    type("!");
    expect(screen.getByTestId("bang-menu-item-u-ab12cd")).toBeDefined();
    expect(screen.queryByText("New shell…")).toBeNull();
    expect(screen.queryByTestId("bang-menu-item-zsh")).toBeNull();
  });

  it("shows no menu at all with no running shells and ≤1 type", () => {
    render(
      <Composer
        {...composerProps({ bang: bangControl({ shells: [], declaredTypes: ["zsh"] }) })}
      />,
    );
    type("!");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });

  it("shows no menu for non-owners", () => {
    render(<Composer {...composerProps({ bang: bangControl({ isOwner: false }) })} />);
    type("!");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });

  it("shows no menu while attachments are present", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    attachFile();
    type("!");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });

  it("shows live shells while declared types are loading or empty", () => {
    render(<Composer {...composerProps({ bang: bangControl({ declaredTypes: [] }) })} />);
    type("!");
    expect(screen.getByTestId("bang-menu-item-u-ab12cd")).toBeDefined();
  });

  it("reopens the menu when the caret moves back inside the bang token", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!u-ab12cd echo hi");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
    // Move the caret back inside `!u-ab12cd` and fire a selection change.
    const ta = textarea();
    ta.setSelectionRange(4, 4);
    fireEvent.select(ta);
    expect(screen.getByTestId("bang-shell-menu")).toBeDefined();
  });

  it("closes once the token is complete (whitespace typed)", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!u-ab12cd");
    expect(screen.getByTestId("bang-shell-menu")).toBeDefined();
    type("!u-ab12cd ");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });

  it("never opens the slash menu for bang input, and vice versa", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!");
    expect(screen.queryByTestId(/slash-menu-item/)).toBeNull();
    type("/");
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
  });
});

describe("bang menu keyboard behavior", () => {
  it("Tab completes the preselected running shell as `!<key> `", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!");
    fireEvent.keyDown(textarea(), { key: "Tab" });
    expect(textarea().value).toBe("!u-ab12cd ");
  });

  it("ArrowDown crosses sections and Enter completes a type token", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!");
    fireEvent.keyDown(textarea(), { key: "ArrowDown" });
    pressEnter();
    expect(textarea().value).toBe("!zsh ");
  });

  it("Enter with the menu open completes instead of submitting", () => {
    const bang = bangControl();
    render(<Composer {...composerProps({ bang })} />);
    type("!");
    pressEnter();
    expect(textarea().value).toBe("!u-ab12cd ");
    expect(bang.run).not.toHaveBeenCalled();
  });

  it("Escape dismisses the menu but preserves the input", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!u-");
    fireEvent.keyDown(textarea(), { key: "Escape" });
    expect(screen.queryByTestId("bang-shell-menu")).toBeNull();
    expect(textarea().value).toBe("!u-");
  });
});

describe("bang submit interception", () => {
  it("`! <cmd>` runs a new default-type shell and never calls onSend", async () => {
    const bang = bangControl();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("! echo hello");
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      {
        kind: "new",
        type: "zsh",
        command: "echo hello",
      } satisfies BangAction,
      "! echo hello",
    );
    expect(onSend).not.toHaveBeenCalled();
    await waitFor(() => expect(textarea().value).toBe(""));
  });

  it("bare `!` creates and focuses a new default shell", () => {
    const bang = bangControl();
    render(<Composer {...composerProps({ bang })} />);
    type("!");
    // The menu is open with a preselected row — Escape first, so Enter
    // reaches submit (the completion path is covered above).
    fireEvent.keyDown(textarea(), { key: "Escape" });
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith({ kind: "new", type: "zsh", command: null }, "!");
  });

  it("`!<key> <cmd>` sends into the running shell", () => {
    const bang = bangControl();
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd make test");
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      {
        kind: "send",
        terminalId: "terminal_zsh_u-ab12cd",
        command: "make test",
      },
      "!u-ab12cd make test",
    );
  });

  it("`!<key>` alone focuses the shell", () => {
    const bang = bangControl();
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd");
    fireEvent.keyDown(textarea(), { key: "Escape" });
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      { kind: "focus", terminalId: "terminal_zsh_u-ab12cd" },
      "!u-ab12cd",
    );
  });

  it("`!<type> <cmd>` spawns that declared type", () => {
    const bang = bangControl();
    render(<Composer {...composerProps({ bang })} />);
    type("!bash echo hi");
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      { kind: "new", type: "bash", command: "echo hi" },
      "!bash echo hi",
    );
  });

  it("unknown target: inline error, input preserved, nothing sent anywhere", () => {
    const bang = bangControl();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("!nosuch echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea().value).toBe("!nosuch echo hi");
    expect(screen.getByText(/No shell `nosuch`/)).toBeDefined();
  });

  it("non-owner submit: inline error, nothing executed", () => {
    const bang = bangControl({ isOwner: false });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("! echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText("Only the session owner can run shell commands")).toBeDefined();
  });

  it("no-shell-access agent: inline error names the gap", () => {
    const bang = bangControl({ shells: [], declaredTypes: [] });
    render(<Composer {...composerProps({ bang })} />);
    type("! echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(screen.getByText("this agent has no shell access")).toBeDefined();
  });

  it.each([
    ["loading", { metadataLoading: true, metadataError: false }],
    ["failed", { metadataLoading: false, metadataError: true }],
    ["empty", { metadataLoading: false, metadataError: false }],
  ])("live-shell send is allowed with %s declared-type metadata", (_label, metadata) => {
    const bang = bangControl({ declaredTypes: [], ...metadata });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      { kind: "send", terminalId: "terminal_zsh_u-ab12cd", command: "pwd" },
      "!u-ab12cd pwd",
    );
    expect(onSend).not.toHaveBeenCalled();
  });

  it.each([
    ["loading", { metadataLoading: true, metadataError: false }],
    ["failed", { metadataLoading: false, metadataError: true }],
    ["empty", { metadataLoading: false, metadataError: false }],
  ])("live-shell focus is allowed with %s declared-type metadata", (_label, metadata) => {
    const bang = bangControl({ declaredTypes: [], ...metadata });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd");
    fireEvent.keyDown(textarea(), { key: "Escape" });
    pressEnter();
    expect(bang.run).toHaveBeenCalledWith(
      { kind: "focus", terminalId: "terminal_zsh_u-ab12cd" },
      "!u-ab12cd",
    );
  });

  it("a spawn waits for loading declared-type metadata", () => {
    const bang = bangControl({
      shells: [],
      declaredTypes: [],
      metadataLoading: true,
    });
    render(<Composer {...composerProps({ bang })} />);
    type("! echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(screen.getByText("Loading shells…")).toBeDefined();
  });

  it("a spawn reports failed declared-type metadata without claiming no access", () => {
    const bang = bangControl({
      shells: [],
      declaredTypes: [],
      metadataError: true,
    });
    render(<Composer {...composerProps({ bang })} />);
    type("! echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(screen.getByText("Couldn't load shell access")).toBeDefined();
  });

  it("attachments + bang input: hard error, no event", () => {
    const bang = bangControl();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    attachFile();
    type("! echo hi");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByText("Shell commands can't carry attachments")).toBeDefined();
    expect(textarea().value).toBe("! echo hi");
  });

  it("execution failure keeps the input and surfaces the server message", async () => {
    const bang = bangControl({
      run: vi.fn().mockRejectedValue(new Error("shell u-ab12cd is not running")),
    });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    await waitFor(() => expect(screen.getByText("shell u-ab12cd is not running")).toBeDefined());
    expect(textarea().value).toBe("!u-ab12cd pwd");
  });

  it("keeps a 200 unknown command cleared and shows the informational note without retrying", async () => {
    const bang = bangControl({
      run: vi.fn().mockResolvedValue({ status: "unknown" }),
    });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    await waitFor(() =>
      expect(
        screen.getByText("Delivery unknown — check the terminal before retrying."),
      ).toBeDefined(),
    );
    expect(textarea().value).toBe("");
    expect(bang.run).toHaveBeenCalledTimes(1);
  });

  it("a failure does NOT clobber input the user typed while it was in flight", async () => {
    let rejectRun: (e: Error) => void = () => {};
    const bang = bangControl({
      run: vi.fn().mockReturnValue(
        new Promise<void>((_, reject) => {
          rejectRun = reject;
        }),
      ),
    });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    // Composer cleared at dispatch; user starts a fresh message before the
    // command fails.
    expect(textarea().value).toBe("");
    type("a new message");
    rejectRun(new Error("shell u-ab12cd is not running"));
    await waitFor(() => expect(screen.getByText("shell u-ab12cd is not running")).toBeDefined());
    // The newer draft survives — the failed command's text is not restored
    // over it.
    expect(textarea().value).toBe("a new message");
  });

  it("does NOT resurrect the command after the user types then deletes a newer draft", async () => {
    let rejectRun: (e: Error) => void = () => {};
    const bang = bangControl({
      run: vi.fn().mockReturnValue(
        new Promise<void>((_, reject) => {
          rejectRun = reject;
        }),
      ),
    });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    expect(textarea().value).toBe("");
    // User starts a fresh draft, then clears it — composer is empty again, but
    // by newer intent, not the pending command's cleared state.
    type("draft");
    type("");
    rejectRun(new Error("shell u-ab12cd is not running"));
    await waitFor(() => expect(screen.getByText("shell u-ab12cd is not running")).toBeDefined());
    // The stale command must not reappear over the deliberately-emptied box.
    expect(textarea().value).toBe("");
  });

  it("does NOT restore over an attachment-only draft created while the request is in flight", async () => {
    let rejectRun: (error: Error) => void = () => {};
    const bang = bangControl({
      run: vi.fn().mockReturnValue(
        new Promise((_, reject) => {
          rejectRun = reject;
        }),
      ),
    });
    render(<Composer {...composerProps({ bang })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    attachFile();
    rejectRun(new Error("response lost"));
    await waitFor(() => expect(screen.getByText("response lost")).toBeDefined());
    expect(textarea().value).toBe("");
    expect(screen.getByText("shot.png")).toBeDefined();
  });

  it("does NOT resurrect the command after a newer normal message clears the composer", async () => {
    let rejectRun: (e: Error) => void = () => {};
    const bang = bangControl({
      run: vi.fn().mockReturnValue(
        new Promise<void>((_, reject) => {
          rejectRun = reject;
        }),
      ),
    });
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("!u-ab12cd pwd");
    pressEnter();
    // User sends a normal message before the command fails — it clears the box.
    type("a normal message");
    pressEnter();
    expect(onSend).toHaveBeenCalledWith("a normal message", undefined);
    expect(textarea().value).toBe("");
    rejectRun(new Error("shell u-ab12cd is not running"));
    await waitFor(() => expect(screen.getByText("shell u-ab12cd is not running")).toBeDefined());
    expect(textarea().value).toBe("");
  });

  it("`\\!` escape falls through to a normal send with the backslash stripped", () => {
    const bang = bangControl();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type("\\!important note");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("!important note", undefined);
  });

  it("a leading space defeats interception (plain send)", () => {
    const bang = bangControl();
    const onSend = vi.fn();
    render(<Composer {...composerProps({ bang, onSend })} />);
    type(" ! not a command");
    pressEnter();
    expect(bang.run).not.toHaveBeenCalled();
    expect(onSend).toHaveBeenCalledWith("! not a command", undefined);
  });

  it("without bang wiring, `!` input falls through to a plain send", () => {
    const onSend = vi.fn();
    render(<Composer {...composerProps({ onSend })} />);
    type("! echo hi");
    pressEnter();
    expect(onSend).toHaveBeenCalledWith("! echo hi", undefined);
  });
});

describe("bang highlight tint", () => {
  it("mounts the overlay with the tinted `!<target>` token", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    type("!u-ab12cd echo hi");
    expect(textarea().getAttribute("data-bang-command")).toBe("true");
    const overlay = screen.getByTestId("composer-highlight-overlay");
    expect(overlay.querySelector("span")?.textContent).toBe("!u-ab12cd");
  });

  it("keeps the tint with attachments present (submit still intercepts)", () => {
    render(<Composer {...composerProps({ bang: bangControl() })} />);
    attachFile();
    type("!u-ab12cd echo hi");
    // The overlay must not vanish just because a file is attached — submit
    // hard-errors on bang+attachments, so it isn't a normal message.
    expect(textarea().getAttribute("data-bang-command")).toBe("true");
  });

  it("does not tint without bang wiring", () => {
    render(<Composer {...composerProps({})} />);
    type("!u-ab12cd echo hi");
    expect(textarea().getAttribute("data-bang-command")).toBeNull();
  });
});

describe("splitBangCommand", () => {
  it("splits the token from the rest", () => {
    expect(splitBangCommand("!zsh echo hi")).toEqual({ token: "!zsh", after: " echo hi" });
    expect(splitBangCommand("!")).toEqual({ token: "!", after: "" });
    expect(splitBangCommand("plain")).toBeNull();
  });
});
