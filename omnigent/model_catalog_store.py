"""The shared on-disk model-catalog store (model-flows-design.md §1.2).

One probe result, many consumers: whoever ran a harness's ``list_models``
(the host at boot, the runner at launch when the file is absent, a live
codex session writing back) persists the catalog here, keyed by harness and
a launch-config fingerprint, and every surface — the pre-launch picker, the
in-session gear, launch resolution and validation — reads the same bytes.
Because writer and readers share one file, host/runner drift and
probe-vs-session mismatch are impossible by construction.

The store holds only verbatim harness answers; nothing else ever writes it.
A fingerprint mismatch is a miss (never a "close enough" hit), so an answer
probed under one config can never serve another.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

_logger = logging.getLogger(__name__)


def fingerprint_of(*parts: object) -> str:
    """
    Stable fingerprint of a resolved harness configuration.

    :param parts: Hashable configuration facets — resolved overrides, env
        pairs, binary identity. Stringified in order.
    :returns: A short hex digest.
    """
    digest = hashlib.sha256()
    for part in parts:
        digest.update(repr(part).encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest()[:16]


#: Catalog entries at or below this age are authoritative cache hits. Older
#: entries are retained only as a fallback while the store refreshes them.
CATALOG_STALE_AFTER_S = 3600.0


class CatalogFreshness(Enum):
    """Authority state of catalog rows returned by a probe."""

    FRESH = "fresh"
    STALE = "stale"
    MISSING = "missing"


class CatalogRefreshFailureKind(Enum):
    """Sanitized reason a live catalog refresh did not produce rows."""

    AUTH = "auth"
    TIMEOUT = "timeout"
    CLI_ABSENT = "cli_absent"
    EMPTY = "empty"
    OTHER = "other"


class CatalogRefreshError(Exception):
    """Structured refresh failure safe to surface in launch diagnostics."""

    def __init__(self, kind: CatalogRefreshFailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


@dataclass(frozen=True)
class CatalogResult:
    """Catalog rows together with their authority state."""

    rows: list[dict[str, Any]] | None
    freshness: CatalogFreshness
    refresh_error: CatalogRefreshError | None = None


def _data_dir() -> Path:
    """Return the omnigent data dir (must stay in lock-step with
    ``omnigent.host.local_server._local_data_dir`` /
    ``omnigent.chat._omnigent_persistent_dir``).

    :returns: ``$OMNIGENT_DATA_DIR`` when set, else ``~/.omnigent``.
    """
    value = os.environ.get("OMNIGENT_DATA_DIR")
    if value:
        return Path(value).expanduser()
    return Path.home() / ".omnigent"


def catalog_path(harness: str, fingerprint: str) -> Path:
    """Return the catalog file path for one (harness, fingerprint).

    :param harness: Canonical harness name, e.g. ``"claude-native"``.
    :param fingerprint: The launch-config fingerprint (:func:`fingerprint_of`).
    :returns: ``<data-dir>/cache/model-catalogs/<harness>-<fingerprint>.json``.
    """
    return _data_dir() / "cache" / "model-catalogs" / f"{harness}-{fingerprint}.json"


def read_catalog(harness: str, fingerprint: str) -> list[dict[str, Any]] | None:
    """Read the stored catalog rows for one (harness, fingerprint).

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :returns: The verbatim rows, or ``None`` on a miss / damaged file.
    """
    path = catalog_path(harness, fingerprint)
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    rows = payload.get("models") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def catalog_age_s(harness: str, fingerprint: str) -> float | None:
    """Age of the stored catalog in seconds, or ``None`` on a miss."""
    path = catalog_path(harness, fingerprint)
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def catalog_is_stale(harness: str, fingerprint: str) -> bool:
    """
    Whether the stored catalog is older than :data:`CATALOG_STALE_AFTER_S`.

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :returns: ``True`` for an entry past the TTL. ``False`` for a fresh
        entry — and for no entry at all, since rows served without a file
        can only have come from a probe that just ran.
    """
    age = catalog_age_s(harness, fingerprint)
    return age is not None and age > CATALOG_STALE_AFTER_S


def write_catalog(harness: str, fingerprint: str, rows: list[dict[str, Any]]) -> None:
    """Persist catalog rows atomically (best-effort; failures only log).

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :param rows: Verbatim harness rows to persist.
    """
    path = catalog_path(harness, fingerprint)
    payload = {
        "harness": harness,
        "fingerprint": fingerprint,
        "written_at": time.time(),
        "models": rows,
    }
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as tmp:
                json.dump(payload, tmp, separators=(",", ":"))
            os.replace(tmp_name, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
    except OSError:
        _logger.warning("could not persist the %s model catalog", harness, exc_info=True)


#: In-flight probes, keyed (harness, fingerprint) — the thin single-flight
#: wrapper the design keeps process-side: concurrent misses join one probe
#: instead of each spawning CLI processes.
_inflight: dict[tuple[str, str], asyncio.Task[list[dict[str, Any]] | None]] = {}


async def ensure_catalog(
    harness: str,
    fingerprint: str,
    resolve: Callable[[], Awaitable[list[dict[str, Any]] | None]],
) -> list[dict[str, Any]] | None:
    """Store-first catalog access with stale-while-revalidate semantics.

    Any cache hit serves immediately. An over-age hit starts a single-flight
    background refresh, while a miss waits for that same shared probe.

    :param harness: Canonical harness name.
    :param fingerprint: The launch-config fingerprint.
    :param resolve: Probe coroutine factory producing verbatim rows.
    :returns: Catalog rows, or ``None`` when no catalog could be obtained.
    """
    cached = read_catalog(harness, fingerprint)
    if cached is not None:
        if catalog_is_stale(harness, fingerprint):
            _refresh_in_background(harness, fingerprint, resolve)
        return cached
    return await asyncio.shield(_start_refresh(harness, fingerprint, resolve))


def _start_refresh(
    harness: str,
    fingerprint: str,
    resolve: Callable[[], Awaitable[list[dict[str, Any]] | None]],
) -> asyncio.Task[list[dict[str, Any]] | None]:
    """Return the single in-flight refresh task for one catalog key."""
    key = (harness, fingerprint)
    task = _inflight.get(key)
    if task is not None and not task.done():
        return task

    async def _run() -> list[dict[str, Any]] | None:
        try:
            rows = await resolve()
            if rows:
                write_catalog(harness, fingerprint, rows)
            return rows
        finally:
            _inflight.pop(key, None)

    task = asyncio.create_task(_run(), name=f"model-catalog-{harness}")
    _inflight[key] = task
    return task


def _refresh_in_background(
    harness: str,
    fingerprint: str,
    resolve: Callable[[], Awaitable[list[dict[str, Any]] | None]],
) -> None:
    """Start a sanitized fire-and-forget refresh for stale shared reads."""
    key = (harness, fingerprint)
    task = _inflight.get(key)
    if task is not None and not task.done():
        return
    task = _start_refresh(harness, fingerprint, resolve)

    def _consume_result(done: asyncio.Task[list[dict[str, Any]] | None]) -> None:
        try:
            done.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001 — stale rows remain available
            failure = _refresh_failure(exc)
            _logger.warning(
                "background %s catalog refresh failed (%s)",
                harness,
                failure.kind.value,
            )

    task.add_done_callback(_consume_result)


def _refresh_failure(
    exc: Exception,
) -> CatalogRefreshError:
    """Return a structured failure without provider response contents."""
    if isinstance(exc, CatalogRefreshError):
        return exc
    if isinstance(exc, TimeoutError):
        return CatalogRefreshError(
            CatalogRefreshFailureKind.TIMEOUT,
            "model catalog refresh timed out",
        )
    if isinstance(exc, FileNotFoundError):
        return CatalogRefreshError(
            CatalogRefreshFailureKind.CLI_ABSENT,
            "model catalog refresh could not launch the provider CLI",
        )
    return CatalogRefreshError(
        CatalogRefreshFailureKind.OTHER,
        "model catalog refresh could not reach the provider",
    )


def _empty_refresh_failure() -> CatalogRefreshError:
    return CatalogRefreshError(
        CatalogRefreshFailureKind.EMPTY,
        "model catalog refresh returned no models",
    )


async def ensure_authoritative_catalog_result(
    harness: str,
    fingerprint: str,
    resolve: Callable[[], Awaitable[list[dict[str, Any]] | None]],
) -> CatalogResult:
    """Synchronously refresh stale rows and return authority provenance.

    A recent cache or successful live probe is fresh. An over-age cache is
    re-probed; if the probe fails, its existing rows are returned stale with
    the sanitized failure. A failed cold probe is missing with that failure.
    Callers must opt into waiting; shared catalog reads use :func:`ensure_catalog`.
    """
    cached = read_catalog(harness, fingerprint)
    age = catalog_age_s(harness, fingerprint) if cached is not None else None
    if cached is not None and age is not None and age <= CATALOG_STALE_AFTER_S:
        return CatalogResult(cached, CatalogFreshness.FRESH)

    task = _start_refresh(harness, fingerprint, resolve)
    try:
        rows = await asyncio.shield(task)
    except (CatalogRefreshError, TimeoutError, OSError) as exc:
        failure = _refresh_failure(exc)
    else:
        if rows:
            return CatalogResult(rows, CatalogFreshness.FRESH)
        failure = _empty_refresh_failure()
    if cached is not None:
        return CatalogResult(cached, CatalogFreshness.STALE, failure)
    return CatalogResult(None, CatalogFreshness.MISSING, failure)


def default_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the catalog's single ``isDefault`` row, if any.

    :param rows: Catalog rows.
    :returns: The default row, or ``None``.
    """
    return next((row for row in rows if row.get("isDefault") is True), None)


def catalog_contains(rows: list[dict[str, Any]], token: str) -> bool:
    """Whether *token* names a catalog row (by ``id`` or wire ``model``).

    :param rows: Catalog rows.
    :param token: A picker row id or wire model id.
    :returns: ``True`` when some row's ``id`` or ``model`` equals *token*.
    """
    return any(row.get("id") == token or row.get("model") == token for row in rows)


__all__ = [
    "CATALOG_STALE_AFTER_S",
    "CatalogFreshness",
    "CatalogRefreshError",
    "CatalogRefreshFailureKind",
    "CatalogResult",
    "catalog_age_s",
    "catalog_contains",
    "catalog_is_stale",
    "catalog_path",
    "default_row",
    "ensure_authoritative_catalog_result",
    "ensure_catalog",
    "fingerprint_of",
    "read_catalog",
    "write_catalog",
]
