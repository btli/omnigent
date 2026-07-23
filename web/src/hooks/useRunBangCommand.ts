import { useCallback, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { authenticatedFetch } from "../lib/identity";
import type { BangAction } from "@/lib/composerBang";
import { useChatStore } from "@/store/chatStore";
import { useFocusShell } from "@/shell/ShellFocusContext";
import {
  createTerminal,
  reconcileTerminal,
  terminalInfoFromResource,
  terminalsQueryKey,
  type TerminalInfo,
} from "@/hooks/useTerminals";
import {
  claimShellAttempt,
  clearShellAttempt,
  peekShellAttempt,
  ShellResponseLossError,
} from "@/lib/shellAttempt";

interface ShellCommandResponse {
  item_id?: string;
  item?: Record<string, unknown>;
  terminal?: Record<string, unknown> | null;
  sequence?: number;
}

export interface BangRunResult {
  status: "ok" | "unknown";
}

function actionFingerprint(action: Exclude<BangAction, { kind: "error" | "needsTypes" }>): string {
  switch (action.kind) {
    case "new":
      return JSON.stringify(["new", action.type, action.command]);
    case "send":
      return JSON.stringify(["send", action.terminalId, action.command]);
    case "focus":
      return JSON.stringify(["focus", action.terminalId]);
  }
}

async function postShellCommand(
  conversationId: string,
  data: Record<string, unknown>,
): Promise<ShellCommandResponse> {
  let response: Response;
  try {
    response = await authenticatedFetch(
      `/v1/sessions/${encodeURIComponent(conversationId)}/events`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ type: "shell_command", data }),
      },
    );
  } catch (error) {
    throw new ShellResponseLossError(error);
  }
  if (!response.ok) {
    let message = `shell command failed: ${response.status} ${response.statusText}`;
    try {
      const body = (await response.json()) as { error?: { message?: string } };
      if (body.error?.message) message = body.error.message;
    } catch {
      // The HTTP status still proves a definite server rejection.
    }
    throw new Error(message);
  }
  try {
    const body = (await response.json()) as unknown;
    if (!body || typeof body !== "object" || Array.isArray(body)) {
      throw new Error("shell command returned an unrecognized response shape");
    }
    return body as ShellCommandResponse;
  } catch (error) {
    throw new ShellResponseLossError(error);
  }
}

export interface RunBangCommand {
  run: (action: BangAction, originalText: string) => Promise<BangRunResult>;
  /**
   * Discard the retained response-loss attempt. When `currentText` is given
   * and still equals the retained attempt's `originalText`, the attempt is
   * kept so an identical resubmit reuses its id (server-side idempotency);
   * clearing happens only on a real divergence.
   */
  clearAttempt: (currentText?: string) => void;
  pending: boolean;
}

interface InFlight {
  fingerprint: string;
  originalText: string;
  promise: Promise<BangRunResult>;
}

export function useRunBangCommand(conversationId: string | null): RunBangCommand {
  const queryClient = useQueryClient();
  const focusShell = useFocusShell();
  // In-flight tracking is scoped per conversation: the composer doesn't
  // remount on a session switch, so a request left running in one session
  // must not block (or show `pending` on) another's composer. The state set
  // drives re-render; the ref holds the promises for the single-flight mutex.
  const [pendingConversations, setPendingConversations] = useState<ReadonlySet<string>>(
    () => new Set(),
  );
  const inFlightRef = useRef<Map<string, InFlight>>(new Map());
  const pending = conversationId !== null && pendingConversations.has(conversationId);

  const clearAttempt = useCallback(
    (currentText?: string) => {
      if (conversationId === null) return;
      if (currentText !== undefined) {
        const retained = peekShellAttempt("bang", conversationId);
        // Keep the attempt while the draft still reads as the exact command
        // that produced it, so an identical resubmit reuses its id.
        if (retained !== null && retained.originalText === currentText) return;
      }
      clearShellAttempt("bang", conversationId);
    },
    [conversationId],
  );

  const run = useCallback(
    (action: BangAction, originalText: string): Promise<BangRunResult> => {
      if (action.kind === "error") {
        clearAttempt();
        return Promise.reject(new Error(action.reason));
      }
      if (action.kind === "needsTypes") {
        clearAttempt();
        return Promise.reject(new Error("shell types are not available"));
      }
      if (conversationId === null) return Promise.reject(new Error("no active session"));

      const fingerprint = actionFingerprint(action);
      const active = inFlightRef.current.get(conversationId);
      if (active !== undefined) {
        if (active.fingerprint === fingerprint && active.originalText === originalText) {
          return active.promise;
        }
        return Promise.reject(new Error("another shell command is already in flight"));
      }

      const focusIfActive = (terminalId: string, knownTerminals?: TerminalInfo[]) => {
        if (useChatStore.getState().conversationId !== conversationId) return;
        if (
          knownTerminals !== undefined &&
          !knownTerminals.some((item) => item.id === terminalId)
        ) {
          return;
        }
        focusShell?.(`terminal:${terminalId}`);
      };

      const reconcileCreatedAndFocus = (info: TerminalInfo, sequence: number | undefined) => {
        const terminals = reconcileTerminal(queryClient, conversationId, {
          kind: "created",
          info,
          sequence,
        });
        focusIfActive(info.id, terminals);
      };

      if (action.kind === "focus") {
        clearAttempt();
        focusIfActive(action.terminalId);
        return Promise.resolve({ status: "ok" });
      }

      const attempt = claimShellAttempt("bang", conversationId, originalText, fingerprint);
      const execute = async (): Promise<BangRunResult> => {
        try {
          if (action.kind === "new" && action.command === null) {
            const result = await createTerminal(conversationId, action.type, attempt.attemptId);
            reconcileCreatedAndFocus(result.terminal, result.sequence);
            clearShellAttempt("bang", conversationId, attempt.attemptId);
            return { status: "ok" };
          }

          const response =
            action.kind === "new"
              ? await postShellCommand(conversationId, {
                  action: "spawn",
                  terminal: action.type,
                  command: action.command,
                  attempt_id: attempt.attemptId,
                })
              : await postShellCommand(conversationId, {
                  action: "send",
                  terminal_id: action.terminalId,
                  command: action.command,
                  attempt_id: attempt.attemptId,
                });

          if (response.item && typeof response.item === "object") {
            useChatStore.getState().appendConversationItem(conversationId, response.item);
          }
          const unknown = response.item?.status === "unknown";
          if (action.kind === "new") {
            const resource = response.terminal;
            const info =
              resource && typeof resource === "object" ? terminalInfoFromResource(resource) : null;
            if (info !== null) {
              reconcileCreatedAndFocus(info, response.sequence);
            }
          } else {
            const cached = queryClient.getQueryData<TerminalInfo[]>(
              terminalsQueryKey(conversationId),
            );
            focusIfActive(action.terminalId, cached);
          }
          if (!unknown) clearShellAttempt("bang", conversationId, attempt.attemptId);
          return { status: unknown ? "unknown" : "ok" };
        } catch (error) {
          if (!(error instanceof ShellResponseLossError)) {
            clearShellAttempt("bang", conversationId, attempt.attemptId);
          }
          throw error;
        }
      };

      // The closures below capture THIS conversationId, so a late settle
      // reconciles and clears its own session's slot even after the user has
      // switched away.
      const markPending = (isPending: boolean) =>
        setPendingConversations((prev) => {
          if (prev.has(conversationId) === isPending) return prev;
          const next = new Set(prev);
          if (isPending) next.add(conversationId);
          else next.delete(conversationId);
          return next;
        });

      markPending(true);
      const promise = execute().finally(() => {
        if (inFlightRef.current.get(conversationId)?.promise === promise) {
          inFlightRef.current.delete(conversationId);
          markPending(false);
        }
      });
      inFlightRef.current.set(conversationId, { fingerprint, originalText, promise });
      return promise;
    },
    [clearAttempt, conversationId, focusShell, queryClient],
  );

  return { run, clearAttempt, pending };
}
