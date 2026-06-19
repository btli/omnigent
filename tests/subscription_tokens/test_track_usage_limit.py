"""Tests for TrackUsageLimitUseCase (in-memory fake repository)."""

from __future__ import annotations

from omnigent.subscription_tokens.application.ports.ports import UsageLimitStateRepository
from omnigent.subscription_tokens.application.use_cases.track_usage_limit import (
    TrackUsageLimitUseCase,
)
from omnigent.subscription_tokens.domain.value_objects.limit_state import (
    LimitDetectionResult,
    LimitState,
)


class FakeStateRepo(UsageLimitStateRepository):
    def __init__(self) -> None:
        self.states: dict[str, LimitState] = {}

    def find(self, credential_id: str) -> LimitState | None:
        return self.states.get(credential_id)

    def find_many(self, credential_ids: list[str]) -> dict[str, LimitState]:
        return {c: self.states[c] for c in credential_ids if c in self.states}

    def upsert(self, state: LimitState, *, enforce_staleness: bool = True) -> bool:
        prior = self.states.get(state.credential_id)
        if (
            prior is not None
            and enforce_staleness
            and prior.last_checked_at is not None
            and state.last_checked_at is not None
            and prior.last_checked_at > state.last_checked_at
        ):
            return False
        self.states[state.credential_id] = state
        return True

    def observe(self, state: LimitState, *, enforce_staleness: bool = True) -> tuple[bool, bool]:
        prior = self.states.get(state.credential_id)
        now = state.last_checked_at if state.last_checked_at is not None else 0
        was_available = prior is None or prior.is_available_now(now)
        wrote = self.upsert(state, enforce_staleness=enforce_staleness)
        return wrote, was_available


def test_track_marks_newly_limited_once() -> None:
    repo = FakeStateRepo()
    uc = TrackUsageLimitUseCase(repo)

    first = uc.execute(
        LimitDetectionResult("c1", is_limited=True, source="reactive", observed_at=100)
    )
    assert first.was_newly_limited is True
    assert first.wrote is True

    # A second "still limited" observation is not newly-limited.
    second = uc.execute(
        LimitDetectionResult("c1", is_limited=True, source="reactive", observed_at=200)
    )
    assert second.was_newly_limited is False


def test_track_respects_staleness_guard() -> None:
    repo = FakeStateRepo()
    uc = TrackUsageLimitUseCase(repo)
    uc.execute(LimitDetectionResult("c1", is_limited=True, source="poller", observed_at=200))
    stale = uc.execute(
        LimitDetectionResult("c1", is_limited=False, source="poller", observed_at=100)
    )
    assert stale.wrote is False
    assert stale.was_newly_limited is False


def test_track_manual_bypasses_staleness() -> None:
    repo = FakeStateRepo()
    uc = TrackUsageLimitUseCase(repo)
    uc.execute(LimitDetectionResult("c1", is_limited=True, source="poller", observed_at=200))
    manual = uc.execute(
        LimitDetectionResult("c1", is_limited=False, source="manual", observed_at=50)
    )
    assert manual.wrote is True
