"""Tests for syncing the parsed ``pools:`` config into the DB."""

from __future__ import annotations

from omnigent.db.db_models import SqlCredentialPool, SqlProviderAccount
from omnigent.db.utils import ManagedSessionMaker
from omnigent.subscription_tokens.config.pool_config import account_id_for, load_pools, pool_id_for
from omnigent.subscription_tokens.config.pool_config_syncer import sync_pools


def _config(members: list[dict[str, object]]) -> dict[str, object]:
    return {"pools": {"claude-pool": {"family": "anthropic", "members": members}}}


def test_sync_inserts_pools_and_accounts(session_maker: ManagedSessionMaker) -> None:
    config = _config(
        [
            {"name": "a", "kind": "subscription", "claude_config_dir": "~/.a"},
            {"name": "b", "kind": "api_key", "api_key_ref": "env:K"},
        ]
    )
    result = sync_pools(session_maker, load_pools(config))
    assert result.pools_upserted == 1
    assert result.accounts_upserted == 2
    assert result.accounts_deactivated == 0

    with session_maker() as session:
        pool = session.get(SqlCredentialPool, pool_id_for("claude-pool"))
        assert pool is not None
        assert pool.family == "anthropic"
        acct = session.get(SqlProviderAccount, account_id_for("claude-pool", "a"))
        assert acct is not None
        assert acct.kind == "subscription"
        assert acct.claude_config_dir == "~/.a"
        assert acct.is_active is True


def test_sync_persists_oauth_token_ref(session_maker: ManagedSessionMaker) -> None:
    # A token-ref subscription round-trips through the migrated column.
    config = _config(
        [{"name": "sub", "kind": "subscription", "oauth_token_ref": "env:CLAUDE_OAUTH_A"}]
    )
    sync_pools(session_maker, load_pools(config))
    with session_maker() as session:
        acct = session.get(SqlProviderAccount, account_id_for("claude-pool", "sub"))
        assert acct is not None
        assert acct.oauth_token_ref == "env:CLAUDE_OAUTH_A"
        assert acct.claude_config_dir is None


def test_sync_is_idempotent_and_updates_fields(session_maker: ManagedSessionMaker) -> None:
    sync_pools(session_maker, load_pools(_config([{"name": "a", "claude_config_dir": "~/.a"}])))
    # Re-sync with a changed config dir → update in place, same row count.
    result = sync_pools(
        session_maker, load_pools(_config([{"name": "a", "claude_config_dir": "~/.a-moved"}]))
    )
    assert result.accounts_upserted == 1
    with session_maker() as session:
        acct = session.get(SqlProviderAccount, account_id_for("claude-pool", "a"))
        assert acct is not None
        assert acct.claude_config_dir == "~/.a-moved"


def test_sync_deactivates_removed_accounts(session_maker: ManagedSessionMaker) -> None:
    sync_pools(
        session_maker,
        load_pools(
            _config(
                [
                    {"name": "a", "claude_config_dir": "~/.a"},
                    {"name": "b", "claude_config_dir": "~/.b"},
                ]
            )
        ),
    )
    # Drop account "b" from the config.
    result = sync_pools(
        session_maker, load_pools(_config([{"name": "a", "claude_config_dir": "~/.a"}]))
    )
    assert result.accounts_deactivated == 1
    with session_maker() as session:
        b = session.get(SqlProviderAccount, account_id_for("claude-pool", "b"))
        assert b is not None
        assert b.is_active is False  # preserved, not deleted
        a = session.get(SqlProviderAccount, account_id_for("claude-pool", "a"))
        assert a is not None
        assert a.is_active is True


def test_sync_deletes_pools_absent_from_config(session_maker: ManagedSessionMaker) -> None:
    sync_pools(session_maker, load_pools(_config([{"name": "a", "claude_config_dir": "~/.a"}])))
    # Re-sync under a different pool name: the old pool row must be removed so
    # it can't keep winning family selection with no active members.
    renamed = {"pools": {"new-pool": {"family": "anthropic", "members": [{"name": "a"}]}}}
    result = sync_pools(session_maker, load_pools(renamed))
    assert result.pools_deleted == 1
    with session_maker() as session:
        assert session.get(SqlCredentialPool, pool_id_for("claude-pool")) is None
        assert session.get(SqlCredentialPool, pool_id_for("new-pool")) is not None
