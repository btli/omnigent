"""Tests for shared owner-private Project assignment resolution."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from omnigent.server.project_assignment import resolve_owned_project_id


@dataclass
class _Project:
    id: str


class _Store:
    def __init__(self, project: _Project | None = None, error: Exception | None = None) -> None:
        self.project = project
        self.error = error
        self.calls: list[tuple[str, str | None]] = []

    def get(self, project_id: str, *, user_id: str | None) -> _Project | None:
        self.calls.append((project_id, user_id))
        if self.error is not None:
            raise self.error
        return self.project


def test_resolve_owned_project_id_returns_canonical_owned_id() -> None:
    store = _Store(_Project("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"))
    assert (
        resolve_owned_project_id(
            store,  # type: ignore[arg-type]
            "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
            user_id="alice@example.com",
        )
        == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )
    assert store.calls == [("AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA", "alice@example.com")]


def test_resolve_owned_project_id_returns_none_when_missing() -> None:
    assert (
        resolve_owned_project_id(
            _Store(),  # type: ignore[arg-type]
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            user_id=None,
        )
        is None
    )


def test_absent_store_is_owned_by_callers_not_shared_helper() -> None:
    with pytest.raises(AttributeError):
        resolve_owned_project_id(  # type: ignore[arg-type]
            None,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            user_id=None,
        )


def test_resolve_owned_project_id_propagates_store_exception() -> None:
    error = RuntimeError("database unavailable")
    with pytest.raises(RuntimeError, match="database unavailable"):
        resolve_owned_project_id(
            _Store(error=error),  # type: ignore[arg-type]
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            user_id="alice@example.com",
        )
