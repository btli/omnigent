// Compact card for runner-side terminal commands (!cmd). Two variants:
//
//   input  — the command line. Legacy TUI-observer items show a terminal
//            prompt icon and the raw command text; composer bang receipts
//            (`action` set) add a target-shell chip, error styling when
//            delivery failed, and an optional click-to-focus affordance
//            for the shell's rail tab.
//   output — the combined stdout/stderr result (TUI observer only).
//            Collapsible so long output doesn't dominate the view.

import { ChevronRightIcon, SquareTerminalIcon } from "lucide-react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ReceiptShellState } from "@/shell/terminalStatus";

interface TerminalCommandCardProps {
  kind: "input" | "output";
  input: string | null;
  stdout: string | null;
  stderr: string | null;
  /** `"spawn"` = new shell + run; `"send"` = into an existing shell. Absent on legacy items. */
  action?: "spawn" | "send";
  /** Target shell resource id, e.g. `terminal_zsh_u-ab12cd`. */
  terminalId?: string;
  /** Shell type for display, e.g. `zsh`. */
  terminalName?: string;
  /** Display session key, e.g. `u-ab12cd`. */
  sessionKey?: string;
  /** Delivery outcome (not the command's exit code). */
  status?: "ok" | "error" | "unknown";
  /** Human-readable failure when `status="error"`. */
  error?: string;
  /** Human author (email) of a web-originated receipt — shown as a hover
   *  title so authorship reads in multi-user sessions. */
  createdBy?: string;
  /** Target shell state from the cache. Absent keeps the pre-state
   *  rendering so legacy snapshots hold; `"gone"` is non-actionable. */
  shellState?: ReceiptShellState;
  /** Focuses the target shell's rail tab; the chip is a button when set. */
  onFocusShell?: (terminalId: string) => void;
}

export function TerminalCommandCard({
  kind,
  input,
  stdout,
  stderr,
  action,
  terminalId,
  terminalName,
  sessionKey,
  status,
  error,
  createdBy,
  shellState,
  onFocusShell,
}: TerminalCommandCardProps) {
  if (kind === "input") {
    // `action` is the receipt discriminator: without it the item is a
    // legacy TUI-observer record and MUST render byte-identically to the
    // pre-receipt card — so status/error are ignored entirely, even if a
    // stray value slips past the plumbing's shared gate.
    const isReceipt = action !== undefined;
    const isError = isReceipt && status === "error";
    const isUnknown = isReceipt && status === "unknown";
    // `→ zsh · u-ab12cd` (send) / `→ new zsh · u-ab12cd` (spawn); the
    // resource id is the fallback when display fields are missing.
    const shellLabel =
      [terminalName, sessionKey].filter(Boolean).join(" · ") || terminalId || "shell";
    const chipText = `→ ${action === "spawn" ? "new " : ""}${shellLabel}`;
    const chipClass =
      "shrink-0 rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground";
    // A closed/gone shell earns a subtle "closed" hint on the chip; a gone
    // shell is also non-actionable, so it is never a button even with a
    // handler. Absent `shellState` keeps the pre-state rendering intact.
    const isClosedShell = shellState === "closed" || shellState === "gone";
    const canFocusShell = Boolean(onFocusShell) && Boolean(terminalId) && shellState !== "gone";
    const chipChildren = isClosedShell ? (
      <>
        {chipText}
        <span className="ml-1 text-muted-foreground/60">closed</span>
      </>
    ) : (
      chipText
    );
    return (
      <div
        className={
          isError
            ? "group not-prose w-full rounded-md border border-destructive/50 px-2 py-0.5"
            : isUnknown
              ? "group not-prose w-full rounded-md border border-border/70 bg-muted/20 px-2 py-0.5"
              : "group not-prose w-full"
        }
        data-testid="terminal-command-card"
        data-terminal-kind="input"
        data-terminal-action={action}
        data-terminal-status={isReceipt ? status : undefined}
        title={isReceipt && createdBy ? `Run by ${createdBy}` : undefined}
      >
        <span className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs">
          <SquareTerminalIcon
            className={
              isError
                ? "size-3 shrink-0 text-destructive"
                : isUnknown
                  ? "size-3 shrink-0 text-amber-500 dark:text-amber-400"
                  : "size-3 shrink-0 text-emerald-500 dark:text-emerald-400"
            }
          />
          <span className="min-w-0 flex-1 truncate font-mono">
            <span className="font-semibold text-foreground">$</span> {input ?? ""}
          </span>
          {action !== undefined &&
            (canFocusShell ? (
              <button
                type="button"
                data-testid="terminal-command-chip"
                data-shell-state={shellState}
                className={`${chipClass} cursor-pointer transition-colors hover:text-foreground`}
                onClick={() => onFocusShell?.(terminalId as string)}
              >
                {chipChildren}
              </button>
            ) : (
              <span
                data-testid="terminal-command-chip"
                data-shell-state={shellState}
                aria-disabled={shellState === "gone" ? true : undefined}
                className={chipClass}
              >
                {chipChildren}
              </span>
            ))}
        </span>
        {isError && typeof error === "string" && error.length > 0 && (
          <div
            data-testid="terminal-command-error"
            className="pl-[1.125rem] text-destructive/90 text-xs"
          >
            {error}
          </div>
        )}
        {isUnknown && (
          <div
            data-testid="terminal-command-unknown"
            className="pl-[1.125rem] text-muted-foreground text-xs"
          >
            Delivery unknown
          </div>
        )}
      </div>
    );
  }

  const hasStdout = typeof stdout === "string" && stdout.length > 0;
  const hasStderr = typeof stderr === "string" && stderr.length > 0;
  const hasOutput = hasStdout || hasStderr;

  const trigger = (
    <span
      className="flex w-full items-center gap-1.5 py-0.5 text-left text-muted-foreground text-xs transition-colors hover:text-foreground"
      data-testid="terminal-command-card"
      data-terminal-kind="output"
    >
      <SquareTerminalIcon className="size-3 shrink-0 text-emerald-500 dark:text-emerald-400" />
      <span className="min-w-0 flex-1 truncate font-mono text-muted-foreground/80">
        {hasOutput ? "output" : "(no output)"}
      </span>
      {hasOutput && (
        <ChevronRightIcon className="size-3 shrink-0 transition-transform group-data-[state=open]:rotate-90" />
      )}
    </span>
  );

  if (!hasOutput) {
    return <div className="group not-prose w-full">{trigger}</div>;
  }

  return (
    <Collapsible defaultOpen={false} className="group not-prose w-full">
      <CollapsibleTrigger className="w-full cursor-pointer">{trigger}</CollapsibleTrigger>
      <CollapsibleContent className="mt-1 ml-2 space-y-2 border-l pl-3 py-1 data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
        {hasStdout && (
          <CodeBlock code={stdout as string} language="bash">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">stdout</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        )}
        {hasStderr && (
          <CodeBlock code={stderr as string} language="bash">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">stderr</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}
