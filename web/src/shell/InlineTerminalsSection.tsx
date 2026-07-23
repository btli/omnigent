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
//
// Inline hosting is CONTROLLED: the active shell (and the open-tab set) is
// owned by AppShell — mirroring how Files lift `selectedFilePath`/`openFiles`
// — so the shell's identity can surface as a tab in the shared top header
// strip (see WorkspacePanel's ShellTabsStrip) beside the open file tabs.
// `activeKey`/`onOpenShell`/`onReturnToList` wire that up; when omitted, the
// section stays list-only (the mobile drawer path).

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
  /**
   * AppShell's single "hosts shells inline, not center" verdict, passed
   * down so this section never recomputes it from a possibly-stale label
   * source. When omitted (mobile drawer / unit tests) it falls back to the
   * context-derived ``!isNativeWrapper``. Combined with ``inline`` it
   * decides whether a row hosts in-rail or hands off to ``onExpand``.
   */
  hostsShellsInline?: boolean;
  /**
   * Whether a successful label source has settled the session's shape
   * (native wrapper vs inline-hosting). While false the routing verdict is
   * not yet authoritative, so shell-create is parked/disabled — starting a
   * shell against a mislabeled default could capture the wrong host and
   * leave a highlighted tab that renders no terminal. Default true (the
   * self-hosting paths that don't supply it aren't shape-sensitive).
   */
  sessionLabelsReady?: boolean;
  /**
   * Active shell tab key when inline hosting is CONTROLLED by the parent
   * (desktop rail). Mirrors the Files tab's `selectedFilePath`: null shows
   * the shell list, a key hosts that shell. Providing `onOpenShell` opts
   * into controlled mode; omit both to keep the section list-only /
   * self-hosting (mobile drawer + unit tests).
   */
  activeKey?: string | null;
  /**
   * Select a shell (controlled mode) — the parent records the open tab and
   * sets it active. The create->inventory gap marker stays internal to this
   * component (see ``pendingCreatedKeyRef``), so this exposes only the key.
   * ``source`` is the conversation the selection originated in; the parent
   * drops the mutation when navigation has since moved to another session
   * (a pending create in A must not rewrite B's workspace). ``pendingInventory``
   * is true on the create path (the shell isn't in the inventory yet) so the
   * parent can bridge the create→inventory gap for its off-surface cleanup —
   * this component owns the same bridge only while it stays mounted.
   */
  onOpenShell?: (key: string, source: string, pendingInventory: boolean) => void;
  /**
   * Deselect the active shell back to the list (controlled mode).
   * `unexpected` is true when the shell vanished on its own (closed out
   * from under the rail) rather than via the back affordance.
   */
  onReturnToList?: (unexpected: boolean) => void;
}

export function InlineTerminalsSection({
  conversationId,
  onExpand,
  inline = false,
  readOnly = false,
  hostsShellsInline,
  sessionLabelsReady = true,
  activeKey: controlledActiveKey,
  onOpenShell,
  onReturnToList,
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

  // Host the shell inside the rail for every session EXCEPT native-CLI
  // wrappers. The Shells list is already the user-shell inventory: the
  // embedded REPL / native vendor pane is excluded by
  // `inventoryTerminals`, so every row here is a user-created shell.
  // Chat-first SDK sessions (polly/debby) are terminal-first only because
  // the runner hosts an embedded REPL — their user shells belong inline
  // beside the chat, so we gate on `isNativeWrapper`, never
  // `isTerminalFirst`. Native wrappers keep routing to `onExpand`, which
  // opens the shell full-screen in the main column via MainTerminalView —
  // their established, chat-replacing UX.
  // Prefer AppShell's single hostsShellsInline verdict when supplied (the
  // controlled desktop rail) so this section can't recompute a DIFFERENT
  // answer from a label source that resolved on a different tick. Fall back to
  // the context-derived ``!isNativeWrapper`` only for the self-hosting paths
  // (mobile drawer / unit tests) that don't lift the verdict.
  const isNativeWrapper = terminalFirstCtx?.isNativeWrapper ?? false;
  const hostsInlineVerdict = hostsShellsInline ?? !isNativeWrapper;
  const hostInline = inline && hostsInlineVerdict;

  // Inline hosting: which shell is shown in the rail. Null shows the list;
  // a closed/disappeared shell falls back to the list below. CONTROLLED
  // when the parent supplies `onOpenShell` (desktop rail lifts the active
  // shell so it can render as a top-strip tab, mirroring Files); otherwise
  // self-hosted via local state (list-only paths + unit tests).
  const controlled = onOpenShell !== undefined;
  const [localActiveKey, setLocalActiveKey] = useState<string | null>(null);
  const activeKey = controlled ? (controlledActiveKey ?? null) : localActiveKey;
  const [announcement, setAnnouncement] = useState("");
  const shellListRef = useRef<HTMLDivElement>(null);
  const shouldFocusListRef = useRef(false);
  const pendingCreatedKeyRef = useRef<string | null>(null);
  const activeTerminal = terminals.find((t) => terminalTabKey(t) === activeKey) ?? null;
  // Latest routing inputs, so a create callback that fires AFTER an async
  // POST resolves re-checks the CURRENT verdict rather than the one captured
  // when creation started. Between "New shell" click and POST completion the
  // session labels can resolve (native wrapper revealed), flipping the
  // routing target — reading the ref keeps a native session from capturing an
  // inline-owned shell tab that renders no terminal.
  const hostInlineRef = useRef(hostInline);
  hostInlineRef.current = hostInline;
  const conversationIdRef = useRef(conversationId);
  conversationIdRef.current = conversationId;

  const returnToList = useCallback(
    (unexpected: boolean) => {
      shouldFocusListRef.current = true;
      pendingCreatedKeyRef.current = null;
      setAnnouncement(
        unexpected ? "The active shell closed unexpectedly. Focus returned to the shell list." : "",
      );
      if (controlled) {
        onReturnToList?.(unexpected);
      } else {
        setLocalActiveKey(null);
      }
    },
    [controlled, onReturnToList],
  );

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
  // full-screen takeover (mobile / native-CLI wrappers). New-shell creation
  // follows the same split so a freshly-created shell lands in the active
  // surface.
  // `pendingInventory` marks the creation path: a freshly-created shell
  // is selected before the terminals hook exposes it, so record it in
  // pendingCreatedKeyRef to bridge the create->inventory gap (the focus
  // effect above keeps the selection instead of misreading the gap as an
  // unexpected close). Selecting an existing shell clears the ref.
  const openShell = useCallback(
    (key: string, pendingInventory: boolean) => {
      // ``pendingInventory`` is the create path — its callback runs after an
      // async POST, so re-read the LATEST routing verdict (ref, not the value
      // captured when the click happened). A native-wrapper label that
      // resolved mid-flight flips the target: route full-screen via onExpand
      // instead of capturing an inline-owned tab that renders no terminal.
      const source = conversationIdRef.current;
      if (!hostInlineRef.current) {
        onExpand(key);
        return;
      }
      pendingCreatedKeyRef.current = pendingInventory ? key : null;
      setAnnouncement("");
      if (controlled) {
        // Tag the mutation with its source conversation. AppShell rejects the
        // whole UI mutation (file/panel clear, tab select, rail open, active
        // shell, persistence) when navigation has moved on, so a pending
        // create in A can't rewrite B's workspace. ``pendingInventory`` lets
        // AppShell bridge the create→inventory gap for its off-surface cleanup
        // if this section unmounts before the shell surfaces.
        onOpenShell?.(key, source, pendingInventory);
      } else {
        setLocalActiveKey(key);
      }
    },
    [onExpand, controlled, onOpenShell],
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
            // Park shell-create until the routing verdict is authoritative:
            // a shell started against a mislabeled default could complete
            // after native labels arrive and capture the wrong host. Only
            // gates the controlled inline rail; self-hosting paths pass
            // sessionLabelsReady=true (their shape isn't ambiguous here).
            disabled={hostInline && !sessionLabelsReady}
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
