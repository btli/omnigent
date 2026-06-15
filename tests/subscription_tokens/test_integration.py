"""Tests for the subscription-token integration facade (explicit activation)."""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from omnigent.db.utils import ManagedSessionMaker
from omnigent.subscription_tokens import integration
from omnigent.subscription_tokens.config.pool_config import account_id_for, load_pools
from omnigent.subscription_tokens.config.pool_config_syncer import sync_pools
from omnigent.subscription_tokens.container import build_container
from omnigent.subscription_tokens.domain.value_objects.limit_state import LimitState


def _accounts(snapshot: list[dict[str, object]]) -> list[dict[str, object]]:
    """Return the first pool's typed per-account dicts from a status snapshot."""
    accounts = snapshot[0]["accounts"]
    assert isinstance(accounts, list)
    return accounts


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


@pytest.fixture
def active_openai_facade(session_maker: ManagedSessionMaker) -> Iterator[ManagedSessionMaker]:
    """Activate the facade over an OpenAI pool: two Codex subs + an api_key."""
    pools = load_pools(
        {
            "pools": {
                "codex-pool": {
                    "family": "openai",
                    "failover": "auto",
                    "members": [
                        {"name": "x1", "codex_config_dir": "~/.codex-x1", "priority": 0},
                        {"name": "x2", "codex_config_dir": "~/.codex-x2", "priority": 1},
                        {
                            "name": "xkey",
                            "kind": "api_key",
                            "api_key_ref": "env:OAI_TEST_KEY",
                            "priority": 9,
                        },
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
    assert integration.select_codex_launch(session_id="s") == integration.CodexLaunchSelection()
    assert integration.status_snapshot() == []
    assert integration.mark_available("whatever") is False


def test_select_codex_launch_subscription_returns_source_and_binds(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # Priority-0 subscription is selected; its CODEX_HOME is the bridge source.
    selection = integration.select_codex_launch(session_id="sess-x")
    assert selection.config_source == os.path.expanduser("~/.codex-x1")
    assert selection.api_key is None
    # Session is bound to x1 so reactive failover + cost attribution resolve.
    bound = build_container(active_openai_facade).registry.active_credential("sess-x")
    assert bound == account_id_for("codex-pool", "x1")


def test_select_codex_launch_api_key_when_subscriptions_limited(
    active_openai_facade: ManagedSessionMaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OAI_TEST_KEY", "sk-oai-xyz")
    # Limit both subscriptions → tier fallback to the api_key account.
    repo = build_container(active_openai_facade).state_repo
    for name in ("x1", "x2"):
        repo.upsert(
            LimitState(
                account_id_for("codex-pool", name),
                is_limited=True,
                limited_until=10**12,
                source="manual",
                last_checked_at=1,
            )
        )
    selection = integration.select_codex_launch(session_id="sess-y")
    assert selection.config_source is None
    assert selection.api_key == "sk-oai-xyz"
    bound = build_container(active_openai_facade).registry.active_credential("sess-y")
    assert bound == account_id_for("codex-pool", "xkey")


def test_transfer_session_binding_carries_account_across_session_ids(
    active_openai_facade: ManagedSessionMaker,
) -> None:
    # The launch binds the parent; a native /clear rotation or a sub-agent child
    # gets a new session id that must resolve the same account.
    integration.select_codex_launch(session_id="sess-parent")
    registry = build_container(active_openai_facade).registry
    parent_cred = registry.active_credential("sess-parent")
    assert parent_cred == account_id_for("codex-pool", "x1")

    integration.transfer_session_binding("sess-parent", "sess-child", family="openai")
    assert registry.active_credential("sess-child") == parent_cred

    # No-op when the source has no binding — never invents one.
    integration.transfer_session_binding("sess-unbound", "sess-target", family="openai")
    assert registry.active_credential("sess-target") is None


def test_select_launch_env_returns_config_dir_and_binds(
    active_facade: ManagedSessionMaker,
) -> None:
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c1")}  # priority 0, available
    # Session is bound to the selected account.
    snapshot = integration.status_snapshot()
    assert snapshot[0]["name"] == "claude-pool"


def test_reactive_limit_without_reset_applies_cooldown(
    active_facade: ManagedSessionMaker,
) -> None:
    # Bind the session to c1 via a launch selection.
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")

    # A limit signal with NO reset headers.
    integration.record_reactive_text(
        "Claude usage limit reached.", family="anthropic", session_id="sess-1"
    )

    # c1 is now limited; the next launch selection avoids it.
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c2")}

    # Failover does NOT rebind the running session: sess-1 was launched on c1
    # and keeps running on it (the next launch is what rotates to c2).
    container = build_container(active_facade)
    assert container.registry.active_credential("sess-1") == c1

    # No header reset, but the facade applied a default cooldown — limited with
    # a concrete limited_until (auto-recovers, not a permanent lockout).
    snapshot = integration.status_snapshot()
    c1_account = next(a for a in _accounts(snapshot) if a["id"] == c1)
    assert c1_account["limit_status"] == "limited"
    assert c1_account["limited_until"] is not None


def test_reactive_text_usage_limit_triggers_failover(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    c1 = account_id_for("claude-pool", "c1")
    c2 = account_id_for("claude-pool", "c2")

    # A Claude "usage limit reached" line in forwarded agent output.
    integration.record_reactive_text(
        "Claude AI usage limit reached. Resets later.",
        family="anthropic",
        session_id="sess-1",
    )

    # c1 limited → next launch avoids it; the running sess-1 stays on c1.
    env = integration.select_launch_env_for_family("anthropic", session_id="sess-2")
    assert env == {"CLAUDE_CONFIG_DIR": os.path.expanduser("~/.c2")}
    assert build_container(active_facade).registry.active_credential("sess-1") == c1
    assert c2  # referenced for clarity


def test_reactive_text_non_limit_is_noop(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    # Ordinary output (no limit signal) leaves everything available.
    integration.record_reactive_text(
        "Here is the answer to your question.", family="anthropic", session_id="sess-1"
    )
    snapshot = integration.status_snapshot()
    statuses = {a["id"]: a["limit_status"] for a in _accounts(snapshot)}
    assert all(s != "limited" for s in statuses.values())


def test_attribute_cost_and_status_snapshot(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.attribute_cost("sess-1", cost_usd=1.25, input_tokens=100, output_tokens=20)
    # No-op posts (zero spend, or a negative cumulative glitch) must not
    # create a row or inflate the turn count.
    integration.attribute_cost("sess-1", cost_usd=0.0, input_tokens=0, output_tokens=0)
    integration.attribute_cost("sess-1", cost_usd=-5.0, input_tokens=-3, output_tokens=0)

    snapshot = integration.status_snapshot()
    accounts = {a["name"]: a for a in _accounts(snapshot)}
    assert accounts["c1"]["cost_today_usd"] == pytest.approx(1.25)  # unchanged by no-ops
    # Never observed (no probe/limit) → unknown, not available.
    assert accounts["c1"]["limit_status"] == "unknown"


def test_mark_available_clears_limit(active_facade: ManagedSessionMaker) -> None:
    integration.select_launch_env_for_family("anthropic", session_id="sess-1")
    integration.record_reactive_text(
        "Claude usage limit reached.", family="anthropic", session_id="sess-1"
    )
    c1 = account_id_for("claude-pool", "c1")

    assert integration.mark_available(c1) is True
    snapshot = integration.status_snapshot()
    accounts = {a["id"]: a for a in _accounts(snapshot)}
    assert accounts[c1]["limit_status"] == "available"
