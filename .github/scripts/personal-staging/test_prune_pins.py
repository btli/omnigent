"""Offline tests for the personal-ring pin pruner.

No network and no gh: the API surface is a fake client that records the
calls, so every test asserts on the plan and the deletion order.
"""

from __future__ import annotations

import datetime as dt

import prune_pins as prune
import pytest

TODAY = dt.date(2026, 8, 31)


class FakeGh:
    """Records deletions in call order."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def delete_branch(self, name: str) -> None:
        self.calls.append(f"branch:{name}")

    def delete_tag(self, name: str) -> None:
        self.calls.append(f"tag:{name}")

    def delete_release(self, release_id: int) -> None:
        self.calls.append(f"release:{release_id}")


def test_parses_the_three_pin_shapes():
    assert prune.parse_pin("production-20260821") == (
        "production",
        dt.date(2026, 8, 21),
        (dt.date(2026, 8, 21), 0),
    )
    assert prune.parse_pin("nightly-20260829-rerun4") == (
        "nightly",
        dt.date(2026, 8, 29),
        (dt.date(2026, 8, 29), 4),
    )
    assert prune.parse_pin("v0.12.0.dev20260830") == (
        "dev",
        dt.date(2026, 8, 30),
        (dt.date(2026, 8, 30), (0, 12, 0)),
    )


@pytest.mark.parametrize(
    "name",
    [
        "production-latest",
        "nightly-latest",
        "pr-demos-2026-08-29",
        "v0.1.0",
        "v0.1.0rc4",
        "archive/main-2026-08-14",
        "main",
        "staging",
        "production",
        "homelab",
        "feat/omnigent-production-ring",
        # A datestamp that is not a calendar date is never a pin.
        "nightly-20261332",
        # Neighbouring shapes that must not widen into the pin families.
        "nightly-2026083",
        "production-20260821-rerun",
        "v0.12.0.dev2026083",
    ],
)
def test_never_matches_refs_outside_the_pin_families(name):
    assert prune.parse_pin(name) is None


def test_collect_pairs_refs_and_attaches_releases():
    pins = prune.collect(
        tags=["nightly-20260810", "v0.9.0.dev20260810", "production-latest"],
        branches=["nightly-20260810", "main", "v0.9.0.dev20260810"],
        releases=[{"id": 7, "tag_name": "nightly-20260810"}],
    )
    by_name = {p.name: p for p in pins}
    assert set(by_name) == {"nightly-20260810", "v0.9.0.dev20260810"}

    pin = by_name["nightly-20260810"]
    assert (pin.has_tag, pin.has_branch, pin.release_id) == (True, True, 7)

    # A dev tag has no branch namespace: a same-named branch is not its pin.
    dev = by_name["v0.9.0.dev20260810"]
    assert (dev.has_tag, dev.has_branch, dev.release_id) == (True, False, None)


def test_collect_sweeps_a_half_pin_branch():
    pins = prune.collect(tags=[], branches=["nightly-20260810"], releases=[])
    assert len(pins) == 1
    assert pins[0].is_half
    assert (pins[0].has_branch, pins[0].has_tag) == (True, False)


def test_dev_pin_never_attaches_or_deletes_a_same_named_release():
    old_dev = "v0.9.0.dev20260810"
    pins = prune.collect(
        tags=[old_dev, "v0.12.0.dev20260830"],
        branches=[],
        releases=[{"id": 42, "tag_name": old_dev}],
    )
    pruned = prune.plan(pins, TODAY, keep_per_family=1)
    assert [p.name for p in pruned.delete] == [old_dev]
    assert pruned.delete[0].release_id is None

    gh = FakeGh()
    prune.execute(pruned, gh)
    assert gh.calls == [f"tag:{old_dev}"]


def test_cutoff_is_inclusive_of_the_window_edge():
    pins = prune.collect(
        tags=[f"nightly-2026081{d}" for d in (5, 6, 7, 8)],
        branches=[],
        releases=[],
    )
    pruned = prune.plan(pins, TODAY, keep_days=14, keep_per_family=1)
    assert pruned.cutoff == dt.date(2026, 8, 17)
    # 0817 is exactly 14 days out and survives; 0816 does not.
    assert {p.name for p in pruned.keep_recent} == {"nightly-20260817", "nightly-20260818"}
    assert {p.name for p in pruned.delete} == {"nightly-20260815", "nightly-20260816"}
    # The floor covers the newest pin in the family, which is already inside
    # the window — so a healthy ring never has the floor rescue anything.
    assert pruned.keep_floor == []


def test_floor_is_per_family_and_beats_age():
    # An abandoned ring: every pin is far older than the window.
    pins = prune.collect(
        tags=[f"production-202607{d:02d}" for d in range(1, 9)]
        + [f"nightly-202607{d:02d}" for d in range(1, 9)],
        branches=[],
        releases=[],
    )
    pruned = prune.plan(pins, TODAY, keep_days=14, keep_per_family=3)
    kept = {p.name for p in pruned.keep_floor}
    assert kept == {
        "production-20260708",
        "production-20260707",
        "production-20260706",
        "nightly-20260708",
        "nightly-20260707",
        "nightly-20260706",
    }
    assert not pruned.keep_recent
    assert len(pruned.delete) == 10


def test_floor_orders_reruns_after_their_parent():
    pins = prune.collect(
        tags=["nightly-20260701", "nightly-20260702", "nightly-20260702-rerun1"],
        branches=[],
        releases=[],
    )
    pruned = prune.plan(pins, TODAY, keep_days=14, keep_per_family=1)
    assert [p.name for p in pruned.keep_floor] == ["nightly-20260702-rerun1"]


def test_same_day_dev_tags_order_by_version():
    pins = prune.collect(
        tags=["v0.11.0.dev20260701", "v0.12.0.dev20260701"], branches=[], releases=[]
    )
    pruned = prune.plan(pins, TODAY, keep_days=14, keep_per_family=1)
    assert [p.name for p in pruned.keep_floor] == ["v0.12.0.dev20260701"]


def test_deletes_branch_then_release_then_tag():
    pins = prune.collect(
        tags=["nightly-20260810", "nightly-20260830"],
        branches=["nightly-20260810", "nightly-20260830"],
        releases=[{"id": 42, "tag_name": "nightly-20260810"}],
    )
    pruned = prune.plan(pins, TODAY, keep_per_family=1)
    assert [p.name for p in pruned.delete] == ["nightly-20260810"]
    gh = FakeGh()
    prune.execute(pruned, gh)
    # stage.py's pin_name() hard-errors on a branch without its tag but
    # tolerates a tag without its branch, so the branch must go first.
    assert gh.calls == ["branch:nightly-20260810", "release:42", "tag:nightly-20260810"]


def test_circuit_breaker_aborts_before_any_deletion():
    pins = prune.collect(
        tags=[f"nightly-202606{d:02d}" for d in range(1, 21)], branches=[], releases=[]
    )
    pruned = prune.plan(pins, TODAY, keep_per_family=1)
    gh = FakeGh()
    with pytest.raises(prune.PruneError, match="circuit breaker"):
        prune.execute(pruned, gh, max_delete=5)
    assert gh.calls == []


def test_rejects_a_zero_retention_window():
    with pytest.raises(prune.PruneError):
        prune.plan([], TODAY, keep_days=0)
    with pytest.raises(prune.PruneError):
        prune.plan([], TODAY, keep_per_family=0)


def test_dry_run_client_issues_no_delete_calls(monkeypatch):
    called = []
    monkeypatch.setattr(prune.subprocess, "run", lambda *a, **_kw: called.append(a))
    client = prune.GhClient("btli/omnigent", dry_run=True)
    client.delete_branch("nightly-20260810")
    client.delete_tag("nightly-20260810")
    client.delete_release(1)
    assert called == []


def test_plan_against_the_forks_real_inventory():
    """Snapshot of btli/omnigent on 2026-08-31: the production ring is
    entirely inside the window, so only aged nightly pins and dev tags go."""
    nightly = [f"nightly-202608{d:02d}" for d in range(10, 31)]
    nightly += ["nightly-20260811-rerun1", "nightly-20260811-rerun2", "nightly-20260829-rerun4"]
    production = [f"production-202608{d:02d}" for d in range(21, 31)]
    dev = [f"v0.9.0.dev2026081{d}" for d in (0, 1)]
    dev += [f"v0.10.0.dev2026081{d}" for d in range(2, 9)]
    dev += [f"v0.11.0.dev202608{d:02d}" for d in range(19, 26)]
    dev += [f"v0.12.0.dev202608{d:02d}" for d in range(25, 31)]
    keep = ["production-latest", "nightly-latest", "pr-demos-2026-08-29", "v0.1.0"]

    pins = prune.collect(
        tags=nightly + production + dev + keep,
        branches=nightly + production + ["main", "staging", "production", "homelab"],
        releases=[{"id": i, "tag_name": t} for i, t in enumerate(nightly + production)],
    )
    pruned = prune.plan(pins, TODAY)

    deleted = {p.name for p in pruned.delete}
    # Nothing from the production ring is old enough to retire yet.
    assert not [n for n in deleted if n.startswith("production-")]
    assert deleted == {
        "nightly-20260810",
        "nightly-20260811",
        "nightly-20260811-rerun1",
        "nightly-20260811-rerun2",
        "nightly-20260812",
        "nightly-20260813",
        "nightly-20260814",
        "nightly-20260815",
        "nightly-20260816",
        "v0.9.0.dev20260810",
        "v0.9.0.dev20260811",
        "v0.10.0.dev20260812",
        "v0.10.0.dev20260813",
        "v0.10.0.dev20260814",
        "v0.10.0.dev20260815",
        "v0.10.0.dev20260816",
    }
    # Every retired nightly pin is a complete branch+tag+release triple.
    assert all(
        p.has_branch and p.has_tag and p.release_id is not None
        for p in pruned.delete
        if p.family == "nightly"
    )
    assert pruned.ref_count == 25
    assert pruned.release_count == 9
    # The floating and demo refs were never candidates.
    assert not (deleted & set(keep))


def test_summary_reports_the_plan():
    pins = prune.collect(
        tags=["nightly-20260810", "nightly-20260830"],
        branches=["nightly-20260810", "nightly-20260830"],
        releases=[{"id": 1, "tag_name": "nightly-20260810"}],
    )
    pruned = prune.plan(pins, TODAY, keep_per_family=1)
    text = prune.summarize(pruned, dry_run=True, keep_per_family=1)
    assert "Would delete: **1** pins (2 refs, 1 releases)" in text
    assert "`nightly-20260810`" in text
    assert "2026-08-17" in text
