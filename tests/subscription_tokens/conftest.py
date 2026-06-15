"""Shared fixtures for subscription-token tests.

Provides a real, migrated SQLite database (file-backed under ``tmp_path``)
and a managed session maker bound to it — the same construction the
production stores use, so repository tests exercise real SQL and FK
behaviour.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.db.utils import (
    ManagedSessionMaker,
    clear_engine_cache,
    get_or_create_engine,
    make_managed_session_maker,
)


@pytest.fixture
def session_maker(tmp_path: Path) -> Iterator[ManagedSessionMaker]:
    """Yield a managed session maker over a fresh migrated SQLite DB."""
    uri = f"sqlite:///{tmp_path / 'subscription_tokens.db'}"
    engine = get_or_create_engine(uri)
    try:
        yield make_managed_session_maker(engine)
    finally:
        clear_engine_cache()


@pytest.fixture
def immediate_session_maker(tmp_path: Path) -> ManagedSessionMaker:
    """A ``BEGIN IMMEDIATE`` session maker over the same DB as *session_maker*.

    Exercises the production cross-process path used by
    :meth:`SqlUsageLimitStateRepository.observe`.
    """
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'subscription_tokens.db'}")
    return make_managed_session_maker(engine, immediate=True)
