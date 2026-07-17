import { CLAUDE_NATIVE_MODELS } from "./claudeNativeModels";
import { NATIVE_CODING_AGENTS, nativeCodingAgentForHarness } from "./nativeCodingAgents";

export const CLAUDE_NATIVE_EFFORTS = [
  { value: "low", label: "Low" },
  { value: "medium", label: "Medium" },
  { value: "high", label: "High" },
  { value: "xhigh", label: "xHigh" },
  { value: "max", label: "Max" },
] as const;

export interface ModelOption {
  id: string;
  label: string;
}

export interface EffortOption {
  value: string;
  label: string;
}

export interface HarnessOption {
  id: string;
  label: string;
}

/**
 * Whether a project harness default resolves to Claude Code's native wrapper.
 * Alias-aware via the canonical native-agent registry. Only this harness gets
 * the static model/effort catalog — mirroring the in-session picker, where
 * SDK sessions have no native model picker.
 */
export function isClaudeNativeHarness(harness: string | null): boolean {
  return nativeCodingAgentForHarness(harness)?.key === "claude";
}

const NATIVE_HARNESS_OPTIONS: readonly HarnessOption[] = [...NATIVE_CODING_AGENTS]
  .sort((a, b) => a.sortRank - b.sortRank)
  .map((agent) => ({ id: agent.harness, label: agent.displayName }));

/**
 * Harness options for the project defaults picker: the native terminal
 * wrappers (Claude Code, Codex, …) followed by the brain-harness catalog.
 * `useBrainHarnessLabels()` deliberately excludes native wrappers, so both
 * sources are needed for the full set a project can pin.
 */
export function harnessOptionsForProject(
  brainLabels: Record<string, string>,
): readonly HarnessOption[] {
  const options = [...NATIVE_HARNESS_OPTIONS];
  const seen = new Set(options.map((option) => option.id));
  for (const [id, label] of Object.entries(brainLabels)) {
    if (!seen.has(id)) options.push({ id, label });
  }
  return options;
}

export function modelOptionsForHarness(harness: string | null): readonly ModelOption[] {
  return isClaudeNativeHarness(harness) ? CLAUDE_NATIVE_MODELS : [];
}

export function effortOptionsForHarness(harness: string | null): readonly EffortOption[] {
  return isClaudeNativeHarness(harness) ? CLAUDE_NATIVE_EFFORTS : [];
}
