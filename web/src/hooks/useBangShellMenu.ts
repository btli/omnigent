import { type KeyboardEvent, type RefObject, useLayoutEffect, useRef, useState } from "react";

import type { TerminalInfo } from "@/hooks/useTerminals";
import { parseBangCommand, shellBangToken, targetableShells } from "@/lib/composerBang";

/**
 * `!`-shell-command autocomplete controller for the chat composer,
 * mirroring the `useMentionBrowser` split: the composer owns the textarea
 * value; this hook owns the open/dismiss state, the section composition
 * (running shells on top, declared "New shell…" types below), the flat
 * keyboard index across both sections, and the consumed-key contract.
 *
 * The menu shows only while the caret is still inside the bang token —
 * the value starts with `!` and contains no whitespace yet (same
 * "no space typed" rule as the slash menu). Escape dismisses without
 * clearing the input; any edit reopens.
 */

/** One row of the bang menu, in flat render/keyboard order. */
export interface BangMenuRow {
  /** Section discriminator: a running user shell, or a declared type. */
  kind: "shell" | "type";
  /**
   * Completion token inserted after `!` — the session key (`u-ab12cd`)
   * for shells, the declared name (`zsh`) for types.
   */
  token: string;
  /** Shell type for display, e.g. `zsh`. */
  name: string;
  /** Shell rows: whether the tmux session is still running. */
  running?: boolean;
  /** Type rows: the spec default (`declared[0]`) on native sessions. */
  isDefault?: boolean;
  /**
   * Whether Tab/Enter/click can complete this row. Exited shells render
   * (dimmed) but never complete — the user learns why an old label
   * stopped working instead of the row silently vanishing.
   */
  completable: boolean;
}

/**
 * Compose + filter the menu rows for a query (the text after `!`).
 *
 * Sections and suppression per the spec: running user shells first
 * (agent panes excluded — they are plumbing, never targetable), then the
 * declared types ONLY when 2+ are declared (bare `!` already covers a
 * single/default type). Rows whose completion token doesn't start with
 * the query are dropped; a fully-filtered section disappears with its
 * header. Exported for direct unit testing.
 *
 * @param shells - The session's user-shell inventory.
 * @param declaredTypes - Declared terminal type names, default first.
 * @param query - Text typed after `!`, e.g. `"u-a"`.
 * @param hasDefaultType - Native sessions declare `$SHELL` first, so the
 *   first type row carries a "(default)" tag.
 */
export function buildBangMenuRows(
  shells: TerminalInfo[],
  declaredTypes: string[],
  query: string,
  hasDefaultType: boolean,
): BangMenuRow[] {
  const rows: BangMenuRow[] = [];
  const targets = targetableShells(shells);
  const keyCounts = new Map<string, number>();
  for (const shell of targets) {
    if (shell.session !== "") {
      keyCounts.set(shell.session, (keyCounts.get(shell.session) ?? 0) + 1);
    }
  }
  for (const shell of targets) {
    const preferred = shellBangToken(shell);
    const ambiguousKey =
      shell.session !== "" &&
      ((keyCounts.get(shell.session) ?? 0) > 1 || declaredTypes.includes(shell.session));
    let token = ambiguousKey ? shell.id : preferred;
    let parsed = parseBangCommand(`!${token}`, { shells: targets, declaredTypes });
    const resolvesToShell = parsed.kind === "focus" && parsed.terminalId === shell.id;
    if (shell.running && !resolvesToShell && token !== shell.id) {
      token = shell.id;
      parsed = parseBangCommand(`!${token}`, { shells: targets, declaredTypes });
    }
    const roundTrips = parsed.kind === "focus" && parsed.terminalId === shell.id;
    if (shell.running && !roundTrips) continue;
    if (!token.startsWith(query)) continue;
    rows.push({
      kind: "shell",
      token,
      name: shell.name,
      running: shell.running,
      completable: shell.running && roundTrips,
    });
  }
  if (declaredTypes.length >= 2) {
    declaredTypes.forEach((name, i) => {
      if (!name.startsWith(query)) return;
      const parsed = parseBangCommand(`!${name}`, { shells: targets, declaredTypes });
      rows.push({
        kind: "type",
        token: name,
        name,
        isDefault: hasDefaultType && i === 0,
        completable: parsed.kind === "new" && parsed.type === name && parsed.command === null,
      });
    });
  }
  return rows;
}

/** Inputs the host composer supplies. */
export interface BangShellMenuParams {
  /** The textarea value (untrimmed) and a setter that flags the draft dirty. */
  text: string;
  setText: (next: string) => void;
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  /** Master gate: owner-only, no attachments, feature wired. */
  enabled: boolean;
  /** The session's user-shell inventory (SSE-live). */
  shells: TerminalInfo[];
  /** Declared terminal type names from the agent spec, default first. */
  declaredTypes: string[];
  /** Native sessions have a meaningful default type (`$SHELL` first). */
  hasDefaultType?: boolean;
  /**
   * Caret offset in `text`. The menu is open only while the caret sits
   * inside the first (`!<token>`) segment, so moving out of it (past a
   * space) closes the menu and moving back in reopens it. Defaults to the
   * end of `text` when unset — matching plain typing, where the caret
   * trails the value.
   */
  caret?: number;
  /** Updates the host's tracked caret after programmatic completion. */
  setCaret?: (next: number) => void;
  /** Monotonic composer mutation revision used to scope dismissals. */
  revision: number;
  /** On mobile, Enter inserts a newline rather than acting on the menu. */
  isMobile?: boolean;
}

export interface BangShellMenu {
  /** Whether the panel should render. */
  open: boolean;
  /** Flat row list across both sections, in render order. */
  rows: BangMenuRow[];
  /** Index of the highlighted row in `rows` (-1 = none). */
  activeIndex: number;
  /** Complete a row's token into the textarea: `!<token> `. */
  complete: (row: BangMenuRow) => void;
  /** Handle a key event for the open menu; returns true when consumed. */
  handleKeyDown: (e: KeyboardEvent<HTMLTextAreaElement>) => boolean;
  /** Dismiss until the next edit (Escape / blur). */
  dismiss: () => void;
}

export function useBangShellMenu(params: BangShellMenuParams): BangShellMenu {
  const {
    text,
    setText,
    textareaRef,
    enabled,
    shells,
    declaredTypes,
    hasDefaultType = false,
    caret,
    setCaret,
    revision,
    isMobile = false,
  } = params;
  // The arrowed-to row, tracked by identity (kind:token) AND a monotonic
  // query epoch (below): the effective highlight is DERIVED each render —
  // the tracked row if it survived the current filter under the SAME epoch,
  // else the first completable row. A pure reorder / exit transition under
  // an unchanged query keeps the selection on its row; ANY query change
  // (including returning to a previously-typed query) invalidates it, so
  // the highlight snaps back to the first row. Deriving (instead of a
  // render-phase index reset) keeps the top-hit preselect working under
  // StrictMode's dev double-render, which discards a render-phase setState.
  const [selection, setSelection] = useState<{ key: string; epoch: number } | null>(null);
  const pendingCaretRef = useRef<number | null>(null);
  // Escape dismisses for the CURRENT text only (input preserved, unlike the
  // slash menu) — any edit changes the text and reopens. Completion does NOT
  // use this: after completing, the caret lands past the token, so the
  // caret-based trigger keeps the menu closed while re-entry reopens it.
  const [dismissedFor, setDismissedFor] = useState<{
    revision: number;
    token: string;
  } | null>(null);

  // Open while the caret is inside the first (`!<token>`) segment: `!`
  // first (the untrimmed value — a leading space defeats interception),
  // and the caret at or before the first whitespace. Once the caret moves
  // past the token into the command, the menu closes; moving back in
  // reopens it.
  const firstSpace = text.search(/\s/);
  const tokenEnd = firstSpace === -1 ? text.length : firstSpace;
  const caretPos = caret ?? text.length;
  const inBangToken = enabled && text.startsWith("!") && caretPos >= 1 && caretPos <= tokenEnd;
  const tokenIdentity = text.slice(0, tokenEnd);
  const query = inBangToken ? text.slice(1, tokenEnd) : "";
  const rows = inBangToken ? buildBangMenuRows(shells, declaredTypes, query, hasDefaultType) : [];
  const dismissed = dismissedFor?.revision === revision && dismissedFor.token === tokenIdentity;
  const open = rows.length > 0 && !dismissed;

  // Monotonic epoch that bumps whenever the token query changes. A selection
  // is stamped with the epoch it was made under; it survives only while the
  // epoch is unchanged (pure reorder/status), and revisiting a
  // previously-typed query yields a FRESH epoch — never rematching a stale
  // selection. The ref mutation is StrictMode-safe: idempotent on the
  // double-render replay (the guard is false the second time).
  const queryEpochRef = useRef<{ query: string; epoch: number }>({ query, epoch: 0 });
  if (queryEpochRef.current.query !== query) {
    queryEpochRef.current = { query, epoch: queryEpochRef.current.epoch + 1 };
  }
  const queryEpoch = queryEpochRef.current.epoch;

  const completableIndices = rows.flatMap((row, i) => (row.completable ? [i] : []));
  const keyOf = (row: BangMenuRow) => `${row.kind}:${row.token}`;
  const trackedIndex =
    selection !== null && selection.epoch === queryEpoch
      ? rows.findIndex((row) => row.completable && keyOf(row) === selection.key)
      : -1;
  const activeIndex = !open ? -1 : trackedIndex >= 0 ? trackedIndex : (completableIndices[0] ?? -1);

  useLayoutEffect(() => {
    const pendingCaret = pendingCaretRef.current;
    if (pendingCaret === null) return;
    pendingCaretRef.current = null;
    const textarea = textareaRef.current;
    textarea?.setSelectionRange(pendingCaret, pendingCaret);
    textarea?.focus();
  }, [text, textareaRef]);

  const complete = (row: BangMenuRow) => {
    if (!row.completable) return;
    const suffix = text.slice(tokenEnd);
    const replacement = `!${row.token}`;
    const next = suffix === "" ? `${replacement} ` : replacement + suffix;
    const nextCaret = suffix === "" ? next.length : replacement.length;
    pendingCaretRef.current = nextCaret;
    setText(next);
    setCaret?.(nextCaret);
    setSelection(null);
  };

  const dismiss = () => {
    if (!open) return;
    setDismissedFor({ revision, token: tokenIdentity });
    setSelection(null);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>): boolean => {
    if (!open) return false;
    // Arrow keys cycle the completable rows only — exited shells are
    // visible context, not selectable targets.
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (completableIndices.length === 0) return true;
      const pos = completableIndices.indexOf(activeIndex);
      const nextPos =
        e.key === "ArrowDown"
          ? (pos + 1) % completableIndices.length
          : pos <= 0
            ? completableIndices.length - 1
            : pos - 1;
      setSelection({ key: keyOf(rows[completableIndices[nextPos]!]!), epoch: queryEpoch });
      return true;
    }
    // `isComposing` Enter commits an IME candidate, not a menu completion —
    // let it fall through so the composed text stays in the draft.
    if (
      e.key === "Tab" ||
      (e.key === "Enter" && !e.shiftKey && !isMobile && !e.nativeEvent.isComposing)
    ) {
      const active = activeIndex >= 0 ? rows[activeIndex] : undefined;
      if (active?.completable) {
        e.preventDefault();
        complete(active);
        return true;
      }
      // Nothing completable — let Enter fall through to submit (bare `!`
      // is a valid create-and-focus action) and Tab to the browser.
      return false;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      dismiss();
      return true;
    }
    return false;
  };

  return { open, rows, activeIndex, complete, handleKeyDown, dismiss };
}
