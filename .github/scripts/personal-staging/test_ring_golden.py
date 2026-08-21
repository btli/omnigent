"""Golden-commit regression for the staging ring.

Locks the staging composition byte-for-byte so the ``Ring`` refactor (and any
later change) cannot silently alter what ``stage`` mints. A FIXED, fully
date-frozen 2-PR scenario is composed for ``--date 20260820`` and the whole
result is snapshotted: every minted merge-commit object (``git cat-file -p``),
the exact ``git push`` argv (refspecs + leases), the returned merge report, and
the human-facing ``notes``/``summarize`` renderings. All of it must equal a
checked-in golden under ``_golden/``.

Every commit here pins ``GIT_*_DATE`` and identity, so the base sha, each PR
head sha, and therefore every minted merge sha are reproducible across machines
and git versions (commit-object encoding is stable). Regenerate the golden with
``WRITE_GOLDEN=1`` after a *deliberate* change; a bare run is the gate.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import pytest
import stage as stage_mod

GOLDEN = Path(__file__).resolve().parent / "_golden" / "staging_report.json"
DATE = dt.date(2026, 8, 20)

# Frozen dates: the base + each PR head. stage() stamps every merge commit with
# the PR head's committer date, so freezing these freezes the minted shas.
BASE_DATE = "2026-08-01T00:00:00+00:00"
PR4_DATE = "2026-08-02T09:15:00+00:00"
PR9_DATE = "2026-08-03T18:30:00+00:00"

_IDENT = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd, *args, check=True, env=None):
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env={**os.environ, **env} if env else None,
    )


def commit_file(repo: Path, name: str, content: str, msg: str, date: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    # ``-c commit.gpgsign=false`` is load-bearing: a signed base/PR commit would
    # embed a non-deterministic signature and break sha reproducibility (the
    # minted merges already disable signing via stage.COMMIT_IDENT).
    git(
        repo,
        "-c",
        "commit.gpgsign=false",
        "commit",
        "--no-verify",
        "-m",
        msg,
        env={**_IDENT, "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date},
    )
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def build_scenario(tmp_path: Path):
    """Two upstream PRs (#4, #9), each one commit off a frozen base, exposed as
    refs/pull/N/head — the minimal ascending-merge staging composition."""
    upstream = tmp_path / "upstream.git"
    fork = tmp_path / "fork.git"
    work = tmp_path / "work"
    seed = tmp_path / "seed"
    for bare in (upstream, fork):
        bare.mkdir()
        git(bare, "init", "--bare")

    seed.mkdir()
    git(seed, "init", "-b", "main")
    commit_file(
        seed,
        "pyproject.toml",
        '[project]\nname = "x"\nversion = "1.2.3.dev0"\n',
        "base",
        BASE_DATE,
    )
    commit_file(seed, "a.txt", "base\n", "add a", BASE_DATE)
    git(seed, "push", str(upstream), "main")

    heads = {}
    for number, fname, content, date in (
        (4, "four.txt", "4\n", PR4_DATE),
        (9, "nine.txt", "9\n", PR9_DATE),
    ):
        branch = f"pr-{number}"
        git(seed, "checkout", "-q", "main")
        git(seed, "checkout", "-q", "-b", branch)
        heads[number] = commit_file(seed, fname, content, f"pr {number}", date)
        git(seed, "push", str(upstream), f"{branch}:refs/pull/{number}/head")

    work.mkdir()
    git(work, "init", "-b", "main")
    git(work, "config", "user.name", "t")
    git(work, "config", "user.email", "t@t")
    git(work, "remote", "add", "upstream", str(upstream))
    git(work, "remote", "add", "origin", str(fork))

    prs = [
        {"number": 4, "headRefName": "pr-4", "headRefOid": heads[4]},
        {"number": 9, "headRefName": "pr-9", "headRefOid": heads[9]},
    ]
    return work, prs


def compose_snapshot(tmp_path: Path, monkeypatch) -> dict:
    """Run the staging composition and capture the full byte-level result:
    the merge report, the exact push argv, every minted merge-commit object,
    and the rendered notes/summary."""
    work, prs = build_scenario(tmp_path)

    pushes: list[list[str]] = []
    real_git = stage_mod.git

    def spy(cwd, *args, **kwargs):
        if args and args[0] == "push":
            pushes.append(list(args))
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(stage_mod, "git", spy)
    report = stage_mod.stage(work, prs, DATE)
    monkeypatch.setattr(stage_mod, "git", real_git)

    # Minted merge commits, oldest-first, dumped whole (tree/parents/author/
    # committer/message) so any drift in identity, date, or ordering is caught.
    shas = real_git(
        work,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{report['upstream_sha']}..{report['staging_sha']}",
    ).stdout.split()
    merge_commits = [real_git(work, "cat-file", "-p", sha).stdout for sha in shas]

    return {
        "report": report,
        "pushed": pushes,
        "merge_commits": merge_commits,
        "notes": stage_mod.notes(report, signed=True),
        "summary": stage_mod.summarize(report),
    }


def test_staging_composition_matches_golden(tmp_path, monkeypatch):
    snapshot = compose_snapshot(tmp_path, monkeypatch)
    text = json.dumps(snapshot, indent=2, sort_keys=True) + "\n"

    if os.environ.get("WRITE_GOLDEN"):
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(text)
        pytest.skip("golden regenerated (WRITE_GOLDEN=1)")

    assert GOLDEN.read_text() == text, (
        "staging composition drifted from the golden fixture; if this change is "
        "intentional, regenerate with WRITE_GOLDEN=1"
    )
