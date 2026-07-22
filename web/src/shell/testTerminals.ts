// Shared test fixture: build a TerminalInfo with sensible defaults. Used by
// the shell suites (AppShell / InlineTerminalsSection / WorkspacePanel) so the
// same shape is constructed one way across tests.
import type { TerminalInfo } from "@/hooks/useTerminals";

export function makeTerminal(
  id: string,
  name: string,
  session: string,
  overrides: Partial<TerminalInfo> = {},
): TerminalInfo {
  return { id, name, session, running: true, ...overrides };
}
