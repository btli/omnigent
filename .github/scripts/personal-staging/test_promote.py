from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import promote
import pytest

SHA = "a" * 40
SUMS = f"{'1' * 64}  ./omnigent-staging-debug.apk\n{'2' * 64}  ./merge-report.json\n"


def test_build_complete_lists_assets_and_digests():
    doc = promote.build_complete(
        "nightly-20261010", SHA, SUMS, "sha256:" + "3" * 64, "sha256:" + "4" * 64
    )
    assert doc == {
        "schema": 1,
        "tag": "nightly-20261010",
        "sha": SHA,
        "assets": {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64},
        "images": {"server": "sha256:" + "3" * 64, "host": "sha256:" + "4" * 64},
    }


@pytest.mark.parametrize(
    "tag,sha,server",
    [
        ("production-20261010", SHA, "sha256:" + "3" * 64),
        ("nightly-20261010", "xyz", "sha256:" + "3" * 64),
        ("nightly-20261010", SHA, "latest"),
    ],
)
def test_build_complete_rejects_bad_inputs(tag, sha, server):
    with pytest.raises(promote.PromoteError):
        promote.build_complete(tag, sha, SUMS, server, "sha256:" + "4" * 64)


def test_build_complete_rejects_malformed_sums():
    with pytest.raises(promote.PromoteError):
        promote.build_complete(
            "nightly-20261010", SHA, "garbage\n", "sha256:" + "3" * 64, "sha256:" + "4" * 64
        )


def test_build_complete_cli(tmp_path, capsys):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(SUMS)
    assert (
        promote.main(
            [
                "build-complete",
                "--tag",
                "nightly-20261010",
                "--sha",
                SHA,
                "--sums",
                str(sums),
                "--server-digest",
                "sha256:" + "3" * 64,
                "--host-digest",
                "sha256:" + "4" * 64,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["assets"]["merge-report.json"] == "2" * 64


def git(cwd, *args):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, text=True, capture_output=True
    ).stdout.strip()


class Repo:
    """Bare fork + working clone; nightly/production tags are pushed lightweight."""

    def __init__(self, tmp: Path):
        self.fork = tmp / "fork.git"
        self.work = tmp / "work"
        self.fork.mkdir()
        git(self.fork, "init", "--bare", "-q")
        self.work.mkdir()
        git(self.work, "init", "-q", "-b", "main")
        git(self.work, "config", "user.name", "t")
        git(self.work, "config", "user.email", "t@t")
        git(self.work, "remote", "add", "origin", str(self.fork))
        self.base = self.commit("README", "base\n")
        git(self.work, "push", "-q", "origin", "main")

    def commit(self, name: str, content: str) -> str:
        p = self.work / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        git(self.work, "add", name)
        git(self.work, "commit", "-q", "--no-verify", "-m", name)
        return git(self.work, "rev-parse", "HEAD")

    def tag(self, name: str, sha: str) -> None:
        git(self.work, "push", "-q", "origin", f"+{sha}:refs/tags/{name}")

    def annotated_tag(self, name: str, sha: str) -> None:
        git(self.work, "tag", "-f", "-a", "-m", name, name, sha)
        git(self.work, "push", "-q", "-f", "origin", f"refs/tags/{name}")


class FakeGitHub:
    def __init__(self):
        self.statuses_by_sha: dict[str, list[dict]] = {}
        self.releases: dict[str, dict[str, object]] = {}  # tag -> {asset name: json-or-bytes}

    def statuses(self, sha):
        return self.statuses_by_sha.get(sha, [])

    def release_assets(self, tag):
        r = self.releases.get(tag)
        return None if r is None else sorted(r)

    def release_json(self, tag, name):
        r = self.releases.get(tag) or {}
        v = r.get(name)
        return v if isinstance(v, dict) else None


LOGIN = "soak-bot"
DATE = "20261011"


def status(state, login=LOGIN, context="soak/homelab"):
    return {
        "context": context,
        "state": state,
        "creator": {"login": login},
        "target_url": "https://soak/run/1",
    }


UPSTREAM = "a" * 40  # upstream main: never local, never diffed by the promote gate


def complete_nightly(repo, gh, tag, sha, base_sha=None, upstream_sha=UPSTREAM):
    """Tag *sha* as *tag* with a release carrying build-complete.json + merge report.

    *base_sha* is fork main as the nightly composed on it (default: the repo root).
    """
    repo.tag(tag, sha)
    bc = {
        "schema": 1,
        "tag": tag,
        "sha": sha,
        "assets": {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64},
        "images": {"server": "sha256:" + "3" * 64, "host": "sha256:" + "4" * 64},
    }
    gh.releases[tag] = {
        "omnigent-staging-debug.apk": b"apk",
        "merge-report.json": {"upstream_sha": upstream_sha, "base_sha": base_sha or repo.base},
        "SHA256SUMS": b"",
        "build-complete.json": bc,
    }


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def run_plan(repo, gh, **kw):
    args = {
        "trigger": "dispatch",
        "sha": None,
        "nightly": None,
        "soak_login": LOGIN,
        "approve_migration": "",
        "allow_older": False,
        "date": DATE,
    }
    args.update(kw)
    return promote.plan(repo.work, "origin", gh, **args)


def test_plan_promotes_trusted_status(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status("success")]
    p = run_plan(repo, gh, trigger="status", sha=sha)
    assert p["action"] == "promote"
    assert p["nightly"] == "nightly-20261010" and p["sha"] == sha
    assert p["production_tag"] == "production-20261011" and p["tag_created"] is True
    assert p["source"]["soak_target_url"] == "https://soak/run/1"
    assert p["source"]["production_tag"] == "production-20261011"
    assert p["assets"] == {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64}


def test_plan_status_disabled_without_login(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status("success")]
    assert run_plan(repo, gh, trigger="status", sha=sha, soak_login="")["action"] == "refused"


@pytest.mark.parametrize(
    "state,action", [("failure", "ignored"), ("error", "ignored"), ("pending", "ignored")]
)
def test_plan_ignores_non_success(repo, state, action):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status(state)]
    assert run_plan(repo, gh, trigger="status", sha=sha)["action"] == action


def test_plan_refuses_when_newest_status_untrusted(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status("success", login="mallory"), status("success")]
    assert run_plan(repo, gh, trigger="status", sha=sha)["action"] == "refused"


def test_plan_ignores_other_contexts(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status("success", context="ci/other")]
    assert run_plan(repo, gh, trigger="status", sha=sha)["action"] == "ignored"


def test_plan_refuses_incomplete_nightly(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    repo.tag("nightly-20261010", sha)
    gh.releases["nightly-20261010"] = {"omnigent-staging-debug.apk": b"apk"}
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "refused"


def test_plan_refuses_marker_sha_mismatch(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.releases["nightly-20261010"]["build-complete.json"]["sha"] = "b" * 40
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "refused"


def test_plan_refuses_marker_asset_missing_from_release(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    del gh.releases["nightly-20261010"]["omnigent-staging-debug.apk"]
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "refused"


def test_plan_picks_newest_nightly_tag_at_sha(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    complete_nightly(repo, gh, "nightly-20261010-rerun1", sha)
    gh.statuses_by_sha[sha] = [status("success")]
    assert run_plan(repo, gh, trigger="status", sha=sha)["nightly"] == "nightly-20261010-rerun1"


def test_plan_refuses_sha_without_nightly_tag(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    gh.statuses_by_sha[sha] = [status("success")]
    assert run_plan(repo, gh, trigger="status", sha=sha)["action"] == "refused"


def _promoted(repo, gh, ptag, psha, source_tag, latest=False):
    repo.tag(ptag, psha)
    gh.releases[ptag] = {"source.json": {"source_tag": source_tag, "sha": psha}}
    if latest:
        _latest_at(repo, gh, psha, source_tag)


def _latest_at(repo, gh, sha, source_tag, *, release=True):
    """Point production-latest at *sha*; *release* also gives it a matching source.json."""
    repo.tag("production-latest", sha)
    if release:
        gh.releases["production-latest"] = {"source.json": {"source_tag": source_tag, "sha": sha}}


def test_plan_forward_only(repo):
    gh = FakeGitHub()
    old = repo.commit("a.txt", "a\n")
    new = repo.commit("b.txt", "b\n")
    complete_nightly(repo, gh, "nightly-20261009", old)
    complete_nightly(repo, gh, "nightly-20261010", new)
    _promoted(repo, gh, "production-20261010", new, "nightly-20261010")
    assert run_plan(repo, gh, nightly="nightly-20261009")["action"] == "refused"
    assert run_plan(repo, gh, nightly="nightly-20261009", allow_older=True)["action"] == "promote"


def test_plan_noop_when_production_already_there(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010", latest=True)
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "noop"


def test_plan_legacy_pin_date_floor(repo):
    gh = FakeGitHub()
    legacy = repo.commit("legacy.txt", "l\n")
    repo.tag("production-20261010", legacy)
    gh.releases["production-20261010"] = {"merge-report.json": {}}
    old = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261009", old)
    same_day = repo.commit("b.txt", "b\n")
    complete_nightly(repo, gh, "nightly-20261010", same_day)
    assert run_plan(repo, gh, nightly="nightly-20261009")["action"] == "refused"
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "promote"


def test_plan_migration_gate_blocks_until_exact_approval(repo):
    gh = FakeGitHub()
    # fork main already carries a fork-only migration; the candidate adds another on top
    main = repo.commit(
        "omnigent/db/migrations/versions/0001_x.py", 'revision = "x"\ndown_revision = None\n'
    )
    sha = repo.commit(
        "omnigent/db/migrations/versions/0002_y.py", 'revision = "y"\ndown_revision = "x"\n'
    )
    complete_nightly(repo, gh, "nightly-20261010", sha, base_sha=main, upstream_sha=repo.base)
    blocked = run_plan(repo, gh, nightly="nightly-20261010")
    assert blocked["action"] == "blocked" and blocked["gate"]["blocked"] is True
    assert sha in blocked["gate"]["approval_hint"]
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration="f" * 40)[
        "action"
    ] == ("blocked")
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration=sha)["action"] == (
        "promote"
    )


def test_plan_fork_only_migration_on_main_does_not_gate(repo):
    gh = FakeGitHub()
    # upstream lacks the fork-only migration fork main carries; the candidate adds none
    main = repo.commit(
        "omnigent/db/migrations/versions/0001_x.py", 'revision = "x"\ndown_revision = None\n'
    )
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha, base_sha=main, upstream_sha=repo.base)
    p = run_plan(repo, gh, nightly="nightly-20261010")
    assert p["action"] == "promote" and p["gate"]["blocked"] is False
    assert p["source"]["approve_migration"] is None


@pytest.mark.parametrize(
    "report",
    [
        {"upstream_sha": "a" * 40},  # no base_sha
        {"upstream_sha": "a" * 40, "base_sha": "abc123"},
        {"upstream_sha": "a" * 40, "base_sha": "G" * 40},
        {"upstream_sha": "a" * 40, "base_sha": None},
        ["not", "an", "object"],
    ],
)
def test_plan_refuses_missing_or_invalid_base_sha(repo, report):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.releases["nightly-20261010"]["merge-report.json"] = report
    p = run_plan(repo, gh, nightly="nightly-20261010")
    assert p["action"] == "refused" and "base_sha" in p["reason"]


def test_plan_unfetchable_base_sha_is_operational_error(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha, base_sha="d" * 40)
    with pytest.raises(promote.PromoteError, match="cannot fetch"):
        run_plan(repo, gh, nightly="nightly-20261010")


def test_plan_refuses_shipped_migration_rewrite(repo):
    gh = FakeGitHub()
    path = "omnigent/db/migrations/versions/0001_x.py"
    shipped = repo.commit(path, 'revision = "x"\ndown_revision = None\n')
    _promoted(repo, gh, "production-20261009", shipped, "nightly-20261009")
    rewritten = repo.commit(path, 'revision = "x"\ndown_revision = None\n# edited\n')
    complete_nightly(repo, gh, "nightly-20261010", rewritten)
    p = run_plan(repo, gh, nightly="nightly-20261010", approve_migration=rewritten)
    assert p["action"] == "refused" and (
        "append-only" in p["reason"] or "migration history" in p["reason"]
    )


def test_plan_cli_writes_plan_for_refusal_and_error(repo, tmp_path, monkeypatch):
    gh = FakeGitHub()
    monkeypatch.setattr(promote, "GitHub", lambda _repo: gh)
    out = tmp_path / "plan.json"
    base = [
        "plan",
        "--workdir",
        str(repo.work),
        "--repo",
        "o/r",
        "--soak-login",
        LOGIN,
        "--out",
        str(out),
    ]
    assert promote.main([*base, "--trigger", "dispatch", "--nightly", "nightly-20990101"]) == 0
    assert json.loads(out.read_text())["action"] == "refused"
    assert promote.main([*base, "--trigger", "status"]) == 1  # status without --sha
    assert json.loads(out.read_text())["action"] == "error"


def test_plan_resumes_partial_promotion_reusing_tag(repo):
    gh = FakeGitHub()
    old = repo.commit("a.txt", "a\n")
    new = repo.commit("b.txt", "b\n")
    complete_nightly(repo, gh, "nightly-20261009", old)
    complete_nightly(repo, gh, "nightly-20261010", new)
    _promoted(repo, gh, "production-20261009", old, "nightly-20261009", latest=True)
    repo.tag("production-20261010", new)  # tag pushed, production-latest never moved
    p = run_plan(repo, gh, nightly="nightly-20261010", date="20261012")
    assert p["action"] == "promote"
    assert p["production_tag"] == "production-20261010" and p["tag_created"] is False
    assert p["source"]["production_tag"] == "production-20261010"
    assert p["prev_tag"] == "production-20261009" and p["prev_pin"] == old


def test_plan_noop_only_when_latest_points_at_sha(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010")
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "promote"
    repo.annotated_tag("production-latest", sha)  # annotated latest is peeled
    gh.releases["production-latest"] = {
        "source.json": {"source_tag": "nightly-20261010", "sha": sha}
    }
    assert run_plan(repo, gh, nightly="nightly-20261010")["action"] == "noop"


def test_plan_noop_requires_latest_release_showing_the_build(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010", latest=True)
    p = run_plan(repo, gh, nightly="nightly-20261010")
    assert p["action"] == "noop" and "production_tag" not in p


@pytest.mark.parametrize(
    "latest_release",
    [
        None,  # tag moved, release never created
        {},  # release exists, source.json never uploaded
        {"source.json": {"source_tag": "nightly-20261009", "sha": "e" * 40}},  # stale
        {"source.json": ["not", "an", "object"]},
    ],
)
def test_plan_repairs_partially_switched_latest(repo, latest_release):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010")
    _latest_at(repo, gh, sha, "nightly-20261010", release=False)
    if latest_release is not None:
        gh.releases["production-latest"] = latest_release
    p = run_plan(repo, gh, nightly="nightly-20261010", date="20261012")
    assert p["action"] == "promote"
    assert p["production_tag"] == "production-20261010" and p["tag_created"] is False


def test_plan_unreadable_latest_source_is_an_error_not_a_match(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010", latest=True)
    real = gh.release_json

    def flaky(tag, name):
        if tag == "production-latest":
            raise promote.PromoteError("cannot download production-latest/source.json")
        return real(tag, name)

    gh.release_json = flaky
    with pytest.raises(promote.PromoteError):
        run_plan(repo, gh, nightly="nightly-20261010")


def test_plan_reuses_newest_tag_when_several_at_sha(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    repo.tag("production-20261010", sha)
    repo.tag("production-20261011-rerun1", sha)
    p = run_plan(repo, gh, nightly="nightly-20261010")
    assert p["production_tag"] == "production-20261011-rerun1" and p["tag_created"] is False


def _rolled_back(repo, gh):
    """production-20261010 (new) exists, but production-latest was rolled back to old."""
    old = repo.commit("a.txt", "a\n")
    new = repo.commit("b.txt", "b\n")
    complete_nightly(repo, gh, "nightly-20261009", old)
    complete_nightly(repo, gh, "nightly-20261010", new)
    _promoted(repo, gh, "production-20261009", old, "nightly-20261009")
    _promoted(repo, gh, "production-20261010", new, "nightly-20261010")
    repo.tag("production-latest", old)
    return old, new


def test_plan_rollback_reuses_old_production_tag(repo):
    gh = FakeGitHub()
    _, new = _rolled_back(repo, gh)
    repo.tag("production-latest", new)
    assert run_plan(repo, gh, nightly="nightly-20261009")["action"] == "refused"
    p = run_plan(repo, gh, nightly="nightly-20261009", allow_older=True)
    assert p["action"] == "promote"
    assert p["production_tag"] == "production-20261009" and p["tag_created"] is False


def test_plan_forward_only_uses_current_production_not_newest_pin(repo):
    gh = FakeGitHub()
    _rolled_back(repo, gh)
    mid = repo.commit("c.txt", "c\n")
    complete_nightly(repo, gh, "nightly-20261009-rerun1", mid)
    # newer than current production's source (20261009), older than the newest pin's (20261010)
    p = run_plan(repo, gh, nightly="nightly-20261009-rerun1")
    assert p["action"] == "promote" and p["prev_tag"] == "production-20261009"
    older = repo.commit("d.txt", "d\n")
    complete_nightly(repo, gh, "nightly-20261008", older)
    assert run_plan(repo, gh, nightly="nightly-20261008")["action"] == "refused"


def test_plan_prev_pin_for_migration_gate_is_current_production(repo):
    gh = FakeGitHub()
    old, _ = _rolled_back(repo, gh)
    # candidate adds a migration relative to current production (old) only
    mig = repo.commit(
        "omnigent/db/migrations/versions/0001_x.py", 'revision = "x"\ndown_revision = None\n'
    )
    complete_nightly(repo, gh, "nightly-20261011", mig, base_sha=mig)
    p = run_plan(repo, gh, nightly="nightly-20261011")
    assert p["action"] == "blocked" and p["prev_pin"] == old


@pytest.mark.parametrize("state", ["failure", "error", "pending"])
def test_plan_ignored_status_reports_state(repo, state):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status(state)]
    p = run_plan(repo, gh, trigger="status", sha=sha)
    assert p["action"] == "ignored" and p["state"] == state
    assert p["sha"] == sha and p["nightly"] == "nightly-20261010"


def test_plan_status_refuses_dispatch_only_inputs(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    gh.statuses_by_sha[sha] = [status("success")]
    for kw in ({"approve_migration": sha}, {"allow_older": True}):
        p = run_plan(repo, gh, trigger="status", sha=sha, **kw)
        assert p["action"] == "refused" and "dispatch" in p["reason"]


def test_plan_refusals_carry_sha_and_nightly_when_known(repo):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    repo.tag("nightly-20261010", sha)
    gh.releases["nightly-20261010"] = {}
    p = run_plan(repo, gh, nightly="nightly-20261010")
    assert p["action"] == "refused" and p["sha"] == sha and p["nightly"] == "nightly-20261010"


def test_plan_fetch_failure_is_operational_error(repo, monkeypatch):
    gh = FakeGitHub()
    sha = repo.commit("a.txt", "a\n")
    complete_nightly(repo, gh, "nightly-20261010", sha)
    # candidate commit absent locally and the fetch fails
    git(repo.work, "reset", "-q", "--hard", repo.base)
    git(repo.work, "reflog", "expire", "--expire=now", "--all")
    git(repo.work, "gc", "-q", "--prune=now")
    git(repo.work, "remote", "set-url", "origin", str(repo.fork) + "-gone")
    monkeypatch.setattr(promote, "_tags", lambda *_: {"nightly-20261010": sha})
    with pytest.raises(promote.PromoteError):
        run_plan(repo, gh, nightly="nightly-20261010")


def _manifest(files: dict[str, bytes]) -> dict:
    return {"assets": {n: hashlib.sha256(b).hexdigest() for n, b in files.items()}}


def test_verify_assets_accepts_exact_match(tmp_path):
    files = {"omnigent-staging-debug.apk": b"apk", "merge-report.json": b"{}"}
    for n, b in files.items():
        (tmp_path / n).write_bytes(b)
    promote.verify_assets(_manifest(files), tmp_path)


def test_verify_assets_rejects_hash_mismatch(tmp_path):
    (tmp_path / "omnigent-staging-debug.apk").write_bytes(b"tampered")
    with pytest.raises(promote.PromoteError, match="sha256"):
        promote.verify_assets(_manifest({"omnigent-staging-debug.apk": b"apk"}), tmp_path)


def test_verify_assets_rejects_extra_and_missing(tmp_path):
    (tmp_path / "extra.bin").write_bytes(b"x")
    with pytest.raises(promote.PromoteError):
        promote.verify_assets(_manifest({"omnigent-staging-debug.apk": b"apk"}), tmp_path)


def test_verify_assets_cli_exit_codes(tmp_path, capsys):
    files = tmp_path / "files"
    files.mkdir()
    (files / "a.bin").write_bytes(b"a")
    manifest = tmp_path / "build-complete.json"
    manifest.write_text(json.dumps(_manifest({"a.bin": b"a"})))
    args = ["verify-assets", "--manifest", str(manifest), "--dir", str(files)]
    assert promote.main(args) == 0
    (files / "a.bin").write_bytes(b"tampered")
    assert promote.main(args) == 1
    assert "sha256" in capsys.readouterr().out
    manifest.write_text("not json")
    assert promote.main(args) == 1


def test_pin_creates_tag(repo):
    sha = repo.commit("a.txt", "a\n")
    assert promote.pin(repo.work, "origin", sha, "production-20261011") is True
    assert git(repo.fork, "rev-parse", "refs/tags/production-20261011") == sha


def test_pin_reuses_tag_for_same_sha(repo):
    sha = repo.commit("a.txt", "a\n")
    assert promote.pin(repo.work, "origin", sha, "production-20261011") is True
    assert promote.pin(repo.work, "origin", sha, "production-20261011") is False


def test_pin_reuses_annotated_tag_for_same_sha(repo):
    sha = repo.commit("a.txt", "a\n")
    repo.annotated_tag("production-20261011", sha)
    assert promote.pin(repo.work, "origin", sha, "production-20261011") is False


def test_pin_refuses_tag_at_different_sha(repo):
    a = repo.commit("a.txt", "a\n")
    b = repo.commit("b.txt", "b\n")
    promote.pin(repo.work, "origin", a, "production-20261011")
    with pytest.raises(promote.PromoteError, match="production-20261011"):
        promote.pin(repo.work, "origin", b, "production-20261011")
    assert git(repo.fork, "rev-parse", "refs/tags/production-20261011") == a


@pytest.mark.parametrize(
    "tag,sha",
    [
        ("nightly-20261011", None),
        ("production-2026", None),
        ("v1.0", None),
        ("production-20261011", "abc123"),
    ],
)
def test_pin_rejects_malformed_inputs(repo, tag, sha):
    real = repo.commit("a.txt", "a\n")
    with pytest.raises(promote.PromoteError):
        promote.pin(repo.work, "origin", sha or real, tag)
    assert git(repo.fork, "tag", "-l") == ""


def test_pin_cli_prints_created_then_exists_and_errors(repo, capsys):
    a = repo.commit("a.txt", "a\n")
    b = repo.commit("b.txt", "b\n")
    base = ["pin", "--workdir", str(repo.work), "--tag", "production-20261011"]
    assert promote.main([*base, "--sha", a]) == 0
    assert capsys.readouterr().out.strip() == "created"
    assert promote.main([*base, "--sha", a]) == 0
    assert capsys.readouterr().out.strip() == "exists"
    assert promote.main([*base, "--sha", b]) == 1
    assert "production-20261011" in capsys.readouterr().out


def _promote_workflow() -> dict:
    import yaml

    path = Path(__file__).resolve().parents[2] / "workflows/personal-promote.yml"
    return yaml.safe_load(path.read_text())


def test_status_trigger_is_gated_on_trusted_soak_verdicts():
    workflow = _promote_workflow()
    assert set(workflow[True]) == {"status", "workflow_dispatch"}
    for job in ("test-promote", "promote"):
        gate = workflow["jobs"][job]["if"]
        assert "github.event_name != 'status'" in gate
        assert "github.event.context == 'soak/homelab'" in gate
        assert "vars.SOAK_STATUS_LOGIN != ''" in gate
        assert "github.ref == 'refs/heads/main'" in gate
    # Job-level lock: skipped status runs never take the single pending slot.
    assert "concurrency" not in workflow
    assert workflow["jobs"]["promote"]["concurrency"] == {
        "group": "personal-promote",
        "cancel-in-progress": False,
    }


def test_status_plan_takes_event_values_only_through_env():
    steps = _promote_workflow()["jobs"]["promote"]["steps"]
    [plan_step] = [s for s in steps if s.get("id") == "plan"]
    assert plan_step["env"]["EVENT_SHA"] == "${{ github.event.sha }}"
    assert plan_step["env"]["SOAK_LOGIN"] == "${{ vars.SOAK_STATUS_LOGIN }}"
    script = plan_step["run"]
    assert "${{" not in script
    status_args = script.split('if [ "$GITHUB_EVENT_NAME" = "status" ]; then', 1)[1]
    status_args = status_args.split("else", 1)[0]
    assert '--trigger status --sha "$EVENT_SHA" --soak-login "$SOAK_LOGIN"' in status_args
    assert "approve" not in status_args and "allow-older" not in status_args
    [alert] = [s for s in steps if s.get("name") == "Alert on a failed soak verdict"]
    assert "steps.plan.outputs.action == 'ignored'" in alert["if"]
    assert 'msg="soak $STATE for $sha ($nightly) — $RUN_URL"' in alert["run"]
