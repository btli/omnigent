import {
  BookOpenIcon,
  BotIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { describe, expect, it } from "vitest";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { resolveAgentIcon } from "./subagentIcons";

describe("resolveAgentIcon", () => {
  it("uses a brand icon for a full native child wrapper", () => {
    expect(resolveAgentIcon({ kind: "child", wrapper: "codex-native-ui", tool: "reviewer" })).toBe(
      CodexIcon,
    );
  });

  it("falls back to BotIcon for a root with an unknown wrapper and harness", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: "unknown-wrapper",
        harness: "agents_sdk",
        agentName: "custom-agent",
      }),
    ).toBe(BotIcon);
  });

  it.each([
    ["Explore", SearchIcon],
    ["deep-researcher", BookOpenIcon],
    ["planner", CompassIcon],
    ["architect", CompassIcon],
    ["code-reviewer", ScanSearchIcon],
    ["pr-test-analyzer", FlaskConicalIcon],
    ["frontend_engineer", Code2Icon],
    ["documentation", FileTextIcon],
    ["technical-writer", FileTextIcon],
  ])("falls back from an unrecognized child wrapper to the %s role icon", (tool, expected) => {
    expect(resolveAgentIcon({ kind: "child", wrapper: null, tool })).toBe(expected);
  });

  it.each([null, "general-purpose"])("falls back to OttoIcon for child tool %s", (tool) => {
    expect(resolveAgentIcon({ kind: "child", wrapper: null, tool })).toBe(OttoIcon);
  });

  it("lets -subagent wrappers fall through to role icons", () => {
    expect(
      resolveAgentIcon({
        kind: "child",
        wrapper: "claude-code-native-ui-subagent",
        tool: "Explore",
      }),
    ).toBe(SearchIcon);
    expect(
      resolveAgentIcon({
        kind: "child",
        wrapper: "claude-code-native-ui-subagent",
        tool: "claude",
      }),
    ).toBe(OttoIcon);
    expect(
      resolveAgentIcon({
        kind: "child",
        wrapper: "claude-code-native-ui-subagent",
        tool: "Explore",
      }),
    ).not.toBe(ClaudeIcon);
  });
});
