import { describe, expect, it } from "vitest";
import { findTerminalByTabKey, type TerminalInfo, terminalTabKey } from "./useTerminals";

function terminal(overrides: Partial<TerminalInfo> & { id: string }): TerminalInfo {
  return { name: "bash", session: "s1", running: true, ...overrides };
}

describe("findTerminalByTabKey", () => {
  const terminals = [terminal({ id: "terminal_bash_s1" }), terminal({ id: "terminal_zsh_s2" })];

  it("resolves the terminal addressed by its tab key", () => {
    const target = terminals[1];
    expect(findTerminalByTabKey(terminals, terminalTabKey(target))).toBe(target);
  });

  it("returns null when no terminal matches the key", () => {
    expect(findTerminalByTabKey(terminals, "terminal:terminal_missing")).toBeNull();
  });

  it("returns null against an empty cache", () => {
    expect(findTerminalByTabKey([], "terminal:terminal_bash_s1")).toBeNull();
  });
});
