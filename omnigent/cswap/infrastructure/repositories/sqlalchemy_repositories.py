"""SQLAlchemy adapters for the cswap application ports.

Concrete, synchronous implementations of the repository / sink / registry
ports backed by the tables in :mod:`omnigent.db.db_models`. Each takes a
:data:`~omnigent.db.utils.ManagedSessionMaker` (commits on success), the
same construction omnigent's other stores use.

The limit-state row stores up to two windows in ``(window_5h_pct,
reset_at_5h)`` and ``(window_7d_pct, reset_at_7d)`` slots. For
subscriptions these are literally the 5h/7d windows; for API keys they
hold the first two reported budgets. Routing reads only aggregate headroom
and earliest reset, so the slot labels do not affect selection.
"""

from __future__ import annotations

from typing import cast

from sqlalchemy import select

from omnigent.cswap.application.ports.ports import (
    CostAttributionSink,
    CredentialPoolRepository,
    SessionCredentialRegistry,
    UsageLimitStateRepository,
)
from omnigent.cswap.domain.entities.credential_pool import CredentialPool
from omnigent.cswap.domain.entities.provider_account import ProviderAccount
from omnigent.cswap.domain.value_objects.enums import (
    AccountKind,
    DetectionSource,
    FailoverMode,
    Family,
)
from omnigent.cswap.domain.value_objects.limit_state import LimitState
from omnigent.cswap.domain.value_objects.usage_window import UsageWindow
from omnigent.db.db_models import (
    SqlCredentialPool,
    SqlProviderAccount,
    SqlProviderAccountCost,
    SqlProviderAccountLimitState,
    SqlSessionCredentialBinding,
)
from omnigent.db.utils import ManagedSessionMaker, now_epoch


def _account_from_row(row: SqlProviderAccount) -> ProviderAccount:
    """Reconstruct a :class:`ProviderAccount` from its DB row."""
    return ProviderAccount(
        id=row.id,
        name=row.name,
        family=cast("Family", row.family),
        kind=cast("AccountKind", row.kind),
        priority=row.priority,
        pool_id=row.pool_id,
        claude_config_dir=row.claude_config_dir,
        codex_config_dir=row.codex_config_dir,
        api_key_ref=row.api_key_ref,
        is_active=row.is_active,
    )


def _limit_state_from_row(row: SqlProviderAccountLimitState) -> LimitState:
    """Reconstruct a :class:`LimitState` from its DB row."""
    windows: list[UsageWindow] = []
    if row.window_5h_pct is not None or row.reset_at_5h is not None:
        windows.append(UsageWindow("5h", row.window_5h_pct, row.reset_at_5h))
    if row.window_7d_pct is not None or row.reset_at_7d is not None:
        windows.append(UsageWindow("7d", row.window_7d_pct, row.reset_at_7d))
    return LimitState(
        credential_id=row.credential_id,
        is_limited=row.is_limited,
        windows=tuple(windows),
        source=cast("DetectionSource | None", row.detection_source),
        last_checked_at=row.last_checked_at,
    )


class SqlUsageLimitStateRepository(UsageLimitStateRepository):
    """:class:`UsageLimitStateRepository` over ``provider_account_limit_states``."""

    def __init__(self, session_maker: ManagedSessionMaker) -> None:
        """:param session_maker: Managed session factory."""
        self._session = session_maker

    def find(self, credential_id: str) -> LimitState | None:
        """Return the stored state for *credential_id*, or ``None``."""
        with self._session() as session:
            row = session.get(SqlProviderAccountLimitState, credential_id)
            return _limit_state_from_row(row) if row is not None else None

    def find_many(self, credential_ids: list[str]) -> dict[str, LimitState]:
        """Return stored states keyed by id (absent ids omitted)."""
        if not credential_ids:
            return {}
        with self._session() as session:
            rows = session.execute(
                select(SqlProviderAccountLimitState).where(
                    SqlProviderAccountLimitState.credential_id.in_(credential_ids)
                )
            ).scalars()
            return {row.credential_id: _limit_state_from_row(row) for row in rows}

    def upsert(self, state: LimitState, *, enforce_staleness: bool = True) -> bool:
        """Write *state*, honouring the staleness guard (see port docs)."""
        with self._session() as session:
            row = session.get(SqlProviderAccountLimitState, state.credential_id)
            if (
                row is not None
                and enforce_staleness
                and row.last_checked_at is not None
                and state.last_checked_at is not None
                and row.last_checked_at > state.last_checked_at
            ):
                return False

            windows = list(state.windows)
            w5 = windows[0] if windows else None
            w7 = windows[1] if len(windows) > 1 else None
            limit_status = (
                "limited"
                if state.is_limited
                else ("available" if (state.source or state.windows) else "unknown")
            )
            now = now_epoch()
            if row is None:
                row = SqlProviderAccountLimitState(
                    credential_id=state.credential_id,
                    limit_status=limit_status,
                    is_limited=state.is_limited,
                    window_5h_pct=w5.utilization_pct if w5 else None,
                    window_7d_pct=w7.utilization_pct if w7 else None,
                    reset_at_5h=w5.reset_at if w5 else None,
                    reset_at_7d=w7.reset_at if w7 else None,
                    detection_source=state.source,
                    last_checked_at=state.last_checked_at,
                    updated_at=now,
                )
                session.add(row)
            else:
                row.limit_status = limit_status
                row.is_limited = state.is_limited
                row.window_5h_pct = w5.utilization_pct if w5 else None
                row.window_7d_pct = w7.utilization_pct if w7 else None
                row.reset_at_5h = w5.reset_at if w5 else None
                row.reset_at_7d = w7.reset_at if w7 else None
                row.detection_source = state.source
                row.last_checked_at = state.last_checked_at
                row.updated_at = now
            return True


class SqlCredentialPoolRepository(CredentialPoolRepository):
    """:class:`CredentialPoolRepository` over the synced pool/account tables."""

    def __init__(self, session_maker: ManagedSessionMaker) -> None:
        """:param session_maker: Managed session factory."""
        self._session = session_maker

    def find_pool_for_family(self, family: str) -> CredentialPool | None:
        """Return the active pool serving *family* (with members), or ``None``."""
        with self._session() as session:
            pool_row = (
                session.execute(
                    select(SqlCredentialPool)
                    .where(SqlCredentialPool.family == family)
                    .order_by(SqlCredentialPool.name)
                )
                .scalars()
                .first()
            )
            if pool_row is None:
                return None
            member_rows = session.execute(
                select(SqlProviderAccount)
                .where(SqlProviderAccount.pool_id == pool_row.id)
                .where(SqlProviderAccount.is_active.is_(True))
                .order_by(SqlProviderAccount.priority, SqlProviderAccount.name)
            ).scalars()
            members = tuple(_account_from_row(r) for r in member_rows)
            return CredentialPool(
                id=pool_row.id,
                name=pool_row.name,
                family=cast("Family", pool_row.family),
                failover_mode=cast("FailoverMode", pool_row.failover_mode),
                members=members,
            )

    def find_account(self, credential_id: str) -> ProviderAccount | None:
        """Return the account for *credential_id*, or ``None``."""
        with self._session() as session:
            row = session.get(SqlProviderAccount, credential_id)
            return _account_from_row(row) if row is not None else None

    def accounts_for_family(self, family: str) -> list[ProviderAccount]:
        """Return all active accounts serving *family*."""
        with self._session() as session:
            rows = session.execute(
                select(SqlProviderAccount)
                .where(SqlProviderAccount.family == family)
                .where(SqlProviderAccount.is_active.is_(True))
                .order_by(SqlProviderAccount.priority, SqlProviderAccount.name)
            ).scalars()
            return [_account_from_row(r) for r in rows]


class SqlSessionCredentialRegistry(SessionCredentialRegistry):
    """:class:`SessionCredentialRegistry` over ``session_credential_bindings``."""

    def __init__(self, session_maker: ManagedSessionMaker) -> None:
        """:param session_maker: Managed session factory."""
        self._session = session_maker

    def bind(self, session_id: str, credential_id: str, family: str) -> None:
        """Record (or rebind) the active account for *session_id*."""
        now = now_epoch()
        with self._session() as session:
            row = session.get(SqlSessionCredentialBinding, session_id)
            if row is None:
                session.add(
                    SqlSessionCredentialBinding(
                        session_id=session_id,
                        credential_id=credential_id,
                        family=family,
                        bound_at=now,
                    )
                )
            else:
                row.credential_id = credential_id
                row.family = family
                row.bound_at = now

    def active_credential(self, session_id: str) -> str | None:
        """Return the active account id for *session_id*, or ``None``."""
        with self._session() as session:
            row = session.get(SqlSessionCredentialBinding, session_id)
            return row.credential_id if row is not None else None


class SqlCostAttributionSink(CostAttributionSink):
    """:class:`CostAttributionSink` over ``provider_account_costs``."""

    def __init__(self, session_maker: ManagedSessionMaker) -> None:
        """:param session_maker: Managed session factory."""
        self._session = session_maker

    def record_credential_cost(
        self,
        credential_id: str,
        day_utc: str,
        *,
        cost_usd: float,
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Increment the per-account, per-day cost rollup (UPSERT add)."""
        now = now_epoch()
        with self._session() as session:
            row = session.get(SqlProviderAccountCost, (credential_id, day_utc))
            if row is None:
                session.add(
                    SqlProviderAccountCost(
                        credential_id=credential_id,
                        day_utc=day_utc,
                        cost_usd=cost_usd,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        turn_count=1,
                        updated_at=now,
                    )
                )
            else:
                row.cost_usd += cost_usd
                row.input_tokens += input_tokens
                row.output_tokens += output_tokens
                row.turn_count += 1
                row.updated_at = now
