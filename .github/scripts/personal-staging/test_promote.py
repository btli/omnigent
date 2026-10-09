from __future__ import annotations

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
        git(self.work, "push", "-q", "origin", f"{sha}:refs/tags/{name}")


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


def complete_nightly(repo, gh, tag, sha, upstream_sha=None):
    """Tag *sha* as *tag* with a release carrying build-complete.json + merge report."""
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
        "merge-report.json": {"upstream_sha": upstream_sha or repo.base},
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


def _promoted(repo, gh, ptag, psha, source_tag):
    repo.tag(ptag, psha)
    gh.releases[ptag] = {"source.json": {"source_tag": source_tag, "sha": psha}}


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
    _promoted(repo, gh, "production-20261010", sha, "nightly-20261010")
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
    sha = repo.commit(
        "omnigent/db/migrations/versions/0001_x.py", 'revision = "x"\ndown_revision = None\n'
    )
    complete_nightly(repo, gh, "nightly-20261010", sha)
    blocked = run_plan(repo, gh, nightly="nightly-20261010")
    assert blocked["action"] == "blocked" and blocked["gate"]["blocked"] is True
    assert sha in blocked["gate"]["approval_hint"]
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration="f" * 40)[
        "action"
    ] == ("blocked")
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration=sha)["action"] == (
        "promote"
    )


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
