// Tests for the `!`-shell autocomplete controller: section composition +
// suppression rules (buildBangMenuRows), and the hook's open/dismiss,
// preselect, flat keyboard nav, and completion contract — driven through
// a minimal textarea harness so keydown flows like the real composer.

import { useRef, useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { TerminalInfo } from "@/hooks/useTerminals";
import { parseBangCommand } from "@/lib/composerBang";
import { buildBangMenuRows, useBangShellMenu } from "./useBangShellMenu";

function shell(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_zsh_u-ab12cd",
    name: "zsh",
    session: "u-ab12cd",
    running: true,
    ...overrides,
  };
}

describe("buildBangMenuRows", () => {
  it("puts running shells on top and declared types below", () => {
    const rows = buildBangMenuRows([shell()], ["zsh", "bash"], "", true);
    expect(rows.map((r) => r.kind)).toEqual(["shell", "type", "type"]);
    expect(rows.map((r) => r.token)).toEqual(["u-ab12cd", "zsh", "bash"]);
  });

  it("suppresses the types section with 0 or 1 declared types", () => {
    expect(buildBangMenuRows([shell()], [], "", false).map((r) => r.kind)).toEqual(["shell"]);
    expect(buildBangMenuRows([shell()], ["zsh"], "", true).map((r) => r.kind)).toEqual(["shell"]);
  });

  it("returns no rows when both sections are empty", () => {
    expect(buildBangMenuRows([], ["zsh"], "", true)).toEqual([]);
  });

  it("excludes agent panes from the running section", () => {
    const rows = buildBangMenuRows(
      [shell({ id: "terminal_claude_main", name: "claude", session: "main" }), shell()],
      [],
      "",
      false,
    );
    expect(rows.map((r) => r.token)).toEqual(["u-ab12cd"]);
  });

  it("marks exited shells visible but non-completable", () => {
    const rows = buildBangMenuRows([shell({ running: false })], [], "", false);
    expect(rows).toHaveLength(1);
    expect(rows[0]!.completable).toBe(false);
    expect(rows[0]!.running).toBe(false);
  });

  it("filters rows by completion-token prefix, dropping emptied sections", () => {
    const rows = buildBangMenuRows([shell()], ["zsh", "bash"], "u-", true);
    expect(rows.map((r) => r.token)).toEqual(["u-ab12cd"]);
    const typeRows = buildBangMenuRows([shell()], ["zsh", "bash"], "ba", true);
    expect(typeRows.map((r) => r.token)).toEqual(["bash"]);
  });

  it("tags the first declared type as default only on native sessions", () => {
    const native = buildBangMenuRows([], ["zsh", "bash"], "", true);
    expect(native.map((r) => r.isDefault)).toEqual([true, false]);
    const sdk = buildBangMenuRows([], ["build", "test"], "", false);
    expect(sdk.map((r) => r.isDefault)).toEqual([false, false]);
  });

  it("falls back to the resource id as token when the session key is empty", () => {
    const rows = buildBangMenuRows([shell({ session: "" })], [], "", false);
    expect(rows[0]!.token).toBe("terminal_zsh_u-ab12cd");
  });

  it("round-trips every completable row across colliding shell keys and types", () => {
    const collidingShell = shell({
      id: "terminal_zsh_u-collision",
      session: "zsh",
    });
    const shells = [collidingShell];
    const declaredTypes = ["zsh", "bash"];
    const rows = buildBangMenuRows(shells, declaredTypes, "", true);

    expect(rows[0]).toMatchObject({
      kind: "shell",
      token: "terminal_zsh_u-collision",
      completable: true,
    });
    expect(rows.find((row) => row.kind === "type" && row.name === "zsh")).toMatchObject({
      completable: false,
    });
    for (const row of rows.filter((candidate) => candidate.completable)) {
      const parsed = parseBangCommand(`!${row.token}`, { shells, declaredTypes });
      if (row.kind === "shell") {
        expect(parsed).toEqual({ kind: "focus", terminalId: collidingShell.id });
      } else {
        expect(parsed).toEqual({ kind: "new", type: row.name, command: null });
      }
    }
  });

  it("uses each full resource id when two running shells share a key", () => {
    const shells = [
      shell({ id: "terminal_zsh_u-first", session: "shared" }),
      shell({ id: "terminal_bash_u-second", name: "bash", session: "shared" }),
    ];
    const rows = buildBangMenuRows(shells, [], "", false);
    expect(rows.map((row) => row.token)).toEqual([
      "terminal_zsh_u-first",
      "terminal_bash_u-second",
    ]);
    expect(
      rows.map((row) => parseBangCommand(`!${row.token}`, { shells, declaredTypes: [] })),
    ).toEqual([
      { kind: "focus", terminalId: "terminal_zsh_u-first" },
      { kind: "focus", terminalId: "terminal_bash_u-second" },
    ]);
  });

  it("omits a running row when even its full id cannot round-trip", () => {
    const victim = shell({ id: "terminal_victim", name: "victim", session: "shared" });
    const shells = [
      shell({ id: "terminal_blocker", name: "blocker", session: "terminal_victim" }),
      victim,
      shell({ id: "terminal_peer", name: "peer", session: "shared" }),
    ];
    const rows = buildBangMenuRows(shells, [], "", false);
    expect(rows).not.toContainEqual(
      expect.objectContaining({ name: victim.name, token: victim.id }),
    );
    for (const row of rows.filter((candidate) => candidate.completable)) {
      const action = parseBangCommand(`!${row.token}`, { shells, declaredTypes: [] });
      expect(action.kind).toBe("focus");
    }
  });
});

/** Textarea harness driving the hook exactly like the composer does. */
function Harness({
  shells,
  declaredTypes,
  enabled = true,
  initial = "",
}: {
  shells: TerminalInfo[];
  declaredTypes: string[];
  enabled?: boolean;
  initial?: string;
}) {
  const [text, setText] = useState(initial);
  const [caret, setCaret] = useState<number | null>(null);
  const [revision, setRevision] = useState(0);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menu = useBangShellMenu({
    text,
    // Mirror the composer: completion resets the caret to the end so the
    // caret-based trigger closes the menu (and re-entry reopens it).
    setText: (next) => {
      setText(next);
      setRevision((current) => current + 1);
    },
    setCaret,
    textareaRef,
    enabled,
    shells,
    declaredTypes,
    hasDefaultType: true,
    caret: caret ?? undefined,
    revision,
  });
  return (
    <div>
      <textarea
        ref={textareaRef}
        aria-label="input"
        value={text}
        onChange={(e) => {
          setText(e.target.value);
          setCaret(e.target.selectionStart);
          setRevision((current) => current + 1);
        }}
        onSelect={(e) => setCaret(e.currentTarget.selectionStart)}
        onKeyDown={(e) => {
          if (menu.handleKeyDown(e)) return;
        }}
      />
      <output aria-label="state">
        {JSON.stringify({
          open: menu.open,
          activeIndex: menu.activeIndex,
          tokens: menu.rows.map((r) => r.token),
        })}
      </output>
    </div>
  );
}

function harnessState(): { open: boolean; activeIndex: number; tokens: string[] } {
  return JSON.parse(screen.getByLabelText("state").textContent ?? "{}");
}

function input(): HTMLTextAreaElement {
  return screen.getByLabelText("input") as HTMLTextAreaElement;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("useBangShellMenu", () => {
  const shells = [
    shell(),
    shell({ id: "terminal_bash_u-xy98zw", name: "bash", session: "u-xy98zw" }),
  ];

  it("opens only while the caret is inside the bang token", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    expect(harnessState().open).toBe(false);
    fireEvent.change(input(), { target: { value: "!" } });
    expect(harnessState().open).toBe(true);
    // Whitespace ends the token — the command follows, menu must close.
    fireEvent.change(input(), { target: { value: "!u-ab12cd " } });
    expect(harnessState().open).toBe(false);
  });

  it("stays closed when disabled (non-owner / attachments)", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} enabled={false} />);
    fireEvent.change(input(), { target: { value: "!" } });
    expect(harnessState().open).toBe(false);
  });

  it("stays closed when both sections are empty", () => {
    render(<Harness shells={[]} declaredTypes={["zsh"]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    expect(harnessState().open).toBe(false);
  });

  it("preselects the first completable row and cycles with arrows across sections", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    // Rows: u-ab12cd, u-xy98zw, zsh, bash — first shell preselected.
    expect(harnessState()).toMatchObject({ activeIndex: 0 });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState().activeIndex).toBe(1);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState().activeIndex).toBe(2); // crosses into the types section
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(harnessState().activeIndex).toBe(0);
    fireEvent.keyDown(input(), { key: "ArrowUp" });
    expect(harnessState().activeIndex).toBe(3); // wraps to the last row
  });

  it("skips exited shells in keyboard navigation", () => {
    render(
      <Harness
        shells={[
          shell({ running: false }),
          shell({ id: "terminal_bash_u-xy98zw", name: "bash", session: "u-xy98zw" }),
        ]}
        declaredTypes={[]}
      />,
    );
    fireEvent.change(input(), { target: { value: "!" } });
    // Row 0 is exited — preselect lands on the running shell at index 1.
    expect(harnessState().activeIndex).toBe(1);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState().activeIndex).toBe(1); // only one completable row
  });

  it("Tab completes the highlighted token as `!<token> `", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    fireEvent.keyDown(input(), { key: "Tab" });
    expect(input().value).toBe("!u-ab12cd ");
    expect(harnessState().open).toBe(false);
  });

  it("Enter completes like Tab", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!zs" } });
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input().value).toBe("!zsh ");
  });

  it("Enter falls through (not consumed) when nothing is completable", () => {
    render(<Harness shells={[shell({ running: false })]} declaredTypes={[]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    expect(harnessState().open).toBe(true);
    // Only an exited row is listed — Enter must not be consumed (the
    // composer's submit gets it: bare `!` is a valid create-and-focus).
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input().value).toBe("!");
  });

  it("Escape dismisses without clearing the input; an edit reopens", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!u-" } });
    expect(harnessState().open).toBe(true);
    fireEvent.keyDown(input(), { key: "Escape" });
    expect(harnessState().open).toBe(false);
    expect(input().value).toBe("!u-");
    fireEvent.change(input(), { target: { value: "!u-a" } });
    expect(harnessState().open).toBe(true);
    fireEvent.change(input(), { target: { value: "!u-" } });
    expect(harnessState().open).toBe(true);
  });

  it("narrowing the query re-preselects the first remaining row", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState().activeIndex).toBe(2);
    fireEvent.change(input(), { target: { value: "!u-x" } });
    expect(harnessState()).toMatchObject({ tokens: ["u-xy98zw"], activeIndex: 0 });
  });

  it("broadening the query resets the highlight to the first row (no stale selection)", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    // Filter down to the `bash` type row and explicitly select it.
    fireEvent.change(input(), { target: { value: "!bash" } });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState()).toMatchObject({ tokens: ["bash"], activeIndex: 0 });
    // Broaden back to `!`: the highlight must snap to the first running
    // shell, not stay pinned to `bash` at its new index.
    fireEvent.change(input(), { target: { value: "!" } });
    expect(harnessState()).toMatchObject({ tokens: ["u-ab12cd", "u-xy98zw", "zsh", "bash"] });
    expect(harnessState().activeIndex).toBe(0);
  });

  it("preserves the highlight across a pure status change (row exits) under the same query", () => {
    const { rerender } = render(<Harness shells={shells} declaredTypes={[]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    fireEvent.keyDown(input(), { key: "ArrowDown" }); // select u-xy98zw
    expect(harnessState()).toMatchObject({ activeIndex: 1 });
    // The first shell exits (running → false). Query unchanged, so the
    // selection stays on u-xy98zw wherever it now sits.
    rerender(
      <Harness
        shells={[
          shell({ running: false }),
          shell({ id: "terminal_bash_u-xy98zw", name: "bash", session: "u-xy98zw" }),
        ]}
        declaredTypes={[]}
      />,
    );
    expect(harnessState().activeIndex).toBe(1);
  });

  it("reopens the menu when the caret moves back inside the bang token", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!u-ab12cd echo hi" } });
    // Caret trails the value (past the space) — menu closed.
    expect(harnessState().open).toBe(false);
    // Move the caret back inside `!u-ab12cd`.
    input().setSelectionRange(4, 4);
    fireEvent.select(input());
    expect(harnessState().open).toBe(true);
    expect(harnessState().tokens).toEqual(["u-ab12cd"]);
    // Move it back out past the space — menu closes again.
    input().setSelectionRange(12, 12);
    fireEvent.select(input());
    expect(harnessState().open).toBe(false);
  });

  it("caret re-entry + complete preserves the command suffix and lands before it", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!u- echo important" } });
    input().setSelectionRange(3, 3);
    fireEvent.select(input());
    expect(harnessState().open).toBe(true);
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "Tab" });
    expect(input().value).toBe("!u-xy98zw echo important");
    expect(input().selectionStart).toBe("!u-xy98zw".length);
    expect(input().selectionEnd).toBe("!u-xy98zw".length);
  });

  it("reopens after Tab-completion when the caret moves back into the token", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!u-ab12cd" } });
    expect(harnessState().open).toBe(true);
    fireEvent.keyDown(input(), { key: "Tab" });
    // Completed to `!u-ab12cd ` with the caret past the token — menu closed.
    expect(input().value).toBe("!u-ab12cd ");
    expect(harnessState().open).toBe(false);
    // Move the caret back inside the just-completed token — it must reopen
    // (text is unchanged, so a text-keyed dismissal would wrongly stay shut).
    input().setSelectionRange(4, 4);
    fireEvent.select(input());
    expect(harnessState().open).toBe(true);
  });

  it("resets to the first row when returning to a previously-visited query", () => {
    render(<Harness shells={shells} declaredTypes={["zsh", "bash"]} />);
    fireEvent.change(input(), { target: { value: "!" } });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    fireEvent.keyDown(input(), { key: "ArrowDown" });
    expect(harnessState().activeIndex).toBe(2); // zsh selected at `!`
    // Narrow so the row set changes, then return to the SAME `!` query.
    fireEvent.change(input(), { target: { value: "!z" } });
    fireEvent.change(input(), { target: { value: "!" } });
    // The stale selection must not resurrect — highlight snaps to row 0.
    expect(harnessState().activeIndex).toBe(0);
  });
});
