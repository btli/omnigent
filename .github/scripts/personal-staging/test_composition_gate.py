from __future__ import annotations

import composition_gate
import pytest

HEADING = "Staging composition incomplete — nightly not built"


def _report(applied=0, skipped=(), excluded=()):
    return {
        "applied": [{"pr": n, "branch": f"b{n}", "source": "open"} for n in range(applied)],
        "skipped": list(skipped),
        "excluded": list(excluded),
    }


def test_complete_report_has_one_line_summary():
    complete, md = composition_gate.composition_report(_report(applied=3))
    assert complete is True
    assert md.strip() == "Staging composition complete: all 3 entries merged."
    assert HEADING not in md


def test_conflict_skip_lists_paths():
    skip = {
        "pr": 42,
        "branch": "feat/x",
        "source": "open",
        "conflict_paths": ["a/b.py", "c.txt"],
    }
    complete, md = composition_gate.composition_report(_report(applied=2, skipped=[skip]))
    assert complete is False
    assert HEADING in md
    assert "| PR | Source | Branch | Issue |" in md
    assert "| #42 | `open` | `feat/x` | `a/b.py`, `c.txt` |" in md


def test_unfetchable_extra_shows_reason():
    skip = {
        "pr": 7,
        "branch": "fix/y",
        "source": "extra",
        "conflict_paths": [],
        "reason": "pinned head unreachable",
    }
    complete, md = composition_gate.composition_report(_report(skipped=[skip]))
    assert complete is False
    assert "| #7 | `extra` | `fix/y` | `pinned head unreachable` |" in md


def test_branch_pin_uses_branch_name_as_identity():
    skip = {
        "pr": None,
        "branch": "wip/pin",
        "source": "extra-branch",
        "conflict_paths": ["z.py"],
    }
    complete, md = composition_gate.composition_report(_report(skipped=[skip]))
    assert complete is False
    assert "| `wip/pin` | `extra-branch` | `wip/pin` | `z.py` |" in md
    assert "#None" not in md


def test_hostile_branch_name_is_inert():
    skip = {
        "pr": 9,
        "branch": "x`|\n# injected",
        "source": "open",
        "conflict_paths": ["p|q`r"],
    }
    _, md = composition_gate.composition_report(_report(skipped=[skip]))
    row = next(line for line in md.splitlines() if line.startswith("| #9 "))
    # Exactly the 4 cells: the hostile text cannot add columns or lines.
    assert row.count("|") == 5  # 4 cells
    assert "`x'/ # injected`" in row
    assert "\n# injected" not in md


def test_excluded_entries_do_not_make_composition_incomplete():
    report = _report(applied=2, excluded=[{"pr": 5, "reason": "held out via exclude.txt"}])
    complete, md = composition_gate.composition_report(report)
    assert complete is True
    assert "#5" not in md


def test_cli_incomplete(tmp_path, capsys):
    import json

    report = tmp_path / "merge-report.json"
    report.write_text(
        json.dumps(
            _report(skipped=[{"pr": 1, "branch": "a", "source": "open", "conflict_paths": ["f"]}])
        )
    )
    out = tmp_path / "out.md"
    assert composition_gate.main(["--report", str(report), "--summary", str(out)]) == 0
    assert capsys.readouterr().out.strip() == "complete=false"
    assert HEADING in out.read_text()


def test_cli_complete(tmp_path, capsys):
    import json

    report = tmp_path / "merge-report.json"
    report.write_text(json.dumps(_report(applied=1)))
    out = tmp_path / "out.md"
    assert composition_gate.main(["--report", str(report), "--summary", str(out)]) == 0
    assert capsys.readouterr().out.strip() == "complete=true"
    assert "all 1 entries merged" in out.read_text()


def test_cli_requires_arguments():
    with pytest.raises(SystemExit):
        composition_gate.main([])


def _workflow():
    from pathlib import Path

    import yaml

    path = Path(__file__).resolve().parents[2] / "workflows/personal-staging.yml"
    return yaml.safe_load(path.read_text())["jobs"]


def test_workflow_stops_builds_when_composition_incomplete():
    jobs = _workflow()
    assert jobs["integrate"]["outputs"]["complete"] == "${{ steps.gate.outputs.complete }}"
    assert jobs["verify"]["if"] == "needs.integrate.outputs.complete == 'true'"
    # publish also checks the gate directly, not only through the skipped builds
    assert "needs.integrate.outputs.complete == 'true'" in jobs["publish"]["if"]
    assert jobs["android-build"]["needs"] == ["integrate", "verify"]
    assert jobs["desktop-build"]["needs"] == ["integrate", "verify"]


def test_workflow_composition_gate_job_is_secretless_and_fails():
    gate = _workflow()["composition-gate"]
    assert gate["needs"] == "integrate"
    assert gate["if"] == (
        "needs.integrate.result == 'success' && needs.integrate.outputs.complete != 'true'"
    )
    uses = [s.get("uses", "") for s in gate["steps"]]
    assert not any(u.startswith("actions/checkout") for u in uses)
    text = str(gate)
    assert "secrets.HA_NOTIFY_HMAC" in text
    assert text.count("secrets.") == 1
    assert gate["steps"][-1]["run"].rstrip().endswith("exit 1")
    # untrusted values reach run: only through env:
    assert not any("${{" in s.get("run", "") for s in gate["steps"])
