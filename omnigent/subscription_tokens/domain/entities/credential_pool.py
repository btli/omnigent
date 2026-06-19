"""The :class:`CredentialPool` entity.

A named, family-scoped group of :class:`ProviderAccount` members that the
router rotates through. The pool knows how to project its members into
:class:`RotationCandidate` objects (joining each with its observed
:class:`LimitState`), which is the bridge from configuration to the pure
:class:`RotationPolicy`.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.subscription_tokens.domain.entities.provider_account import ProviderAccount
from omnigent.subscription_tokens.domain.value_objects.enums import (
    MAX_HEADROOM,
    FailoverMode,
    Family,
    RotationMode,
)
from omnigent.subscription_tokens.domain.value_objects.limit_state import LimitState
from omnigent.subscription_tokens.domain.value_objects.rotation_policy import RotationCandidate


@dataclass(frozen=True)
class CredentialPool:
    """A rotation pool of provider accounts for one family.

    :param id: Stable pool id, e.g. ``"pool_<hex>"``.
    :param name: Human label, e.g. ``"claude-pool"``.
    :param family: The provider family this pool serves.
    :param failover_mode: How to react when the active account is limited.
    :param members: The pool's accounts (any order; ranked at selection).
    :param rotation_mode: How available members are ranked at selection
        (``max_headroom`` default, or ``soonest_reset``).
    """

    id: str
    name: str
    family: Family
    failover_mode: FailoverMode
    members: tuple[ProviderAccount, ...]
    rotation_mode: RotationMode = MAX_HEADROOM

    def to_candidates(self, states: dict[str, LimitState]) -> list[RotationCandidate]:
        """Join active members with their limit states into candidates.

        :param states: Map of ``credential_id`` → observed
            :class:`LimitState`. Members absent from the map are treated
            as never-observed (an empty, available state).
        :returns: One :class:`RotationCandidate` per active member.
        """
        candidates: list[RotationCandidate] = []
        for member in self.members:
            if not member.is_active:
                continue
            state = states.get(member.id) or LimitState(credential_id=member.id)
            candidates.append(
                RotationCandidate(
                    credential_id=member.id,
                    priority=member.priority,
                    kind=member.kind,
                    limit_state=state,
                )
            )
        return candidates

    def member_ids(self) -> list[str]:
        """Return the ids of all (including inactive) members."""
        return [m.id for m in self.members]
