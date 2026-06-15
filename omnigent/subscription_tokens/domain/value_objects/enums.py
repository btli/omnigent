"""Shared literal enums for the multi-subscription domain.

These are the small closed vocabularies used across the subscription-token value
objects, entities, and adapters. They are defined here — rather than
imported from :mod:`omnigent.onboarding.provider_config` — so the domain
layer stays free of any dependency on configuration parsing.

:data:`Family` and :data:`AccountKind` discriminate *which* credential we
are talking about; :data:`FailoverMode` controls how the system reacts
when one is exhausted; :data:`LimitStatus` and :data:`DetectionSource`
describe an account's observed usage-limit state and how it was learned.
"""

from __future__ import annotations

from typing import Literal, get_args

#: The provider family a credential serves. Mirrors the two families
#: ``provider_config`` recognises (``anthropic`` / ``openai``) without
#: importing it.
Family = Literal["anthropic", "openai"]

#: How a credential authenticates. ``subscription`` is a Claude/Codex
#: Pro/Max login isolated in its own config dir; ``api_key`` is a raw
#: provider API key. Subscriptions are preferred at selection time; api
#: keys are the tier-fallback.
AccountKind = Literal["subscription", "api_key"]

#: What the system does when the active account hits its usage limit.
FailoverMode = Literal["notify", "auto", "disabled"]

#: An account's coarse usage-limit state. ``unknown`` means we have never
#: observed it (no probe, no reactive signal).
LimitStatus = Literal["available", "limited", "unknown"]

#: How a :class:`~omnigent.subscription_tokens.domain.value_objects.limit_state.LimitState`
#: observation was learned. ``manual`` bypasses the staleness guard.
DetectionSource = Literal["reactive", "poller", "manual"]

ANTHROPIC: Family = "anthropic"
OPENAI: Family = "openai"

SUBSCRIPTION: AccountKind = "subscription"
API_KEY: AccountKind = "api_key"

NOTIFY: FailoverMode = "notify"
AUTO: FailoverMode = "auto"
DISABLED: FailoverMode = "disabled"

VALID_FAMILIES: tuple[Family, ...] = get_args(Family)
VALID_ACCOUNT_KINDS: tuple[AccountKind, ...] = get_args(AccountKind)
VALID_FAILOVER_MODES: tuple[FailoverMode, ...] = get_args(FailoverMode)
