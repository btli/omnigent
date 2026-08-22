"""Tests for the shared on-disk model-catalog store (model-flows design §1.2)."""

from __future__ import annotations

import asyncio
import fcntl
import os
import time
from pathlib import Path

import pytest

from omnigent import model_catalog_store as store

_ROWS = [
    {"id": "sonnet", "model": "claude-sonnet-5", "displayName": "Sonnet 5"},
    {
        "id": "opus[1m]",
        "model": "claude-opus-4-8[1m]",
        "displayName": "Opus 4.8 (1M context)",
        "isDefault": True,
    },
]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))


def test_write_then_read_round_trips_verbatim() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_fingerprint_mismatch_is_a_miss_never_a_close_hit() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    assert store.read_catalog("claude-native", "abc124") is None
    assert store.read_catalog("codex-native", "abc123") is None


def test_damaged_file_reads_as_a_miss() -> None:
    path = store.catalog_path("claude-native", "abc123")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json")
    assert store.read_catalog("claude-native", "abc123") is None


def test_rows_without_ids_are_dropped_on_read() -> None:
    store.write_catalog("claude-native", "abc123", [*_ROWS, {"displayName": "no id"}])
    assert store.read_catalog("claude-native", "abc123") == _ROWS


def test_default_row_and_membership_helpers() -> None:
    assert store.default_row(_ROWS) == _ROWS[1]
    assert store.default_row([_ROWS[0]]) is None
    assert store.catalog_contains(_ROWS, "sonnet")
    assert store.catalog_contains(_ROWS, "claude-opus-4-8[1m]")
    assert not store.catalog_contains(_ROWS, "haiku")


def test_catalog_age_reports_and_misses() -> None:
    assert store.catalog_age_s("claude-native", "abc123") is None
    store.write_catalog("claude-native", "abc123", _ROWS)
    age = store.catalog_age_s("claude-native", "abc123")
    assert age is not None and age >= 0.0


@pytest.mark.asyncio
async def test_stale_catalog_is_returned_with_failed_refresh_marked_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    path = store.catalog_path("claude-native", "abc123")
    old = time.time() - store.CATALOG_STALE_AFTER_S - 1
    os.utime(path, (old, old))

    async def _offline() -> None:
        raise OSError("offline")

    result = await store.ensure_catalog("claude-native", "abc123", _offline)

    assert result.rows == _ROWS
    assert result.freshness is store.CatalogFreshness.STALE
    assert result.refresh_error == "provider credentials or network unavailable"


@pytest.mark.asyncio
async def test_hanging_stale_refresh_is_bounded_and_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    path = store.catalog_path("claude-native", "abc123")
    old = time.time() - store.CATALOG_STALE_AFTER_S - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(store, "CATALOG_REFRESH_TIMEOUT_S", 0.02)
    calls = 0

    async def _hang() -> None:
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()

    first, second = await asyncio.gather(
        store.ensure_catalog("claude-native", "abc123", _hang),
        store.ensure_catalog("claude-native", "abc123", _hang),
    )

    assert calls == 1
    assert first.rows == second.rows == _ROWS
    assert first.freshness is second.freshness is store.CatalogFreshness.STALE
    assert first.refresh_error == second.refresh_error == "model catalog refresh timed out"


@pytest.mark.asyncio
async def test_cross_process_lock_wait_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    path = store.catalog_path("claude-native", "abc123")
    old = time.time() - store.CATALOG_STALE_AFTER_S - 1
    os.utime(path, (old, old))
    monkeypatch.setattr(store, "CATALOG_LOCK_TIMEOUT_S", 0.02)
    lock_path = path.with_suffix(".lock")
    lock_path.touch()
    lock_handle = lock_path.open("a+")
    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    called = False

    async def _refresh() -> list[dict[str, object]]:
        nonlocal called
        called = True
        return []

    try:
        result = await store.ensure_catalog("claude-native", "abc123", _refresh)
    finally:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

    assert result.rows == _ROWS
    assert result.freshness is store.CatalogFreshness.STALE
    assert result.refresh_error == "model catalog refresh lock timed out"
    assert not called


@pytest.mark.asyncio
async def test_future_mtime_is_stale_until_revalidated() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    path = store.catalog_path("claude-native", "abc123")
    future = time.time() + store.CATALOG_CLOCK_SKEW_TOLERANCE_S + 1
    os.utime(path, (future, future))
    refreshed = [{"id": "haiku", "model": "claude-haiku-5", "isDefault": True}]

    async def _refresh() -> list[dict[str, object]]:
        return refreshed

    result = await store.ensure_catalog("claude-native", "abc123", _refresh)

    assert result.rows == refreshed
    assert result.freshness is store.CatalogFreshness.FRESH


@pytest.mark.asyncio
async def test_small_future_mtime_is_clamped_fresh() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    path = store.catalog_path("claude-native", "abc123")
    os.utime(path, (time.time() + 1, time.time() + 1))
    calls = 0

    async def _refresh() -> None:
        nonlocal calls
        calls += 1

    result = await store.ensure_catalog("claude-native", "abc123", _refresh)

    assert result.rows == _ROWS
    assert result.freshness is store.CatalogFreshness.FRESH
    assert calls == 0


@pytest.mark.asyncio
async def test_successful_empty_catalog_is_fresh_and_persisted() -> None:
    calls = 0

    async def _empty() -> list[dict[str, object]]:
        nonlocal calls
        calls += 1
        return []

    first = await store.ensure_catalog("claude-native", "abc123", _empty)
    second = await store.ensure_catalog("claude-native", "abc123", _empty)

    assert first.rows == second.rows == []
    assert first.freshness is second.freshness is store.CatalogFreshness.FRESH
    assert first.refresh_error is second.refresh_error is None
    assert calls == 1
