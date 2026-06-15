"""Tests for the cswap integration facade (explicit activation)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from omnigent.cswap import integration
from omnigent.cswap.config.pool_config import account_id_for, load_pools
from omnigent.cswap.config.pool_config_syncer import sync_pools
from omnigent.cswap.container import build_container
from omnigent.db.utils import ManagedSessionMaker


@pytest.fixture
def active_facade(session_maker: ManagedSessionMaker) -> Iterator[ManagedSessionMaker]:
    """Activate the facade over a seeded two-subscription claude pool."""
    pools = load_pools(
        {
            "pools": {
                "claude-pool": {
                    "family": "anthropic",
                    "failover": "auto",
                    "members": [
                        {"name": "c1", "claude_config_dir": "~/.c1", "priority": 0},
                        {"name": "c2", "claude_config_dir": "~/.c2", "priority": 1},
                    ],
                }
            }
        }
    )
    sync_pools(session_maker, pools)
    integration.activate(build_container(session_maker), pools)
    try:
        yield session_maker
    finally:
        integration.deactivate()


def test_inactive_facade_is_noop() -> None:
    integration.deactivate()
    assert integration.is_active() is False
    assert integration.select_launch_env_for_family("anthropic") == {}
    assert integration.status_snapshot() == []
    assert integration.mark_available("whatever") is False


def test_select_launch_env_returns_config_dir_and_binds(
    active_facade: ManagedSessionMaker,
) -> None:
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c1")}  # priority 0, available
    # Session is bound to the selected account.
    snapshot = integration.status_snapshot()
    assert snapshot[0]["name"] == "claude-pool"


def test_reactive_429_triggers_failover_rebind(active_facade: ManagedSessionMaker) -> None:
    # Bind the session to c1 via a launch selection.
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")

    integration.record_rate_limited(family="anthropic", session_id="sess-1")

    # c1 is now limited; the next launch selection avoids it.
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c2")}

    # Auto failover rebound sess-1 to c2.
    container = build_container(active_facade)
    assert container.registry.active_credential("sess-1") == c2

    # c1 was limited with no header reset, but the facade applies a default
    # cooldown so it is NOT permanently locked out (auto-recovers, not unknown).
    snapshot = integration.status_snapshot()
    c1_account = next(a for a in snapshot[0]["accounts"] if a["id"] == c1)  # type: ignore[index]
    assert c1_account["limit_status"] == "limited"
    assert c1_account["earliest_reset_at"] is not None


def test_attribute_cost_and_status_snapshot(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.attribute_cost("sess-1", cost_usd=1.25, input_tokens=100, output_tokens=20)

    snapshot = integration.status_snapshot()
    accounts = {a["name"]: a for a in snapshot[0]["accounts"]}  # type: ignore[index]
    assert accounts["c1"]["cost_today_usd"] == pytest.approx(1.25)
    # Never observed (no probe/limit) → unknown, not available.
    assert accounts["c1"]["limit_status"] == "unknown"


def test_mark_available_clears_limit(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.record_rate_limited(family="anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")

    assert integration.mark_available(c1) is True
    snapshot = integration.status_snapshot()
    accounts = {a["id"]: a for a in snapshot[0]["accounts"]}  # type: ignore[index]
    assert accounts[c1]["limit_status"] == "available"
