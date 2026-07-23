import { AGENT_TERMINAL_IDS, type TerminalInfo } from "@/hooks/useTerminals";

/**
 * Pure ``!``-shell-command grammar for the chat composer, mirroring the
 * framework-free split of ``composerMentions.ts``: the composer intercepts
 * bang input client-side (like slash commands) and routes it to the shell
 * subsystem instead of the agent. Kept free of React/state so the grammar
 * table is unit-testable without rendering.
 *
 * Grammar of the first whitespace-delimited token (value must START with
 * ``!`` — untrimmed, so a leading space defeats interception):
 *
 * - ``! <cmd>``        → new shell of the default declared type, run ``<cmd>``
 * - ``!`` alone        → new default-type shell, no command
 * - ``!<target> <cmd>``→ running shell → send ``<cmd>``; declared type → new shell
 * - ``!<target>`` alone→ running shell → focus; declared type → create + focus
 * - ``\!...``          → escape: normal chat message with the ``\`` stripped
 */

/** Live data a bang token resolves against at submit time. */
export interface BangContext {
  /**
   * The session's user-shell inventory (``inventoryTerminals(...)``).
   * Agent panes are filtered out again here defensively — they are never
   * targetable regardless of what the caller passes.
   */
  shells: TerminalInfo[];
  /** Declared terminal type names from the agent spec, default type first. */
  declaredTypes: string[];
}

/** What a submitted bang input resolves to. */
export type BangAction =
  | {
      /** Spawn a new shell of ``type``; run ``command`` in it when non-null. */
      kind: "new";
      type: string;
      command: string | null;
    }
  | {
      /** Send ``command`` into the running shell ``terminalId``. */
      kind: "send";
      terminalId: string;
      command: string;
    }
  | {
      /** No command — just focus the running shell ``terminalId`` in the rail. */
      kind: "focus";
      terminalId: string;
    }
  | {
      /** Resolution needs the agent's declared shell types to finish loading. */
      kind: "needsTypes";
      /** Candidate type token, empty for the default type. */
      target: string;
      /** Command suffix preserved verbatim while resolution is deferred. */
      command: string | null;
    }
  | {
      /** Unresolvable input; ``reason`` feeds the inline composer error bar. */
      kind: "error";
      reason: string;
    };

/**
 * The single interception guard (mirrors ``isSlashCommandText``): true when
 * the untrimmed composer value starts with ``!``. Callers check
 * :func:`stripBangEscape` first so ``\!`` text is never intercepted.
 */
export function isBangCommandText(text: string): boolean {
  return text.startsWith("!");
}

/**
 * ``\!`` escape hatch: returns the message to send as a normal chat message
 * (the leading ``\`` stripped, so the agent sees literal ``!...``), or
 * ``null`` when the text is not an escaped bang.
 */
export function stripBangEscape(text: string): string | null {
  return text.startsWith("\\!") ? text.slice(1) : null;
}

export const NO_SHELL_ACCESS = "this agent has no shell access";
export const SHELL_COMMANDS_NO_ATTACHMENTS = "Shell commands can't carry attachments";

/** Remove agent-owned panes, which are never valid bang-command targets. */
export function targetableShells(shells: TerminalInfo[]): TerminalInfo[] {
  return shells.filter((shell) => !AGENT_TERMINAL_IDS.has(shell.id));
}

/** Preferred address token for a shell menu row. */
export function shellBangToken(shell: TerminalInfo): string {
  return shell.session !== "" ? shell.session : shell.id;
}

/**
 * Resolve a submitted bang input against live session data.
 *
 * The first whitespace-delimited token after ``!`` is the target; everything
 * after one separating whitespace character is the command, taken verbatim
 * (quoting, ``$VARS``, pipes, embedded newlines, and even bare whitespace are
 * the target shell's business — never interpreted or trimmed here). Only a
 * truly empty remainder (nothing after the token) counts as "no command".
 *
 * Target resolution is exact-token match only, first match wins:
 * running shell by session key → running shell by full resource id →
 * declared shell type → error. Running-key-first means targeting existing
 * state (the riskier intent) can never be shadowed by a type name.
 */
export function parseBangCommand(text: string, ctx: BangContext): BangAction {
  if (!isBangCommandText(text)) {
    return { kind: "error", reason: "not a shell command" };
  }
  const rest = text.slice(1);
  const sep = rest.search(/\s/);
  const target = sep === -1 ? rest : rest.slice(0, sep);
  // Strip exactly one separator character; the remainder is verbatim —
  // whitespace-only remainders are real command content, not "no command".
  const rawCommand = sep === -1 ? null : rest.slice(sep + 1);
  const command = rawCommand !== null && rawCommand.length > 0 ? rawCommand : null;

  // Agent panes are plumbing, not inventory — never targetable.
  const shells = targetableShells(ctx.shells);

  if (target === "") {
    const defaultType = ctx.declaredTypes[0];
    if (defaultType === undefined) {
      return { kind: "needsTypes", target, command };
    }
    return { kind: "new", type: defaultType, command };
  }

  const resolved =
    shells.find((s) => s.session !== "" && s.session === target) ??
    shells.find((s) => s.id === target);
  if (resolved !== undefined) {
    if (!resolved.running) {
      return { kind: "error", reason: `shell ${target} is not running` };
    }
    return command !== null
      ? { kind: "send", terminalId: resolved.id, command }
      : { kind: "focus", terminalId: resolved.id };
  }

  if (ctx.declaredTypes.includes(target)) {
    return { kind: "new", type: target, command };
  }

  if (ctx.declaredTypes.length === 0) {
    return { kind: "needsTypes", target, command };
  }
  return { kind: "error", reason: `No shell \`${target}\` — press ! to see running shells` };
}
