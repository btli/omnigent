import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_MODELS } from "./claudeNativeModels";
import {
  CLAUDE_NATIVE_EFFORTS,
  effortOptionsForHarness,
  harnessOptionsForProject,
  isClaudeNativeHarness,
  modelOptionsForHarness,
} from "./harnessCatalog";

describe("harnessCatalog", () => {
  it("returns Claude models only for the claude-native wrapper", () => {
    expect(modelOptionsForHarness("claude-native").map((option) => option.id)).toEqual(
      CLAUDE_NATIVE_MODELS.map((option) => option.id),
    );
  });

  // SDK harnesses have no native model picker in-session either — the static
  // alias catalog belongs to Claude Code's wrapper alone.
  it.each(["claude-sdk", "claude_sdk", "codex-native", "cursor", null])(
    "does not expose a model catalog for %s",
    (harness) => {
      expect(modelOptionsForHarness(harness)).toEqual([]);
    },
  );

  it("returns all five Claude effort levels for claude-native", () => {
    expect(effortOptionsForHarness("claude-native", "sonnet")).toEqual(
      CLAUDE_NATIVE_EFFORTS,
    );
    expect(CLAUDE_NATIVE_EFFORTS.map((option) => option.value)).toEqual([
      "low",
      "medium",
      "high",
      "xhigh",
      "max",
    ]);
  });

  it.each(["claude-sdk", "codex-native"])(
    "does not expose effort levels for %s",
    (harness) => {
      expect(effortOptionsForHarness(harness, "gpt-5.4")).toEqual([]);
    },
  );

  it.each([
    ["claude-native", true],
    ["claude-sdk", false],
    ["claude_sdk", false],
    ["codex-native", false],
    ["cursor", false],
    [null, false],
  ] as const)("classifies %s as claude-native: %s", (harness, expected) => {
    expect(isClaudeNativeHarness(harness)).toBe(expected);
  });

  it("offers native wrappers even when the brain catalog omits them", () => {
    const options = harnessOptionsForProject({});
    const ids = options.map((option) => option.id);
    expect(ids).toContain("claude-native");
    expect(ids).toContain("codex-native");
    expect(options.find((option) => option.id === "claude-native")?.label).toBe(
      "Claude Code",
    );
  });

  it("appends brain harnesses after natives without duplicating ids", () => {
    const options = harnessOptionsForProject({
      "claude-sdk": "Claude SDK",
      "claude-native": "Shadowed label",
    });
    const ids = options.map((option) => option.id);
    expect(ids.filter((id) => id === "claude-native")).toHaveLength(1);
    // The canonical native registry names the row, not the brain catalog.
    expect(options.find((option) => option.id === "claude-native")?.label).toBe(
      "Claude Code",
    );
    expect(ids.indexOf("claude-native")).toBeLessThan(ids.indexOf("claude-sdk"));
  });
});
