import { BotIcon, SearchIcon } from "lucide-react";
import { describe, expect, it } from "vitest";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { resolveSubagentIcon } from "./subagentIcons";

describe("resolveSubagentIcon", () => {
  it("resolves a branded root through its wrapper identity", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: "codex-native-ui",
        harness: null,
        agentName: null,
      }),
    ).toBe(CodexIcon);
  });

  it("preserves named-agent precedence for roots", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: null,
        harness: "claude-sdk",
        agentName: "nessie",
      }),
    ).toBe(NessieIcon);
  });

  it("uses a brand icon for a full native child wrapper", () => {
    expect(
      resolveSubagentIcon({ kind: "child", wrapper: "codex-native-ui", tool: "reviewer" }),
    ).toBe(CodexIcon);
  });

  it("uses a role icon for native sub-agent children", () => {
    expect(
      resolveSubagentIcon({
        kind: "child",
        wrapper: "codex-native-ui-subagent",
        tool: "Explore",
      }),
    ).toBe(SearchIcon);
  });

  it("falls back for unknown root and child identities", () => {
    expect(
      resolveSubagentIcon({
        kind: "root",
        wrapper: "unknown-wrapper",
        harness: "agents-sdk",
        agentName: "custom-agent",
      }),
    ).toBe(BotIcon);
    expect(resolveSubagentIcon({ kind: "child", wrapper: null, tool: "general-purpose" })).toBe(
      OttoIcon,
    );
  });
});
