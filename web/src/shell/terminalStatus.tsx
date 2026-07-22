import { TerminalIcon } from "lucide-react";
import { type ConnectionState } from "@/components/blocks/TerminalSession";
import { type TerminalInfo } from "@/hooks/useTerminals";
import { cn } from "@/lib/utils";

/**
 * Derived per-terminal status shown in terminal selectors.
 *
 * The terminal resource's ``running`` flag is per-terminal. The live
 * WebSocket bridge state and activity timestamps are only known for
 * mounted terminals.
 */
export type TerminalStatus = "active" | "idle" | "connecting" | "error" | "closed";

export const STATUS_CONFIG: Record<TerminalStatus, { label: string; className: string }> = {
  active: { label: "Active", className: "bg-emerald-500" },
  idle: { label: "Idle", className: "bg-muted-foreground/55" },
  connecting: { label: "Connecting", className: "bg-amber-500 animate-pulse" },
  error: { label: "Error", className: "bg-red-500" },
  // Softened from a stark solid black/white blob to a muted foreground tone so
  // a closed dot reads as "inactive", not an alarm — especially in the compact
  // tab strip where it sits beside busier chrome.
  closed: { label: "Closed", className: "bg-muted-foreground/40" },
};

/** ``name · session`` identity string for a terminal (session omitted if absent). */
export function terminalLabel(t: TerminalInfo): string {
  return t.session ? `${t.name} · ${t.session}` : t.name;
}

/**
 * Status DOT only (no text word) — for compact contexts like the top tab
 * strip where the full badge's label would make tabs busier than file tabs.
 * The accessible name is preserved via ``aria-label``/``title`` on the dot.
 *
 * :param status: Terminal-local display status, e.g. ``"idle"``.
 */
export function TerminalStatusDot({ status }: { status: TerminalStatus }) {
  const { label, className } = STATUS_CONFIG[status];
  return (
    <span
      aria-label={label}
      title={label}
      className={cn("inline-block size-1.5 shrink-0 rounded-full", className)}
    />
  );
}

/**
 * Render a visible status dot and label for a terminal tab or selector row.
 *
 * :param status: Terminal-local display status, e.g. ``"idle"``.
 */
export function TerminalStatusBadge({ status }: { status: TerminalStatus }) {
  const { label, className } = STATUS_CONFIG[status];
  return (
    <span
      aria-label={label}
      title={label}
      className="inline-flex shrink-0 items-center gap-1 text-muted-foreground text-xs"
    >
      <span className={cn("inline-block size-1.5 rounded-full", className)} />
      <span>{label}</span>
    </span>
  );
}

/**
 * Identity chip for a terminal's inline header: icon, name, optional
 * session key, and status dot. Used by the rail's inline shell header.
 *
 * :param terminal: Terminal whose name/session drive the chip.
 * :param status: Terminal-local display status for the trailing badge.
 * :param className: Extra classes on the outer span (e.g. ``min-w-0``).
 */
export function TerminalIdentityChip({
  terminal,
  status,
  className,
}: {
  terminal: TerminalInfo;
  status: TerminalStatus;
  className?: string;
}) {
  return (
    <span
      className={cn(
        "flex items-center gap-1.5 rounded-sm bg-muted px-2 py-1 text-foreground text-xs",
        className,
      )}
    >
      <TerminalIcon className="size-3 shrink-0" />
      <span className="max-w-[8rem] truncate">{terminal.name}</span>
      {terminal.session && (
        <span className="shrink-0 text-muted-foreground/60">· {terminal.session}</span>
      )}
      <TerminalStatusBadge status={status} />
    </span>
  );
}

/**
 * Derive the display status for a single terminal.
 *
 * :param terminal: Terminal resource entry from ``useTerminals``.
 * :param connectionState: Live bridge state for this terminal when it is
 *     mounted. Pass ``null`` for inactive terminals.
 * :param isActive: Best-effort activity flag from recent PTY output.
 * :returns: Terminal display status.
 */
export function deriveTerminalStatus(
  terminal: TerminalInfo,
  connectionState: ConnectionState | null,
  isActive = false,
): TerminalStatus {
  if (connectionState?.kind === "closed") return "closed";
  if (connectionState?.kind === "error") return "error";
  if (connectionState?.kind === "connecting") return "connecting";
  if (!terminal.running) return "closed";
  if (isActive) return "active";
  return "idle";
}
