"""Shared atomic-UPSERT helper for the cswap SQLAlchemy adapters.

Several cswap writes (config→DB sync of pools/accounts, session bindings)
are plain "insert it, or overwrite it" upserts that must be safe under
concurrent writers across **processes** (the server, runners, and the CLI
all sync/bind against the same machine-global DB). A naive
``session.get`` + ``session.add`` check-then-insert races: two processes
both see no row, and one loses with a primary-key ``IntegrityError``.

:func:`atomic_upsert` does an ``UPDATE`` first; on a miss it inserts inside
a SAVEPOINT and, if a concurrent insert won the primary-key race, retries
the ``UPDATE``. Any other ``IntegrityError`` (e.g. a foreign-key violation)
is surfaced, not swallowed.
"""

from __future__ import annotations

from collections.abc import Mapping

from sqlalchemy import CursorResult, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from omnigent.db.db_models import Base


def rowcount(result: object) -> int:
    """Return the affected-row count of a Core ``execute`` result, or ``0``."""
    return result.rowcount if isinstance(result, CursorResult) else 0


def atomic_upsert(
    session: Session,
    model: type[Base],
    *,
    where: ColumnElement[bool],
    values: Mapping[str, object],
    insert_values: Mapping[str, object],
) -> None:
    """Insert *insert_values*, or update *values* where *where* — atomically.

    :param session: The active session (its outer transaction is preserved).
    :param model: The ORM model class.
    :param where: Primary-key predicate selecting the row to update.
    :param values: Columns to set on UPDATE (no primary key).
    :param insert_values: Full row (incl. primary key) for the INSERT.
    """
    upd = update(model).where(where).values(**values)
    if rowcount(session.execute(upd)):
        return
    try:
        with session.begin_nested():
            session.add(model(**insert_values))
            session.flush()  # surface a concurrent-insert IntegrityError here
    except IntegrityError:
        if not rowcount(session.execute(upd)):
            raise  # not a PK race (e.g. an FK violation) — surface it
