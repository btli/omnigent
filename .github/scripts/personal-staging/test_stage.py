"""Offline tests for the personal-staging composer.

Every scenario runs against throwaway local git repos: a bare "upstream"
(with refs/pull/N/head refs standing in for GitHub PR refs), a bare "fork"
(push target), and a working clone. No network, no gh.
"""

from __future__ import annotations

import datetime as dt
import json
import re
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
    assert first["tag"] == f"nightly-{STAMP}"
    assert first["pin_created"] is True

    # identical rerun: same commit, no new pin
    again = env.run([pr])
    assert again["tag"] == f"nightly-{STAMP}"
    assert again["pin_created"] is False
    assert again["staging_sha"] == first["staging_sha"]

    # upstream moved: same-day rerun gets a -rerun1 pin, day tag stays immutable
    env.advance_main("d.txt", "new\n")
    third = env.run([pr])
    assert third["tag"] == f"nightly-{STAMP}-rerun1"
    assert env.fork_ref(f"refs/tags/nightly-{STAMP}") == first["staging_sha"]
    assert env.fork_ref(f"refs/tags/nightly-{STAMP}-rerun1") == third["staging_sha"]
    assert env.fork_ref("refs/heads/staging") == third["staging_sha"]


def test_dev_tag_mirrors_nightly_scheme(env):
    report = env.run([])
    assert report["dev_tag"] == f"v1.2.3.dev{STAMP}"
    assert env.fork_ref(f"refs/tags/v1.2.3.dev{STAMP}") == report["staging_sha"]
    # same-day rerun repoints the dev tag to the new staging commit
    env.advance_main("e.txt", "e\n")
    second = env.run([])
    assert env.fork_ref(f"refs/tags/v1.2.3.dev{STAMP}") == second["staging_sha"]


def test_only_allowed_refs_pushed(env):
    """Allowlist: the run may only create staging, the nightly-* pin, and the
    dev tag — any pre-existing ref (here the deploy branch) stays untouched."""
    git(env.seed, "checkout", "-q", "main")
    testing_sha = git(env.seed, "rev-parse", "HEAD").stdout.strip()
    git(env.seed, "push", str(env.fork), "main:refs/heads/testing")
    env.advance_main("f.txt", "f\n")

    env.run([env.add_pr(8, "g.txt", "g\n")])
    assert env.fork_ref("refs/heads/testing") == testing_sha

    refs = [
        line.split("\t")[1]
        for line in git(env.work, "ls-remote", str(env.fork)).stdout.strip().splitlines()
    ]
    allowed = re.compile(
        r"refs/heads/staging$"
        r"|refs/(heads|tags)/nightly-\d{8}(-rerun\d+)?$"
        r"|refs/tags/v\d+\.\d+\.\d+\.dev\d{8}$"
    )
    assert all(allowed.search(r) for r in refs if r != "refs/heads/testing"), refs


def test_non_conflict_merge_failure_raises(env):
    """A merge that fails without unmerged index entries (here: an untracked
    file the PR would overwrite) is a broken workspace, not a skippable PR."""
    pr = env.add_pr(12, "z.txt", "pr content\n")
    (env.work / "z.txt").write_text("local droppings\n")
    with pytest.raises(stage_mod.StageError, match="failed without conflicts"):
        env.run([pr])


def test_already_merged_pr_applies_without_commit(env):
    git(env.seed, "checkout", "-q", "main")
    head = git(env.seed, "rev-parse", "HEAD").stdout.strip()
    git(env.seed, "push", str(env.upstream), "main:refs/pull/13/head")
    pr = {"number": 13, "headRefName": "pr-13", "headRefOid": head}

    report = env.run([pr])
    assert [p["pr"] for p in report["applied"]] == [13]
    # nothing to merge: staging is upstream HEAD itself, no merge commit minted
    assert report["staging_sha"] == report["upstream_sha"]


def test_notes_escape_untrusted_names_and_signed_variant():
    report = {
        "date": STAMP,
        "upstream_sha": "u" * 40,
        "staging_sha": "s" * 40,
        "dev_tag": f"v1.2.3.dev{STAMP}",
        "applied": [{"pr": 1, "branch": "evil`[link](https://x)`|", "oid": "a" * 40}],
        "skipped": [{"pr": 2, "branch": "bad`name", "conflict_paths": ["web/`x`.ts"]}],
    }
    out = stage_mod.notes(report, signed=True)
    assert "shared debug keystore" in out
    assert "upgrade in place" in out
    # untrusted backticks/pipes are neutralized inside code spans
    assert "evil`" not in out and "`evil'[link](https://x)'/`" in out
    assert "merge conflict:" in out and "`web/'x'.ts`" in out

    report["pin_created"] = True
    report["tag"] = f"nightly-{STAMP}"
    summary = stage_mod.summarize(report)
    assert "bad`name" not in summary
    assert "| Skipped #2 |" in summary and "web/'x'.ts" in summary


def test_pin_branch_repaired_and_mismatch_fails(env):
    pr = env.add_pr(14, "j.txt", "j\n")
    first = env.run([pr])
    pin = f"refs/heads/nightly-{STAMP}"

    # a missing twin branch is repaired on rerun (tag untouched, no new pin)
    git(env.fork, "update-ref", "-d", pin)
    again = env.run([pr])
    assert again["pin_created"] is False
    assert env.fork_ref(pin) == first["staging_sha"]

    # a divergent pin branch means someone moved an immutable pin — fail,
    # never clobber
    git(env.fork, "update-ref", pin, first["upstream_sha"])
    with pytest.raises(stage_mod.StageError, match="pin branch"):
        env.run([pr])
    assert env.fork_ref(pin) == first["upstream_sha"]


def test_truncation_guard(env, tmp_path):
    full = [{"number": i} for i in range(stage_mod.PR_LIST_LIMIT)]
    assert stage_mod.check_not_truncated(full[:5]) == full[:5]
    with pytest.raises(stage_mod.StageError, match="truncated"):
        stage_mod.check_not_truncated(full)

    # the --prs-json path runs through the same guard, before any git work
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps(full))
    with pytest.raises(stage_mod.StageError, match="truncated"):
        stage_mod.main(
            [
                "stage",
                "--workdir",
                str(env.work),
                "--date",
                STAMP,
                "--prs-json",
                str(prs_json),
                "--report",
                str(tmp_path / "r.json"),
            ]
        )


def test_summary_written_on_success_and_failure(env, tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    prs_json = tmp_path / "prs.json"
    prs_json.write_text("[]")

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
            str(tmp_path / "r.json"),
        ]
    )
    assert rc == 0
    text = summary.read_text()
    assert "## Personal staging nightly" in text and f"nightly-{STAMP}" in text

    # a broken remote must still leave a failure line in the summary
    with pytest.raises(stage_mod.StageError):
        stage_mod.main(
            [
                "stage",
                "--workdir",
                str(env.work),
                "--date",
                STAMP,
                "--upstream-remote",
                "nonexistent",
                "--prs-json",
                str(prs_json),
                "--report",
                str(tmp_path / "r2.json"),
            ]
        )
    assert "**FAILED:**" in summary.read_text()
    capsys.readouterr()


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
