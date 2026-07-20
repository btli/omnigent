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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { TerminalView } from "@/components/blocks/TerminalView";
import { inventoryTerminals, terminalTabKey, useTerminals } from "@/hooks/useTerminals";
import { NewTerminalButton } from "./NewTerminalButton";
import { useTerminalFirst } from "./TerminalFirstContext";
import { TerminalIdentityChip, TerminalStatusBadge } from "./terminalStatus";
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
  const [announcement, setAnnouncement] = useState("");
  const shellListRef = useRef<HTMLDivElement>(null);
  const shouldFocusListRef = useRef(false);
  const pendingCreatedKeyRef = useRef<string | null>(null);
  const activeTerminal = terminals.find((t) => terminalTabKey(t) === activeKey) ?? null;

  const returnToList = useCallback((unexpected: boolean) => {
    shouldFocusListRef.current = true;
    pendingCreatedKeyRef.current = null;
    setAnnouncement(
      unexpected ? "The active shell closed unexpectedly. Focus returned to the shell list." : "",
    );
    setActiveKey(null);
  }, []);

  useEffect(() => {
    if (!hostInline || !activeKey) return;
    // onCreated fires before the terminals hook necessarily exposes the
    // cache update, so a freshly-created shell is briefly absent from the
    // inventory. pending marks that gap.
    const pending = pendingCreatedKeyRef.current === activeKey;
    // The pending shell surfaced — drop its marker.
    if (activeTerminal && pending) pendingCreatedKeyRef.current = null;
    // The selection vanished without a pending marker — it closed
    // unexpectedly, so fall back to the list.
    if (!activeTerminal && !pending) returnToList(true);
  }, [activeKey, activeTerminal, hostInline, returnToList]);

  // `returnToList` is the only setter of shouldFocusListRef, and it nulls
  // activeKey in the same call — so when the ref is set, activeKey is
  // already null and the list is rendered and focusable. activeKey stays
  // in the deps to re-run once that null commit lands.
  useEffect(() => {
    if (!hostInline || !shouldFocusListRef.current) return;
    shellListRef.current?.focus();
    shouldFocusListRef.current = false;
  }, [activeKey, hostInline]);

  // Row click: host the shell in the rail (inline) or hand it off for a
  // full-screen takeover (mobile / terminal-first). New-shell creation
  // follows the same split so a freshly-created shell lands in the active
  // surface.
  // `pendingInventory` marks the creation path: a freshly-created shell
  // is selected before the terminals hook exposes it, so record it in
  // pendingCreatedKeyRef to bridge the create->inventory gap (the focus
  // effect above keeps the selection instead of misreading the gap as an
  // unexpected close). Selecting an existing shell clears the ref.
  const openShell = useCallback(
    (key: string, pendingInventory: boolean) => {
      if (!hostInline) {
        onExpand(key);
        return;
      }
      pendingCreatedKeyRef.current = pendingInventory ? key : null;
      setAnnouncement("");
      setActiveKey(key);
    },
    [hostInline, onExpand],
  );

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-card">
      {hostInline && (
        <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
          {announcement}
        </div>
      )}
      {hostInline && activeTerminal ? (
        <>
          {/* Shell header — back to the list + identity, mirroring
            MainTerminalView's chrome-free shell header. */}
          <div className="flex shrink-0 items-center gap-1.5 border-b border-border px-2 py-1.5">
            <button
              type="button"
              aria-label="Back to shells"
              onClick={() => returnToList(false)}
              className="flex items-center gap-1 rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            >
              <ChevronLeftIcon className="size-4 shrink-0" />
            </button>
            <TerminalIdentityChip
              terminal={activeTerminal}
              status={getStatus(activeTerminal)}
              className="min-w-0"
            />
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
        </>
      ) : (
        /* Always a plain top-aligned list: a virtual "+ New shell" row
           first (gated inside NewTerminalButton on the agent's terminal
           access — leading keeps it at a fixed spot instead of drifting
           down as shells accumulate), then the shell rows. With zero
           shells the virtual row is the whole list — no centered
           empty-state copy. */
        <div
          ref={hostInline ? shellListRef : undefined}
          role={hostInline ? "region" : undefined}
          aria-label={hostInline ? "Shell list" : undefined}
          tabIndex={hostInline ? -1 : undefined}
          className="flex min-h-0 flex-1 flex-col overflow-y-auto py-1 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset"
        >
          <NewTerminalButton
            conversationId={conversationId}
            onCreated={(key) => openShell(key, true)}
            variant="row"
          />
          {terminals.map((t) => (
            <button
              key={terminalTabKey(t)}
              type="button"
              className="flex w-full items-center gap-2 px-2 py-1.5 text-left hover:bg-accent/60"
              onClick={() => openShell(terminalTabKey(t), false)}
            >
              <TerminalIcon className="size-3.5 shrink-0 text-muted-foreground" />
              {t.session && <span className="shrink-0 text-xs font-medium">{t.session}</span>}
              <span className="truncate text-xs text-muted-foreground/70">{t.name}</span>
              <span className="flex-1" />
              <TerminalStatusBadge status={getStatus(t)} />
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
