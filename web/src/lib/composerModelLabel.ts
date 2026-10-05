// Canonical model / effort label formatting for the composer, shared by the
// landing dialog and the in-session chat composer so both surfaces render the
// same label for the same model.
//
// Pure leaf module (no React, no store) so the landing screen, the chat page,
// the harness config controls, and the store can all depend on one source of
// truth without a circular import.

import { SMART_ROUTING_LABEL } from "@/lib/agentLabels";
import { supportsEffortControl } from "@/lib/sessionCapabilities";
import { codexEffortLevelsForModel } from "@/lib/codexNativeModels";
import { nativeCodingAgentForHarness } from "@/lib/nativeCodingAgents";
import { fusionModelLabel, isFusionModelUid } from "@/lib/devinFusion";
import type { NativeModelOption } from "@/lib/types";

const EFFORT_LEVELS = ["low", "medium", "high"] as const;

/** Anthropic-side efforts for claude-native sessions (matches ANTHROPIC_EFFORTS in reasoning_effort.py). */
const CLAUDE_NATIVE_EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"] as const;

/** Pi thinking ladder (matches PI_EFFORTS in reasoning_effort.py; ``ultra`` aliases to ``max`` on Pi so omitted). */
const PI_NATIVE_EFFORT_LEVELS = [
  "none",
  "minimal",
  "low",
  "medium",
  "high",
  "xhigh",
  "max",
] as const;

// Label-less sessions inherit their wrapper from the harness; sub-agent
// children own no input surface, so they must not inherit it.
export function effectiveWrapperLabel(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
): string | undefined {
  const label = conv?.labels?.["omnigent.wrapper"];
  if (label != null) return label;
  if (conv?.parentSessionId != null) return undefined;
  return nativeCodingAgentForHarness(conv?.harness)?.wrapperLabel;
}

export function effortLevelsForConv(
  conv:
    | {
        labels?: Record<string, string | null> | null;
        harness?: string | null;
        parentSessionId?: string | null;
      }
    | null
    | undefined,
  codexModelOptions: readonly NativeModelOption[] = [],
  currentModel: string | null = null,
): readonly string[] {
  switch (effectiveWrapperLabel(conv)) {
    case "claude-code-native-ui":
      return CLAUDE_NATIVE_EFFORT_LEVELS;
    // Devin encodes effort in model variants, whose available rungs differ per model.
    case "devin-native-ui":
    case "codex-native-ui":
      return codexEffortLevelsForModel(codexModelOptions, currentModel);
    case "pi-native-ui":
      return PI_NATIVE_EFFORT_LEVELS;
    default:
      return EFFORT_LEVELS;
  }
}

export function shouldShowComposerEffort(
  session: Parameters<typeof supportsEffortControl>[0],
  effortLevels: readonly string[],
): boolean {
  return supportsEffortControl(session) && effortLevels.length > 0;
}

const DISPLAY_ONLY_CATALOG_PREFIXES = ["databricks-", "system.ai."] as const;

/** The native-catalog fields a model label is built from. A superset like
 *  {@link NativeModelOption} is assignable to this. */
export interface NativeModelLabelFields {
  id: string;
  model?: string;
  displayName?: string;
  isDefault?: boolean;
}

export function nativeModelLabel(option: NativeModelLabelFields): string {
  const label = option.displayName ?? option.model ?? option.id;
  // Some provider catalogs repeat the transport id as their display name.
  // Hide its mechanical namespace while preserving real advertised labels.
  const isTransportLabel = [option.id, option.model].some(
    (id) => id != null && (label === id || label === id.slice(id.indexOf("/") + 1)),
  );
  if (option.displayName != null && !isTransportLabel) return label;
  for (const prefix of DISPLAY_ONLY_CATALOG_PREFIXES) {
    if (label.startsWith(prefix)) return label.slice(prefix.length);
  }
  return label;
}

export function defaultModelLabel(options: readonly NativeModelLabelFields[]): string {
  const defaultOption = options.find((option) => option.isDefault);
  return defaultOption ? `Default (${nativeModelLabel(defaultOption)})` : "Default";
}

export function compactModelTriggerLabel(value: string): string {
  const withoutDefault = /^Default \((.*)\)$/.exec(value)?.[1] ?? value;
  return withoutDefault.replace(/\s*\((\d+(?:\.\d+)?[KMG]) context\)/gi, " $1");
}

export function composerModelChipLabel({
  modelSummary,
  modelLabelLoading = false,
  nativeDisplayName,
  harnessLabel,
  session,
  model = null,
  modelOptions = [],
  showEffort = shouldShowComposerEffort(session, effortLevelsForConv(session, modelOptions, model)),
  effort = null,
  routingOn = false,
}: {
  modelSummary?: string | null;
  modelLabelLoading?: boolean;
  nativeDisplayName?: string | null;
  harnessLabel?: string | null;
  session?: Parameters<typeof supportsEffortControl>[0];
  model?: string | null;
  modelOptions?: readonly NativeModelOption[];
  showEffort?: boolean;
  effort?: string | null;
  routingOn?: boolean;
}): { label: string; effortLabel: string | null } {
  return {
    label: routingOn
      ? SMART_ROUTING_LABEL
      : modelLabelLoading
        ? ""
        : compactModelTriggerLabel(modelSummary ?? nativeDisplayName ?? harnessLabel ?? "Session"),
    effortLabel: showEffort && !routingOn ? formatStatusEffortLabel(effort) : null,
  };
}

export function formatStatusModelLabel(
  model: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const raw = model?.trim();
  if (!raw) return null;
  if (isFusionModelUid(raw)) {
    const descriptor = codexModelOptions.find((candidate) => candidate.fusion)?.fusion;
    if (descriptor) return fusionModelLabel(descriptor, raw);
  }
  const option =
    codexModelOptions.find((candidate) => candidate.id === raw) ??
    codexModelOptions.find((candidate) => candidate.model === raw);
  return option ? nativeModelLabel(option) : raw;
}

/** Normalize a reasoning-effort value to its display label — the single place
 *  ``xhigh`` becomes ``xHigh``. Any other value is capitalized. */
export function normalizeEffortLabel(effort: string): string {
  if (effort.toLowerCase() === "xhigh") return "xHigh";
  return effort.charAt(0).toUpperCase() + effort.slice(1);
}

/** Display label for a reasoning-effort value, or ``null`` when unset. */
export function formatStatusEffortLabel(effort: string | null): string | null {
  if (!effort) return null;
  return normalizeEffortLabel(effort);
}

/**
 * Compose the current model and effort for the composer status tray.
 *
 * @param model - Model override or bound model id.
 * @param effort - Current reasoning effort override, if any.
 * @returns Compact label such as ``"gpt-5.5 xHigh"``, or ``null`` when neither is known.
 */
export function formatModelEffortStatusLabel(
  model: string | null,
  effort: string | null,
  codexModelOptions: readonly NativeModelOption[] = [],
): string | null {
  const modelLabel = formatStatusModelLabel(model, codexModelOptions);
  const effortLabel = formatStatusEffortLabel(effort);
  const parts = [modelLabel, effortLabel].filter((p): p is string => p != null && p.length > 0);
  return parts.length > 0 ? parts.join(" ") : null;
}
