// TypeScript types mirroring the server's `ConversationItem` discriminated
// union, plus fetch helpers for cursor-paginated history loading.
//
// The server flattens the union — each item carries its type-
// specific fields directly alongside the common ones (`id`, `type`,
// `response_id`, `status`), with no nested `{type, data}` wrapper.
//
// Source of truth for the shape: `omnigent/entities/conversation.py`
// + `omnigent/server/routes/conversations.py:54-67` (`to_api_dict`).
//
// We model only the fields the renderer needs. Unknown future types are
// passed through silently as `BaseItem & Record<string, unknown>` so the
// translator can skip them without crashing.

import type { MessageContentBlock } from "./blocks";

export interface BaseItem {
  id: string;
  type: string;
  response_id: string;
  status: string;
}

export interface MessageItem extends BaseItem {
  type: "message";
  role: "user" | "assistant";
  content: MessageContentBlock[];
  /** Agent name; assistant-only. Server alias for `agent`. */
  model?: string;
  /** Human author email; omitted for agent/tool/system items. */
  created_by?: string;
  /** Hidden durable context such as injected skill instructions. */
  is_meta?: boolean;
  /** Assistant-only marker for durable partial text from an interrupted turn. */
  interrupted?: boolean;
}

export interface FunctionCallItem extends BaseItem {
  type: "function_call";
  name: string;
  /** JSON string per OpenAI's Responses API spec; parse before use. */
  arguments: string;
  call_id: string;
  model?: string;
}

export interface FunctionCallOutputItem extends BaseItem {
  type: "function_call_output";
  call_id: string;
  output: string;
}

/**
 * Persisted error banner. Mirrors `response.error` so historical
 * hydration can render the same destructive banner as the live stream.
 */
export interface ErrorItem extends BaseItem {
  type: "error";
  source: string;
  code: string;
  message: string;
}

export interface ReasoningItem extends BaseItem {
  type: "reasoning";
  model: string;
  summary: Array<{ type: string; text: string }>;
  content?: Array<{ type: string; text: string }>;
}

/** The provider-native tool item types the runtime persists today. */
export const NATIVE_TOOL_ITEM_TYPES = new Set<string>([
  "web_search_call",
  "file_search_call",
  "code_interpreter_call",
  "computer_call",
  "image_generation_call",
  "mcp_call",
  "mcp_list_tools",
]);

/**
 * Native tool items carry provider-specific fields directly on the item
 * (no nested `data` slot — the whole item IS the data). The translator
 * forwards the whole record into `NativeToolBlock.data`.
 */
export type NativeToolItem = BaseItem & {
  type:
    | "web_search_call"
    | "file_search_call"
    | "code_interpreter_call"
    | "computer_call"
    | "image_generation_call"
    | "mcp_call"
    | "mcp_list_tools";
} & Record<string, unknown>;

export interface CompactionItem extends BaseItem {
  type: "compaction";
  summary?: string;
  last_item_id?: string;
  model?: string;
  token_count?: number;
}

/**
 * A Claude Code slash-command invocation from the embedded TUI's
 * JSONL transcript. Lives in NON_CONTENT_ITEM_TYPES server-side so
 * downstream LLMs don't see a phantom tool call.
 */
export interface SlashCommandItem extends BaseItem {
  type: "slash_command";
  /**
   * `"skill"` for plugin/Skill invocations, `"command"` for surfaced
   * CLI built-ins. Absent on items persisted before the field was
   * added — translator coerces missing/unknown values to `"skill"`.
   */
  kind?: "skill" | "command";
  /** Command name with leading `/` stripped, e.g. `dev-productivity:simplify`. */
  name: string;
  /** Raw `<command-args>` text; empty when invoked with no args. */
  arguments: string;
  /** `<local-command-stdout>` text; absent when no stdout (server strips via exclude_none). */
  output?: string;
  /** Harness/agent name — server alias for the `agent` field. */
  model?: string;
}

/**
 * A runner-side terminal command (`!cmd`). Two producers: the Claude Code
 * embedded-TUI observer (legacy — one `kind="input"` item plus one
 * `kind="output"` item per invocation, no `action`), and the web composer's
 * bang-command receipts (always `kind="input"`, with `action` and the
 * target-shell fields set; output stays in the live terminal).
 */
export interface TerminalCommandItem extends BaseItem {
  type: "terminal_command";
  /** `"input"` for the command text; `"output"` for stdout/stderr. */
  kind: "input" | "output";
  /** The raw command string; present when `kind="input"`. */
  input?: string;
  /** Captured stdout; present when `kind="output"`. */
  stdout?: string;
  /** Captured stderr; present when `kind="output"`. */
  stderr?: string;
  /** `"spawn"` = new shell + run; `"send"` = into an existing shell. Absent on legacy TUI-observer items. */
  action?: "spawn" | "send";
  /** Target shell resource id, e.g. `terminal_zsh_u-ab12cd`. */
  terminal_id?: string;
  /** Shell type for display, e.g. `zsh`. */
  terminal_name?: string;
  /** Display session key, e.g. `u-ab12cd`. */
  session_key?: string;
  /** Human-readable failure when the delivery outcome is `"error"`. */
  error?: string;
  /** Human author email; present on web-originated receipts. */
  created_by?: string;
}

/**
 * Delivery outcome of a bang-command receipt (not the command's exit
 * code). The server flattens the data payload over the item envelope, so
 * a receipt's outcome arrives in the item-level `status` slot — `"ok"`,
 * `"error"`, or `"unknown"` on receipts, and a lifecycle status
 * (`"completed"`) on legacy TUI-observer items.
 *
 * `action` is the receipt discriminator (absent = legacy), so a status is
 * surfaced ONLY when a valid `action` accompanies it — a legacy item that
 * somehow carries a receipt status must keep rendering exactly as legacy.
 * Single source of the rule for both the SSE and reload chokepoints.
 */
export function terminalCommandStatus(
  action: unknown,
  status: unknown,
): "ok" | "error" | "unknown" | undefined {
  if (action !== "spawn" && action !== "send") return undefined;
  return status === "ok" || status === "error" || status === "unknown" ? status : undefined;
}

/**
 * The optional bang-receipt fields shared by terminal-command items,
 * events, and blocks. Authorship (`createdBy`) is deliberately excluded —
 * it rides the block context, not the receipt payload.
 */
export interface TerminalReceiptFields {
  action?: "spawn" | "send";
  terminalId?: string;
  terminalName?: string;
  sessionKey?: string;
  status?: "ok" | "error" | "unknown";
  error?: string;
}

/**
 * Copy the optional receipt fields off a wire/snake-cased source (raw item
 * or persisted `TerminalCommandItem`), gating `status` through
 * `terminalCommandStatus`. Absent fields stay omitted (not set to
 * `undefined`) so legacy TUI-observer items hydrate byte-identically.
 */
export function receiptFieldsFromWire(rec: {
  action?: unknown;
  terminal_id?: unknown;
  terminal_name?: unknown;
  session_key?: unknown;
  status?: unknown;
  error?: unknown;
}): TerminalReceiptFields {
  const status = terminalCommandStatus(rec.action, rec.status);
  return {
    ...(rec.action === "spawn" || rec.action === "send" ? { action: rec.action } : {}),
    ...(typeof rec.terminal_id === "string" ? { terminalId: rec.terminal_id } : {}),
    ...(typeof rec.terminal_name === "string" ? { terminalName: rec.terminal_name } : {}),
    ...(typeof rec.session_key === "string" ? { sessionKey: rec.session_key } : {}),
    ...(status !== undefined ? { status } : {}),
    ...(typeof rec.error === "string" ? { error: rec.error } : {}),
  };
}

/**
 * Copy the optional receipt fields off an already-camel source (event or
 * block). Absent fields stay omitted so legacy items stay byte-identical.
 */
export function receiptFieldsFromCamel(src: TerminalReceiptFields): TerminalReceiptFields {
  return {
    ...(src.action !== undefined ? { action: src.action } : {}),
    ...(src.terminalId !== undefined ? { terminalId: src.terminalId } : {}),
    ...(src.terminalName !== undefined ? { terminalName: src.terminalName } : {}),
    ...(src.sessionKey !== undefined ? { sessionKey: src.sessionKey } : {}),
    ...(src.status !== undefined ? { status: src.status } : {}),
    ...(src.error !== undefined ? { error: src.error } : {}),
  };
}

/**
 * An intelligent-model-router decision item. Display-only (server-side
 * NON_CONTENT_ITEM_TYPES), so the model never sees it; the web UI renders
 * it as a muted chip at its transcript position.
 */
export interface RoutingDecisionItem extends BaseItem {
  type: "routing_decision";
  /** Model id the router chose, e.g. `databricks-claude-opus-4-8`. */
  model: string;
  /** `true` when the brain ran on `model`; `false` = "would have picked". */
  applied: boolean;
  /** The router's one-line rationale. */
  rationale: string;
  /** Sub-agent name when mirrored into the parent session; undefined otherwise. */
  agent?: string;
}

export type ConversationItem =
  | MessageItem
  | FunctionCallItem
  | FunctionCallOutputItem
  | ErrorItem
  | ReasoningItem
  | NativeToolItem
  | CompactionItem
  | SlashCommandItem
  | RoutingDecisionItem
  | TerminalCommandItem
  | (BaseItem & Record<string, unknown>);

export function isMessageItem(item: ConversationItem): item is MessageItem {
  return item.type === "message";
}

export function isFunctionCallItem(item: ConversationItem): item is FunctionCallItem {
  return item.type === "function_call";
}

export function isFunctionCallOutputItem(item: ConversationItem): item is FunctionCallOutputItem {
  return item.type === "function_call_output";
}

export function isErrorItem(item: ConversationItem): item is ErrorItem {
  return item.type === "error";
}

export function isReasoningItem(item: ConversationItem): item is ReasoningItem {
  return item.type === "reasoning";
}

export function isNativeToolItem(item: ConversationItem): item is NativeToolItem {
  return NATIVE_TOOL_ITEM_TYPES.has(item.type);
}

export function isCompactionItem(item: ConversationItem): item is CompactionItem {
  return item.type === "compaction";
}

export function isSlashCommandItem(item: ConversationItem): item is SlashCommandItem {
  return item.type === "slash_command";
}

export function isRoutingDecisionItem(item: ConversationItem): item is RoutingDecisionItem {
  return item.type === "routing_decision";
}

export function isTerminalCommandItem(item: ConversationItem): item is TerminalCommandItem {
  return item.type === "terminal_command";
}

// Cursor-paginated history fetching lives in `sessionsApi.ts`
// (`fetchSessionItemsPage`) so it shares the authenticated fetch
// wrapper and the windowed-load contract with the initial bind.
