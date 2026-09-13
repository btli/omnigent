from __future__ import annotations

from detect_staleness import compare_reports


def test_detects_applied_to_skipped_and_preserves_seed_context():
    previous = {
        "applied": [
            {"pr": 5825, "rerere_paths": ["seeded.py"]},
            {"pr": 10},
        ],
        "skipped": [],
    }
    current_skip = {
        "pr": 5825,
        "source": "extra",
        "conflict_paths": ["seeded.py"],
    }

    result = compare_reports(previous, {"applied": [], "skipped": [current_skip]})

    assert result == {
        "regressions": [
            {
                "pr": 5825,
                "previous_rerere_paths": ["seeded.py"],
                "current": current_skip,
            }
        ],
        "new_pins": [],
        "skipped_pins": [current_skip],
    }


def test_detects_first_run_skipped_extra_but_not_previously_seen_pin():
    previous = {
        "applied": [],
        "skipped": [{"pr": 20, "source": "extra"}],
    }
    first_skip = {"pr": 21, "source": "extra", "conflict_paths": ["new.py"]}
    repeated_skip = {"pr": 20, "source": "extra", "conflict_paths": ["old.py"]}

    result = compare_reports(previous, {"skipped": [repeated_skip, first_skip]})

    assert result == {
        "regressions": [],
        "new_pins": [{"pr": 21, "current": first_skip}],
        "skipped_pins": [repeated_skip, first_skip],
    }


def test_persistently_skipped_pin_remains_at_the_current_level():
    skipped = {"pr": 40, "source": "extra", "conflict_paths": ["still-broken.py"]}

    result = compare_reports(
        {"applied": [], "skipped": [skipped]},
        {"applied": [], "skipped": [skipped]},
    )

    assert result == {"regressions": [], "new_pins": [], "skipped_pins": [skipped]}


def test_first_run_without_baseline_flags_skipped_extra():
    skipped = {"pr": 41, "source": "extra", "conflict_paths": ["first-run.py"]}

    result = compare_reports({}, {"applied": [], "skipped": [skipped]})

    assert result == {
        "regressions": [],
        "new_pins": [{"pr": 41, "current": skipped}],
        "skipped_pins": [skipped],
    }


def test_new_open_pr_skip_is_not_classified_as_new_pin():
    result = compare_reports(
        {"applied": [], "skipped": []},
        {"skipped": [{"pr": 30, "source": "open", "conflict_paths": ["open.py"]}]},
    )

    assert result == {"regressions": [], "new_pins": [], "skipped_pins": []}
