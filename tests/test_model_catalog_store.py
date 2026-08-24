"""Tests for the shared on-disk model-catalog store (model-flows design §1.2)."""

from __future__ import annotations

import asyncio
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


def _make_catalog_stale() -> None:
    path = store.catalog_path("claude-native", "abc123")
    stale_time = time.time() - store.CATALOG_STALE_AFTER_S - 1.0
    os.utime(path, (stale_time, stale_time))


@pytest.mark.asyncio
async def test_catalog_result_recent_cache_hit_is_fresh_without_a_probe() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    calls: list[int] = []

    async def _resolve() -> list[dict[str, object]]:
        calls.append(1)
        return [{"id": "new"}]

    result = await store.ensure_catalog_result("claude-native", "abc123", _resolve)

    assert result == store.CatalogResult(_ROWS, store.CatalogFreshness.FRESH)
    assert calls == []


@pytest.mark.asyncio
async def test_catalog_result_cold_cache_miss_probes_and_persists_fresh_rows() -> None:
    calls: list[int] = []

    async def _resolve() -> list[dict[str, object]]:
        calls.append(1)
        return _ROWS

    result = await store.ensure_catalog_result("claude-native", "abc123", _resolve)

    assert result == store.CatalogResult(_ROWS, store.CatalogFreshness.FRESH)
    assert calls == [1]
    assert store.read_catalog("claude-native", "abc123") == _ROWS


@pytest.mark.asyncio
async def test_catalog_result_stale_cached_hit_refreshes_to_fresh_rows() -> None:
    old_rows = [{"id": "old", "model": "claude-old"}]
    store.write_catalog("claude-native", "abc123", old_rows)
    _make_catalog_stale()

    async def _resolve() -> list[dict[str, object]]:
        return _ROWS

    result = await store.ensure_catalog_result("claude-native", "abc123", _resolve)

    assert result == store.CatalogResult(_ROWS, store.CatalogFreshness.FRESH)
    assert store.read_catalog("claude-native", "abc123") == _ROWS


@pytest.mark.asyncio
async def test_catalog_result_stale_cached_miss_keeps_rows_with_empty_failure() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    _make_catalog_stale()

    async def _empty() -> list[dict[str, object]] | None:
        return None

    result = await store.ensure_catalog_result("claude-native", "abc123", _empty)

    assert result.rows == _ROWS
    assert result.freshness is store.CatalogFreshness.STALE
    assert result.refresh_error is not None
    assert result.refresh_error.kind is store.CatalogRefreshFailureKind.EMPTY
    assert result.refresh_error.message == "model catalog refresh returned no models"


@pytest.mark.asyncio
async def test_catalog_result_refresh_failure_with_cache_preserves_rows_and_provenance() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    _make_catalog_stale()

    async def _unauthorized() -> list[dict[str, object]]:
        raise store.CatalogRefreshError(
            store.CatalogRefreshFailureKind.AUTH,
            "Claude model catalog authentication failed",
        )

    result = await store.ensure_catalog_result("claude-native", "abc123", _unauthorized)

    assert result.rows == _ROWS
    assert result.freshness is store.CatalogFreshness.STALE
    assert result.refresh_error is not None
    assert result.refresh_error.kind is store.CatalogRefreshFailureKind.AUTH
    assert result.refresh_error.message == "Claude model catalog authentication failed"
    assert store.read_catalog("claude-native", "abc123") == _ROWS


@pytest.mark.asyncio
async def test_catalog_result_refresh_failure_without_cache_is_sanitized_and_missing() -> None:
    async def _offline() -> list[dict[str, object]]:
        raise OSError("secret provider response")

    result = await store.ensure_catalog_result("claude-native", "abc123", _offline)

    assert result.rows is None
    assert result.freshness is store.CatalogFreshness.MISSING
    assert result.refresh_error is not None
    assert result.refresh_error.kind is store.CatalogRefreshFailureKind.OTHER
    assert result.refresh_error.message == "model catalog refresh could not reach the provider"
    assert "secret provider response" not in result.refresh_error.message


@pytest.mark.asyncio
async def test_catalog_result_coalesces_stale_refresh_failure_with_consistent_provenance() -> None:
    store.write_catalog("claude-native", "abc123", _ROWS)
    _make_catalog_stale()
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def _timeout() -> list[dict[str, object]]:
        calls.append(1)
        started.set()
        await release.wait()
        raise store.CatalogRefreshError(
            store.CatalogRefreshFailureKind.TIMEOUT,
            "Claude model catalog refresh timed out",
        )

    first = asyncio.create_task(store.ensure_catalog_result("claude-native", "abc123", _timeout))
    await started.wait()
    second = asyncio.create_task(store.ensure_catalog_result("claude-native", "abc123", _timeout))
    release.set()
    first_result, second_result = await asyncio.gather(first, second)

    assert calls == [1]
    for result in (first_result, second_result):
        assert result.rows == _ROWS
        assert result.freshness is store.CatalogFreshness.STALE
        assert result.refresh_error is not None
        assert result.refresh_error.kind is store.CatalogRefreshFailureKind.TIMEOUT
        assert result.refresh_error.message == "Claude model catalog refresh timed out"
