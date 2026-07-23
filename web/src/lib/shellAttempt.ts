export interface ShellAttempt {
  attemptId: string;
  conversationId: string;
  originalText: string;
  fingerprint: string;
  createdAt: number;
}

export class ShellResponseLossError extends Error {
  constructor(cause: unknown) {
    super(cause instanceof Error ? cause.message : String(cause));
    this.name = "ShellResponseLossError";
  }
}

const attempts = new Map<string, ShellAttempt>();
const STORAGE_PREFIX = "omnigent:shell-attempt:";

function attemptKey(scope: string, conversationId: string): string {
  return `${scope}:${conversationId}`;
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function loadAttempt(key: string): ShellAttempt | null {
  const memory = attempts.get(key);
  if (memory !== undefined) return memory;
  try {
    const raw = storage()?.getItem(`${STORAGE_PREFIX}${key}`);
    if (!raw) return null;
    const value = JSON.parse(raw) as Partial<ShellAttempt>;
    if (
      typeof value.attemptId !== "string" ||
      typeof value.conversationId !== "string" ||
      typeof value.originalText !== "string" ||
      typeof value.fingerprint !== "string" ||
      typeof value.createdAt !== "number"
    ) {
      return null;
    }
    const attempt = value as ShellAttempt;
    attempts.set(key, attempt);
    return attempt;
  } catch {
    return null;
  }
}

function saveAttempt(key: string, attempt: ShellAttempt): void {
  attempts.set(key, attempt);
  try {
    storage()?.setItem(`${STORAGE_PREFIX}${key}`, JSON.stringify(attempt));
  } catch {
    // Memory persistence still covers component unmounts in this page.
  }
}

export function claimShellAttempt(
  scope: string,
  conversationId: string,
  originalText: string,
  fingerprint: string,
): ShellAttempt {
  const key = attemptKey(scope, conversationId);
  const existing = loadAttempt(key);
  if (
    existing !== null &&
    existing.originalText === originalText &&
    existing.fingerprint === fingerprint
  ) {
    return existing;
  }
  const attempt: ShellAttempt = {
    attemptId: crypto.randomUUID(),
    conversationId,
    originalText,
    fingerprint,
    createdAt: Date.now(),
  };
  saveAttempt(key, attempt);
  return attempt;
}

/** The retained attempt for this scope, or null. Does not mutate the ledger. */
export function peekShellAttempt(scope: string, conversationId: string): ShellAttempt | null {
  return loadAttempt(attemptKey(scope, conversationId));
}

export function clearShellAttempt(
  scope: string,
  conversationId: string,
  expectedAttemptId?: string,
): void {
  const key = attemptKey(scope, conversationId);
  const current = loadAttempt(key);
  if (expectedAttemptId !== undefined && current?.attemptId !== expectedAttemptId) return;
  attempts.delete(key);
  try {
    storage()?.removeItem(`${STORAGE_PREFIX}${key}`);
  } catch {
    // The in-memory record is already gone.
  }
}

export function resetShellAttemptsForTests(): void {
  for (const key of attempts.keys()) {
    try {
      storage()?.removeItem(`${STORAGE_PREFIX}${key}`);
    } catch {
      // Test cleanup can proceed with the in-memory clear.
    }
  }
  attempts.clear();
}
