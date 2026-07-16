import type { Host } from "@/hooks/useHosts";
import type { Conversation } from "@/hooks/useConversations";
import type { HostWorktree } from "@/hooks/useHostWorktrees";

type ProjectPrefillPhase = "host" | "workspace" | "branch" | "settled";

export interface ProjectPrefillState {
  /** Stable project id this machine is seeding for; "" = plain visit. */
  projectId: string;
  phase: ProjectPrefillPhase;
  agentSeeded: boolean;
  seededWorkspace: string | null;
}

export function initialPrefillState(projectId: string): ProjectPrefillState {
  const plain = projectId === "";
  return {
    projectId,
    phase: plain ? "settled" : "host",
    agentSeeded: plain,
    seededWorkspace: null,
  };
}

export function prefillDone(state: ProjectPrefillState): boolean {
  return state.phase === "settled" && state.agentSeeded;
}

interface ProjectPrefillInputs {
  newest: Conversation | null | undefined;
  newestFailed: boolean;
  hosts: Host[] | undefined;
  agents: { id: string }[] | undefined;
  sandboxSelected: boolean;
  selectedHostId: string | null;
  lastAgentId: string | null;
  sourceWorktrees: HostWorktree[] | undefined;
  sourceWorktreesFailed: boolean;
  workspaceTrimmed: string;
  branchName: string;
  prefilledBranch: string;
  hostWorktrees: HostWorktree[] | undefined;
  hostWorktreesFailed: boolean;
}

interface ProjectPrefillWrites {
  hostId?: string;
  agentId?: string;
  workspace?: string;
  branch?: string;
}

export function projectPrefillStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
): { state: ProjectPrefillState; writes: ProjectPrefillWrites } | null {
  const writes: ProjectPrefillWrites = {};
  let next = state;

  if (!state.agentSeeded) {
    const { newest, newestFailed, agents, hosts, lastAgentId } = inputs;
    const lookupDone = newest !== undefined || newestFailed;
    const needsHosts = newest != null && newest.host_id != null;
    if (lookupDone && agents !== undefined && (!needsHosts || hosts !== undefined)) {
      next = { ...next, agentSeeded: true };
      const hostId = newest?.host_id;
      const hostUsable =
        hostId == null ||
        (hosts ?? []).some((host) => host.host_id === hostId && host.status === "online");
      const agentId = newest?.agent_id;
      if (hostUsable && agentId != null && agents.some((agent) => agent.id === agentId)) {
        writes.agentId = agentId;
      } else if (lastAgentId) {
        writes.agentId = lastAgentId;
      }
    }
  }

  const location = locationStep(next, inputs, writes);
  if (location !== null) next = location;
  if (next === state) return null;
  return { state: next, writes };
}

function settled(state: ProjectPrefillState): ProjectPrefillState {
  return { ...state, phase: "settled" };
}

function locationStep(
  state: ProjectPrefillState,
  inputs: ProjectPrefillInputs,
  writes: ProjectPrefillWrites,
): ProjectPrefillState | null {
  if (state.phase === "host") {
    const { newest, newestFailed, hosts } = inputs;
    if ((newest === undefined && !newestFailed) || hosts === undefined) return null;
    return { ...state, phase: "workspace" };
  }

  if (state.phase === "workspace") {
    const { newest, hosts, sourceWorktrees, sourceWorktreesFailed, selectedHostId } = inputs;
    const hostId = newest?.host_id ?? null;
    const sourceWorkspace = newest?.workspace ?? null;
    if (
      newest == null ||
      hostId === null ||
      sourceWorkspace === null ||
      inputs.sandboxSelected ||
      (selectedHostId !== null && selectedHostId !== hostId) ||
      !(hosts ?? []).some((host) => host.host_id === hostId && host.status === "online")
    ) {
      return settled(state);
    }
    if (newest.git_branch == null) {
      writes.hostId = hostId;
      writes.workspace = sourceWorkspace;
      return { ...state, phase: "branch", seededWorkspace: sourceWorkspace };
    }
    if (sourceWorktreesFailed) return settled(state);
    if (sourceWorktrees === undefined) return null;
    const main = sourceWorktrees.find((worktree) => worktree.is_main);
    if (main === undefined) return settled(state);
    writes.hostId = hostId;
    writes.workspace = main.path;
    return { ...state, phase: "branch", seededWorkspace: main.path };
  }

  if (state.phase === "branch") {
    const { workspaceTrimmed, hostWorktrees, hostWorktreesFailed, branchName, prefilledBranch } =
      inputs;
    if (inputs.sandboxSelected) return settled(state);
    if (workspaceTrimmed === "" || workspaceTrimmed !== state.seededWorkspace) {
      return settled(state);
    }
    if (hostWorktreesFailed) return settled(state);
    if (hostWorktrees === undefined) return null;
    if (!hostWorktrees.some((worktree) => worktree.is_main)) return settled(state);
    if (branchName === "" && prefilledBranch === "") {
      const suffix = crypto.randomUUID().replace(/-/g, "").slice(0, 8);
      writes.branch = `worktree-${suffix}`;
    }
    return settled(state);
  }

  return null;
}
