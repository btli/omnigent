"""Offline tests for the personal-staging composer.

Every scenario runs against throwaway local git repos: a bare "upstream"
(with refs/pull/N/head refs standing in for GitHub PR refs), a bare "fork"
(push target), and a working clone. No network, no gh.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import sys
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

    def advance_pr(self, number: int, filename: str, content: str) -> dict:
        """New head on an existing PR branch, force-pushed to its pull ref."""
        branch = f"pr-{number}"
        git(self.seed, "checkout", "-q", branch)
        oid = commit_file(self.seed, filename, content, f"pr {number} update")
        git(self.seed, "push", "-f", str(self.upstream), f"{branch}:refs/pull/{number}/head")
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

    def run(self, prs, date=DATE, **kwargs) -> dict:
        return stage_mod.stage(self.work, prs, date, **kwargs)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


@pytest.fixture
def pushes(monkeypatch):
    """Record the argv of every `git push` the composer issues (still running
    them), so tests can assert exactly which refs and leases were used."""
    recorded: list[tuple] = []
    real_git = stage_mod.git

    def spy(cwd, *args, **kwargs):
        if args and args[0] == "push":
            recorded.append(args)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(stage_mod, "git", spy)
    return recorded


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch):
    """The extras fetch sleeps between retries in production; no test waits."""
    monkeypatch.setattr(stage_mod, "EXTRA_FETCH_BACKOFF_S", 0)


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
    assert report["skipped"] == [
        {"pr": 3, "branch": "pr-3", "conflict_paths": ["a.txt"], "source": "open"}
    ]
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


def test_hostile_tomllib_shadow_is_never_imported(env, tmp_path):
    """A merged PR drops tomllib.py next to stage.py mid-run (sys.path[0]);
    the parser must already be cached from module load, so the shadow module
    never executes."""
    script_dir_rel = ".github/scripts/personal-staging"
    (env.seed / script_dir_rel).mkdir(parents=True)
    shutil.copy(stage_mod.__file__, env.seed / script_dir_rel / "stage.py")
    git(env.seed, "add", script_dir_rel)
    git(env.seed, "commit", "--no-verify", "-m", "vendor composer")
    git(env.seed, "push", str(env.upstream), "main")

    canary = tmp_path / "canary"
    pr = env.add_pr(
        21,
        f"{script_dir_rel}/tomllib.py",
        f"open({str(canary)!r}, 'w').write('pwned')\nraise RuntimeError('pwned')\n",
    )
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([pr]))

    # materialize the trusted checkout, then run the composer FROM the work
    # tree so a shadow module merged next to it would win module resolution
    git(env.work, "fetch", "upstream", "main")
    git(env.work, "checkout", "--detach", "FETCH_HEAD")
    proc = subprocess.run(
        [
            sys.executable,
            str(env.work / script_dir_rel / "stage.py"),
            "stage",
            "--workdir",
            str(env.work),
            "--date",
            STAMP,
            "--prs-json",
            str(prs_json),
            "--report",
            str(tmp_path / "r.json"),
        ],
        text=True,
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert not canary.exists()
    report = json.loads((tmp_path / "r.json").read_text())
    assert [p["pr"] for p in report["applied"]] == [21]


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


def test_parse_extras_comments_blanks_and_missing(tmp_path):
    manifest = tmp_path / "extras.txt"
    manifest.write_text("# pinned fixes\n\n12  # trailing comment\n7\n12\n")
    # the contract is WHICH numbers are pinned — comments and blank lines are
    # ignored; ordering and duplicates are normalized later by merge_stream
    assert sorted(set(stage_mod.parse_extras(manifest))) == [7, 12]
    assert stage_mod.parse_extras(tmp_path / "missing.txt") == []


def test_parse_extras_garbage_line_fails_loud(tmp_path):
    manifest = tmp_path / "extras.txt"
    manifest.write_text("12\nnot-a-number\n")
    with pytest.raises(stage_mod.StageError, match="invalid PR number"):
        stage_mod.parse_extras(manifest)


def test_merge_stream_union_dedupe_open_wins():
    open_prs = [{"number": 9, "headRefName": "pr-9", "headRefOid": "x" * 40}]
    stream = stage_mod.merge_stream(open_prs, [9, 4, 4, 20])
    assert [(p["number"], p["source"]) for p in stream] == [
        (4, "extra"),
        (9, "open"),
        (20, "extra"),
    ]
    # the open entry wins the dedupe: its pinned head survives
    assert stream[1]["headRefOid"] == "x" * 40


def test_extra_pr_merges_from_pull_ref_with_source(env):
    open_pr = env.add_pr(3, "o.txt", "o\n")
    env.add_pr(8, "x.txt", "x\n")  # stands in for a closed PR: ref exists, not listed open
    report = env.run(stage_mod.merge_stream([open_pr], [8]))
    assert [(p["pr"], p["source"]) for p in report["applied"]] == [(3, "open"), (8, "extra")]
    assert "merge PR #8 (pull/8/head" in env.fork_log("staging")


def test_extra_confirmed_deleted_is_loud_advisory_skip(env):
    """ls-remote proves the ref is gone: skip it, keep composing, and tell the
    operator to edit the manifest."""
    ok = env.add_pr(2, "k.txt", "k\n")
    report = env.run(stage_mod.merge_stream([ok], [999]))
    assert [p["pr"] for p in report["applied"]] == [2]
    assert report["skipped"] == [
        {
            "pr": 999,
            "branch": "pull/999/head",
            "conflict_paths": [],
            "reason": stage_mod.EXTRA_MISSING,
            "source": "extra",
        }
    ]
    # distinct from conflict skips, and loud in the human surfaces
    assert "remove from extras.txt" in stage_mod.notes(report, signed=True)
    report["pin_created"] = True
    assert "remove from extras.txt" in stage_mod.summarize(report)


def _break_ref(monkeypatch, ref_fragment: str, *, commands: tuple[str, ...], fail_times: int):
    """Make the named git commands fail for one ref, like a transport blip."""
    real_git = stage_mod.git
    seen: list[tuple] = []

    def flaky(cwd, *args, **kwargs):
        if args[:1] in {(c,) for c in commands} and any(ref_fragment in a for a in args):
            seen.append(args)
            if len(seen) <= fail_times:
                return subprocess.CompletedProcess(args, 128, "", "fatal: unable to access")
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(stage_mod, "git", flaky)
    return seen


def test_extra_transport_failure_blocks_push_and_spares_the_manifest(env, monkeypatch):
    """An unreachable upstream must not masquerade as a deleted ref: staging
    keeps its previous content and nobody is told to delete a live pin."""
    ok = env.add_pr(3, "t.txt", "t\n")
    env.add_pr(4, "u.txt", "u\n")
    stream = stage_mod.merge_stream([ok], [4])
    first = env.run(stream, staging_only=True)
    assert [p["pr"] for p in first["applied"]] == [3, 4]

    _break_ref(monkeypatch, "pull/4/head", commands=("fetch", "ls-remote"), fail_times=99)
    report = env.run(stream, staging_only=True)

    skip = report["skipped"][0]
    assert skip["reason"] == stage_mod.EXTRA_FETCH_FAILED
    assert "extras.txt" not in skip["reason"]
    assert report["blocked"] == [4]
    assert report["pushed"] is False
    # previous staging content preserved — no silent regression
    assert env.fork_ref("refs/heads/staging") == first["staging_sha"]
    assert "NOT pushed" in stage_mod.summarize(report)


def test_extra_transport_failure_fails_the_nightly_before_publishing(env, monkeypatch):
    env.add_pr(4, "u.txt", "u\n")
    _break_ref(monkeypatch, "pull/4/head", commands=("fetch", "ls-remote"), fail_times=99)
    with pytest.raises(stage_mod.StageError, match="infrastructure failure"):
        env.run(stage_mod.merge_stream([], [4]))
    assert env.fork_ref("refs/heads/staging") == ""


def test_extra_fetch_succeeds_after_retry(env, monkeypatch):
    env.add_pr(5, "r.txt", "r\n")
    seen = _break_ref(monkeypatch, "pull/5/head", commands=("fetch",), fail_times=1)
    report = env.run(stage_mod.merge_stream([], [5]), staging_only=True)
    assert len(seen) == 2  # one blip, then the retry lands
    assert [(p["pr"], p["source"]) for p in report["applied"]] == [(5, "extra")]
    assert report["skipped"] == [] and report["blocked"] == []


def test_staging_only_pushes_only_staging_with_lease(env, pushes):
    pr = env.add_pr(5, "s.txt", "s\n")
    report = env.run([pr], staging_only=True)

    assert report["pushed"] is True
    assert env.fork_ref("refs/heads/staging") == report["staging_sha"]
    # exactly one push, leased, carrying exactly one refspec
    assert pushes == [
        (
            "push",
            "--force-with-lease=refs/heads/staging:",
            "origin",
            f"{report['staging_sha']}:refs/heads/staging",
        )
    ]
    refs = [
        line.split("\t")[1]
        for line in git(env.work, "ls-remote", str(env.fork)).stdout.strip().splitlines()
    ]
    assert refs == ["refs/heads/staging"]
    for key in ("branch", "tag", "dev_tag", "pin_created"):
        assert key not in report


def test_staging_only_lease_pins_the_previous_remote_sha(env, pushes):
    """The repeat-run case: the lease must carry the sha staging actually had,
    not the cold-start empty value."""
    pr = env.add_pr(30, "l.txt", "l\n")
    first = env.run([pr], staging_only=True)
    pushes.clear()

    env.advance_main("l2.txt", "l2\n")
    second = env.run([pr], staging_only=True)
    assert pushes == [
        (
            "push",
            f"--force-with-lease=refs/heads/staging:{first['staging_sha']}",
            "origin",
            f"{second['staging_sha']}:refs/heads/staging",
        )
    ]


def test_staging_only_noop_fast_path_skips_push(env, pushes):
    pr = env.add_pr(6, "n.txt", "n\n")
    first = env.run([pr], staging_only=True)
    assert first["pushed"] is True
    pushes.clear()

    second = env.run([pr], staging_only=True)
    assert second["pushed"] is False
    assert second["staging_sha"] == first["staging_sha"]
    assert pushes == []
    assert "unchanged — push skipped" in stage_mod.summarize(second)


def test_staging_only_reports_change_causes(env):
    pr = env.add_pr(7, "c7.txt", "7\n")
    env.run([pr], staging_only=True)

    env.advance_main("c8.txt", "8\n")
    second = env.run([pr], staging_only=True)
    assert second["pushed"] is True
    assert second["causes"] == ["upstream HEAD"]

    env.add_pr(9, "c9.txt", "9\n")
    third = env.run(stage_mod.merge_stream([pr], [9]), staging_only=True)
    assert third["causes"] == ["extras"]


def test_staging_only_cause_open_pr_head_moved(env):
    pr = env.add_pr(31, "o.txt", "o\n")
    env.run([pr], staging_only=True)
    moved = env.advance_pr(31, "o.txt", "o2\n")
    second = env.run([moved], staging_only=True)
    assert second["causes"] == ["open PR set"]


def test_staging_only_cause_extra_removed_then_promoted(env):
    keep = env.add_pr(32, "k.txt", "k\n")
    extra = env.add_pr(33, "x.txt", "x\n")
    env.run(stage_mod.merge_stream([keep], [33]), staging_only=True)

    # unpinned from extras.txt: reported as dropped, never as an open-PR change
    dropped = env.run(stage_mod.merge_stream([keep], []), staging_only=True)
    assert dropped["causes"] == ["dropped PR #33"]

    # the same number returns as an open PR — a source transition, not a drop
    promoted = env.run(stage_mod.merge_stream([keep, extra], []), staging_only=True)
    assert promoted["causes"] == ["open PR set"]


def test_staging_only_cause_already_merged_pr_is_not_a_change(env):
    """An open PR already merged upstream mints no merge commit, so the old
    composition can't mention it either: an upstream move must not be
    misreported as an open-PR change."""
    git(env.seed, "checkout", "-q", "main")
    head = git(env.seed, "rev-parse", "HEAD").stdout.strip()
    git(env.seed, "push", str(env.upstream), "main:refs/pull/50/head")
    pr = {"number": 50, "headRefName": "pr-50", "headRefOid": head}
    env.run([pr], staging_only=True)

    env.advance_main("am.txt", "am\n")
    second = env.run([pr], staging_only=True)
    assert second["causes"] == ["upstream HEAD"]


def test_old_composition_decoded_past_any_merge_count(env, monkeypatch):
    """The previous composition is decoded by walking to the real upstream
    base, so a manifest longer than any PR-list bound still yields real
    causes instead of degrading to 'no previous composition'."""
    extras = [env.add_pr(40 + i, f"w{i}.txt", f"{i}\n") for i in range(3)]
    stream = stage_mod.merge_stream([], [p["number"] for p in extras])
    env.run(stream, staging_only=True)

    monkeypatch.setattr(stage_mod, "PR_LIST_LIMIT", 1)
    env.advance_main("wmain.txt", "m\n")
    second = env.run(stream, staging_only=True)
    assert second["causes"] == ["upstream HEAD"]


def test_cli_staging_only_with_extras_manifest(env, tmp_path, monkeypatch, capsys):
    open_pr = env.add_pr(11, "i.txt", "i\n")
    env.add_pr(12, "j.txt", "j\n")  # reachable only through the manifest
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([open_pr]))
    extras = tmp_path / "extras.txt"
    extras.write_text("# pinned\n12\n999\n")
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report_path = tmp_path / "r.json"

    rc = stage_mod.main(
        [
            "stage",
            "--workdir",
            str(env.work),
            "--date",
            STAMP,
            "--prs-json",
            str(prs_json),
            "--extras",
            str(extras),
            "--staging-only",
            "--report",
            str(report_path),
        ]
    )
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [(p["pr"], p["source"]) for p in report["applied"]] == [(11, "open"), (12, "extra")]
    assert [p["pr"] for p in report["skipped"]] == [999]
    assert report["skipped"][0]["reason"].startswith("extra unfetchable")
    text = summary.read_text()
    assert "## Personal staging hourly" in text
    assert "extra unfetchable" in text
    capsys.readouterr()
