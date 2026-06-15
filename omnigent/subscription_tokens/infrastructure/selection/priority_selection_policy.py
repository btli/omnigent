"""Headroom + reset-aware credential selection.

Joins the family's pool with each member's observed
:class:`~omnigent.subscription_tokens.domain.value_objects.limit_state.LimitState` and
delegates the choice to the pure
:class:`~omnigent.subscription_tokens.domain.value_objects.rotation_policy.RotationPolicy`.
The policy itself stays I/O-free; this adapter supplies it with data.
"""

from __future__ import annotations

from omnigent.subscription_tokens.application.ports.ports import (
    CredentialPoolRepository,
    CredentialSelectionPolicy,
    UsageLimitStateRepository,
)
from omnigent.subscription_tokens.domain.entities.provider_account import ProviderAccount
from omnigent.subscription_tokens.domain.value_objects.rotation_policy import RotationPolicy


class PriorityCredentialSelectionPolicy(CredentialSelectionPolicy):
    """Select an account for a family using the rotation policy."""

    def __init__(
        self,
        pool_repo: CredentialPoolRepository,
        state_repo: UsageLimitStateRepository,
    ) -> None:
        """:param pool_repo: Source of the family's pool + members.
        :param state_repo: Source of per-account limit states.
        """
        self._pool_repo = pool_repo
        self._state_repo = state_repo

    def select_for_family(
        self,
        family: str,
        now: int,
        *,
        exclude_credential_id: str | None = None,
        best_effort: bool = True,
    ) -> ProviderAccount | None:
        """Return the chosen account for *family*, or ``None`` (see port docs)."""
        pool = self._pool_repo.find_pool_for_family(family)
        if pool is None:
            return None
        active = [m for m in pool.members if m.is_active]
        if not active:
            return None
        states = self._state_repo.find_many([m.id for m in active])
        candidates = pool.to_candidates(states)
        chosen = RotationPolicy.select(
            candidates,
            now,
            allow_tier_fallback=True,
            best_effort=best_effort,
            exclude_credential_id=exclude_credential_id,
        )
        if chosen is None:
            return None
        return next((m for m in pool.members if m.id == chosen), None)
