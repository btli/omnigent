"""Sync the parsed ``pools:`` config into the DB.

``~/.omnigent/config.yaml`` is the source of truth for which pools and
accounts exist; the DB mirrors them so the limit-state / cost / binding
tables can reference ``provider_accounts.id`` by foreign key. This runs at
server startup and is idempotent (account/pool ids are deterministic).

Accounts that disappear from the config are **deactivated**, not deleted,
so their cost history and limit state survive a config edit.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select

from omnigent.db.db_models import SqlCredentialPool, SqlProviderAccount
from omnigent.db.utils import ManagedSessionMaker, now_epoch
from omnigent.subscription_tokens.domain.entities.credential_pool import CredentialPool
from omnigent.subscription_tokens.infrastructure.sql_upsert import atomic_upsert


@dataclass(frozen=True)
class SyncResult:
    """Summary of a sync pass.

    :param pools_upserted: Number of pool rows inserted or updated.
    :param accounts_upserted: Number of account rows inserted or updated.
    :param accounts_deactivated: Accounts present in the DB but absent from
        the config, marked inactive.
    :param pools_deleted: Pool rows present in the DB but absent from the
        config, deleted.
    """

    pools_upserted: int
    accounts_upserted: int
    accounts_deactivated: int
    pools_deleted: int


def sync_pools(session_maker: ManagedSessionMaker, pools: dict[str, CredentialPool]) -> SyncResult:
    """Reconcile DB pool/account rows with the parsed config.

    :param session_maker: Managed session factory (commits on success).
    :param pools: Parsed pools from
        :func:`omnigent.subscription_tokens.config.pool_config.load_pools`.
    :returns: A :class:`SyncResult` with the row counts touched.
    """
    now = now_epoch()
    desired_account_ids: set[str] = set()
    desired_pool_ids = {pool.id for pool in pools.values()}
    pools_upserted = 0
    accounts_upserted = 0
    accounts_deactivated = 0
    pools_deleted = 0

    with session_maker() as session:
        for pool in pools.values():
            pool_fields = {
                "name": pool.name,
                "family": pool.family,
                "failover_mode": pool.failover_mode,
                "updated_at": now,
            }
            atomic_upsert(
                session,
                SqlCredentialPool,
                where=SqlCredentialPool.id == pool.id,
                values=pool_fields,
                insert_values={"id": pool.id, "created_at": now, **pool_fields},
            )
            pools_upserted += 1

            for member in pool.members:
                desired_account_ids.add(member.id)
                acct_fields = {
                    "pool_id": member.pool_id,
                    "name": member.name,
                    "family": member.family,
                    "kind": member.kind,
                    "priority": member.priority,
                    "claude_config_dir": member.claude_config_dir,
                    "codex_config_dir": member.codex_config_dir,
                    "api_key_ref": member.api_key_ref,
                    "is_active": True,
                    "updated_at": now,
                }
                atomic_upsert(
                    session,
                    SqlProviderAccount,
                    where=SqlProviderAccount.id == member.id,
                    values=acct_fields,
                    insert_values={"id": member.id, "created_at": now, **acct_fields},
                )
                accounts_upserted += 1

        # Deactivate accounts that are still active in the DB but no longer
        # in the config — preserving their cost/limit-state history.
        stale = session.execute(
            select(SqlProviderAccount).where(SqlProviderAccount.is_active.is_(True))
        ).scalars()
        for row in stale:
            if row.id not in desired_account_ids:
                row.is_active = False
                row.updated_at = now
                accounts_deactivated += 1

        # Delete pool rows no longer in the config so a renamed/removed pool
        # can't keep winning family selection with no active members
        # (provider_accounts.pool_id is ON DELETE SET NULL).
        for pool_row in session.execute(select(SqlCredentialPool)).scalars():
            if pool_row.id not in desired_pool_ids:
                session.delete(pool_row)
                pools_deleted += 1

    return SyncResult(
        pools_upserted=pools_upserted,
        accounts_upserted=accounts_upserted,
        accounts_deactivated=accounts_deactivated,
        pools_deleted=pools_deleted,
    )
