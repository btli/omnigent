import type { ComponentType, SVGProps } from "react";
import {
  BookOpenIcon,
  Code2Icon,
  CompassIcon,
  FileTextIcon,
  FlaskConicalIcon,
  ScanSearchIcon,
  SearchIcon,
} from "lucide-react";
import { iconForAgent } from "@/components/AgentCard";
import { OttoIcon } from "@/components/icons/OttoIcon";
import { nativeCodingAgentForWrapper } from "@/lib/nativeCodingAgents";

export type AgentIcon = ComponentType<SVGProps<SVGSVGElement>>;

export type SubagentIconSource =
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

export function iconForAgentType(tool: string | null): AgentIcon {
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

/** Resolve a root or child glyph while keeping catalog branding authoritative. */
export function resolveSubagentIcon(source: SubagentIconSource): AgentIcon {
  const nativeAgent = nativeCodingAgentForWrapper(source.wrapper);
  if (source.kind === "root") {
    return iconForAgent({
      name: nativeAgent?.agentName ?? source.agentName ?? "",
      harness: nativeAgent?.harness ?? source.harness,
    });
  }
  if (nativeAgent !== undefined) {
    return iconForAgent({ name: nativeAgent.agentName, harness: nativeAgent.harness });
  }
  if (source.tool === "pi") return iconForAgent({ name: "", harness: "pi" });
  return iconForAgentType(source.tool);
}
