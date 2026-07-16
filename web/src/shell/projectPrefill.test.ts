import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  initialPrefillState,
  prefillDone,
  projectPrefillStep,
  resolvePrefillValue,
} from "./projectPrefill";

describe("project prefill identity", () => {
  it("anchors a project visit on its stable project id", () => {
    expect(initialPrefillState("proj_sprint_42")).toEqual({
      projectId: "proj_sprint_42",
      phase: "host",
      agentSeeded: false,
      seededWorkspace: null,
    });
  });

  it("starts a plain visit settled", () => {
    expect(prefillDone(initialPrefillState(""))).toBe(true);
  });
});

describe("project prefill source precedence", () => {
  it("uses a value set by the resolved project bundle", () => {
    expect(
      resolvePrefillValue({ bundle: "bundle-model", fallback: "session-model", edited: false }),
    ).toBe("bundle-model");
  });

  it("falls back when the project bundle leaves the field unset", () => {
    expect(
      resolvePrefillValue({ bundle: undefined, fallback: "session-workspace", edited: false }),
    ).toBe("session-workspace");
  });

  it("keeps a manual edit over the bundle and fallback", () => {
    expect(
      resolvePrefillValue({
        bundle: "bundle-harness",
        fallback: "session-harness",
        manual: "manual-harness",
        edited: true,
      }),
    ).toBe("manual-harness");
  });

  const newest = {
    id: "session_1",
    object: "conversation",
    title: null,
    created_at: 1,
    updated_at: 1,
    labels: {},
    permission_level: 4,
    host_id: "host_session",
    workspace: "/session/workspace",
    git_branch: null,
  } satisfies Conversation;
  const defaults = {
    host_type: "external" as const,
    host_id: "host_bundle",
    workspace: "/bundle/workspace",
    git: null,
    harness_override: null,
    model_override: null,
    reasoning_effort: null,
    row_version: 7,
  };
  const state = {
    ...initialPrefillState("project_1"),
    phase: "workspace" as const,
    agentSeeded: true,
  };
  const inputs = {
    newest,
    newestFailed: false,
    defaults,
    defaultsFailed: false,
    hosts: [
      { host_id: "host_bundle", name: "bundle", owner: "me", status: "online" as const },
      { host_id: "host_session", name: "session", owner: "me", status: "online" as const },
    ],
    agents: [],
    sandboxSelected: false,
    selectedHostId: null,
    lastAgentId: null,
    sourceWorktrees: undefined,
    sourceWorktreesFailed: false,
    workspaceTrimmed: "",
    branchName: "",
    prefilledBranch: "",
    hostWorktrees: undefined,
    hostWorktreesFailed: false,
    hostEdited: false,
    workspaceEdited: false,
  } satisfies Parameters<typeof projectPrefillStep>[1];

  it("writes bundle location fields ahead of the newest session", () => {
    expect(projectPrefillStep(state, inputs)?.writes).toMatchObject({
      hostId: "host_bundle",
      workspace: "/bundle/workspace",
    });
  });

  it("uses newest-session location fields when the bundle leaves them unset", () => {
    const result = projectPrefillStep(state, {
      ...inputs,
      defaults: { ...defaults, host_id: null, workspace: null },
    });
    expect(result?.writes).toMatchObject({
      hostId: "host_session",
      workspace: "/session/workspace",
    });
  });

  it("does not write location fields after a manual edit", () => {
    const result = projectPrefillStep(state, {
      ...inputs,
      selectedHostId: "host_session",
      workspaceTrimmed: "/manual/workspace",
      hostEdited: true,
      workspaceEdited: true,
    });
    expect(result?.writes).toEqual({});
  });
});
