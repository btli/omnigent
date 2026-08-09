"""Offline tests for the personal-staging composer.

Every scenario runs against throwaway local git repos: a bare "upstream"
(with refs/pull/N/head refs standing in for GitHub PR refs), a bare "fork"
(push target), and a working clone. No network, no gh.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest
import stage as stage_mod

DATE = dt.date(2026, 8, 9)
STAMP = "20260809"


def git(cwd, *args, check=True):
    return subprocess.run(["git", *args], cwd=cwd, check=check, text=True, capture_output=True)


def commit_file(repo: Path, name: str, content: str, msg: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "--no-verify", "-m", msg)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class Env:
    """upstream bare repo + fork bare repo + working clone with both remotes."""

    def __init__(self, tmp_path: Path):
        self.upstream = tmp_path / "upstream.git"
        self.fork = tmp_path / "fork.git"
        self.work = tmp_path / "work"
        self.seed = tmp_path / "seed"
        for bare in (self.upstream, self.fork):
            bare.mkdir()
            git(bare, "init", "--bare")
        self.seed.mkdir()
        git(self.seed, "init", "-b", "main")
        git(self.seed, "config", "user.name", "t")
        git(self.seed, "config", "user.email", "t@t")
        commit_file(
            self.seed, "pyproject.toml", '[project]\nname = "x"\nversion = "1.2.3.dev0"\n', "base"
        )
        commit_file(self.seed, "a.txt", "base\n", "add a")
        git(self.seed, "push", str(self.upstream), "main")

        self.work.mkdir()
        git(self.work, "init", "-b", "main")
        git(self.work, "config", "user.name", "t")
        git(self.work, "config", "user.email", "t@t")
        git(self.work, "remote", "add", "upstream", str(self.upstream))
        git(self.work, "remote", "add", "origin", str(self.fork))

    def add_pr(self, number: int, filename: str, content: str) -> dict:
        """Branch off upstream main, one commit, exposed as refs/pull/N/head."""
        branch = f"pr-{number}"
        git(self.seed, "checkout", "-q", "main")
        git(self.seed, "checkout", "-q", "-b", branch)
        oid = commit_file(self.seed, filename, content, f"pr {number}")
        git(self.seed, "push", str(self.upstream), f"{branch}:refs/pull/{number}/head")
        return {"number": number, "headRefName": branch, "headRefOid": oid}

    def advance_main(self, filename: str, content: str) -> str:
        git(self.seed, "checkout", "-q", "main")
        sha = commit_file(self.seed, filename, content, f"main: {filename}")
        git(self.seed, "push", str(self.upstream), "main")
        return sha

    def fork_ref(self, ref: str) -> str:
        out = git(self.work, "ls-remote", str(self.fork), ref).stdout.strip()
        return out.split("\t")[0] if out else ""

    def fork_log(self, ref: str) -> str:
        return git(self.fork, "log", "--format=%s", ref).stdout

    def run(self, prs, date=DATE) -> dict:
        return stage_mod.stage(self.work, prs, date)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_merges_prs_ascending_and_pushes_staging(env):
    pr9 = env.add_pr(9, "nine.txt", "9\n")
    pr4 = env.add_pr(4, "four.txt", "4\n")
    report = env.run([pr9, pr4])

    assert [p["pr"] for p in report["applied"]] == [4, 9]
    assert report["skipped"] == []
    assert env.fork_ref("refs/heads/staging") == report["staging_sha"]
    log = env.fork_log("staging")
    # ascending: #4 merged first, so #9's merge commit is newer
    assert log.index("merge PR #9") < log.index("merge PR #4")


def test_conflicting_pr_skipped_with_paths(env):
    good = env.add_pr(2, "b.txt", "ok\n")
    bad = env.add_pr(3, "a.txt", "conflicting\n")
    env.advance_main("a.txt", "moved on\n")

    report = env.run([good, bad])
    assert [p["pr"] for p in report["applied"]] == [2]
    assert report["skipped"] == [{"pr": 3, "branch": "pr-3", "conflict_paths": ["a.txt"]}]
    # the good PR still landed on staging
    assert "merge PR #2" in env.fork_log("staging")


def test_all_prs_conflicting_is_not_a_failure(env):
    bad = env.add_pr(5, "a.txt", "clash\n")
    env.advance_main("a.txt", "diverged\n")
    report = env.run([bad])
    assert report["applied"] == []
    assert [p["pr"] for p in report["skipped"]] == [5]
    assert report["staging_sha"] == report["upstream_sha"]
    assert env.fork_ref("refs/heads/staging") == report["upstream_sha"]


def test_same_day_rerun_noop_then_rerun_suffix(env):
    pr = env.add_pr(7, "c.txt", "7\n")
    first = env.run([pr])
    assert first["tag"] == f"testing-{STAMP}"
    assert first["pin_created"] is True

    # identical rerun: same commit, no new pin
    again = env.run([pr])
    assert again["tag"] == f"testing-{STAMP}"
    assert again["pin_created"] is False
    assert again["staging_sha"] == first["staging_sha"]

    # upstream moved: same-day rerun gets a -rerun1 pin, day tag stays immutable
    env.advance_main("d.txt", "new\n")
    third = env.run([pr])
    assert third["tag"] == f"testing-{STAMP}-rerun1"
    assert env.fork_ref(f"refs/tags/testing-{STAMP}") == first["staging_sha"]
    assert env.fork_ref(f"refs/tags/testing-{STAMP}-rerun1") == third["staging_sha"]
    assert env.fork_ref("refs/heads/staging") == third["staging_sha"]


def test_dev_tag_mirrors_nightly_scheme(env):
    report = env.run([])
    assert report["dev_tag"] == f"v1.2.3.dev{STAMP}"
    assert env.fork_ref(f"refs/tags/v1.2.3.dev{STAMP}") == report["staging_sha"]
    # same-day rerun repoints the dev tag to the new staging commit
    env.advance_main("e.txt", "e\n")
    second = env.run([])
    assert env.fork_ref(f"refs/tags/v1.2.3.dev{STAMP}") == second["staging_sha"]


def test_never_touches_testing_branch(env):
    git(env.seed, "checkout", "-q", "main")
    testing_sha = git(env.seed, "rev-parse", "HEAD").stdout.strip()
    git(env.seed, "push", str(env.fork), "main:refs/heads/testing")
    env.advance_main("f.txt", "f\n")

    env.run([env.add_pr(8, "g.txt", "g\n")])
    assert env.fork_ref("refs/heads/testing") == testing_sha


def test_unreachable_pinned_head_is_skipped(env):
    pr = env.add_pr(6, "h.txt", "h\n")
    pr["headRefOid"] = "0" * 40  # force-pushed away between list and fetch
    report = env.run([pr])
    assert report["applied"] == []
    assert report["skipped"][0]["reason"] == "pinned head unreachable"


def test_cli_writes_report_and_notes(env, tmp_path, capsys):
    pr = env.add_pr(11, "i.txt", "i\n")
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([pr]))
    report_path = tmp_path / "merge-report.json"

    rc = stage_mod.main(
        [
            "stage",
            "--workdir",
            str(env.work),
            "--date",
            STAMP,
            "--prs-json",
            str(prs_json),
            "--report",
            str(report_path),
        ]
    )
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [p["pr"] for p in report["applied"]] == [11]

    capsys.readouterr()
    rc = stage_mod.main(["notes", "--report", str(report_path), "--signed", "false"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "#11" in out
    assert "runner-ephemeral keystore" in out
    assert f"v1.2.3.dev{STAMP}" in out
