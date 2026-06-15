"""Use case: record one usage-limit observation.

Reconciles a :class:`LimitDetectionResult` (from the reactive detector or
the proactive poller) with stored state. Its key output is
``was_newly_limited`` — ``True`` only on an off→on transition — which
gates one-shot failover so repeated "still limited" observations do not
re-fire it.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.cswap.application.ports.ports import UsageLimitStateRepository
from omnigent.cswap.domain.value_objects.limit_state import (
    LimitDetectionResult,
    LimitState,
)


@dataclass(frozen=True)
class TrackUsageLimitResult:
    """Outcome of recording an observation.

    :param state: The state projected from the observation.
    :param was_newly_limited: ``True`` when this observation flipped the
        account from available to limited (and was actually written).
    :param wrote: ``False`` when the staleness guard skipped the write.
    """

    state: LimitState
    was_newly_limited: bool
    wrote: bool


class TrackUsageLimitUseCase:
    """Persist a usage-limit observation with a newly-limited gate."""

    def __init__(self, state_repo: UsageLimitStateRepository) -> None:
        """:param state_repo: Limit-state persistence."""
        self._state_repo = state_repo

    def execute(self, detection: LimitDetectionResult) -> TrackUsageLimitResult:
        """Record *detection* and report whether it was a new limit.

        :param detection: The observation to persist.
        :returns: A :class:`TrackUsageLimitResult`.
        """
        prior = self._state_repo.find(detection.credential_id)
        was_available = prior is None or prior.is_available_now(detection.observed_at)
        new_state = detection.to_limit_state()
        wrote = self._state_repo.upsert(new_state, enforce_staleness=detection.source != "manual")
        was_newly_limited = detection.is_limited and was_available and wrote
        return TrackUsageLimitResult(
            state=new_state, was_newly_limited=was_newly_limited, wrote=wrote
        )
