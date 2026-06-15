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

import hashlib
import json
from typing import cast

from sqlalchemy import and_, case, or_, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

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
from omnigent.cswap.infrastructure.sql_upsert import atomic_upsert, rowcount
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


def _advisory_lock_key(credential_id: str) -> int:
    """Map a credential id to a stable signed 64-bit PostgreSQL advisory key."""
    return int.from_bytes(hashlib.sha1(credential_id.encode()).digest()[:8], "big", signed=True)


# Source precedence for breaking a same-(second-resolution)-timestamp staleness
# tie: a manual override wins over reactive, which wins over a poller probe.
_SOURCE_PRIORITY = {"manual": 3, "reactive": 2, "poller": 1}


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


def _upsert_limit_state(
    session: Session,
    state: LimitState,
    *,
    enforce_staleness: bool,
    row_exists: bool | None = None,
) -> bool:
    """Guarded-UPSERT a limit-state row within *session*; return whether written.

    The staleness check lives in the ``UPDATE ... WHERE`` clause so it is
    atomic (concurrent writers cannot read-then-clobber a fresher row). A
    missing row is inserted via a SAVEPOINT, retrying the guarded UPDATE if a
    concurrent insert wins the PK race; a non-PK IntegrityError surfaces.

    :param row_exists: Whether a row already exists, when the caller holds a
        per-credential write lock (``observe``) so existence cannot change
        underneath us. Lets the locked path skip the re-query and a provably
        redundant second guarded UPDATE. Unlocked callers pass ``None`` and
        re-check, since a concurrent insert could have added an older row that
        still needs overwriting.
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
        # Reject strictly-newer stored rows. On an equal second-resolution
        # timestamp, break the tie by source precedence (manual > reactive >
        # poller) so a same-second poller "available" can't clobber a fresh
        # reactive "limited".
        stored_priority = case(
            (cls.detection_source == "manual", 3),
            (cls.detection_source == "reactive", 2),
            (cls.detection_source == "poller", 1),
            else_=0,
        )
        guarded = guarded.where(
            or_(
                cls.last_checked_at.is_(None),
                cls.last_checked_at < state.last_checked_at,
                and_(
                    cls.last_checked_at == state.last_checked_at,
                    stored_priority <= _SOURCE_PRIORITY.get(state.source or "", 0),
                ),
            )
        )
    if rowcount(session.execute(guarded.values(**values))):
        return True
    # No row updated: either none exists (insert) or one exists but the
    # staleness guard rejected it. A locked caller (``row_exists`` supplied)
    # already knows which, and no concurrent writer can change it — so a row
    # that exists was simply staleness-rejected (return False), skipping the
    # redundant re-query and re-UPDATE. An unlocked caller re-queries and, if a
    # row now exists, re-runs the guarded UPDATE so the staleness predicate
    # (not a bare existence check) decides — a concurrently-inserted OLDER row
    # must still be overwritten.
    if row_exists is None and session.get(cls, state.credential_id) is not None:
        return rowcount(session.execute(guarded.values(**values))) > 0
    if row_exists:
        return False
    try:
        with session.begin_nested():
            session.add(cls(credential_id=state.credential_id, **values))
            session.flush()  # surface a concurrent-insert IntegrityError here
        return True
    except IntegrityError:
        if session.get(cls, state.credential_id) is None:
            raise  # not a PK race (e.g. an FK violation) — surface it
        return rowcount(session.execute(guarded.values(**values))) > 0


class SqlUsageLimitStateRepository(UsageLimitStateRepository):
    """:class:`UsageLimitStateRepository` over ``provider_account_limit_states``."""

    def __init__(
        self,
        session_maker: ManagedSessionMaker,
        immediate_session_maker: ManagedSessionMaker | None = None,
    ) -> None:
        """:param session_maker: Managed session factory.
        :param immediate_session_maker: A ``BEGIN IMMEDIATE`` factory used by
            :meth:`observe` to serialize its read+write across processes.
            Defaults to *session_maker* (correct in-process; full
            cross-process atomicity needs the immediate variant).
        """
        self._session = session_maker
        self._immediate = immediate_session_maker or session_maker

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
            return _upsert_limit_state(session, state, enforce_staleness=enforce_staleness)

    def observe(self, state: LimitState, *, enforce_staleness: bool = True) -> tuple[bool, bool]:
        """Atomically write *state* and report ``(wrote, prior_was_available)``.

        The prior read and the write are serialized per credential against
        other processes — so two runners reporting the same limit cannot both
        see the account as available and both decide it was "newly limited"
        (double-firing failover). On SQLite that is the immediate session's
        ``BEGIN IMMEDIATE`` write lock; on PostgreSQL it is a per-credential
        transaction-scoped advisory lock taken before the read.
        """
        now = state.last_checked_at if state.last_checked_at is not None else now_epoch()
        with self._immediate() as session:
            if session.bind is not None and session.bind.dialect.name == "postgresql":
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:k)"),
                    {"k": _advisory_lock_key(state.credential_id)},
                )
            prior = session.get(SqlProviderAccountLimitState, state.credential_id)
            was_available = prior is None or _limit_state_from_row(prior).is_available_now(now)
            # The lock (BEGIN IMMEDIATE / advisory) held since before this read
            # means existence cannot change underneath the upsert, so pass it
            # through to skip a redundant guarded re-UPDATE on staleness reject.
            wrote = _upsert_limit_state(
                session,
                state,
                enforce_staleness=enforce_staleness,
                row_exists=prior is not None,
            )
            return wrote, was_available


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
        """Record (or rebind) the active account for *session_id*.

        Atomic UPSERT (UPDATE, else SAVEPOINT INSERT retrying the UPDATE on a
        concurrent-insert race) so two near-simultaneous first binds for the
        same session can't raise an IntegrityError.
        """
        cls = SqlSessionCredentialBinding
        values = {"credential_id": credential_id, "family": family, "bound_at": now_epoch()}
        with self._session() as session:
            atomic_upsert(
                session,
                cls,
                where=cls.session_id == session_id,
                values=values,
                insert_values={"session_id": session_id, **values},
            )

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
        with self._session() as session:
            atomic_upsert(
                session,
                col,
                where=and_(col.credential_id == credential_id, col.day_utc == day_utc),
                values={
                    "cost_usd": col.cost_usd + cost_usd,
                    "input_tokens": col.input_tokens + input_tokens,
                    "output_tokens": col.output_tokens + output_tokens,
                    "turn_count": col.turn_count + 1,
                    "updated_at": now,
                },
                insert_values={
                    "credential_id": credential_id,
                    "day_utc": day_utc,
                    "cost_usd": cost_usd,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "turn_count": 1,
                    "updated_at": now,
                },
            )
