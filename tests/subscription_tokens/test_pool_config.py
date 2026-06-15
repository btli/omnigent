"""Tests for parsing the ``pools:`` config block."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from omnigent.errors import OmnigentError
from omnigent.subscription_tokens.config.pool_config import (
    account_id_for,
    find_account,
    get_pool_for_family,
    load_pools,
    pool_id_for,
)


def _valid_config() -> dict[str, object]:
    return {
        "providers": {"some-existing": {"kind": "key"}},  # untouched, ignored here
        "pools": {
            "claude-pool": {
                "family": "anthropic",
                "failover": "auto",
                "members": [
                    {
                        "name": "claude-pro-1",
                        "kind": "subscription",
                        "claude_config_dir": "~/.c1",
                        "priority": 0,
                    },
                    {
                        "name": "claude-pro-2",
                        "kind": "subscription",
                        "claude_config_dir": "~/.c2",
                        "priority": 1,
                    },
                    {
                        "name": "claude-api",
                        "kind": "api_key",
                        "api_key_ref": "env:ANTHROPIC_API_KEY",
                        "priority": 10,
                    },
                ],
            },
            "codex-pool": {
                "family": "openai",
                "members": [
                    {"name": "codex-1", "kind": "subscription", "codex_config_dir": "~/.x1"},
                    {
                        "name": "openai-api",
                        "kind": "api_key",
                        "api_key_ref": "env:OPENAI_API_KEY",
                        "priority": 5,
                    },
                ],
            },
        },
    }


def test_load_pools_parses_all_pools_and_members() -> None:
    pools = load_pools(_valid_config())
    assert set(pools) == {"claude-pool", "codex-pool"}

    claude = pools["claude-pool"]
    assert claude.family == "anthropic"
    assert claude.failover_mode == "auto"
    assert [m.name for m in claude.members] == ["claude-pro-1", "claude-pro-2", "claude-api"]
    sub = claude.members[0]
    assert sub.kind == "subscription"
    assert sub.claude_config_dir == "~/.c1"
    assert sub.config_dir() == "~/.c1"
    api = claude.members[2]
    assert api.kind == "api_key"
    assert api.api_key_ref == "env:ANTHROPIC_API_KEY"
    assert api.priority == 10


def test_parses_oauth_token_subscription_member() -> None:
    config: Any = _valid_config()
    config["pools"]["claude-pool"]["members"].append(
        {"name": "claude-oauth", "kind": "subscription", "oauth_token_ref": "env:CLAUDE_OAUTH_1"}
    )
    member = load_pools(config)["claude-pool"].members[-1]
    assert member.kind == "subscription"
    assert member.oauth_token_ref == "env:CLAUDE_OAUTH_1"
    assert member.config_dir() is None  # a token-ref subscription has no config dir


def test_failover_defaults_to_notify() -> None:
    pools = load_pools(_valid_config())
    assert pools["codex-pool"].failover_mode == "notify"


def test_missing_pools_block_returns_empty() -> None:
    assert load_pools({"providers": {}}) == {}
    assert load_pools({}) == {}


def test_ids_are_deterministic() -> None:
    a = load_pools(_valid_config())
    b = load_pools(_valid_config())
    assert a["claude-pool"].id == b["claude-pool"].id == pool_id_for("claude-pool")
    assert a["claude-pool"].members[0].id == account_id_for("claude-pool", "claude-pro-1")
    assert a["claude-pool"].members[0].pool_id == pool_id_for("claude-pool")


def test_get_pool_for_family_and_find_account() -> None:
    pools = load_pools(_valid_config())
    anthropic_pool = get_pool_for_family(pools, "anthropic")
    openai_pool = get_pool_for_family(pools, "openai")
    assert anthropic_pool is not None and anthropic_pool.name == "claude-pool"
    assert openai_pool is not None and openai_pool.name == "codex-pool"

    cid = account_id_for("codex-pool", "codex-1")
    found = find_account(pools, cid)
    assert found is not None
    pool, account = found
    assert pool.name == "codex-pool"
    assert account.name == "codex-1"
    assert find_account(pools, "nonexistent") is None


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda c: c["pools"]["claude-pool"].__setitem__("family", "bogus"), "family"),
        (lambda c: c["pools"]["claude-pool"].__setitem__("failover", "sometimes"), "failover"),
        (lambda c: c["pools"]["claude-pool"].__setitem__("members", []), "non-empty list"),
        (
            lambda c: c["pools"]["claude-pool"]["members"].append(
                {"name": "bad", "kind": "api_key"}
            ),
            "api_key_ref",
        ),
        (
            lambda c: c["pools"]["claude-pool"]["members"].append(
                {"name": "claude-pro-1", "kind": "subscription"}
            ),
            "duplicate",
        ),
        (
            lambda c: c["pools"]["claude-pool"]["members"].append(
                {"name": "wrongdir", "codex_config_dir": "~/.x"}
            ),
            "codex_config_dir",
        ),
        (
            lambda c: c["pools"]["codex-pool"]["members"].append(
                {"name": "wrongdir2", "claude_config_dir": "~/.c"}
            ),
            "claude_config_dir",
        ),
        (
            lambda c: c["pools"]["claude-pool"]["members"].append(
                {
                    "name": "bad-oauth",
                    "kind": "api_key",
                    "api_key_ref": "env:K",
                    "oauth_token_ref": "env:T",
                }
            ),
            "oauth_token_ref",
        ),
        (
            lambda c: c["pools"]["claude-pool"]["members"].append(
                {
                    "name": "both-auth",
                    "kind": "subscription",
                    "claude_config_dir": "~/.c",
                    "oauth_token_ref": "env:T",
                }
            ),
            "not both",
        ),
    ],
)
# ``mutate`` deliberately corrupts a nested, opaque config mapping in place, so
# its input is typed ``Any`` (the project allows explicit Any in tests for such
# JSON-shaped boundaries).
def test_invalid_configs_raise(mutate: Callable[[Any], object], fragment: str) -> None:
    config = _valid_config()
    mutate(config)
    with pytest.raises(OmnigentError) as exc:
        load_pools(config)
    assert fragment in str(exc.value)
