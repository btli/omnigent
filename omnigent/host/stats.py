"""Host resource snapshot piggybacked on the tunnel keepalive ``pong``.

The server pings every host tunnel on a fixed cadence and the host answers with
a ``pong``; the snapshot rides on that existing reply, so the web UI gets CPU,
memory, disk and network readings with no extra request, loop or endpoint. The
host samples only when the ping asks (the server's ``host_stats`` release
feature). Every key is optional on the wire: older hosts send no snapshot at
all, and readers treat absence as "no stats".
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from pathlib import Path

import psutil

from omnigent.util.json_types import JsonObject

_logger = logging.getLogger(__name__)

# Wire keys, all non-negative numbers: a 0-100 percent, byte counts, and
# bytes-per-second rates averaged since the previous sample.
HOST_STATS_KEYS = (
    "cpu_percent",
    "memory_total_bytes",
    "memory_used_bytes",
    "disk_total_bytes",
    "disk_free_bytes",
    "net_rx_bytes_per_s",
    "net_tx_bytes_per_s",
)


def _disk_usage() -> tuple[int, int] | None:
    """Read ``(total, free)`` bytes for the filesystem holding the workspaces.

    That is the runner workspace root when one is configured, else the home
    directory. Runs in a worker thread: a stat on a dead mount can hang.

    :returns: ``(total, free)`` in bytes, or ``None`` when the path is unreadable.
    """
    root = os.environ.get("OMNIGENT_RUNNER_OS_ENV_ROOT")
    path = root if root and os.path.isdir(root) else str(Path.home())
    try:
        usage = psutil.disk_usage(path)
    except OSError:
        return None
    return usage.total, usage.free


class HostStatsSampler:
    """Cheap, non-blocking sampler for the keepalive pong.

    CPU and network rates are deltas against the previous :meth:`sample` call
    (psutil keys the CPU baseline by thread), so always sample on the event
    loop thread. The disk read runs in a worker thread and lands in a later
    sample, so a hung mount can never delay the pong.
    """

    def __init__(self) -> None:
        """Start with no baseline; the first sample only primes the deltas."""
        self._sampled_at: float | None = None
        self._net: tuple[int, int] | None = None
        self._disk: tuple[int, int] | None = None
        self._disk_task: asyncio.Task[None] | None = None

    def sample(self) -> JsonObject | None:
        """Return the current snapshot without blocking.

        :returns: A dict keyed by :data:`HOST_STATS_KEYS` (CPU and network
            appear from the second call on, disk once its first read lands), or
            ``None`` when probing fails — a stats failure must never cost the
            pong it rides on.
        """
        try:
            return self._sample()
        except (psutil.Error, OSError, RuntimeError):
            # RuntimeError also covers a call made with no running event loop.
            _logger.debug("Host stats sample failed", exc_info=True)
            return None

    def _sample(self) -> JsonObject:
        """Probe psutil once; see :meth:`sample`."""
        self._start_disk_read()
        now = time.monotonic()
        cpu = psutil.cpu_percent(interval=None)
        memory = psutil.virtual_memory()
        counters = psutil.net_io_counters()
        net = (counters.bytes_recv, counters.bytes_sent) if counters is not None else None
        stats: JsonObject = {
            "memory_total_bytes": memory.total,
            "memory_used_bytes": memory.total - memory.available,
        }
        previous_at, previous_net = self._sampled_at, self._net
        self._sampled_at, self._net = now, net
        if previous_at is not None:
            # psutil's first non-blocking reading has no baseline and is meaningless.
            stats["cpu_percent"] = cpu
            elapsed = now - previous_at
            if net is not None and previous_net is not None and elapsed > 0:
                stats["net_rx_bytes_per_s"] = round(max(0, net[0] - previous_net[0]) / elapsed)
                stats["net_tx_bytes_per_s"] = round(max(0, net[1] - previous_net[1]) / elapsed)
        if self._disk is not None:
            stats["disk_total_bytes"], stats["disk_free_bytes"] = self._disk
        return stats

    def _start_disk_read(self) -> None:
        """Refresh the cached disk reading off-loop, one read at a time."""
        if self._disk_task is None or self._disk_task.done():
            # Resolve the loop first so an off-loop call raises before a
            # coroutine is created and left un-awaited.
            loop = asyncio.get_running_loop()
            self._disk_task = loop.create_task(self._read_disk(), name="host-stats-disk")

    async def _read_disk(self) -> None:
        """Store the latest disk reading for the next sample."""
        self._disk = await asyncio.to_thread(_disk_usage)


def parse_host_stats(raw: object) -> dict[str, float] | None:
    """Validate a wire snapshot from a host.

    Tolerant like the other host-reported fields: a malformed value drops only
    that key, and a snapshot with nothing usable reads as "no stats".

    :param raw: The pong's ``host_stats`` value, e.g.
        ``{"cpu_percent": 48.0, "memory_total_bytes": 17179869184}``.
    :returns: The known keys whose values are finite non-negative numbers, or
        ``None`` when *raw* is absent, not an object, or has none of them.
    """
    if not isinstance(raw, dict):
        return None
    stats: dict[str, float] = {
        key: value
        for key in HOST_STATS_KEYS
        if isinstance(value := raw.get(key), int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    }
    return stats or None
