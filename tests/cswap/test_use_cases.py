"""Tests for cswap use cases and the selection policy (in-memory fakes)."""

from __future__ import annotations

from omnigent.cswap.application.ports.ports import (
    CredentialPoolRepository,
    FailoverEvent,
    UsageLimitStateRepository,
)
from omnigent.cswap.application.use_cases.failover_on_limit import FailoverOnLimitUseCase
from omnigent.cswap.application.use_cases.select_credential import SelectCredentialUseCase
from omnigent.cswap.application.use_cases.track_usage_limit import TrackUsageLimitUseCase
from omnigent.cswap.domain.entities.credential_pool import CredentialPool
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.enums import FailoverMode
from omnigent.cswap.domain.value_objects.limit_state import (
    LimitDetectionResult,
    LimitState,
)
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow
from omnigent.cswap.infrastructure.selection.priority_credential_selection_policy import (
    PriorityCredentialSelectionPolicy,
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


class FakePoolRepo(CredentialPoolRepository):
    def __init__(self, pool: CredentialPool) -> None:
        self.pool = pool

    def find_pool_for_family(self, family: str) -> CredentialPool | None:
        return self.pool if self.pool.family == family else None

    def find_account(self, credential_id: str) -> ProviderAccount | None:
        return next((m for m in self.pool.members if m.id == credential_id), None)

    def accounts_for_family(self, family: str) -> list[ProviderAccount]:
        return list(self.pool.members) if self.pool.family == family else []


class RecordingNotifier:
    def __init__(self) -> None:
        self.events: list[FailoverEvent] = []

    def notify(self, event: FailoverEvent) -> None:
        self.events.append(event)


def _pool(failover: FailoverMode = "auto") -> CredentialPool:
    return CredentialPool(
        id="pool_1",
        name="claude",
        family="anthropic",
        failover_mode=failover,
        members=(
            ProviderAccount("c1", "c1", "anthropic", "subscription", 0),
            ProviderAccount("c2", "c2", "anthropic", "subscription", 1),
            ProviderAccount("capi", "capi", "anthropic", "api_key", 9, api_key_ref="env:K"),
        ),
    )


# ── TrackUsageLimitUseCase ─────────────────────────────────


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


# ── Selection policy + SelectCredentialUseCase ─────────────


def test_selection_prefers_headroom_then_tier_fallback() -> None:
    repo = FakeStateRepo()
    pool_repo = FakePoolRepo(_pool())
    # c1 nearly exhausted, c2 has headroom -> pick c2.
    repo.states["c1"] = LimitState("c1", windows=(UsageWindow("5h", 95, 9999),), source="poller")
    repo.states["c2"] = LimitState("c2", windows=(UsageWindow("5h", 20, 9999),), source="poller")
    policy = PriorityCredentialSelectionPolicy(pool_repo, repo)
    uc = SelectCredentialUseCase(policy)

    result = uc.execute("anthropic", now=1000)
    assert result.account is not None
    assert result.account.id == "c2"
    assert result.used_tier_fallback is False


def test_selection_tier_fallback_when_all_subs_limited() -> None:
    repo = FakeStateRepo()
    pool_repo = FakePoolRepo(_pool())
    limited = {"is_limited": True, "windows": (UsageWindow("5h", 100, 9999),), "source": "poller"}
    repo.states["c1"] = LimitState("c1", **limited)  # type: ignore[arg-type]
    repo.states["c2"] = LimitState("c2", **limited)  # type: ignore[arg-type]
    policy = PriorityCredentialSelectionPolicy(pool_repo, repo)
    uc = SelectCredentialUseCase(policy)

    result = uc.execute("anthropic", now=1000)
    assert result.account is not None
    assert result.account.id == "capi"
    assert result.used_tier_fallback is True


# ── FailoverOnLimitUseCase ─────────────────────────────────


def _failover(
    failover: FailoverMode, repo: FakeStateRepo
) -> tuple[FailoverOnLimitUseCase, RecordingNotifier]:
    pool_repo = FakePoolRepo(_pool(failover))
    policy = PriorityCredentialSelectionPolicy(pool_repo, repo)
    notifier = RecordingNotifier()
    uc = FailoverOnLimitUseCase(pool_repo, policy, notifier)
    return uc, notifier


def test_failover_disabled_is_noop() -> None:
    uc, notifier = _failover("disabled", FakeStateRepo())
    assert (
        uc.execute(session_id="s", exhausted_credential_id="c1", family="anthropic", now=1) is None
    )
    assert notifier.events == []


def test_failover_recommends_alternate_without_rebinding() -> None:
    # Failover does NOT rebind the running session (it keeps running on the
    # exhausted account); it recommends the account the next launch should use.
    for mode in ("auto", "notify"):
        repo = FakeStateRepo()
        uc, notifier = _failover(mode, repo)  # type: ignore[arg-type]
        event = uc.execute(session_id="s", exhausted_credential_id="c1", family="anthropic", now=1)
        assert event is not None
        assert event.next_credential_id == "c2"  # next available subscription
        assert event.mode == mode
        assert len(notifier.events) == 1
