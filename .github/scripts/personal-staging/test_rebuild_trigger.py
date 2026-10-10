from __future__ import annotations

import rebuild_trigger as rt

SETTLE = 30 * 60
NOW = 1_800_000_000


def test_newest_nightly_orders_reruns_numerically():
    tags = ["nightly-20261009", "nightly-20261010-rerun2", "nightly-20261010-rerun10", "production-20261011"]
    assert rt.newest_nightly(tags) == "nightly-20261010-rerun10"
    assert rt.newest_nightly(["production-20261010"]) is None


def test_composed_heads_counts_only_the_open_stream():
    report = {"applied": [
        {"pr": 1, "oid": "a" * 40, "source": "open"},
        {"pr": 2, "oid": "b" * 40, "source": "extra"},
        {"pr": 0, "oid": "c" * 40, "source": "upstream"},
    ]}
    assert rt.composed_heads(report) == {1: "a" * 40}


def test_diff_reports_updates_openings_and_closures():
    composed = {1: "a" * 40, 2: "b" * 40, 3: "c" * 40}
    current = {1: "a" * 40, 2: "d" * 40, 4: "e" * 40}
    assert rt.diff(current, composed) == [
        {"pr": 2, "kind": "updated", "oid": "d" * 40},
        {"pr": 3, "kind": "closed", "oid": None},
        {"pr": 4, "kind": "opened", "oid": "e" * 40},
    ]


def test_unchanged_prs_never_rebuild():
    assert rt.plan([], {}, NOW, SETTLE)["action"] == "noop"


def test_a_fresh_push_waits_out_the_settle_window():
    changes = [{"pr": 2, "kind": "updated", "oid": "d" * 40}]
    out = rt.plan(changes, {"d" * 40: NOW - 10 * 60}, NOW, SETTLE)
    assert out["action"] == "wait"
    assert out["settle_at"] == NOW + 20 * 60
    assert "20 min left" in out["reason"]


def test_the_newest_push_in_a_burst_resets_the_clock():
    changes = [
        {"pr": 2, "kind": "updated", "oid": "d" * 40},
        {"pr": 4, "kind": "opened", "oid": "e" * 40},
    ]
    times = {"d" * 40: NOW - 3 * 3600, "e" * 40: NOW - 5 * 60}
    assert rt.plan(changes, times, NOW, SETTLE)["action"] == "wait"


def test_settled_changes_rebuild():
    changes = [{"pr": 2, "kind": "updated", "oid": "d" * 40}]
    out = rt.plan(changes, {"d" * 40: NOW - SETTLE}, NOW, SETTLE)
    assert out["action"] == "rebuild"
    assert "#2 updated" in out["reason"]


def test_a_closed_pr_alone_is_already_settled():
    changes = [{"pr": 3, "kind": "closed", "oid": None}]
    assert rt.plan(changes, {}, NOW, SETTLE)["action"] == "rebuild"
