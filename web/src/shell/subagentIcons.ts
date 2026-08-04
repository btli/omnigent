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

const BRAND_ICONS: Partial<Record<NativeCodingAgentIconKind, AgentIcon>> = {
  claude: ClaudeIcon,
  codex: CodexIcon,
  opencode: OpenCodeIcon,
  pi: PiIcon,
  cursor: CursorIcon,
  kiro: KiroIcon,
  goose: GooseIcon,
  kimi: KimiIcon,
  antigravity: AntigravityIcon,
  hermes: HermesIcon,
};

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
  if (nativeIconKind === "claude" || source.harness?.includes("claude")) return ClaudeIcon;
  if (nativeIconKind === "codex" || source.harness?.includes("codex")) return CodexIcon;
  if (nativeIconKind === "opencode" || source.harness?.includes("opencode")) return OpenCodeIcon;
  if (nativeIconKind === "cursor" || source.harness?.includes("cursor")) return CursorIcon;
  if (nativeIconKind === "kiro" || source.harness?.includes("kiro")) return KiroIcon;
  if (nativeIconKind === "goose" || source.harness?.includes("goose")) return GooseIcon;
  if (nativeIconKind === "kimi" || source.harness?.includes("kimi")) return KimiIcon;
  if (nativeIconKind === "antigravity" || source.harness?.includes("antigravity")) {
    return AntigravityIcon;
  }
  // Exact match avoids false positives such as "openapi".
  if (nativeIconKind === "pi" || source.harness === "pi") return PiIcon;
  if (source.agentName === "nessie") return NessieIcon;
  return BotIcon;
}

function iconForCatalog(source: Extract<AgentIconSource, { kind: "catalog" }>): AgentIcon {
  // Nessie uses claude-sdk, so its name must win over the harness fallback.
  if (source.name === "nessie") return NessieIcon;
  const nativeIconKind = nativeCodingAgentForAvailableAgent(source)?.iconKind;
  const nativeIcon = nativeIconKind === undefined ? undefined : BRAND_ICONS[nativeIconKind];
  if (nativeIcon !== undefined) return nativeIcon;

  if (source.harness?.includes("codex")) return CodexIcon;
  if (source.harness?.includes("claude")) return ClaudeIcon;
  if (source.harness?.includes("cursor")) return CursorIcon;
  if (source.harness?.includes("hermes")) return HermesIcon;
  if (source.harness?.includes("kiro")) return KiroIcon;
  if (source.harness?.includes("goose")) return GooseIcon;
  if (source.harness?.includes("kimi")) return KimiIcon;
  if (source.harness === "pi") return PiIcon;
  if (source.harness?.includes("antigravity")) return AntigravityIcon;
  return BotIcon;
}

/** Resolve the decorative glyph for an agent without changing context-specific fallbacks. */
export function resolveAgentIcon(source: AgentIconSource): AgentIcon {
  if (source.kind === "root") return iconForRoot(source);
  if (source.kind === "catalog") return iconForCatalog(source);

  const nativeIconKind = nativeCodingAgentForWrapper(source.wrapper)?.iconKind;
  const nativeIcon = nativeIconKind === undefined ? undefined : BRAND_ICONS[nativeIconKind];
  if (nativeIcon !== undefined) return nativeIcon;
  // Pi scaffold children have no wrapper label, so their exact tool name is authoritative.
  if (source.tool === "pi") return PiIcon;
  return iconForAgentType(source.tool);
}
