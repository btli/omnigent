"""Tests for the SQLAlchemy subscription-token repositories against a real SQLite DB."""

from __future__ import annotations

import pytest

from omnigent.db.utils import ManagedSessionMaker
from omnigent.subscription_tokens.config.pool_config import account_id_for, load_pools
from omnigent.subscription_tokens.config.pool_config_syncer import sync_pools
from omnigent.subscription_tokens.domain.value_objects.limit_state import LimitState
from omnigent.subscription_tokens.domain.value_objects.usage_window import UsageWindow
from omnigent.subscription_tokens.infrastructure.repositories.sqlalchemy_repositories import (
    SqlCostAttributionSink,
    SqlCredentialPoolRepository,
    SqlSessionCredentialRegistry,
    SqlUsageLimitStateRepository,
)


@pytest.fixture
def seeded(session_maker: ManagedSessionMaker) -> ManagedSessionMaker:
    """Seed a claude pool (two subs + an api key) and a codex pool."""
    config = {
        "pools": {
            "claude-pool": {
                "family": "anthropic",
                "failover": "auto",
                "members": [
                    {
                        "name": "c1",
                        "kind": "subscription",
                        "claude_config_dir": "~/.c1",
                        "priority": 0,
                    },
                    {
                        "name": "c2",
                        "kind": "subscription",
                        "claude_config_dir": "~/.c2",
                        "priority": 1,
                    },
                    {"name": "capi", "kind": "api_key", "api_key_ref": "env:K", "priority": 9},
                ],
            },
            "codex-pool": {
                "family": "openai",
                "members": [{"name": "x1", "kind": "subscription", "codex_config_dir": "~/.x1"}],
            },
        }
    }
    sync_pools(session_maker, load_pools(config))
    return session_maker


def test_limit_state_observe_reports_transition(
    seeded: ManagedSessionMaker, immediate_session_maker: ManagedSessionMaker
) -> None:
    # Exercise the real BEGIN IMMEDIATE path used in production.
    repo = SqlUsageLimitStateRepository(seeded, immediate_session_maker)
    cid = account_id_for("claude-pool", "c1")

    def _limited(at: int) -> LimitState:
        return LimitState(
            credential_id=cid,
            is_limited=True,
            limited_until=9000,
            source="reactive",
            last_checked_at=at,
        )

    # First observation: prior is None → was available → a real transition.
    wrote, was_available = repo.observe(_limited(1000))
    assert wrote is True
    assert was_available is True

    # Second observation while still limited: NOT available → not a transition.
    wrote2, was_available2 = repo.observe(_limited(2000))
    assert wrote2 is True
    assert was_available2 is False


def test_observe_rejects_stale_write_under_lock(
    seeded: ManagedSessionMaker, immediate_session_maker: ManagedSessionMaker
) -> None:
    # The locked observe() path must reject an older observation (exercising
    # the row_exists short-circuit that skips the redundant guarded re-UPDATE)
    # without clobbering the fresher stored row.
    repo = SqlUsageLimitStateRepository(seeded, immediate_session_maker)
    cid = account_id_for("claude-pool", "c1")

    fresh = LimitState(
        cid, is_limited=True, limited_until=9000, source="reactive", last_checked_at=2000
    )
    assert repo.observe(fresh) == (True, True)

    stale = LimitState(cid, is_limited=False, source="poller", last_checked_at=1000)
    wrote, was_available = repo.observe(stale)
    assert wrote is False  # older observation rejected by the staleness guard
    assert was_available is False  # prior was limited and not yet recovered
    loaded = repo.find(cid)
    assert loaded is not None and loaded.is_limited is True


def test_limit_state_upsert_find_and_staleness(seeded: ManagedSessionMaker) -> None:
    repo = SqlUsageLimitStateRepository(seeded)
    cid = account_id_for("claude-pool", "c1")

    assert repo.find(cid) is None
    state = LimitState(
        credential_id=cid,
        is_limited=True,
        windows=(UsageWindow("5h", 100, 5000), UsageWindow("7d", 30, 9000)),
        source="reactive",
        last_checked_at=1000,
    )
    assert repo.upsert(state) is True

    loaded = repo.find(cid)
    assert loaded is not None
    assert loaded.is_limited is True
    assert loaded.remaining_headroom_pct() == 0  # 5h exhausted
    assert loaded.earliest_reset_at() == 5000
    assert loaded.source == "reactive"

    # Stale write (older observation) is rejected.
    older = LimitState(credential_id=cid, is_limited=False, source="poller", last_checked_at=500)
    assert repo.upsert(older) is False
    after_stale = repo.find(cid)
    assert after_stale is not None and after_stale.is_limited is True

    # Manual override bypasses the staleness guard.
    manual = LimitState(credential_id=cid, is_limited=False, source="manual", last_checked_at=500)
    assert repo.upsert(manual, enforce_staleness=False) is True
    after_manual = repo.find(cid)
    assert after_manual is not None and after_manual.is_limited is False


def test_staleness_same_second_source_precedence(seeded: ManagedSessionMaker) -> None:
    repo = SqlUsageLimitStateRepository(seeded)
    cid = account_id_for("claude-pool", "c1")

    # Reactive "limited" at t=1000.
    assert repo.upsert(
        LimitState(
            cid, is_limited=True, limited_until=9000, source="reactive", last_checked_at=1000
        )
    )
    # A SAME-SECOND poller "available" must NOT clobber the reactive limit
    # (poller has lower source precedence on a timestamp tie).
    assert (
        repo.upsert(LimitState(cid, is_limited=False, source="poller", last_checked_at=1000))
        is False
    )
    after_tie = repo.find(cid)
    assert after_tie is not None and after_tie.is_limited is True
    # A strictly-later poller observation does win.
    assert repo.upsert(LimitState(cid, is_limited=False, source="poller", last_checked_at=2000))
    after_later = repo.find(cid)
    assert after_later is not None and after_later.is_limited is False


def test_limit_state_find_many(seeded: ManagedSessionMaker) -> None:
    repo = SqlUsageLimitStateRepository(seeded)
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")
    repo.upsert(LimitState(credential_id=c1, last_checked_at=1, source="poller"))
    found = repo.find_many([c1, c2, "missing"])
    assert set(found) == {c1}
    assert repo.find_many([]) == {}


def test_pool_repository_reconstructs_pool_and_accounts(seeded: ManagedSessionMaker) -> None:
    repo = SqlCredentialPoolRepository(seeded)
    pool = repo.find_pool_for_family("anthropic")
    assert pool is not None
    assert pool.name == "claude-pool"
    assert pool.failover_mode == "auto"
    assert [m.name for m in pool.members] == ["c1", "c2", "capi"]  # priority order
    api = next(m for m in pool.members if m.name == "capi")
    assert api.kind == "api_key"
    assert api.api_key_ref == "env:K"

    openai_pool = repo.find_pool_for_family("openai")
    assert openai_pool is not None and openai_pool.name == "codex-pool"
    c1_account = repo.find_account(account_id_for("claude-pool", "c1"))
    assert c1_account is not None and c1_account.name == "c1"
    assert repo.find_account("nope") is None
    assert {a.name for a in repo.accounts_for_family("anthropic")} == {"c1", "c2", "capi"}


def test_session_credential_registry_bind_and_rebind(seeded: ManagedSessionMaker) -> None:
    reg = SqlSessionCredentialRegistry(seeded)
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")
    assert reg.active_credential("sess-1") is None
    reg.bind("sess-1", c1, "anthropic")
    assert reg.active_credential("sess-1") == c1
    reg.bind("sess-1", c2, "anthropic")  # rebind (failover)
    assert reg.active_credential("sess-1") == c2


def test_cost_attribution_sink_accumulates(seeded: ManagedSessionMaker) -> None:
    sink = SqlCostAttributionSink(seeded)
    c1 = account_id_for("claude-pool", "c1")
    sink.record_credential_cost(c1, "2026-06-14", cost_usd=0.5, input_tokens=100, output_tokens=20)
    sink.record_credential_cost(c1, "2026-06-14", cost_usd=0.25, input_tokens=50, output_tokens=10)

    from omnigent.db.db_models import SqlProviderAccountCost

    with seeded() as session:
        row = session.get(SqlProviderAccountCost, (c1, "2026-06-14"))
        assert row is not None
        assert row.cost_usd == pytest.approx(0.75)
        assert row.input_tokens == 150
        assert row.output_tokens == 30
        assert row.turn_count == 2
