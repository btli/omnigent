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
import { HermesIcon } from "@/components/icons/HermesIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
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

  it("uses HermesIcon for a Hermes root", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "hermes-native",
        agentName: "hermes-native-ui",
      }),
    ).toBe(HermesIcon);
  });

  it("matches the Pi root harness exactly", () => {
    expect(resolveAgentIcon({ kind: "root", wrapper: null, harness: "pi", agentName: "pi" })).toBe(
      PiIcon,
    );
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "openapi",
        agentName: "spec-generator",
      }),
    ).toBe(BotIcon);
  });

  it("checks root harness brands before the Nessie name", () => {
    expect(
      resolveAgentIcon({
        kind: "root",
        wrapper: null,
        harness: "claude-sdk",
        agentName: "nessie",
      }),
    ).toBe(ClaudeIcon);
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
