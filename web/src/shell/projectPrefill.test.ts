// Pure state-machine tests for the project-prefill location track. The
// component-level rules live in NewChatDialog.projectPrefill.test.tsx; these
// pin the transitions that need mid-flight timing (a user acting between the
// lookup starting and resolving), which the rendered harness can't sequence.
import { describe, expect, it } from "vitest";

import type { Conversation } from "@/hooks/useConversations";
import type { Host } from "@/hooks/useHosts";
import { initialPrefillState, projectPrefillStep } from "./projectPrefill";

const REPO = "/Users/corey/projects/alpha";

const hosts: Host[] = [
  { host_id: "host_1", name: "laptop", owner: "corey", status: "online" },
  { host_id: "host_2", name: "desktop", owner: "corey", status: "online" },
];

function newest(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_prev",
    object: "conversation",
    title: "Previous",
    created_at: 0,
    updated_at: 9,
    labels: { omni_project: "Alpha" },
    host_id: "host_1",
    workspace: REPO,
    git_branch: null,
    agent_id: "ag_hello",
    ...overrides,
  } as Conversation;
}

function inputs(overrides: Partial<Parameters<typeof projectPrefillStep>[1]> = {}) {
  return {
    newest: newest(),
    newestFailed: false,
    hosts,
    agents: [{ id: "ag_hello" }],
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
    ...overrides,
  };
}

/** Run the machine from the start until the given phase is reached. */
function stepTo(phase: string, stepInputs: ReturnType<typeof inputs>) {
  let state = initialPrefillState("Alpha");
  for (let i = 0; i < 10 && state.phase !== phase; i++) {
    const step = projectPrefillStep(state, stepInputs);
    if (step === null) break;
    state = step.state;
  }
  expect(state.phase).toBe(phase);
  return state;
}

describe("projectPrefill workspace phase vs live host pick", () => {
  it("settles without a workspace write when the user switched hosts mid-flight", () => {
    const state = stepTo("workspace", inputs());
    // User picked host_2 while the newest-session (host_1) lookup was in flight.
    const step = projectPrefillStep(state, inputs({ selectedHostId: "host_2" }));
    expect(step).not.toBeNull();
    expect(step!.state.phase).toBe("settled");
    expect(step!.writes.workspace).toBeUndefined();
    expect(step!.writes.branch).toBeUndefined();
  });

  it("proceeds when the live pick matches the newest session's host", () => {
    const state = stepTo("workspace", inputs());
    const step = projectPrefillStep(state, inputs({ selectedHostId: "host_1" }));
    expect(step).not.toBeNull();
    expect(step!.state.phase).toBe("branch");
    expect(step!.writes.workspace).toBe(REPO);
  });

  it("settles without a workspace write when the sandbox is selected", () => {
    const state = stepTo("workspace", inputs());
    const step = projectPrefillStep(state, inputs({ sandboxSelected: true }));
    expect(step).not.toBeNull();
    expect(step!.state.phase).toBe("settled");
    expect(step!.writes.workspace).toBeUndefined();
  });
});
