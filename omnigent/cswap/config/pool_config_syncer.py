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

from omnigent.cswap.domain.entities.credential_pool import CredentialPool
from omnigent.db.db_models import SqlCredentialPool, SqlProviderAccount
from omnigent.db.utils import ManagedSessionMaker, now_epoch


@dataclass(frozen=True)
class SyncResult:
    """Summary of a sync pass.

    :param pools_upserted: Number of pool rows inserted or updated.
    :param accounts_upserted: Number of account rows inserted or updated.
    :param accounts_deactivated: Accounts present in the DB but absent from
        the config, marked inactive.
    """

    pools_upserted: int
    accounts_upserted: int
    accounts_deactivated: int


def sync_pools(session_maker: ManagedSessionMaker, pools: dict[str, CredentialPool]) -> SyncResult:
    """Reconcile DB pool/account rows with the parsed config.

    :param session_maker: Managed session factory (commits on success).
    :param pools: Parsed pools from
        :func:`omnigent.cswap.config.pool_config.load_pools`.
    :returns: A :class:`SyncResult` with the row counts touched.
    """
    now = now_epoch()
    desired_account_ids: set[str] = set()
    pools_upserted = 0
    accounts_upserted = 0
    accounts_deactivated = 0

    with session_maker() as session:
        for pool in pools.values():
            existing_pool = session.get(SqlCredentialPool, pool.id)
            if existing_pool is None:
                session.add(
                    SqlCredentialPool(
                        id=pool.id,
                        name=pool.name,
                        family=pool.family,
                        failover_mode=pool.failover_mode,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing_pool.name = pool.name
                existing_pool.family = pool.family
                existing_pool.failover_mode = pool.failover_mode
                existing_pool.updated_at = now
            pools_upserted += 1

            for member in pool.members:
                desired_account_ids.add(member.id)
                existing = session.get(SqlProviderAccount, member.id)
                if existing is None:
                    session.add(
                        SqlProviderAccount(
                            id=member.id,
                            pool_id=member.pool_id,
                            name=member.name,
                            family=member.family,
                            kind=member.kind,
                            priority=member.priority,
                            claude_config_dir=member.claude_config_dir,
                            codex_config_dir=member.codex_config_dir,
                            api_key_ref=member.api_key_ref,
                            is_active=True,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    existing.pool_id = member.pool_id
                    existing.name = member.name
                    existing.family = member.family
                    existing.kind = member.kind
                    existing.priority = member.priority
                    existing.claude_config_dir = member.claude_config_dir
                    existing.codex_config_dir = member.codex_config_dir
                    existing.api_key_ref = member.api_key_ref
                    existing.is_active = True
                    existing.updated_at = now
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

    return SyncResult(
        pools_upserted=pools_upserted,
        accounts_upserted=accounts_upserted,
        accounts_deactivated=accounts_deactivated,
    )
