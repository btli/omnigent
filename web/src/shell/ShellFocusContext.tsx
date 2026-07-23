// Cross-tree "focus this shell in the rail" channel. AppShell owns the
// terminal surface state (`panelInitialKey` behind `openTerminalsPanel`);
// chat-column consumers (the composer's `!` commands, receipt-card chips)
// live under the router Outlet where prop drilling can't reach, so the
// opener is surfaced through context instead — mirroring FileViewerContext.

import { createContext, useContext } from "react";

/** Focuses a terminal tab key (e.g. `"terminal:terminal_zsh_u-ab12cd"`). */
export type FocusShellFn = (terminalKey: string) => void;

const ShellFocusContext = createContext<FocusShellFn | null>(null);

export const ShellFocusProvider = ShellFocusContext.Provider;

/**
 * The rail shell-focus function, or `null` outside an AppShell tree
 * (tests, storybook-style renders) — callers no-op on `null`.
 */
export function useFocusShell(): FocusShellFn | null {
  return useContext(ShellFocusContext);
}
