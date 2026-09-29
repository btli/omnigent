"""Tests for the host resource snapshot carried on keepalive pongs."""

from __future__ import annotations

import asyncio
from collections import namedtuple
from pathlib import Path

import psutil
import pytest

from omnigent.host import stats as stats_module
from omnigent.host.stats import HostStatsSampler, parse_host_stats

_Memory = namedtuple("_Memory", ["total", "available"])
_Net = namedtuple("_Net", ["bytes_recv", "bytes_sent"])
_Disk = namedtuple("_Disk", ["total", "free"])

_GIB = 1024**3


class _FakeProbes:
    """Scripted psutil readings, advanced one step per ``sample()`` call."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.now = 1000.0
        self.cpu = [0.0, 48.0, 12.5]
        self.net = [_Net(1_000, 500), _Net(61_000, 3_500)]
        self.disk_paths: list[str] = []
        monkeypatch.setattr(stats_module.time, "monotonic", lambda: self.now)
        monkeypatch.setattr(psutil, "cpu_percent", self._cpu_percent)
        monkeypatch.setattr(psutil, "virtual_memory", lambda: _Memory(16 * _GIB, 4 * _GIB))
        monkeypatch.setattr(psutil, "net_io_counters", lambda: self.net.pop(0))
        monkeypatch.setattr(psutil, "disk_usage", self._disk_usage)

    def _cpu_percent(self, interval: float | None = None) -> float:
        # A blocking interval would stall the pong; the sampler must never pass one.
        assert interval is None
        return self.cpu.pop(0)

    def _disk_usage(self, path: str) -> _Disk:
        self.disk_paths.append(path)
        return _Disk(500 * 10**9, 180 * 10**9)


async def _settle_disk_read(sampler: HostStatsSampler) -> None:
    """Let the background disk read started by ``sample()`` finish."""
    task = sampler._disk_task
    assert task is not None
    await asyncio.wait_for(task, timeout=2)


async def test_first_sample_primes_cpu_and_network_baselines(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil's first non-blocking CPU reading is meaningless, so it is withheld."""
    _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()

    assert sampler.sample() == {
        "memory_total_bytes": 16 * _GIB,
        "memory_used_bytes": 12 * _GIB,
    }
    await _settle_disk_read(sampler)


async def test_second_sample_reports_cpu_and_network_throughput(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Network is bytes per second from counter deltas, not link capacity."""
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path))
    probes = _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)
    probes.now += 30.0

    assert sampler.sample() == {
        "cpu_percent": 48.0,
        "memory_total_bytes": 16 * _GIB,
        "memory_used_bytes": 12 * _GIB,
        "net_rx_bytes_per_s": 2_000,
        "net_tx_bytes_per_s": 100,
        "disk_total_bytes": 500 * 10**9,
        "disk_free_bytes": 180 * 10**9,
    }
    assert probes.disk_paths[0] == str(tmp_path)


async def test_disk_falls_back_to_home_without_a_workspace_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unset or missing runner workspace root measures the home filesystem."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OMNIGENT_RUNNER_OS_ENV_ROOT", str(tmp_path / "missing"))
    probes = _FakeProbes(monkeypatch)
    sampler = HostStatsSampler()
    sampler.sample()
    await _settle_disk_read(sampler)

    assert probes.disk_paths == [str(tmp_path)]


async def test_hung_disk_read_never_delays_a_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    """The disk stat runs off-loop, one at a time, so a dead mount can't stall pongs."""
    probes = _FakeProbes(monkeypatch)
    probes.cpu.append(7.0)
    probes.net.append(_Net(61_000, 3_500))
    release = asyncio.Event()
    reads = 0

    async def hung_read() -> None:
        nonlocal reads
        reads += 1
        await release.wait()

    sampler = HostStatsSampler()
    monkeypatch.setattr(sampler, "_read_disk", hung_read)
    samples = []
    for _ in range(3):
        samples.append(sampler.sample())
        # Yield so any disk read a sample started gets to run and be counted.
        await asyncio.sleep(0)
        probes.now += 30.0

    assert samples[1] is not None
    assert "disk_total_bytes" not in samples[1]
    assert reads == 1
    release.set()
    await _settle_disk_read(sampler)


async def test_probe_failure_yields_no_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing probe drops the snapshot instead of failing the pong it rides on."""
    _FakeProbes(monkeypatch)

    def broken() -> None:
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "virtual_memory", broken)
    sampler = HostStatsSampler()

    assert sampler.sample() is None
    await _settle_disk_read(sampler)


def test_sample_needs_the_event_loop_but_does_not_raise() -> None:
    """Called off the loop, the sampler reports no stats rather than raising."""
    assert HostStatsSampler().sample() is None


def test_parse_host_stats_keeps_only_valid_known_readings() -> None:
    """A malformed value drops just that key; unknown keys never pass through."""
    assert parse_host_stats(
        {
            "cpu_percent": 48,
            "memory_total_bytes": 17_179_869_184,
            "memory_used_bytes": -1,
            "disk_free_bytes": "lots",
            "disk_total_bytes": True,
            "net_rx_bytes_per_s": float("inf"),
            "net_tx_bytes_per_s": 310.5,
            "hostname": "bryan-mbp",
        }
    ) == {
        "cpu_percent": 48,
        "memory_total_bytes": 17_179_869_184,
        "net_tx_bytes_per_s": 310.5,
    }


@pytest.mark.parametrize("raw", [None, [], "stats", {}, {"hostname": "bryan-mbp"}])
def test_parse_host_stats_reads_unusable_payloads_as_no_stats(raw: object) -> None:
    """Absence, non-objects and objects with nothing usable all mean no stats."""
    assert parse_host_stats(raw) is None
