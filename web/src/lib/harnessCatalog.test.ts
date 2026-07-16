import { describe, expect, it } from "vitest";

import { CLAUDE_NATIVE_MODELS } from "./claudeNativeModels";
import {
  CLAUDE_NATIVE_EFFORTS,
  effortOptionsForHarness,
  isClaudeFamilyHarness,
  modelOptionsForHarness,
} from "./harnessCatalog";

describe("harnessCatalog", () => {
  it.each(["claude-native", "claude-sdk", "claude_sdk", "claude"])(
    "returns Claude models for %s",
    (harness) => {
      expect(modelOptionsForHarness(harness).map((option) => option.id)).toEqual(
        CLAUDE_NATIVE_MODELS.map((option) => option.id),
      );
    },
  );

  it.each(["codex-native", "cursor", null])(
    "does not expose a cross-harness model catalog for %s",
    (harness) => {
      expect(modelOptionsForHarness(harness)).toEqual([]);
    },
  );

  it("returns all five Claude effort levels", () => {
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

  it("does not expose effort levels for a non-Claude harness", () => {
    expect(effortOptionsForHarness("codex-native", "gpt-5.4")).toEqual([]);
  });

  it.each([
    ["claude-native", true],
    ["claude-sdk", true],
    ["claude_sdk", true],
    ["claude", true],
    ["codex-native", false],
    ["cursor", false],
    [null, false],
  ] as const)("classifies %s as Claude family: %s", (harness, expected) => {
    expect(isClaudeFamilyHarness(harness)).toBe(expected);
  });
});
