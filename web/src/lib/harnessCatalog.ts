import { CLAUDE_NATIVE_MODELS } from "./claudeNativeModels";

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

const CLAUDE_FAMILY_HARNESSES = new Set([
  "claude-native",
  "claude-sdk",
  "claude_sdk",
  "claude",
]);

export function isClaudeFamilyHarness(harness: string | null): boolean {
  return harness !== null && CLAUDE_FAMILY_HARNESSES.has(harness);
}

export function modelOptionsForHarness(harness: string | null): readonly ModelOption[] {
  return isClaudeFamilyHarness(harness) ? CLAUDE_NATIVE_MODELS : [];
}

export function effortOptionsForHarness(
  harness: string | null,
  _model: string | null,
): readonly EffortOption[] {
  return isClaudeFamilyHarness(harness) ? CLAUDE_NATIVE_EFFORTS : [];
}
