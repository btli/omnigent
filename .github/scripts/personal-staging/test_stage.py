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

    def add_fork_branch(self, name: str, filename: str, content: str) -> str:
        """Branch off upstream main, one commit, pushed to the fork remote."""
        git(self.seed, "checkout", "-q", "main")
        git(self.seed, "checkout", "-q", "-b", name)
        oid = commit_file(self.seed, filename, content, f"fork branch {name}")
        git(self.seed, "push", str(self.fork), f"{name}:refs/heads/{name}")
        return oid

    def advance_fork_branch(self, name: str, filename: str, content: str) -> str:
        git(self.seed, "checkout", "-q", name)
        oid = commit_file(self.seed, filename, content, f"fork branch {name} update")
        git(self.seed, "push", str(self.fork), f"{name}:refs/heads/{name}")
        return oid

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


def _rescuable_pr(env) -> dict:
    """A PR whose MERGE conflicts but whose REBASE succeeds.

    Commit A's patch later lands upstream verbatim and then evolves on the
    same lines, so merging the head three-ways conflicts on ``a.txt``;
    replaying commit by commit drops A by patch-id and applies B cleanly.
    """
    env.add_pr(5, "a.txt", "fixed\n")  # commit A: base -> fixed
    git(env.seed, "checkout", "-q", "pr-5")
    oid = commit_file(env.seed, "b5.txt", "5\n", "pr 5 b")  # commit B: new file
    git(env.seed, "push", "-f", str(env.upstream), "pr-5:refs/pull/5/head")
    git(env.seed, "push", str(env.fork), "pr-5:refs/heads/pr-5")
    env.advance_main("a.txt", "fixed\n")  # upstream lands A's content…
    env.advance_main("a.txt", "fixed more\n")  # …then evolves it
    return {"number": 5, "headRefName": "pr-5", "headRefOid": oid}


def test_conflicting_pr_rescued_by_rebase(env):
    pr = _rescuable_pr(env)

    report = env.run([pr])
    [entry] = report["applied"]
    assert entry["pr"] == 5
    assert entry["rebased_from"] == pr["headRefOid"]
    assert entry["pushed_back"] is True
    assert report["skipped"] == []
    # the fork branch was refreshed to the rescued head this run composed
    assert env.fork_ref("refs/heads/pr-5") == entry["oid"]
    assert "merge PR #5" in env.fork_log("staging")


def test_rescue_is_reproducible_and_leased(env):
    pr = _rescuable_pr(env)

    first = env.run([pr], staging_only=True)
    [e1] = first["applied"]
    assert e1["pushed_back"] is True

    # A rerun with the same (now stale) listing rebuilds the byte-identical
    # rescue: the push-back is an up-to-date no-op and the unchanged
    # composition is detected, so no ref actually moves.
    second = env.run([pr], staging_only=True)
    [e2] = second["applied"]
    assert e2["oid"] == e1["oid"]
    assert env.fork_ref("refs/heads/pr-5") == e1["oid"]
    assert second["staging_sha"] == first["staging_sha"]
    assert second["pushed"] is False


def test_rescue_lease_spares_a_branch_that_moved(env):
    pr = _rescuable_pr(env)
    moved = env.advance_fork_branch("pr-5", "newer.txt", "human work\n")

    report = env.run([pr])
    [entry] = report["applied"]
    # the rescue still composed, but the moved branch kept its newer work
    assert entry["rebased_from"] == pr["headRefOid"]
    assert entry["pushed_back"] is False
    assert env.fork_ref("refs/heads/pr-5") == moved


def test_rescue_refuses_a_merge_containing_branch(env):
    _rescuable_pr(env)
    # Graft a merge commit onto the PR head: a linear replay would drop it
    # (and any changes introduced only in its resolution), so the rescue
    # must refuse and leave the branch's normal conflict skip in place.
    git(env.seed, "checkout", "-q", "-b", "side-5", "pr-5~1")
    commit_file(env.seed, "side5.txt", "s\n", "side work")
    git(env.seed, "checkout", "-q", "pr-5")
    git(env.seed, "merge", "--no-ff", "-m", "merge side work", "side-5")
    oid = git(env.seed, "rev-parse", "HEAD").stdout.strip()
    git(env.seed, "push", "-f", str(env.upstream), "pr-5:refs/pull/5/head")
    git(env.seed, "push", "-f", str(env.fork), "pr-5:refs/heads/pr-5")
    pr = {"number": 5, "headRefName": "pr-5", "headRefOid": oid}

    report = env.run([pr])
    assert report["applied"] == []
    assert [p["pr"] for p in report["skipped"]] == [5]
    # the PR branch is untouched — no destructive push-back happened
    assert env.fork_ref("refs/heads/pr-5") == oid


def test_production_ring_never_rescues(env):
    pr = _rescuable_pr(env)

    report = env.run([pr], ring=stage_mod.PRODUCTION)
    assert report["applied"] == []
    assert [p["pr"] for p in report["skipped"]] == [5]
    # the PR branch is untouched — production composes what the PRs say
    assert env.fork_ref("refs/heads/pr-5") == pr["headRefOid"]


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


def test_pin_push_uses_expected_absent_leases(env, pushes):
    report = env.run([])
    push = pushes[-1]

    assert f"--force-with-lease=refs/heads/{report['branch']}:" in push
    assert f"--force-with-lease=refs/tags/{report['tag']}:" in push


def test_pin_creation_race_is_atomic(env, monkeypatch):
    real_git = stage_mod.git
    raced = False
    concurrent_sha = ""
    pin_branch = f"refs/heads/nightly-{STAMP}"

    def race(cwd, *args, **kwargs):
        nonlocal concurrent_sha, raced
        if args and args[0] == "push" and "--atomic" in args and not raced:
            raced = True
            concurrent_sha = git(cwd, "rev-parse", "HEAD^1").stdout.strip()
            git(env.fork, "fetch", str(cwd), concurrent_sha)
            git(env.fork, "update-ref", pin_branch, concurrent_sha)
        return real_git(cwd, *args, **kwargs)

    monkeypatch.setattr(stage_mod, "git", race)
    with pytest.raises(stage_mod.StageError, match="push --atomic"):
        env.run([env.add_pr(31, "race.txt", "race\n")])

    assert raced is True
    assert env.fork_ref(pin_branch) == concurrent_sha
    assert env.fork_ref(f"refs/tags/nightly-{STAMP}") == ""
    assert env.fork_ref("refs/heads/staging") == ""


def test_surviving_pin_branch_never_reuses_deleted_tag_name(env):
    first = env.run([])
    pin_branch = f"refs/heads/nightly-{STAMP}"
    pin_tag = f"refs/tags/nightly-{STAMP}"
    git(env.fork, "update-ref", "-d", pin_tag)
    env.advance_main("descendant.txt", "descendant\n")

    with pytest.raises(stage_mod.StageError, match="tag is absent"):
        env.run([])

    assert env.fork_ref(pin_branch) == first["staging_sha"]
    assert env.fork_ref(pin_tag) == ""


def test_post_push_audit_records_fully_qualified_pin_refs(env):
    report = env.run([])
    pin = f"nightly-{STAMP}"

    assert report["pin_ref"] == f"refs/tags/{pin}"
    assert report["audit"] == [
        {
            "ref": f"refs/tags/{pin}",
            "expected": report["staging_sha"],
            "observed": report["staging_sha"],
        },
        {
            "ref": f"refs/heads/{pin}",
            "expected": report["staging_sha"],
            "observed": report["staging_sha"],
        },
    ]


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


def test_existing_pin_tag_does_not_recreate_missing_compat_branch(env, pushes):
    pr = env.add_pr(14, "j.txt", "j\n")
    first = env.run([pr])
    pin_branch = f"refs/heads/nightly-{STAMP}"
    pin_tag = f"refs/tags/nightly-{STAMP}"

    git(env.fork, "update-ref", "-d", pin_branch)
    git(env.fork, "update-ref", "-d", "refs/heads/staging")
    pushes.clear()
    again = env.run([pr])

    assert again["pin_created"] is False
    assert env.fork_ref(pin_tag) == first["staging_sha"]
    assert env.fork_ref(pin_branch) == ""
    assert len(pushes) == 1
    assert not any(arg.endswith(f":{pin_branch}") for arg in pushes[0])
    assert not any(arg.endswith(f":{pin_tag}") for arg in pushes[0])


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
    assert stage_mod.parse_extras(manifest) == ([12, 7, 12], [])
    assert stage_mod.parse_extras(tmp_path / "missing.txt") == ([], [])


def test_parse_extras_garbage_line_fails_loud(tmp_path):
    manifest = tmp_path / "extras.txt"
    manifest.write_text("12\nnot-a-number\n")
    with pytest.raises(stage_mod.StageError, match="invalid extras entry"):
        stage_mod.parse_extras(manifest)


def test_parse_exclusions_comments_blanks_and_missing(tmp_path):
    manifest = tmp_path / "exclude.txt"
    manifest.write_text("# held out\n\n12  # trailing comment\n7\n12\n")
    assert stage_mod.parse_exclusions(manifest) == [12, 7, 12]
    assert stage_mod.parse_exclusions(tmp_path / "missing.txt") == []


@pytest.mark.parametrize("entry", ["branch:foo", "abc"])
def test_parse_exclusions_invalid_entry_fails_loud(tmp_path, entry):
    manifest = tmp_path / "exclude.txt"
    manifest.write_text(f"12\n{entry}\n")
    message = f"{manifest}:2: invalid exclude entry {entry!r}"
    with pytest.raises(stage_mod.StageError, match=re.escape(message)):
        stage_mod.parse_exclusions(manifest)


def test_parse_extras_branch_homelab(tmp_path):
    manifest = tmp_path / "extras.txt"
    manifest.write_text("branch:homelab\n")
    assert stage_mod.parse_extras(manifest) == ([], ["homelab"])


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ("branch:-evil", r"must not start with '-'"),
        ("branch:main", r"self-reference"),
        ("branch:staging", r"self-reference"),
        ("branch:", r"invalid branch name"),
        ("branch:foo..bar", r"invalid branch name"),
        ("branch:foo bar", r"invalid branch name"),
        ("branch:@{-1}", r"invalid branch name"),
        ("tag:homelab", r"invalid extras entry"),
        ("foo:bar", r"invalid extras entry"),
    ],
)
def test_parse_extras_rejects_invalid_branch_pins(tmp_path, entry, match):
    manifest = tmp_path / "extras.txt"
    manifest.write_text(f"{entry}\n")
    with pytest.raises(stage_mod.StageError, match=match):
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


@pytest.mark.parametrize(
    "ring", [stage_mod.STAGING, stage_mod.PRODUCTION], ids=["staging", "production"]
)
def test_bot_successor_pins_parse_and_compose(env, ring):
    """Bot-owned successors are absent from the btli open stream, so each
    ring must parse and compose its numeric pull-ref pins."""
    pins, branches = stage_mod.parse_extras(ring.extras_file, ring)
    assert pins == [5825, 5828]
    assert branches == (["homelab"] if ring is stage_mod.STAGING else [])

    env.add_pr(5825, "battery.txt", "backoff\n")
    env.add_pr(5828, "touch.txt", "touch gating\n")
    report = env.run(stage_mod.merge_stream([], pins), ring=ring)

    assert [(p["pr"], p["source"]) for p in report["applied"]] == [
        (5825, "extra"),
        (5828, "extra"),
    ]
    assert report["skipped"] == []


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
    text = stage_mod.summarize(report)
    assert "NOT pushed" in text
    assert "candidate (not pushed)" in text
    assert f"| Remote staging | `{first['staging_sha']}` |" in text
    assert "| Skipped #4 |" in text


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
    text = stage_mod.summarize(second)
    assert "no-op: staging unchanged; 1 PRs applied, 0 skipped" in text
    assert "| Skipped #" not in text
    assert "candidate (not pushed)" in text
    assert f"| Remote staging | `{first['staging_sha']}` |" in text


def test_summarize_hourly_noop_collapses_skip_detail():
    """No-op hours must not dump the full skip table — one count line only."""
    skipped = [
        {"pr": 90 + i, "branch": f"b{i}", "conflict_paths": [f"p{i}.txt"], "source": "open"}
        for i in range(5)
    ]
    report = {
        "staging_only": True,
        "upstream_sha": "u" * 40,
        "staging_sha": "c" * 40,
        "remote_staging_sha": "c" * 40,
        "pushed": False,
        "blocked": [],
        "causes": [],
        "applied": [{"pr": i} for i in range(3)],
        "skipped": skipped,
    }
    text = stage_mod.summarize(report)
    assert "no-op: staging unchanged; 3 PRs applied, 5 skipped" in text
    assert "| Skipped #" not in text
    assert "candidate (not pushed)" in text
    assert f"| Remote staging | `{'c' * 40}` |" in text


def test_summarize_hourly_push_keeps_full_skip_table():
    """A real push still renders every skip row — the detail that matters."""
    report = {
        "staging_only": True,
        "upstream_sha": "u" * 40,
        "staging_sha": "s" * 40,
        "remote_staging_sha": "r" * 40,
        "pushed": True,
        "blocked": [],
        "causes": ["upstream HEAD"],
        "applied": [{"pr": 1}],
        "skipped": [
            {
                "pr": 99,
                "branch": "pull/99/head",
                "conflict_paths": [],
                "reason": stage_mod.EXTRA_MISSING,
                "source": "extra",
            }
        ],
    }
    text = stage_mod.summarize(report)
    assert "pushed (changed: upstream HEAD)" in text
    assert "| Staging |" in text and "candidate (not pushed)" not in text
    assert "| Skipped #99 |" in text and "remove from extras.txt" in text
    assert "| Applied / skipped | 1 / 1 |" in text


def test_summarize_hourly_blocked_labels_candidate_not_staging():
    report = {
        "staging_only": True,
        "upstream_sha": "u" * 40,
        "staging_sha": "c" * 40,
        "remote_staging_sha": "r" * 40,
        "pushed": False,
        "blocked": [4],
        "causes": [],
        "applied": [],
        "skipped": [
            {
                "pr": 4,
                "branch": "pull/4/head",
                "conflict_paths": [],
                "reason": stage_mod.EXTRA_FETCH_FAILED,
                "source": "extra",
            }
        ],
    }
    text = stage_mod.summarize(report)
    assert "NOT pushed" in text
    assert "candidate (not pushed)" in text and f"| Remote staging | `{'r' * 40}` |" in text
    assert "| Skipped #4 |" in text
    assert "| Staging |" not in text.replace("Remote staging", "")


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


def test_old_composition_decoded_past_any_merge_count(env):
    """composition_of walks first-parent to the real upstream base, so a
    multi-merge previous composition still yields real causes instead of
    degrading to 'no previous composition'."""
    extras = [env.add_pr(40 + i, f"w{i}.txt", f"{i}\n") for i in range(3)]
    stream = stage_mod.merge_stream([], [p["number"] for p in extras])
    env.run(stream, staging_only=True)

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


def test_cli_exclusion_wins_over_open_and_extra_sources(env, tmp_path, capsys):
    excluded_pr = env.add_pr(11, "excluded.txt", "excluded\n")
    surviving_pr = env.add_pr(12, "surviving.txt", "surviving\n")
    env.add_fork_branch("homelab", "homelab.txt", "lab\n")
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([excluded_pr, surviving_pr]))
    extras = tmp_path / "extras.txt"
    extras.write_text("11\nbranch:homelab\n")
    exclusions = tmp_path / "exclude.txt"
    exclusions.write_text("11\n999\n")
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
            "--exclude",
            str(exclusions),
            "--staging-only",
            "--report",
            str(report_path),
        ]
    )

    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [(p["pr"], p["source"]) for p in report["applied"]] == [
        (12, "open"),
        (None, "extra-branch"),
    ]
    assert report["excluded"] == [
        {"pr": 11, "reason": "held out via exclude.txt"},
        {"pr": 999, "reason": "held out via exclude.txt"},
    ]
    assert git(env.work, "show", f"{report['staging_sha']}:homelab.txt").stdout == "lab\n"
    capsys.readouterr()


def test_branch_pin_merges_from_fork_and_records_sha(env, tmp_path):
    oid = env.add_fork_branch("homelab", "homelab.txt", "lab\n")
    extras = tmp_path / "extras.txt"
    extras.write_text("branch:homelab\n")
    prs_json = tmp_path / "prs.json"
    prs_json.write_text("[]")
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
    assert [(p["branch"], p["source"], p["oid"]) for p in report["applied"]] == [
        ("homelab", "extra-branch", oid)
    ]
    assert report["skipped"] == []
    assert git(env.work, "show", f"{report['staging_sha']}:homelab.txt").stdout == "lab\n"


def test_branch_pin_composition_decodes_and_reruns_reproducibly(env):
    oid = env.add_fork_branch("homelab", "homelab.txt", "lab\n")
    pin = {"source": "extra-branch", "headRefName": "homelab", "number": None}

    first = env.run([pin], staging_only=True)
    assert stage_mod.composition_of(env.work, first["staging_sha"]) == (
        first["upstream_sha"],
        {"branch:homelab": ("homelab", oid[:12])},
    )

    same = env.run([pin], staging_only=True)
    assert same["pushed"] is False
    assert same["staging_sha"] == first["staging_sha"]

    env.advance_fork_branch("homelab", "homelab-2.txt", "lab 2\n")
    changed = env.run([pin], staging_only=True)
    assert changed["pushed"] is True
    assert changed["causes"] == ["extras"]


def test_branch_pin_transport_failure_blocks_push(env, tmp_path, monkeypatch, capsys):
    oid = env.add_fork_branch("homelab", "homelab.txt", "lab\n")
    pin = {"source": "extra-branch", "headRefName": "homelab", "number": None}
    first = env.run([pin], staging_only=True)
    assert first["applied"][0]["oid"] == oid

    extras = tmp_path / "extras.txt"
    extras.write_text("branch:homelab\n")
    prs_json = tmp_path / "prs.json"
    prs_json.write_text("[]")
    report_path = tmp_path / "r.json"
    _break_ref(monkeypatch, "refs/heads/homelab", commands=("fetch", "ls-remote"), fail_times=99)

    assert (
        stage_mod.main(
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
        == 0
    )
    report = json.loads(report_path.read_text())
    assert report["blocked"] == ["homelab"]
    assert report["pushed"] is False
    assert report["skipped"] == [
        {
            "pr": None,
            "branch": "homelab",
            "conflict_paths": [],
            "reason": stage_mod.EXTRA_FETCH_FAILED,
            "source": "extra-branch",
        }
    ]
    assert env.fork_ref("refs/heads/staging") == first["staging_sha"]
    warning = capsys.readouterr().out
    assert "could not reach remote for extras branch:homelab" in warning
    assert "could not reach upstream" not in warning

    with pytest.raises(stage_mod.StageError, match="branch:homelab"):
        env.run([pin])
    assert env.fork_ref("refs/heads/staging") == first["staging_sha"]


def test_production_excludes_drafts(env, tmp_path, monkeypatch, capsys):
    """isDraft is the production gate: a draft PR never reaches the production
    composition, while the very same list still fully composes for staging."""
    ready = {**env.add_pr(3, "ready.txt", "r\n"), "isDraft": False}
    draft = {**env.add_pr(5, "draft.txt", "d\n"), "isDraft": True}
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([ready, draft]))
    no_extras = tmp_path / "no-extras.txt"  # missing file == no extras
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
            "--ring",
            "production",
            "--prs-json",
            str(prs_json),
            "--extras",
            str(no_extras),
            "--report",
            str(report_path),
        ]
    )
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [p["pr"] for p in report["applied"]] == [3]
    assert report["skipped"] == []
    assert "merge PR #5" not in env.fork_log("production")
    assert "## Personal production nightly" in summary.read_text()

    # same list, staging ring: a draft is composed like any other open PR
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
            str(no_extras),
            "--report",
            str(report_path),
        ]
    )
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [p["pr"] for p in report["applied"]] == [3, 5]
    capsys.readouterr()


def test_production_reads_its_own_extras(env, tmp_path, monkeypatch, capsys):
    """Production consults the manifest handed to it: a PR pin is fetched via
    refs/pull/N/head and composed as an ``extra`` alongside the open stream."""
    open_pr = {**env.add_pr(11, "i.txt", "i\n"), "isDraft": False}
    env.add_pr(12, "j.txt", "j\n")  # reachable only through the manifest
    prs_json = tmp_path / "prs.json"
    prs_json.write_text(json.dumps([open_pr]))
    extras = tmp_path / "extras.txt"
    extras.write_text("12\n")
    report_path = tmp_path / "r.json"

    fetched: list[tuple] = []
    real_fetch = stage_mod.fetch_extra
    monkeypatch.setattr(
        stage_mod, "fetch_extra", lambda *a, **k: (fetched.append(a), real_fetch(*a, **k))[1]
    )

    rc = stage_mod.main(
        [
            "stage",
            "--workdir",
            str(env.work),
            "--date",
            STAMP,
            "--ring",
            "production",
            "--prs-json",
            str(prs_json),
            "--extras",
            str(extras),
            "--report",
            str(report_path),
        ]
    )
    assert rc == 0
    report = json.loads(report_path.read_text())
    assert [(p["pr"], p["source"]) for p in report["applied"]] == [(11, "open"), (12, "extra")]
    assert report["skipped"] == []
    assert len(fetched) == 1
    # the ring's own branch is still a self-reference for any branch pin
    assert "self-reference" in stage_mod._validate_branch_pin("production", stage_mod.PRODUCTION)
    capsys.readouterr()


def test_production_extras_reject_branch_pins(tmp_path):
    """PR pins are reviewable upstream refs; a raw fork branch is not — a
    ``branch:`` line in the production manifest fails loud at parse time."""
    manifest = tmp_path / "extras-production.txt"
    manifest.write_text("3182\nbranch:homelab\n")
    with pytest.raises(stage_mod.StageError, match=r"branch pins are not valid production"):
        stage_mod.parse_extras(manifest, stage_mod.PRODUCTION)


@pytest.mark.parametrize("source", ["extra-branch"])
def test_direct_production_stage_rejects_branch_extras_before_git(env, source):
    with pytest.raises(stage_mod.StageError, match=r"production.*extras"):
        env.run([{"source": source}], ring=stage_mod.PRODUCTION)

    assert git(env.work, "rev-parse", "-q", "--verify", "FETCH_HEAD", check=False).returncode != 0
    assert git(env.work, "ls-remote", str(env.fork)).stdout.strip() == ""


def test_production_pin_prefix(env):
    """The production nightly mints production-YYYYMMDD (branch + tag) at the
    composed sha — no nightly-* ref anywhere."""
    pr = env.add_pr(7, "c.txt", "7\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    assert report["tag"] == f"production-{STAMP}"
    assert report["branch"] == f"production-{STAMP}"
    assert report["pin_created"] is True
    assert env.fork_ref(f"refs/tags/production-{STAMP}") == report["staging_sha"]
    assert env.fork_ref(f"refs/heads/production-{STAMP}") == report["staging_sha"]
    assert env.fork_ref("refs/heads/production") == report["staging_sha"]
    refs = [
        line.split("\t")[1]
        for line in git(env.work, "ls-remote", str(env.fork)).stdout.strip().splitlines()
    ]
    assert not any("nightly" in r for r in refs), refs


def test_production_subject_roundtrip(env):
    """Minted subjects carry the production prefix, decode via
    composition_of(ring=PRODUCTION), and are opaque to the staging regexes."""
    pr = env.add_pr(6, "h.txt", "h\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    subject = env.fork_log("production").splitlines()[0]
    assert subject == f"production: merge PR #6 (pr-6 @ {pr['headRefOid'][:12]})"
    assert stage_mod.composition_of(env.work, report["staging_sha"], stage_mod.PRODUCTION) == (
        report["upstream_sha"],
        {6: ("pr-6", pr["headRefOid"][:12])},
    )
    assert stage_mod.merge_re().fullmatch(subject) is None
    assert stage_mod.branch_merge_re().fullmatch(subject) is None


def test_production_mints_no_dev_tag(env):
    """mint_dev_tag=False: no vX.Y.Z.devYYYYMMDD ref is pushed, the report
    carries no dev_tag, and the human surfaces render without a Dev-tag row."""
    pr = env.add_pr(8, "g.txt", "g\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    assert "dev_tag" not in report
    refs = [
        line.split("\t")[1]
        for line in git(env.work, "ls-remote", str(env.fork)).stdout.strip().splitlines()
    ]
    assert not any(".dev" in r for r in refs), refs
    assert "Dev tag" not in stage_mod.notes(report, signed=True, ring=stage_mod.PRODUCTION)
    assert "Dev tag" not in stage_mod.summarize(report, stage_mod.PRODUCTION)


def test_production_rejects_staging_only(env, tmp_path, capsys):
    """--staging-only is the hourly staging mode; production has no hourly
    path by design, and letting the combination through would force-push
    refs/heads/production BEFORE the migration gate. Rejected at the parser,
    before any fetch or push."""
    prs_json = tmp_path / "prs.json"
    prs_json.write_text("[]")
    with pytest.raises(SystemExit) as exc:
        stage_mod.main(
            [
                "stage",
                "--workdir",
                str(env.work),
                "--date",
                STAMP,
                "--ring",
                "production",
                "--staging-only",
                "--prs-json",
                str(prs_json),
                "--report",
                str(tmp_path / "r.json"),
            ]
        )
    assert exc.value.code == 2
    assert "--staging-only is only valid for --ring staging" in capsys.readouterr().err
    # rejected before any git work: nothing fetched, no ref reached the fork
    assert git(env.work, "ls-remote", str(env.fork)).stdout.strip() == ""
    assert git(env.work, "rev-parse", "-q", "--verify", "FETCH_HEAD", check=False).returncode != 0


def test_production_rejects_missing_isdraft():
    """The draft gate must fail CLOSED: a record whose isDraft is missing or
    non-boolean is indeterminate and must raise (naming the PR), never pass
    as non-draft into the production composition."""
    ok = {"number": 1, "isDraft": False}
    assert stage_mod.filter_drafts([ok, {"number": 2, "isDraft": True}]) == [ok]
    for bad in (
        {"number": 7, "headRefName": "b", "headRefOid": "a" * 40},  # missing
        {"number": 7, "isDraft": None},
        {"number": 7, "isDraft": "false"},
    ):
        with pytest.raises(stage_mod.StageError, match=r"#7"):
            stage_mod.filter_drafts([ok, bad])


def test_production_ring_has_its_own_extras_manifest():
    """Production consults extras, but never staging's manifest: the two are
    separate files so a staging pin cannot reach production by accident."""
    assert stage_mod.PRODUCTION.use_extras
    assert stage_mod.PRODUCTION.extras_file.name == "extras-production.txt"
    assert stage_mod.STAGING.extras_file.name == "extras.txt"
    assert stage_mod.PRODUCTION.extras_file != stage_mod.STAGING.extras_file


def test_production_extra_promotes_a_draft_past_the_gate():
    """A pin in the production extras manifest force-includes a draft PR:
    filter_drafts drops it from the open stream, and merge_stream re-adds it
    as an ``extra`` (composed from refs/pull/N/head) — the manifest, not the
    draft bit, is the override."""
    draft = {"number": 3182, "isDraft": True, "headRefName": "b", "headRefOid": "a" * 40}
    ready = {"number": 5, "isDraft": False, "headRefName": "r", "headRefOid": "b" * 40}
    stream = stage_mod.merge_stream(stage_mod.filter_drafts([draft, ready]), [3182])
    assert [(p["number"], p["source"]) for p in stream] == [(5, "open"), (3182, "extra")]


def test_production_commit_identity(env):
    """Merge commits carry the ring's own identity — a production merge is
    authored omnigent-production, not the staging identity; staging's exact
    bytes are separately locked by the golden."""
    pr = {**env.add_pr(6, "h.txt", "h\n"), "isDraft": False}
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    obj = git(env.work, "cat-file", "-p", report["staging_sha"]).stdout
    assert "author omnigent-production <production@invalid>" in obj
    assert "committer omnigent-production <production@invalid>" in obj
    assert "staging" not in obj

    staging = env.run([pr])
    obj = git(env.work, "cat-file", "-p", staging["staging_sha"]).stdout
    assert "author omnigent-staging <staging@invalid>" in obj
    assert "committer omnigent-staging <staging@invalid>" in obj


@pytest.mark.parametrize("tampering", ["subject", "identity", "first-parent"])
def test_production_identity_tampering_fails_before_push(env, monkeypatch, tampering):
    pr = env.add_pr(21, "identity.txt", "identity\n")
    real_merge_prs = stage_mod.merge_prs

    def tampered_merge_prs(
        cwd, prs, upstream, fork="origin", ring=stage_mod.STAGING, upstream_sha=""
    ):
        applied, skipped = real_merge_prs(cwd, prs, upstream, fork, ring, upstream_sha)
        subject = "production: merge PR #21 (pr-21 @ " + pr["headRefOid"][:12] + ")"
        if tampering == "first-parent":
            stage_mod.git(
                cwd,
                *stage_mod.commit_ident(ring),
                "commit",
                "--allow-empty",
                "--no-verify",
                "-m",
                subject,
            )
        else:
            if tampering == "subject":
                subject = "staging: merge PR #21 (pr-21 @ " + pr["headRefOid"][:12] + ")"
            ident = (
                ["-c", "user.name=wrong", "-c", "user.email=wrong@invalid"]
                if tampering == "identity"
                else stage_mod.commit_ident(ring)
            )
            stage_mod.git(
                cwd,
                *ident,
                "commit",
                "--amend",
                "--no-verify",
                "-m",
                subject,
                env={
                    "GIT_AUTHOR_NAME": "wrong"
                    if tampering == "identity"
                    else "omnigent-production",
                    "GIT_AUTHOR_EMAIL": (
                        "wrong@invalid" if tampering == "identity" else "production@invalid"
                    ),
                },
            )
        return applied, skipped

    monkeypatch.setattr(stage_mod, "merge_prs", tampered_merge_prs)
    with pytest.raises(stage_mod.StageError, match="production identity"):
        env.run([pr], ring=stage_mod.PRODUCTION)

    assert git(env.work, "ls-remote", str(env.fork)).stdout.strip() == ""


def test_production_zero_merge_candidate_is_valid(env):
    report = env.run([], ring=stage_mod.PRODUCTION)

    assert report["staging_sha"] == report["upstream_sha"]
    assert report["applied"] == []
    assert env.fork_ref("refs/heads/production") == report["upstream_sha"]
    assert env.fork_ref(f"refs/tags/production-{STAMP}") == report["upstream_sha"]


def test_notes_production_ring_has_apk_section(tmp_path, capsys):
    """Production ships a re-signed debug APK like staging, so its notes
    carry the APK-signing section in both variants — and the notes CLI
    routes there via --ring (default stays staging, golden-locked)."""
    report = {
        "date": STAMP,
        "upstream_sha": "u" * 40,
        "staging_sha": "s" * 40,
        "applied": [],
        "skipped": [],
    }
    out = stage_mod.notes(report, signed=True, ring=stage_mod.PRODUCTION)
    assert "production ring" in out
    assert "## APK signing" in out and "shared debug keystore" in out
    out = stage_mod.notes(report, signed=False, ring=stage_mod.PRODUCTION)
    assert "## APK signing" in out and "runner-ephemeral keystore" in out

    report_path = tmp_path / "r.json"
    report_path.write_text(json.dumps(report))
    rc = stage_mod.main(
        ["notes", "--report", str(report_path), "--ring", "production", "--signed", "false"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "production ring" in out and "## APK signing" in out

    rc = stage_mod.main(["notes", "--report", str(report_path), "--signed", "false"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "staging ring" in out and "## APK signing" in out


MIGRATIONS_DIR = "omnigent/db/migrations/versions"


def test_migration_touched_on_add(env):
    """A composition whose upstream..candidate diff adds a migration file is
    schema-changing — caught even with no previous pin to compare against."""
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    pr = env.add_pr(3, f"{MIGRATIONS_DIR}/0001_add.py", "rev\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    assert (
        stage_mod.migration_touched(env.work, report["staging_sha"], report["upstream_sha"], None)
        is True
    )


def test_migration_touched_on_removal(env):
    """A candidate that DROPS a migration present in the previous pin is
    schema-changing even though upstream..candidate is clean — the second
    diff leg exists precisely for this."""
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    mig = env.add_pr(4, f"{MIGRATIONS_DIR}/0002_drop.py", "rev\n")
    prev = env.run([mig], ring=stage_mod.PRODUCTION)

    other = env.add_pr(5, "plain.txt", "p\n")
    env.advance_main("bump.txt", "b\n")
    cur = env.run([other], ring=stage_mod.PRODUCTION)
    # the upstream leg alone is clean...
    assert (
        stage_mod.migration_touched(env.work, cur["staging_sha"], cur["upstream_sha"], None)
        is False
    )
    # ...but prev_pin..candidate shows the dropped migration
    assert (
        stage_mod.migration_touched(
            env.work, cur["staging_sha"], cur["upstream_sha"], prev["staging_sha"]
        )
        is True
    )


def test_no_migration_change(env):
    """Code-only candidates auto-promote: neither diff leg touches the
    migrations path."""
    pr = env.add_pr(6, "feat.txt", "f\n")
    prev = env.run([pr], ring=stage_mod.PRODUCTION)
    env.advance_main("more.txt", "m\n")
    cur = env.run([pr], ring=stage_mod.PRODUCTION)
    assert (
        stage_mod.migration_touched(
            env.work, cur["staging_sha"], cur["upstream_sha"], prev["staging_sha"]
        )
        is False
    )
    # the watched path prefix is a parameter, not a baked-in constant
    assert (
        stage_mod.migration_touched(
            env.work, cur["staging_sha"], cur["upstream_sha"], None, prefix="feat.txt"
        )
        is True
    )


def test_production_migration_gate_blocks_push(env):
    """A schema-changing composition must not auto-publish (decision 11): the
    nightly stops before its atomic push, so NOTHING lands on the fork, and
    the report says exactly which sha to approve."""
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    pr = env.add_pr(3, f"{MIGRATIONS_DIR}/0001_add.py", "rev\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)

    assert report["pushed"] is False
    gate = report["migration_gate"]
    assert gate["blocked"] is True
    assert gate["candidate"] == report["staging_sha"]
    assert gate["prev_pin"] is None  # bootstrap: gated on the upstream leg only
    assert f"approve_migration={report['staging_sha']}" in gate["approval_hint"]
    # nothing was pinned, so the report carries no pin fields at all
    assert "tag" not in report and "pin_created" not in report
    # the fork fixture remote received no refs whatsoever
    assert git(env.work, "ls-remote", str(env.fork)).stdout.strip() == ""
    text = stage_mod.summarize(report, stage_mod.PRODUCTION)
    assert "BLOCKED" in text and report["staging_sha"] in text


def test_production_migration_gate_approval_publishes(env):
    """The operator re-dispatch carries the exact candidate sha; composition
    is byte-reproducible, so the approved rerun mints that sha and pushes."""
    (env.seed / MIGRATIONS_DIR).mkdir(parents=True)
    pr = env.add_pr(4, f"{MIGRATIONS_DIR}/0002_add.py", "rev\n")
    blocked = env.run([pr], ring=stage_mod.PRODUCTION)
    assert blocked["migration_gate"]["blocked"] is True

    report = env.run([pr], ring=stage_mod.PRODUCTION, migration_approval=blocked["staging_sha"])
    assert report["staging_sha"] == blocked["staging_sha"]
    assert report["migration_gate"]["blocked"] is False
    assert report["tag"] == f"production-{STAMP}"
    assert env.fork_ref("refs/heads/production") == report["staging_sha"]
    assert env.fork_ref(f"refs/tags/production-{STAMP}") == report["staging_sha"]


def test_production_migration_gate_clean_composes(env):
    """Code-only candidates auto-promote: the gate reports blocked=False and
    the push happens exactly as before the gate existed. The second night
    diffs against the previous production pin (latest tag wins)."""
    pr = env.add_pr(5, "code.txt", "c\n")
    report = env.run([pr], ring=stage_mod.PRODUCTION)
    assert report["migration_gate"] == {
        "blocked": False,
        "candidate": report["staging_sha"],
        "prev_pin": None,
    }
    assert report["tag"] == f"production-{STAMP}"
    assert env.fork_ref("refs/heads/production") == report["staging_sha"]

    env.advance_main("more.txt", "m\n")
    second = env.run([pr], ring=stage_mod.PRODUCTION)
    assert second["migration_gate"]["blocked"] is False
    assert second["migration_gate"]["prev_pin"] == report["staging_sha"]
    assert second["tag"] == f"production-{STAMP}-rerun1"
    assert env.fork_ref("refs/heads/production") == second["staging_sha"]


def test_staging_report_has_no_migration_gate_key(env):
    """The gate is production-only surface: staging reports (nightly and
    hourly) carry no migration_gate key — the golden enforces the bytes."""
    pr = env.add_pr(6, "s.txt", "s\n")
    nightly = env.run([pr])
    assert "migration_gate" not in nightly and "pushed" not in nightly
    hourly = env.run([pr], staging_only=True)
    assert "migration_gate" not in hourly


def test_branch_pin_missing_is_loud_advisory_skip(env, tmp_path):
    extras = tmp_path / "extras.txt"
    extras.write_text("branch:no-such-branch\n")
    prs_json = tmp_path / "prs.json"
    prs_json.write_text("[]")
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
    assert report["applied"] == []
    assert report["skipped"] == [
        {
            "pr": None,
            "branch": "no-such-branch",
            "conflict_paths": [],
            "reason": stage_mod.EXTRA_MISSING,
            "source": "extra-branch",
        }
    ]


def _record_seed(tmp_path: Path, env: Env, pr: dict, resolutions: dict[str, str]) -> Path:
    """Merge *pr* onto current upstream main in a throwaway clone, resolve the
    conflicts with *resolutions* ({path: content}), and return an rr-cache
    seed directory holding the recorded entries."""
    clone = tmp_path / f"seedgen-{pr['number']}"
    # -b: the bare upstream's HEAD may name a branch that was never pushed
    # (init.defaultBranch vs the harness's "main"), which some git versions
    # clone as an unborn checkout — the merge below would silently no-op.
    git(tmp_path, "clone", "-b", "main", str(env.upstream), str(clone))
    git(clone, "config", "user.name", "t")
    git(clone, "config", "user.email", "t@t")
    git(clone, "config", "rerere.enabled", "true")
    git(clone, "fetch", "origin", f"refs/pull/{pr['number']}/head")
    git(clone, "merge", "--no-ff", "--no-commit", pr["headRefOid"], check=False)
    for path, content in resolutions.items():
        (clone / path).write_text(content)
        git(clone, "add", path)
    git(clone, "commit", "--no-verify", "-m", "record resolution")
    seed = tmp_path / f"rr-seed-{pr['number']}"
    shutil.copytree(clone / ".git" / "rr-cache", seed)
    assert any(seed.iterdir()), "seed recording produced no rr-cache entries"
    return seed


def test_rr_cache_seed_resolves_conflicting_pr(env, tmp_path):
    pr = env.add_pr(3, "a.txt", "pr side\n")
    env.advance_main("a.txt", "main side\n")
    seed = _record_seed(tmp_path, env, pr, {"a.txt": "reconciled\n"})

    report = env.run([pr], rr_cache_dir=seed)

    assert report["skipped"] == []
    (applied,) = report["applied"]
    assert applied["pr"] == 3
    assert applied["rerere_paths"] == ["a.txt"]
    assert git(env.fork, "show", "staging:a.txt").stdout == "reconciled\n"
    # The resolution is a composition input: a rerun reproduces the same sha.
    rerun = env.run([pr], rr_cache_dir=seed)
    assert rerun["staging_sha"] == report["staging_sha"]
    # Notes call out which paths the committed resolution covered.
    assert "resolved from rr-cache: `a.txt`" in stage_mod.notes(report, signed=True)


def test_rr_cache_partial_coverage_still_skips(env, tmp_path):
    branch = "pr-3"
    git(env.seed, "checkout", "-q", "main")
    git(env.seed, "checkout", "-q", "-b", branch)
    commit_file(env.seed, "a.txt", "pr alpha\n", "pr 3 a")
    oid = commit_file(env.seed, "b.txt", "pr beta\n", "pr 3 b")
    git(env.seed, "push", str(env.upstream), f"{branch}:refs/pull/3/head")
    pr = {"number": 3, "headRefName": branch, "headRefOid": oid}
    env.advance_main("a.txt", "main alpha\n")
    env.advance_main("b.txt", "main beta\n")
    seed = _record_seed(tmp_path, env, pr, {"a.txt": "res a\n", "b.txt": "res b\n"})
    # Drop the b.txt entry so the seed covers only half the merge.
    for entry in list(seed.iterdir()):
        if "beta" in (entry / "preimage").read_text():
            shutil.rmtree(entry)

    report = env.run([pr], rr_cache_dir=seed)

    assert report["applied"] == []
    (skipped,) = report["skipped"]
    assert skipped["conflict_paths"] == ["a.txt", "b.txt"]


def test_delete_modify_conflict_never_auto_resolves(env, tmp_path):
    pr = env.add_pr(3, "a.txt", "pr side\n")
    git(env.seed, "checkout", "-q", "main")
    git(env.seed, "rm", "-q", "a.txt")
    git(env.seed, "commit", "--no-verify", "-m", "main: drop a.txt")
    git(env.seed, "push", str(env.upstream), "main")
    seed = tmp_path / "rr-seed"
    seed.mkdir()

    report = env.run([pr], rr_cache_dir=seed)

    assert report["applied"] == []
    (skipped,) = report["skipped"]
    assert skipped["conflict_paths"] == ["a.txt"]


def test_seed_rerere_rejects_malformed_entries(env, tmp_path):
    missing = tmp_path / "no-such-dir"
    assert stage_mod.seed_rerere(env.work, missing) == 0
    assert stage_mod.seed_rerere(env.work, None) == 0

    bad_name = tmp_path / "seed-bad-name"
    (bad_name / "not-a-hash").mkdir(parents=True)
    with pytest.raises(stage_mod.StageError, match="not a conflict-hash"):
        stage_mod.seed_rerere(env.work, bad_name)

    half = tmp_path / "seed-half"
    entry = half / ("ab" * 20)
    entry.mkdir(parents=True)
    (entry / "preimage").write_text("x")
    with pytest.raises(stage_mod.StageError, match="preimage/postimage"):
        stage_mod.seed_rerere(env.work, half)


def test_cli_rr_cache_flag_is_plumbed(env, tmp_path, capsys):
    pr = env.add_pr(3, "a.txt", "pr side\n")
    env.advance_main("a.txt", "main side\n")
    seed = _record_seed(tmp_path, env, pr, {"a.txt": "reconciled\n"})
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
            "--rr-cache",
            str(seed),
        ]
    )
    assert rc == 0
    capsys.readouterr()
    report = json.loads(report_path.read_text())
    assert report["applied"][0]["rerere_paths"] == ["a.txt"]


def test_own_prs_filters_on_the_records_author():
    """An ignored --author flag (observed: gh's search-backed filter coming
    back as a plain unfiltered listing) must never compose someone else's PR."""
    mine = {"number": 7, "headRefName": "b7", "author": {"login": "btli"}}
    upper = {"number": 8, "headRefName": "b8", "author": {"login": "BTLI"}}
    foreign = {"number": 9, "headRefName": "b9", "author": {"login": "mallory"}}

    kept = stage_mod.own_prs([mine, upper, foreign])

    assert [p["number"] for p in kept] == [7, 8]
    assert all("author" not in p for p in kept)


def test_own_prs_fails_loud_on_missing_author():
    for record in ({"number": 7}, {"number": 7, "author": {}}, {"number": 7, "author": "btli"}):
        with pytest.raises(stage_mod.StageError, match="no author login"):
            stage_mod.own_prs([record])
