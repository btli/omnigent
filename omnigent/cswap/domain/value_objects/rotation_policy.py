"""The :class:`RotationPolicy` — pure account-selection logic.

This encodes the confirmed routing rules with no I/O so they can be
exhaustively unit-tested:

1. Prefer **available** accounts (not limited, or limited but past reset).
2. Prefer the **subscription** tier; only fall through to **api_key**
   (tier fallback) when no subscription is available.
3. Within the chosen tier, pick the account with the **most remaining
   headroom**; break ties by lower configured priority, then id.
4. When nothing is available, optionally best-effort to the account whose
   limit resets **soonest** so a launch is never blocked.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.cswap.domain.value_objects.enums import SUBSCRIPTION, AccountKind
from omnigent.cswap.domain.value_objects.limit_state import LimitState

# Headroom assumed for an account that has never been probed. Optimistic so
# a freshly-added account is usable before the poller has measured it.
_UNKNOWN_HEADROOM = 100


@dataclass(frozen=True)
class RotationCandidate:
    """One account in contention during selection.

    :param credential_id: The account id.
    :param priority: Configured priority; lower is preferred.
    :param kind: Subscription or api_key (drives tier fallback).
    :param limit_state: The account's current usage-limit state.
    """

    credential_id: str
    priority: int
    kind: AccountKind
    limit_state: LimitState

    def headroom(self) -> int:
        """Remaining headroom percent, defaulting unknown to optimistic."""
        pct = self.limit_state.remaining_headroom_pct()
        return _UNKNOWN_HEADROOM if pct is None else pct


class RotationPolicy:
    """Stateless selection strategy over :class:`RotationCandidate` lists."""

    @staticmethod
    def select(
        candidates: list[RotationCandidate],
        now: int,
        *,
        allow_tier_fallback: bool = True,
        best_effort: bool = True,
        exclude_credential_id: str | None = None,
    ) -> str | None:
        """Select the best account, or ``None``.

        :param candidates: The accounts to choose among.
        :param now: Current Unix epoch seconds.
        :param allow_tier_fallback: When ``True``, fall back to api_key
            accounts if no subscription is available.
        :param best_effort: When ``True`` and nothing is available, return
            the soonest-to-reset candidate instead of ``None`` (used at
            launch, which must never block). When ``False``, return
            ``None`` if no account is available (used by failover, which
            then reports "all limited").
        :param exclude_credential_id: An account to omit (the one that
            just hit a limit, during failover).
        :returns: The chosen ``credential_id``, or ``None``.
        """
        pool = [c for c in candidates if c.credential_id != exclude_credential_id]
        if not pool:
            return None

        available = [c for c in pool if c.limit_state.is_available_now(now)]
        tier = RotationPolicy._preferred_tier(available, allow_tier_fallback)
        if tier:
            best = max(tier, key=lambda c: (c.headroom(), -c.priority, c.credential_id))
            return best.credential_id

        if not best_effort:
            return None
        return RotationPolicy._soonest_reset(pool, allow_tier_fallback)

    @staticmethod
    def _preferred_tier(
        available: list[RotationCandidate], allow_tier_fallback: bool
    ) -> list[RotationCandidate]:
        """Return the available candidates of the preferred tier.

        Subscriptions win outright; api_key accounts are returned only when
        no subscription is available and tier fallback is allowed.
        """
        subscriptions = [c for c in available if c.kind == SUBSCRIPTION]
        if subscriptions:
            return subscriptions
        if not allow_tier_fallback:
            return []
        return [c for c in available if c.kind != SUBSCRIPTION]

    @staticmethod
    def _soonest_reset(pool: list[RotationCandidate], allow_tier_fallback: bool) -> str:
        """Best-effort pick when nothing is available: soonest reset.

        Honours the tier preference (subscriptions first) and falls back to
        lowest priority when no reset times are known.
        """
        subscriptions = [c for c in pool if c.kind == SUBSCRIPTION]
        # Prefer the subscription tier; widen to the whole pool only when no
        # subscription exists AND tier fallback is allowed. Never narrow to an
        # empty list (which would make min() raise) — fall back to the pool.
        if subscriptions:
            considered = subscriptions
        elif allow_tier_fallback:
            considered = pool
        else:
            considered = [c for c in pool if c.kind != SUBSCRIPTION] or pool

        def sort_key(c: RotationCandidate) -> tuple[int, int, str]:
            # Rank by the authoritative recovery time (``limited_until``), not
            # the earliest window reset — an account can have a soon-resetting
            # window yet stay limited until a later one, so the earliest window
            # would mis-rank it as recovering first.
            reset = c.limit_state.recovery_eta()
            # Unknown resets sort last (a huge sentinel), then by priority/id.
            return (reset if reset is not None else 1 << 62, c.priority, c.credential_id)

        return min(considered, key=sort_key).credential_id
