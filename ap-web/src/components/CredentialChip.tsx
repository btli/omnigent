// Composer-footer indicator of which subscription / API-key the active
// session is running on (multi-subscription routing). Renders nothing unless
// a credential pool is configured, so single-account setups see no new chrome.
// The binding is stable for the session's lifetime — failover rebinds the next
// launch, not the running process — so this needs no live-update channel.

import { BadgeCheck, KeyRound } from "lucide-react";

import { HoverCard, HoverCardContent, HoverCardTrigger } from "@/components/ui/hover-card";
import { useChatStore } from "@/store/chatStore";
import type { ActiveCredential } from "@/lib/types";
import { cn } from "@/lib/utils";

type LimitStatus = ActiveCredential["limitStatus"];

/** Dot colour per usage-limit state, using the app's status tokens. */
const LIMIT_DOT: Record<LimitStatus, string> = {
  available: "bg-success",
  limited: "bg-warning",
  unknown: "bg-muted-foreground/55",
};

const LIMIT_TEXT: Record<LimitStatus, string> = {
  available: "Available",
  limited: "Limited",
  unknown: "Usage not yet observed",
};

const FAMILY_LABEL: Record<ActiveCredential["family"], string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
};

const KIND_LABEL: Record<ActiveCredential["kind"], string> = {
  subscription: "subscription",
  api_key: "API key",
};

/** The status-dot Tailwind class for a credential's usage-limit state. */
export function credentialDotClass(limitStatus: LimitStatus): string {
  return LIMIT_DOT[limitStatus];
}

/** Human label for a credential's provider + kind, e.g. "Anthropic subscription". */
export function credentialProviderLabel(credential: ActiveCredential): string {
  return `${FAMILY_LABEL[credential.family]} ${KIND_LABEL[credential.kind]}`;
}

/**
 * The credential chip's presentational form. Exported for focused tests; the
 * composer renders {@link CredentialChip}, which reads the store and hides
 * itself when no account is bound.
 */
export function CredentialChipView({ credential }: { credential: ActiveCredential }) {
  // A key icon distinguishes a tier-fallback API key from a subscription login.
  const Icon = credential.kind === "api_key" ? KeyRound : BadgeCheck;
  const limitStatus = credential.limitStatus;
  return (
    <HoverCard openDelay={150} closeDelay={80}>
      <HoverCardTrigger asChild>
        <span
          data-testid="credential-chip"
          data-kind={credential.kind}
          data-limit-status={limitStatus}
          role="img"
          aria-label={`Running on ${KIND_LABEL[credential.kind]} ${credential.name} (${LIMIT_TEXT[limitStatus]})`}
          className="inline-flex h-7 max-w-[12rem] cursor-default items-center gap-1.5 rounded-md px-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          <Icon className="size-3.5 shrink-0" aria-hidden />
          <span className="truncate tabular-nums">{credential.name}</span>
          <span
            aria-hidden
            className={cn("size-1.5 shrink-0 rounded-full", LIMIT_DOT[limitStatus])}
          />
        </span>
      </HoverCardTrigger>
      <HoverCardContent side="top" align="start" className="w-64 space-y-2 text-xs">
        <div className="flex items-center gap-1.5 font-medium text-foreground">
          <Icon className="size-3.5 shrink-0" aria-hidden />
          <span className="truncate">{credential.name}</span>
        </div>
        <dl className="space-y-1 text-muted-foreground">
          <div className="flex items-center justify-between gap-4">
            <dt>Provider</dt>
            <dd className="text-foreground">
              {FAMILY_LABEL[credential.family]} {KIND_LABEL[credential.kind]}
            </dd>
          </div>
          <div className="flex items-center justify-between gap-4">
            <dt>Usage</dt>
            <dd className="inline-flex items-center gap-1.5 text-foreground">
              <span aria-hidden className={cn("size-1.5 rounded-full", LIMIT_DOT[limitStatus])} />
              {LIMIT_TEXT[limitStatus]}
            </dd>
          </div>
        </dl>
        {limitStatus === "limited" ? (
          <p className="text-[11px] text-warning">
            Limit reached — your next launch will switch to another account.
          </p>
        ) : (
          <p className="text-[11px] text-muted-foreground">
            The account this session is running on.
          </p>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}

/**
 * Composer-footer credential chip. Reads the active session's bound account
 * from the store and renders nothing when none is set (single-account setups).
 */
export function CredentialChip() {
  const credential = useChatStore((s) => s.sessionActiveCredential);
  if (!credential) return null;
  return <CredentialChipView credential={credential} />;
}
