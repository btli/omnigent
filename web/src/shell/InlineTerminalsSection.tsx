// Shells tab content for the right-side rail. Two modes:
//
//   - List mode (default, mobile drawer): a virtual "+ New shell" row on
//     top, then the session's shells as rows. Clicking a row hands the
//     shell to `onExpand`, which replaces the main session view with the
//     full-screen shell (mobile) — the rail stays a lightweight index.
//   - Inline mode (`inline`, desktop rail): clicking a row hosts the
//     shell's `TerminalView` INSIDE the rail, exactly mirroring how the
//     Files tab renders a FileViewer inline. Chat stays visible on the
//     left. A back affordance returns to the list so other shells stay
//     reachable. `onExpand` is unused here.

import { ChevronLeftIcon, TerminalIcon } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { inventoryTerminals, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { NewTerminalButton } from "./NewTerminalButton";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalStatusBadge } from "./terminalStatus";
import { useTerminalStatuses } from "./useTerminalStatuses";

interface InlineTerminalsSectionProps {
  conversationId: string;
  /** Open a shell in the main view, keyed by its terminal tab key. */
  onExpand: (terminalKey: string) => void;
  /**
   * Host the selected shell's terminal INSIDE the rail (desktop) rather
   * than handing it to `onExpand` for a full-screen takeover (mobile).
   * Mirrors the Files tab's inline FileViewer. Default false.
   */
  inline?: boolean;
  /**
   * Attach the inline terminal read-only — the viewer can watch but not
   * type. Set for non-owners: a shared PTY's keystrokes carry no per-user
   * identity, so only the owner may drive it (server-enforced). Only
   * meaningful in inline mode. Default false.
   */
  readOnly?: boolean;
}

export function InlineTerminalsSection({
  conversationId,
  onExpand,
  inline = false,
  readOnly = false,
}: InlineTerminalsSectionProps) {
  const { terminals: allTerminals } = useTerminals(conversationId);
  // Inventory view: the agent's own terminal (SDK REPL / native vendor
  // pane) backs the pill's Terminal view and must not appear as a
  // shell row here.
  const terminalFirstCtx = useTerminalFirst();
  const terminals = useMemo(
    () => inventoryTerminals(allTerminals, terminalFirstCtx?.isTerminalFirst ?? false),
    [allTerminals, terminalFirstCtx?.isTerminalFirst],
  );
  const { getStatus, setTerminalConnectionState, markTerminalActive } =
    useTerminalStatuses(terminals);

  // Host the shell inside the rail only for non-terminal-first sessions.
  // Terminal-first sessions (SDK REPL / native wrappers) keep routing to
  // `onExpand`, which opens the shell in the main column via
  // MainTerminalView — their established, chat-replacing UX.
  const isTerminalFirst = terminalFirstCtx?.isTerminalFirst ?? false;
  const hostInline = inline && !isTerminalFirst;

  // Inline hosting only: which shell is shown in the rail. Null shows the
  // list. A closed/disappeared shell falls back to the list below.
  const [activeKey, setActiveKey] = useState<string | null>(null);
  const activeTerminal = terminals.find((t) => terminalTabKey(t) === activeKey) ?? null;
  useEffect(() => {
    if (activeKey && !activeTerminal) setActiveKey(null);
  }, [activeKey, activeTerminal]);

  // Row click: host the shell in the rail (inline) or hand it off for a
  // full-screen takeover (mobile / terminal-first). New-shell creation
  // follows the same split so a freshly-created shell lands in the active
  // surface.
  const openShell = hostInline ? setActiveKey : onExpand;

  if (hostInline && activeTerminal) {
    return (
      <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
        {/* Shell header — back to the list + identity, mirroring
            MainTerminalView's chrome-free shell header. */}
        <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5">
          <button
            type="button"
            aria-label="Back to shells"
            onClick={() => setActiveKey(null)}
            className="flex items-center gap-1 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ChevronLeftIcon className="size-4 shrink-0" />
          </button>
          <span className="flex min-w-0 items-center gap-1.5 rounded-sm bg-muted px-2 py-1 text-foreground text-xs">
            <TerminalIcon className="size-3 shrink-0" />
            <span className="max-w-[8rem] truncate">{activeTerminal.name}</span>
            {activeTerminal.session && (
              <span className="shrink-0 text-muted-foreground/60">· {activeTerminal.session}</span>
            )}
            <TerminalStatusBadge status={getStatus(activeTerminal)} />
          </span>
        </div>
        <div key={activeTerminal.id} className="flex min-h-0 flex-1 flex-col p-2">
          <TerminalView
            sessionId={conversationId}
            terminalId={activeTerminal.id}
            readOnly={readOnly}
            transport={activeTerminal.transport}
            onStateChange={(state) => {
              setTerminalConnectionState(activeTerminal.id, state);
            }}
            onActivity={() => markTerminalActive(activeTerminal.id)}
          />
        </div>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      {/* Always a plain top-aligned list: a virtual "+ New shell" row
          first (gated inside NewTerminalButton on the agent's terminal
          access — leading keeps it at a fixed spot instead of drifting
          down as shells accumulate), then the shell rows. With zero
          shells the virtual row is the whole list — no centered
          empty-state copy. */}
      <div className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1">
        <NewTerminalButton conversationId={conversationId} onCreated={openShell} variant="row" />
        {terminals.map((t) => (
          <button
            key={terminalTabKey(t)}
            type="button"
            className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-accent/60"
            onClick={() => openShell(terminalTabKey(t))}
          >
            <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
            {t.session && <span className="shrink-0 text-xs font-medium">{t.session}</span>}
            <span className="truncate text-xs text-muted-foreground/70">{t.name}</span>
            <span className="flex-1" />
            <TerminalStatusBadge status={getStatus(t)} />
          </button>
        ))}
      </div>
    </div>
  );
}
