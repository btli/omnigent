// Vitest cases for the composer bang grammar — every row of the spec's
// grammar + target-resolution tables, plus the edge cases (escapes,
// multiline verbatim commands, precedence, dead shells, agent panes).

import { describe, expect, it } from "vitest";
import type { TerminalInfo } from "@/hooks/useTerminals";
import {
  type BangContext,
  NO_SHELL_ACCESS,
  SHELL_COMMANDS_NO_ATTACHMENTS,
  isBangCommandText,
  parseBangCommand,
  shellBangToken,
  stripBangEscape,
  targetableShells,
} from "./composerBang";

function shell(overrides: Partial<TerminalInfo> = {}): TerminalInfo {
  return {
    id: "terminal_zsh_u-ab12cd",
    name: "zsh",
    session: "u-ab12cd",
    running: true,
    ...overrides,
  };
}

function ctx(overrides: Partial<BangContext> = {}): BangContext {
  return {
    shells: [shell()],
    declaredTypes: ["zsh", "bash"],
    ...overrides,
  };
}

describe("shared bang grammar primitives", () => {
  it("filters agent panes from targetable shells", () => {
    const agentPane = shell({ id: "terminal_claude_main", session: "main" });
    expect(targetableShells([agentPane, shell()])).toEqual([shell()]);
  });

  it("uses a non-empty session key as the preferred shell token, else the id", () => {
    expect(shellBangToken(shell())).toBe("u-ab12cd");
    expect(shellBangToken(shell({ session: "" }))).toBe("terminal_zsh_u-ab12cd");
  });

  it("exports the shared composer error copy", () => {
    expect(NO_SHELL_ACCESS).toBe("this agent has no shell access");
    expect(SHELL_COMMANDS_NO_ATTACHMENTS).toBe("Shell commands can't carry attachments");
  });
});

describe("isBangCommandText", () => {
  it("matches an untrimmed leading !", () => {
    expect(isBangCommandText("!")).toBe(true);
    expect(isBangCommandText("! ls")).toBe(true);
    expect(isBangCommandText("!zsh ls")).toBe(true);
  });

  it("a leading space defeats interception (untrimmed check)", () => {
    expect(isBangCommandText(" ! ls")).toBe(false);
    expect(isBangCommandText(" !")).toBe(false);
  });

  it("does not match non-bang text or escaped bangs", () => {
    expect(isBangCommandText("ls")).toBe(false);
    expect(isBangCommandText("")).toBe(false);
    expect(isBangCommandText("\\! ls")).toBe(false);
  });
});

describe("stripBangEscape", () => {
  it("strips the backslash from an escaped bang", () => {
    expect(stripBangEscape("\\!important note")).toBe("!important note");
    expect(stripBangEscape("\\!")).toBe("!");
  });

  it("returns null for non-escaped text", () => {
    expect(stripBangEscape("!ls")).toBeNull();
    expect(stripBangEscape("plain")).toBeNull();
    expect(stripBangEscape("")).toBeNull();
    // Double backslash is not the escape form — the message keeps both.
    expect(stripBangEscape("\\\\!x")).toBeNull();
  });
});

describe("parseBangCommand — grammar table", () => {
  it("`! <cmd>` → new shell of the default type running <cmd>", () => {
    expect(parseBangCommand("! echo hi", ctx())).toEqual({
      kind: "new",
      type: "zsh",
      command: "echo hi",
    });
  });

  it("bare `!` → new default-type shell, no command", () => {
    expect(parseBangCommand("!", ctx())).toEqual({ kind: "new", type: "zsh", command: null });
  });

  it("`! ` (token + exactly one separator, empty remainder) → no command", () => {
    expect(parseBangCommand("! ", ctx())).toEqual({ kind: "new", type: "zsh", command: null });
    expect(parseBangCommand("!u-ab12cd ", ctx())).toEqual({
      kind: "focus",
      terminalId: "terminal_zsh_u-ab12cd",
    });
  });

  it("whitespace after the single separator is a verbatim command, not 'no command'", () => {
    expect(parseBangCommand("!   ", ctx())).toEqual({ kind: "new", type: "zsh", command: "  " });
    expect(parseBangCommand("!u-ab12cd   ", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "  ",
    });
    expect(parseBangCommand("!u-ab12cd \t", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "\t",
    });
  });

  it("`!<running-key> <cmd>` → send into that shell", () => {
    expect(parseBangCommand("!u-ab12cd make test", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "make test",
    });
  });

  it("`!<running-key>` alone → focus that shell", () => {
    expect(parseBangCommand("!u-ab12cd", ctx())).toEqual({
      kind: "focus",
      terminalId: "terminal_zsh_u-ab12cd",
    });
  });

  it("`!<full-resource-id> <cmd>` → send (completed tokens stay valid)", () => {
    expect(parseBangCommand("!terminal_zsh_u-ab12cd ls", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "ls",
    });
  });

  it("`!<full-resource-id>` alone → focus", () => {
    expect(parseBangCommand("!terminal_zsh_u-ab12cd", ctx())).toEqual({
      kind: "focus",
      terminalId: "terminal_zsh_u-ab12cd",
    });
  });

  it("`!<shell-type> <cmd>` → new shell of that type", () => {
    expect(parseBangCommand("!bash echo hi", ctx())).toEqual({
      kind: "new",
      type: "bash",
      command: "echo hi",
    });
  });

  it("`!<shell-type>` alone → create + focus, no command", () => {
    expect(parseBangCommand("!bash", ctx())).toEqual({
      kind: "new",
      type: "bash",
      command: null,
    });
  });

  it("unknown target → error, never an agent message", () => {
    const action = parseBangCommand("!nosuch echo hi", ctx());
    expect(action).toEqual({
      kind: "error",
      reason: "No shell `nosuch` — press ! to see running shells",
    });
  });

  it("non-bang text → error (defensive; callers guard with isBangCommandText)", () => {
    expect(parseBangCommand("ls", ctx()).kind).toBe("error");
  });
});

describe("parseBangCommand — command text is verbatim", () => {
  it("preserves quoting, $VARS, pipes, and && untouched", () => {
    const raw = `echo "$HOME" | grep -c / && say 'done'`;
    expect(parseBangCommand(`! ${raw}`, ctx())).toEqual({
      kind: "new",
      type: "zsh",
      command: raw,
    });
  });

  it("preserves embedded newlines (Shift+Enter multiline command)", () => {
    expect(parseBangCommand("!u-ab12cd cd /tmp\nls -la", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "cd /tmp\nls -la",
    });
  });

  it("strips exactly one separator; further leading whitespace is verbatim", () => {
    expect(parseBangCommand("!  echo hi", ctx())).toEqual({
      kind: "new",
      type: "zsh",
      command: " echo hi",
    });
  });

  it("a newline can be the token separator", () => {
    expect(parseBangCommand("!u-ab12cd\nmake", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "make",
    });
  });

  it("a tab can be the token separator", () => {
    expect(parseBangCommand("!\tcmd", ctx())).toEqual({
      kind: "new",
      type: "zsh",
      command: "cmd",
    });
    expect(parseBangCommand("!zsh\tcmd", ctx())).toEqual({
      kind: "new",
      type: "zsh",
      command: "cmd",
    });
    // Tab separator + tab-only remainder: the second tab is the command.
    expect(parseBangCommand("!u-ab12cd\t\t", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "\t",
    });
  });

  it("Unicode-whitespace-only remainders are verbatim commands", () => {
    // NBSP / ideographic-space commands after the ASCII separator, written
    // as escapes so the invisible characters are unambiguous in source.
    expect(parseBangCommand("!u-ab12cd \u00A0", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "\u00A0",
    });
    expect(parseBangCommand("!u-ab12cd \u3000", ctx())).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "\u3000",
    });
  });
});

describe("parseBangCommand — resolution precedence (exact-token, first match wins)", () => {
  it("a running-shell key shadows an identically named declared type", () => {
    const collision = ctx({
      shells: [shell({ id: "terminal_shell_zsh", name: "shell", session: "zsh" })],
      declaredTypes: ["zsh", "bash"],
    });
    expect(parseBangCommand("!zsh ls", collision)).toEqual({
      kind: "send",
      terminalId: "terminal_shell_zsh",
      command: "ls",
    });
    // No-command form of the same collision: the running key still wins,
    // so `!zsh` focuses the shell instead of spawning a new zsh.
    expect(parseBangCommand("!zsh", collision)).toEqual({
      kind: "focus",
      terminalId: "terminal_shell_zsh",
    });
  });

  it("a literal `!u-` token is an unknown target, not a key prefix", () => {
    expect(parseBangCommand("!u- ls", ctx())).toEqual({
      kind: "error",
      reason: "No shell `u-` — press ! to see running shells",
    });
    expect(parseBangCommand("!u-", ctx()).kind).toBe("error");
  });

  it("session key is checked before full resource id", () => {
    const twoShells = ctx({
      shells: [
        shell({ id: "terminal_zsh_u-aa1111", session: "u-aa1111" }),
        // Pathological: second shell's session key equals the first's id.
        shell({ id: "terminal_bash_u-bb2222", name: "bash", session: "terminal_zsh_u-aa1111" }),
      ],
    });
    expect(parseBangCommand("!terminal_zsh_u-aa1111 ls", twoShells)).toEqual({
      kind: "send",
      terminalId: "terminal_bash_u-bb2222",
      command: "ls",
    });
  });

  it("matching is exact-token only — no prefix or fuzzy matches", () => {
    expect(parseBangCommand("!u-ab1 ls", ctx()).kind).toBe("error");
    expect(parseBangCommand("!zs ls", ctx()).kind).toBe("error");
    expect(parseBangCommand("!U-AB12CD ls", ctx()).kind).toBe("error");
  });

  it("an empty session key never matches", () => {
    const blank = ctx({ shells: [shell({ session: "" })] });
    expect(parseBangCommand("! ls", blank).kind).toBe("new");
    expect(parseBangCommand("!zsh ls", blank)).toEqual({
      kind: "new",
      type: "zsh",
      command: "ls",
    });
  });
});

describe("parseBangCommand — dead shells and agent panes", () => {
  it("a matched but not-running shell errors instead of falling through", () => {
    const dead = ctx({ shells: [shell({ running: false })] });
    expect(parseBangCommand("!u-ab12cd ls", dead)).toEqual({
      kind: "error",
      reason: "shell u-ab12cd is not running",
    });
    expect(parseBangCommand("!u-ab12cd", dead)).toEqual({
      kind: "error",
      reason: "shell u-ab12cd is not running",
    });
  });

  it("agent panes are never resolvable, by id or by session key", () => {
    const withAgentPane = ctx({
      shells: [shell({ id: "terminal_claude_main", name: "claude", session: "main" })],
      declaredTypes: ["zsh"],
    });
    expect(parseBangCommand("!terminal_claude_main ls", withAgentPane).kind).toBe("error");
    expect(parseBangCommand("!main ls", withAgentPane).kind).toBe("error");
  });
});

describe("parseBangCommand — deferred declared-type resolution", () => {
  const none = ctx({ shells: [], declaredTypes: [] });

  it("defers bare `!` while declared types are unavailable", () => {
    expect(parseBangCommand("!", none)).toEqual({
      kind: "needsTypes",
      target: "",
      command: null,
    });
    expect(parseBangCommand("! echo hi", none)).toEqual({
      kind: "needsTypes",
      target: "",
      command: "echo hi",
    });
  });

  it("defers an unmatched target until declared types settle", () => {
    expect(parseBangCommand("!zsh echo hi", none)).toEqual({
      kind: "needsTypes",
      target: "zsh",
      command: "echo hi",
    });
  });

  it("a running shell stays targetable even with no declared types", () => {
    const shellsOnly = ctx({ declaredTypes: [] });
    expect(parseBangCommand("!u-ab12cd ls", shellsOnly)).toEqual({
      kind: "send",
      terminalId: "terminal_zsh_u-ab12cd",
      command: "ls",
    });
    // But the spawn path is gone: bare `!` has no default type to use.
    expect(parseBangCommand("!", shellsOnly)).toEqual({
      kind: "needsTypes",
      target: "",
      command: null,
    });
  });
});
