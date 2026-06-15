"""SQLAlchemy adapters for the cswap application ports.

Concrete, synchronous implementations of the repository / sink / registry
ports backed by the tables in :mod:`omnigent.db.db_models`. Each takes a
:data:`~omnigent.db.utils.ManagedSessionMaker` (commits on success), the
same construction omnigent's other stores use.

The limit-state row keeps availability (``is_limited`` + ``limited_until``)
separate from headroom: the usage windows are serialized to a single
``windows_json`` column, preserving each window's own label and count
(subscription ``5h``/``7d``, API-key ``requests``/``tokens``/…). Selection
reads ``is_limited`` + ``limited_until``; the windows are informational.
"""

from __future__ import annotations

import json
from typing import cast

from sqlalchemy import CursorResult, or_, select, update
from sqlalchemy.exc import IntegrityError

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


def _windows_to_json(windows: tuple[UsageWindow, ...]) -> str | None:
    """Serialize usage windows to a JSON array, or ``None`` when empty."""
    if not windows:
        return None
    return json.dumps(
        [
            {"label": w.label, "utilization_pct": w.utilization_pct, "reset_at": w.reset_at}
            for w in windows
        ]
    )


def _opt_int(value: object) -> int | None:
    """Coerce a JSON number to ``int``, or ``None``.

    JSON round-trips can surface a stored integer as a float; the
    :class:`UsageWindow` contract is ``int | None``, so coerce explicitly.
    """
    return int(value) if isinstance(value, (int, float)) else None


def _windows_from_json(raw: str | None) -> tuple[UsageWindow, ...]:
    """Parse a windows JSON array back into :class:`UsageWindow` objects."""
    if not raw:
        return ()
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return ()
    if not isinstance(items, list):
        return ()
    return tuple(
        UsageWindow(
            label=str(item.get("label", "")),
            utilization_pct=_opt_int(item.get("utilization_pct")),
            reset_at=_opt_int(item.get("reset_at")),
        )
        for item in items
        if isinstance(item, dict)
    )


def _rowcount(result: object) -> int:
    """Return the affected-row count of a Core ``execute`` result, or ``0``."""
    return result.rowcount if isinstance(result, CursorResult) else 0


def _status_for(state: LimitState) -> str:
    """Denormalised :data:`LimitStatus` for the stored row.

    Stored at write time as a snapshot for display/filtering; the
    authoritative availability check is :meth:`LimitState.is_available_now`.
    """
    if state.is_limited:
        return "limited"
    return "available" if (state.source or state.windows) else "unknown"


def _limit_state_from_row(row: SqlProviderAccountLimitState) -> LimitState:
    """Reconstruct a :class:`LimitState` from its DB row."""
    return LimitState(
        credential_id=row.credential_id,
        is_limited=row.is_limited,
        limited_until=row.limited_until,
        windows=_windows_from_json(row.windows_json),
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
        """Write *state*, honouring the staleness guard (see port docs).

        The staleness check lives in the ``UPDATE ... WHERE`` clause so it is
        atomic: concurrent observations (the reactive hook runs in worker
        threads) cannot read-then-clobber a fresher row. A missing row is
        inserted via a savepoint, retrying the guarded update if a concurrent
        insert wins the primary-key race.
        """
        cls = SqlProviderAccountLimitState
        values = {
            "limit_status": _status_for(state),
            "is_limited": state.is_limited,
            "limited_until": state.limited_until,
            "windows_json": _windows_to_json(state.windows),
            "detection_source": state.source,
            "last_checked_at": state.last_checked_at,
            "updated_at": now_epoch(),
        }
        guarded = update(cls).where(cls.credential_id == state.credential_id)
        if enforce_staleness and state.last_checked_at is not None:
            guarded = guarded.where(
                or_(cls.last_checked_at.is_(None), cls.last_checked_at <= state.last_checked_at)
            )
        with self._session() as session:
            if _rowcount(session.execute(guarded.values(**values))):
                return True
            # No row updated. If one exists, re-run the guarded UPDATE so the
            # staleness predicate (not a bare existence check) decides the
            # outcome — a row inserted concurrently may be OLDER than ours and
            # must still be overwritten.
            if session.get(cls, state.credential_id) is not None:
                return _rowcount(session.execute(guarded.values(**values))) > 0
            try:
                with session.begin_nested():
                    session.add(cls(credential_id=state.credential_id, **values))
                    session.flush()  # surface a concurrent-insert IntegrityError here
                return True
            except IntegrityError:
                if session.get(cls, state.credential_id) is None:
                    raise  # not a PK race (e.g. an FK violation) — surface it
                return _rowcount(session.execute(guarded.values(**values))) > 0


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
        """Increment the per-account, per-day cost rollup (UPSERT add).

        The increment is a single atomic ``UPDATE ... SET col = col + delta``
        so concurrent turns for the same account/day cannot lose increments.
        The day's first write inserts via a savepoint, retrying the additive
        update if a concurrent insert wins the primary-key race.
        """
        now = now_epoch()
        col = SqlProviderAccountCost
        add = (
            update(col)
            .where(col.credential_id == credential_id, col.day_utc == day_utc)
            .values(
                cost_usd=col.cost_usd + cost_usd,
                input_tokens=col.input_tokens + input_tokens,
                output_tokens=col.output_tokens + output_tokens,
                turn_count=col.turn_count + 1,
                updated_at=now,
            )
        )
        with self._session() as session:
            if _rowcount(session.execute(add)):
                return
            try:
                with session.begin_nested():
                    session.add(
                        col(
                            credential_id=credential_id,
                            day_utc=day_utc,
                            cost_usd=cost_usd,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            turn_count=1,
                            updated_at=now,
                        )
                    )
                    session.flush()  # surface a concurrent-insert IntegrityError here
            except IntegrityError:
                # A concurrent first-insert won the PK race → the additive
                # update now matches. If it still matches nothing, the insert
                # failed for another reason (e.g. an FK violation) — surface it.
                if not _rowcount(session.execute(add)):
                    raise
