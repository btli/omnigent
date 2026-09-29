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
import os
import time
from pathlib import Path

import psutil

from omnigent.util.json_types import JsonObject
from omnigent.util.tunnel_limits import TUNNEL_KEEPALIVE_PING_INTERVAL_S

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

# Byte counts and rates above this (1 EiB) are garbage, not a real machine.
_MAX_BYTES = 2**60
# A disk reading this old means the read is hung, so it's dropped, not resent.
_DISK_MAX_AGE_S = 3 * TUNNEL_KEEPALIVE_PING_INTERVAL_S


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


def _net_bytes() -> tuple[int, int]:
    """Sum ``(received, sent)`` bytes over interfaces that are up, except loopback.

    Loopback carries the runner's own localhost traffic to the server.
    """
    nics = psutil.net_if_stats()
    counters = [
        io
        for name, io in psutil.net_io_counters(pernic=True).items()
        if (nic := nics.get(name)) is not None and nic.isup and "loopback" not in nic.flags
    ]
    return sum(io.bytes_recv for io in counters), sum(io.bytes_sent for io in counters)


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
        self._disk_read_at = 0.0
        self._disk_task: asyncio.Task[None] | None = None
        self._failure_logged = False

    def sample(self) -> JsonObject | None:
        """Return the current snapshot without blocking.

        :returns: A dict keyed by :data:`HOST_STATS_KEYS` (CPU and network
            appear from the second call on, disk once its first read lands), or
            ``None`` when probing fails — a stats failure must never cost the
            pong it rides on.
        """
        try:
            # One disk read at a time, off-loop. The loop is resolved before the
            # coroutine is created, so an off-loop call leaves none un-awaited.
            if self._disk_task is None or self._disk_task.done():
                self._disk_task = asyncio.get_running_loop().create_task(
                    self._read_disk(), name="host-stats-disk"
                )
            now = time.monotonic()
            cpu = psutil.cpu_percent(interval=None)
            memory = psutil.virtual_memory()
            net = _net_bytes()
            stats: JsonObject = {
                "memory_total_bytes": memory.total,
                "memory_used_bytes": memory.total - memory.available,
            }
            previous_at, previous_net = self._sampled_at, self._net
            self._sampled_at, self._net = now, net
            if previous_at is not None and previous_net is not None:
                # psutil's first non-blocking reading has no baseline and is meaningless.
                stats["cpu_percent"] = cpu
                elapsed = now - previous_at
                if elapsed > 0:
                    # A counter reset (an interface restarting) reads as no traffic.
                    stats["net_rx_bytes_per_s"] = round(max(0, net[0] - previous_net[0]) / elapsed)
                    stats["net_tx_bytes_per_s"] = round(max(0, net[1] - previous_net[1]) / elapsed)
            if self._disk is not None and now - self._disk_read_at <= _DISK_MAX_AGE_S:
                stats["disk_total_bytes"], stats["disk_free_bytes"] = self._disk
            return stats
        except Exception:
            # Never cost the pong: report the first failure loudly, then quietly.
            if self._failure_logged:
                _logger.debug("Host stats sample failed", exc_info=True)
            else:
                self._failure_logged = True
                _logger.exception("Host stats sample failed; pongs will carry no stats")
            return None

    async def _read_disk(self) -> None:
        """Cache the latest disk reading for later samples; a failed read caches none."""
        try:
            disk = await asyncio.to_thread(_disk_usage)
        except Exception:
            disk = None
            if self._failure_logged:
                _logger.debug("Host disk read failed", exc_info=True)
            else:
                self._failure_logged = True
                _logger.exception("Host disk read failed; pongs will carry no disk stats")
        self._disk, self._disk_read_at = disk, time.monotonic()


def parse_host_stats(raw: object) -> dict[str, float] | None:
    """Validate a wire snapshot from a host; never raises.

    Tolerant like the other host-reported fields: a malformed value drops only
    that key, and a snapshot with nothing usable reads as "no stats". CPU is
    clamped to 0-100. Byte values above 1 EiB, and a used or free size larger
    than its total, are dropped as implausible.

    :param raw: The pong's ``host_stats`` value, e.g.
        ``{"cpu_percent": 48.0, "memory_total_bytes": 17179869184}``.
    :returns: The known keys with plausible readings, or ``None`` when *raw* is
        absent, not an object, or has none of them.
    """
    if not isinstance(raw, dict):
        return None
    stats: dict[str, float] = {}
    for key in HOST_STATS_KEYS:
        value = raw.get(key)
        # Comparisons, not float(), so an oversized JSON integer can't raise.
        if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
            continue
        if key == "cpu_percent":
            stats[key] = min(max(value, 0), 100)
        elif 0 <= value <= _MAX_BYTES:
            stats[key] = value
    for part, total in (
        ("memory_used_bytes", "memory_total_bytes"),
        ("disk_free_bytes", "disk_total_bytes"),
    ):
        if part in stats and total in stats and stats[part] > stats[total]:
            del stats[part], stats[total]
    return stats or None
