import type { ComponentType, SVGProps } from "react";
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
import { AntigravityIcon } from "@/components/icons/AntigravityIcon";
import { ClaudeIcon } from "@/components/icons/ClaudeIcon";
import { CodexIcon } from "@/components/icons/CodexIcon";
import { CursorIcon } from "@/components/icons/CursorIcon";
import { GooseIcon } from "@/components/icons/GooseIcon";
import { HermesIcon } from "@/components/icons/HermesIcon";
import { KimiIcon } from "@/components/icons/KimiIcon";
import { KiroIcon } from "@/components/icons/KiroIcon";
import { NessieIcon } from "@/components/icons/NessieIcon";
import { OpenCodeIcon } from "@/components/icons/OpenCodeIcon";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { PiIcon } from "@/components/icons/PiIcon";
import {
  nativeCodingAgentForAvailableAgent,
  nativeCodingAgentForWrapper,
  type NativeCodingAgentIconKind,
} from "@/lib/nativeCodingAgents";

export type AgentIcon = ComponentType<SVGProps<SVGSVGElement>>;

export type AgentIconSource =
  | {
      kind: "catalog";
      name: string;
      harness: string | null;
    }
  | {
      kind: "root";
      wrapper: string | null;
      harness: string | null;
      agentName: string | null;
    }
  | {
      kind: "child";
      wrapper: string | null;
      tool: string | null;
    };

type HarnessMatchMode = "substring" | "exact";

interface BrandIconDefinition {
  kind: NativeCodingAgentIconKind;
  icon: AgentIcon;
  harnessMatch: HarnessMatchMode;
  catalogPriority: number | null;
}

// Qwen has no glyph yet; see docs/QWEN_FOLLOWUPS.md.
const BRAND_ICONS = [
  { kind: "claude", icon: ClaudeIcon, harnessMatch: "substring", catalogPriority: 1 },
  { kind: "codex", icon: CodexIcon, harnessMatch: "substring", catalogPriority: 0 },
  { kind: "opencode", icon: OpenCodeIcon, harnessMatch: "substring", catalogPriority: null },
  { kind: "cursor", icon: CursorIcon, harnessMatch: "substring", catalogPriority: 2 },
  { kind: "kiro", icon: KiroIcon, harnessMatch: "substring", catalogPriority: 4 },
  { kind: "goose", icon: GooseIcon, harnessMatch: "substring", catalogPriority: 5 },
  { kind: "kimi", icon: KimiIcon, harnessMatch: "substring", catalogPriority: 6 },
  { kind: "antigravity", icon: AntigravityIcon, harnessMatch: "substring", catalogPriority: 8 },
  { kind: "hermes", icon: HermesIcon, harnessMatch: "substring", catalogPriority: 3 },
  // Exact match avoids false positives such as "openapi".
  { kind: "pi", icon: PiIcon, harnessMatch: "exact", catalogPriority: 7 },
] as const satisfies readonly BrandIconDefinition[];

const CATALOG_HARNESS_BRANDS = BRAND_ICONS.filter((brand) => brand.catalogPriority !== null).sort(
  (left, right) => (left.catalogPriority ?? 0) - (right.catalogPriority ?? 0),
);

function brandIconForKind(kind: NativeCodingAgentIconKind | undefined): AgentIcon | undefined {
  return BRAND_ICONS.find((brand) => brand.kind === kind)?.icon;
}

function harnessMatchesBrand(harness: string | null, brand: (typeof BRAND_ICONS)[number]): boolean {
  return brand.harnessMatch === "exact"
    ? harness === brand.kind
    : (harness?.includes(brand.kind) ?? false);
}

function iconForAgentType(tool: string | null): AgentIcon {
  const normalized = (tool ?? "").toLowerCase();
  if (normalized.includes("explore")) return SearchIcon;
  if (normalized.includes("research")) return BookOpenIcon;
  if (normalized.includes("plan") || normalized.includes("architect")) return CompassIcon;
  if (normalized.includes("review")) return ScanSearchIcon;
  if (normalized.includes("test")) return FlaskConicalIcon;
  if (normalized.includes("doc") || normalized.includes("writ")) return FileTextIcon;
  if (
    normalized.includes("code") ||
    normalized.includes("eng") ||
    normalized.includes("dev") ||
    normalized.includes("front") ||
    normalized.includes("back")
  ) {
    return Code2Icon;
  }
  return OttoIcon;
}

function iconForRoot(source: Extract<AgentIconSource, { kind: "root" }>): AgentIcon {
  const nativeIconKind = nativeCodingAgentForWrapper(source.wrapper)?.iconKind;
  for (const brand of BRAND_ICONS) {
    if (nativeIconKind === brand.kind || harnessMatchesBrand(source.harness, brand)) {
      return brand.icon;
    }
  }
  if (source.agentName === "nessie") return NessieIcon;
  return BotIcon;
}

function iconForCatalog(source: Extract<AgentIconSource, { kind: "catalog" }>): AgentIcon {
  // Nessie uses claude-sdk, so its name must win over the harness fallback.
  if (source.name === "nessie") return NessieIcon;
  const nativeIconKind = nativeCodingAgentForAvailableAgent(source)?.iconKind;
  const nativeIcon = brandIconForKind(nativeIconKind);
  if (nativeIcon !== undefined) return nativeIcon;

  const harnessBrand = CATALOG_HARNESS_BRANDS.find((brand) =>
    harnessMatchesBrand(source.harness, brand),
  );
  if (harnessBrand !== undefined) return harnessBrand.icon;
  return BotIcon;
}

/** Resolve the decorative glyph for an agent using its context-specific fallback rules. */
export function resolveAgentIcon(source: AgentIconSource): AgentIcon {
  if (source.kind === "root") return iconForRoot(source);
  if (source.kind === "catalog") return iconForCatalog(source);

  // A native session's sub-agents all share its brand, so repeating that logo
  // down the tree conveys nothing. Their `-subagent` wrappers intentionally do
  // not resolve here, letting role icons distinguish what each task is doing.
  const nativeIconKind = nativeCodingAgentForWrapper(source.wrapper)?.iconKind;
  const nativeIcon = brandIconForKind(nativeIconKind);
  if (nativeIcon !== undefined) return nativeIcon;
  // Pi scaffold children have no wrapper label, so their exact tool name is authoritative.
  if (source.tool === "pi") return PiIcon;
  return iconForAgentType(source.tool);
}
