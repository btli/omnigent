"""Unit tests for hourly sync-main push soft-fail classification.

Covers ``is_stale_ref_push_rejection``: the safety-critical path that decides
whether a failed ``git push --porcelain origin main`` is a benign stale-ref
race (soft-fail) or everything else (hard-fail the job).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import stage as stage_mod

DIR = Path(__file__).resolve().parent


def _porcelain(*status_lines: str, url: str = "/tmp/fork.git") -> str:
    """Assemble a realistic porcelain transcript (To/status/Done)."""
    body = "\n".join(status_lines)
    return f"To {url}\n{body}\nDone\n"


def test_canonical_non_fast_forward_is_stale_ref():
    text = _porcelain("!\trefs/heads/main:refs/heads/main\t[rejected] (non-fast-forward)")
    assert stage_mod.is_stale_ref_push_rejection(text) is True


def test_fetch_first_and_stale_info_are_stale_ref():
    for reason in ("fetch first", "stale info"):
        text = _porcelain(f"!\tHEAD:refs/heads/main\t[rejected] ({reason})")
        assert stage_mod.is_stale_ref_push_rejection(text) is True, reason


def test_remote_rejected_by_pre_receive_hook_is_hard_fail():
    text = _porcelain(
        "!\trefs/heads/main:refs/heads/main\t[remote rejected] (pre-receive hook declined)"
    )
    assert stage_mod.is_stale_ref_push_rejection(text) is False


def test_hook_prose_fetch_first_with_remote_rejected_is_hard_fail():
    # Exact false-positive from the tribunal: hook reason text says
    # "fetch first" but the structured summary is [remote rejected].
    text = _porcelain("!\trefs/heads/main:refs/heads/main\t[remote rejected] (fetch first)")
    assert stage_mod.is_stale_ref_push_rejection(text) is False


def test_auth_permission_failure_with_no_status_line_is_hard_fail():
    # Transport/auth often leaves porcelain stdout empty (prose on stderr only).
    assert stage_mod.is_stale_ref_push_rejection("") is False
    assert (
        stage_mod.is_stale_ref_push_rejection(
            "remote: Permission to btli/omnigent.git denied to github-actions[bot].\n"
            "fatal: unable to access 'https://github.com/btli/omnigent.git/': "
            "The requested URL returned error: 403\n"
        )
        is False
    )


def test_transport_failure_is_hard_fail():
    assert (
        stage_mod.is_stale_ref_push_rejection(
            "fatal: unable to access 'https://github.com/btli/omnigent.git/': "
            "Could not resolve host: github.com\n"
        )
        is False
    )


def test_unrelated_rejected_markers_in_prose_do_not_soft_fail():
    # Independent greps over a combined transcript would false-positive here.
    prose = (
        "remote: tip: if rejected, fetch first before retrying\n"
        "!\trefs/heads/main:refs/heads/main\t[remote rejected] (hook declined)\n"
        "error: failed to push some refs\n"
    )
    assert stage_mod.is_stale_ref_push_rejection(prose) is False


def test_cli_is_stale_ref_rejection_exit_codes():
    script = str(DIR / "stage.py")
    good = _porcelain("!\trefs/heads/main:refs/heads/main\t[rejected] (non-fast-forward)")
    bad = _porcelain(
        "!\trefs/heads/main:refs/heads/main\t[remote rejected] (pre-receive hook declined)"
    )
    ok = subprocess.run(
        [sys.executable, script, "is-stale-ref-rejection"],
        input=good,
        text=True,
        capture_output=True,
        check=False,
    )
    assert ok.returncode == 0, ok.stderr
    hard = subprocess.run(
        [sys.executable, script, "is-stale-ref-rejection"],
        input=bad,
        text=True,
        capture_output=True,
        check=False,
    )
    assert hard.returncode == 1, hard.stderr


@pytest.mark.parametrize(
    "line",
    [
        "!\trefs/heads/other:refs/heads/other\t[rejected] (non-fast-forward)",
        "!\trefs/heads/main:refs/heads/main\t[rejected] (already exists)",
        "=\trefs/heads/main:refs/heads/main\t[up to date]",
        # Non-'!' flag with a [rejected] summary must hard-fail (not soft-fail).
        " \trefs/heads/main:refs/heads/main\t[rejected] (non-fast-forward)",
    ],
)
def test_non_main_or_other_reasons_are_hard_fail(line: str):
    assert stage_mod.is_stale_ref_push_rejection(_porcelain(line)) is False
