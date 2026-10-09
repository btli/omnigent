# Ring soak-and-promote Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The staging nightly builds every release artifact once. A soaked
nightly is promoted to production by a separate workflow that reuses those
exact artifacts.

**Architecture:** `personal-staging.yml` gains the verify, `.dev` APK and
desktop jobs (moved from `personal-production.yml`) and a non-fatal
`sync-main`. `personal-staging-images.yml` writes `build-complete.json` on
nightly releases. A new stdlib module, `promote.py`, holds every promote
decision behind a JSON plan, and `personal-promote.yml` carries out the plan.
PR 3 adds the `status` trigger and removes production's own compose.

**Tech Stack:** GitHub Actions YAML, Python 3 stdlib (no third-party imports in
privileged jobs), pytest, `gh` CLI, `docker buildx imagetools`.

**Spec:** `docs/superpowers/specs/2026-10-09-ring-soak-promote-design.md`

## Global Constraints

- Repo `btli/omnigent` (fork). Ring code lives in `.github/scripts/personal-staging/`. Workflows are in `.github/workflows/`.
- Privileged jobs (those holding `contents: write`, `packages: write` or the PAT) never check out or execute merged-PR code. They run only stdlib Python from the trusted `main` checkout.
- Jobs that build merged-PR code are secretless, use `persist-credentials: false`, and have a read-only cache.
- Pin all actions by full commit sha, exactly as the existing workflows do (copy the pins used there).
- The soak status context is `soak/homelab`. The trusted creator login comes from repo variable `SOAK_STATUS_LOGIN`; when it's empty, status-triggered promotion is disabled.
- Nightly tags: `nightly-YYYYMMDD[-rerunN]`. Production tags: `production-YYYYMMDD[-rerunN]`, dated by promote date (UTC). Floating: `nightly-latest`, `production-latest`; image channels `staging-nightly`, `production-nightly`.
- Staging asset names: `omnigent-staging-debug.apk`, `omnigent-staging-release-unsigned.aab`, `omnigent-staging-arm64.dmg`, `omnigent-staging-arm64-mac.zip`, `merge-report.json`, `SHA256SUMS`. Promote renames each `omnigent-staging-` prefix to `omnigent-production-`.
- The Android debug APK package id is `ai.omnigent.android.dev` and its versionName is the nightly tag. The release AAB keeps the store id.
- Tests run with: `cd .github/scripts/personal-staging && /Users/bryan.li/Projects/btli/omnigent/.venv/bin/python -m pytest <file> -q`.
- Lint: `/Users/bryan.li/Projects/btli/omnigent/.venv/bin/ruff check` and `ruff format --check` on changed `.py` files. `actionlint` on changed workflows (`brew` has it; if it's missing, run `npx -y actionlint` or report that it was skipped).
- Commits: `git -c user.name="Bryan Li" -c user.email="bryan.li@gmail.com" commit -s`, with the trailer `Co-authored-by: Isaac <no-reply@databricks.com>`.

## Review Focus

1. **Status for a sha with several nightly tags** (a same-day rerun repointed): promote must pick the newest nightly tag at that sha, and still refuse if none has `build-complete.json`. Covered by Task 4 `test_plan_picks_newest_nightly_tag_at_sha`.
2. **Status from an untrusted creator** that arrives after a trusted `success`: the newest `soak/homelab` status wins, so this is a refusal. Covered by Task 4 `test_plan_refuses_when_newest_status_untrusted`.
3. **Asset on the release doesn't match its `build-complete` hash** (a re-uploaded asset): `verify-assets` must fail. Covered by Task 5 `test_verify_assets_rejects_hash_mismatch`.
4. **Rerun after the tag was pushed** but before `production-latest` moved: `pin` must reuse the tag and not mint `-rerun1`. Covered by Task 5 `test_pin_reuses_existing_tag_for_same_sha`.
5. **Legacy composed production pin without `source.json`**: candidate dates on or after the pin date are allowed, older ones refused. Covered by Task 4 `test_plan_legacy_pin_date_floor`.

---

## PR 1 — staging builds everything (branch `feat/rings-staging-builds-all`)

### Task 1: Non-fatal `sync-main` at the start of the staging nightly

**Files:**
- Modify: `.github/workflows/personal-staging.yml`, in the `integrate` job's "Compose staging …" step.

- [ ] **Step 1:** Just before the `stage.py stage` call in that step (after `git remote add upstream …`), insert:

```bash
          # Advance fork main to upstream first. A conflict (a fork-only
          # commit vs upstream) must not stop the nightly: alert, leave main
          # untouched, and compose on the old base with upstream as entry zero.
          if ! python3 .github/scripts/personal-staging/stage.py sync-main \
              --upstream-remote upstream --fork-remote origin; then
            echo "::warning::sync-main failed; composing on the previous fork main"
            echo "SYNC_MAIN_FAILED=1" >> "$GITHUB_ENV"
          fi
```

- [ ] **Step 2:** Add a following step, named "Alert when sync-main failed". It copies the HMAC `ha-notify` block from `personal-production.yml` (lines 158–181, `HA_NOTIFY_HMAC` secret and `RUN_URL`), runs only when `env.SYNC_MAIN_FAILED == '1'`, and sends the message `omnigent fork main could not merge upstream (sync-main conflict); staging composed on the previous base. $RUN_URL`. The integrate job uses environment `staging-push`; make sure the `HA_NOTIFY_HMAC` secret reference also works there (it's a repo secret).
- [ ] **Step 3:** Run `actionlint .github/workflows/personal-staging.yml`. Expect no errors.
- [ ] **Step 4:** Commit with the message `feat(rings): advance fork main from the staging nightly, non-fatally`.

### Task 2: Staging builds verify, the `.dev` APK and the desktop app

**Files:**
- Modify: `.github/workflows/personal-staging.yml` (the `android-build`, `android-sign` and `publish` jobs, plus two new jobs).

- [ ] **Step 1: `verify` job.** Copy `personal-production.yml`'s `verify` job (lines 200–232) into staging after `integrate`, with `needs: integrate` and ref `${{ needs.integrate.outputs.staging_sha }}`. Drop its `if:`. Report text: `## Personal staging nightly — verify job FAILED — the nightly does not build, so no artifacts were published`.
- [ ] **Step 2: `android-build` changes.**
  - Make `needs: [integrate, verify]`.
  - Replace the single gradle call with two invocations:

```bash
          ./gradlew assembleDebug --no-daemon --console=plain \
            -PversionCode="$VERSION_CODE" \
            -PversionName="$VERSION_NAME" \
            -PapplicationIdSuffix=.dev
          ./gradlew bundleRelease --no-daemon --console=plain \
            -PversionCode="$VERSION_CODE"
```

  - Add `VERSION_NAME: ${{ needs.integrate.outputs.tag }}` to the step env.
  - Then copy production's "Verify package id" step (lines 353–366) unchanged, expecting `ai.omnigent.android.dev`.
- [ ] **Step 3: `desktop-build` job.** Copy production's `desktop-build` (lines 486–561) into staging:
  - `needs: [integrate, verify]`, no `if:`;
  - checkout ref `${{ needs.integrate.outputs.staging_sha }}`;
  - staged names `omnigent-staging-arm64.dmg` and `omnigent-staging-arm64-mac.zip`;
  - report text naming the staging nightly.
- [ ] **Step 4: `publish` job.**
  - Make `needs: [integrate, android-sign, desktop-build]`.
  - Add `&& needs.desktop-build.result == 'success'` to its `if:`.
  - Add a "Download desktop assets" step (artifact `desktop-assets`, path `desktop`).
  - In "Assemble", set `got` to `find report assets desktop -mindepth 1 …`, and set `want` to:
    `assets/omnigent-staging-debug.apk assets/omnigent-staging-release-unsigned.aab desktop/omnigent-staging-arm64-mac.zip desktop/omnigent-staging-arm64.dmg report/merge-report.json`
  - Copy both desktop files into `dist/`, and append the same `## macOS desktop` notes block production uses.
- [ ] **Step 5:** Update the header comment in `personal-staging.yml` (lines 1–17) so it says the nightly also verifies the web bundle and builds the `.dev` APK and the macOS app.
- [ ] **Step 6:** Run `actionlint .github/workflows/personal-staging.yml`. Expect no errors.
- [ ] **Step 7:** Commit with the message `feat(rings): build the verified web bundle, .dev APK and desktop app in the staging nightly`.

### Task 3: `build-complete.json` from the image workflow

**Files:**
- Create: `.github/scripts/personal-staging/promote.py` (only the `build-complete` subcommand in this task)
- Create: `.github/scripts/personal-staging/test_promote.py`
- Modify: `.github/workflows/personal-staging-images.yml` (the `promote-channel` job)

**Interfaces:**
- Produces: `build_complete(tag: str, sha: str, sums_text: str, server_digest: str, host_digest: str) -> dict` and the CLI `promote.py build-complete --tag T --sha S --sums FILE --server-digest D --host-digest D` (prints JSON). Schema:
  `{"schema": 1, "tag": T, "sha": S, "assets": {name: sha256}, "images": {"server": D, "host": D}}`.

- [ ] **Step 1: Write the failing tests** in `test_promote.py`:

```python
from __future__ import annotations

import json

import promote
import pytest

SHA = "a" * 40
SUMS = (
    f"{'1' * 64}  ./omnigent-staging-debug.apk\n"
    f"{'2' * 64}  ./merge-report.json\n"
)


def test_build_complete_lists_assets_and_digests():
    doc = promote.build_complete("nightly-20261010", SHA, SUMS, "sha256:" + "3" * 64, "sha256:" + "4" * 64)
    assert doc == {
        "schema": 1,
        "tag": "nightly-20261010",
        "sha": SHA,
        "assets": {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64},
        "images": {"server": "sha256:" + "3" * 64, "host": "sha256:" + "4" * 64},
    }


@pytest.mark.parametrize(
    "tag,sha,server",
    [("production-20261010", SHA, "sha256:" + "3" * 64), ("nightly-20261010", "xyz", "sha256:" + "3" * 64),
     ("nightly-20261010", SHA, "latest")],
)
def test_build_complete_rejects_bad_inputs(tag, sha, server):
    with pytest.raises(promote.PromoteError):
        promote.build_complete(tag, sha, SUMS, server, "sha256:" + "4" * 64)


def test_build_complete_rejects_malformed_sums():
    with pytest.raises(promote.PromoteError):
        promote.build_complete("nightly-20261010", SHA, "garbage\n", "sha256:" + "3" * 64, "sha256:" + "4" * 64)


def test_build_complete_cli(tmp_path, capsys):
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(SUMS)
    assert promote.main(["build-complete", "--tag", "nightly-20261010", "--sha", SHA, "--sums", str(sums),
                         "--server-digest", "sha256:" + "3" * 64, "--host-digest", "sha256:" + "4" * 64]) == 0
    assert json.loads(capsys.readouterr().out)["assets"]["merge-report.json"] == "2" * 64
```

- [ ] **Step 2:** Run `pytest test_promote.py -q`. Expect a FAIL (`ModuleNotFoundError: promote`).
- [ ] **Step 3: Implement** the start of `promote.py`:

```python
#!/usr/bin/env python3
"""Promote a soaked staging nightly to production (personal rings).

Stdlib only: this runs in privileged jobs that must never import third-party
code. Every decision lives here behind a JSON plan; the workflow only moves
bytes and refs the plan names.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NIGHTLY_RE = re.compile(r"nightly-(\d{8})(?:-rerun(\d+))?")
PRODUCTION_RE = re.compile(r"production-(\d{8})(?:-rerun(\d+))?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SUMS_LINE_RE = re.compile(r"([0-9a-f]{64})\s+\*?(?:\./)?([A-Za-z0-9._-]+)")
BUILD_COMPLETE = "build-complete.json"


class PromoteError(Exception):
    """A promote input or rule failed; nothing must be published."""


def tag_key(tag: str, pattern: re.Pattern[str]) -> tuple[str, int] | None:
    """``(YYYYMMDD, rerun)`` for a pin tag of *pattern*'s family, else None."""
    m = pattern.fullmatch(tag)
    return (m.group(1), int(m.group(2) or 0)) if m else None


def parse_sums(text: str) -> dict[str, str]:
    """``sha256sum`` output -> {asset name: hex digest}."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        m = SUMS_LINE_RE.fullmatch(line.strip())
        if not m:
            raise PromoteError(f"malformed SHA256SUMS line: {line!r}")
        out[m.group(2)] = m.group(1)
    if not out:
        raise PromoteError("SHA256SUMS lists no assets")
    return out


def build_complete(tag: str, sha: str, sums_text: str, server_digest: str, host_digest: str) -> dict:
    """The completion marker a nightly release carries once its images verify."""
    if tag_key(tag, NIGHTLY_RE) is None:
        raise PromoteError(f"not a nightly tag: {tag!r}")
    if not SHA_RE.fullmatch(sha):
        raise PromoteError(f"not a full commit sha: {sha!r}")
    for digest in (server_digest, host_digest):
        if not DIGEST_RE.fullmatch(digest):
            raise PromoteError(f"not an image digest: {digest!r}")
    return {
        "schema": 1,
        "tag": tag,
        "sha": sha,
        "assets": parse_sums(sums_text),
        "images": {"server": server_digest, "host": host_digest},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_bc = sub.add_parser("build-complete", help="render a nightly's build-complete.json")
    p_bc.add_argument("--tag", required=True)
    p_bc.add_argument("--sha", required=True)
    p_bc.add_argument("--sums", required=True, type=Path)
    p_bc.add_argument("--server-digest", required=True)
    p_bc.add_argument("--host-digest", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "build-complete":
        doc = build_complete(args.tag, args.sha, args.sums.read_text(), args.server_digest, args.host_digest)
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4:** Run `pytest test_promote.py -q`. Expect a PASS. Then run ruff check and ruff format on both files.
- [ ] **Step 5: Workflow.** In `personal-staging-images.yml`'s `promote-channel` job:
  - add `contents: write` to `permissions`;
  - add `needs.build-and-push.outputs.source_tag` (or whatever output the `source` step exposes for the tag name and sha; add `tag` and `sha` outputs to `build-and-push` from the `source` step if they're absent).

  After "Publish verified channel tags", add:

```yaml
      - name: Checkout trusted main (for promote.py)
        if: startsWith(needs.build-and-push.outputs.tag, 'nightly-')
        uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          ref: main
          persist-credentials: false
      # Written LAST: its presence is what makes this nightly a promote
      # candidate, so it must follow every artifact and the channel move.
      - name: Mark the nightly build complete
        if: startsWith(needs.build-and-push.outputs.tag, 'nightly-')
        env:
          GH_TOKEN: ${{ github.token }}
          TAG: ${{ needs.build-and-push.outputs.tag }}
          SHA: ${{ needs.build-and-push.outputs.sha }}
          SERVER_DIGEST: ${{ needs.build-and-push.outputs.server_digest }}
          HOST_DIGEST: ${{ needs.build-and-push.outputs.host_digest }}
        run: |
          set -euo pipefail
          gh release download "$TAG" -R "$GITHUB_REPOSITORY" -p SHA256SUMS -D "$RUNNER_TEMP" --clobber
          python3 .github/scripts/personal-staging/promote.py build-complete \
            --tag "$TAG" --sha "$SHA" --sums "$RUNNER_TEMP/SHA256SUMS" \
            --server-digest "$SERVER_DIGEST" --host-digest "$HOST_DIGEST" > "$RUNNER_TEMP/build-complete.json"
          gh release upload "$TAG" -R "$GITHUB_REPOSITORY" "$RUNNER_TEMP/build-complete.json" --clobber
```

  Make sure `sha` is the commit sha the tag points at. The `source` step resolves the tag, so expose `git rev-parse "$tag_name^{commit}"` as an output if it doesn't already.
- [ ] **Step 6:** Run `actionlint .github/workflows/personal-staging-images.yml`. Expect no errors. Then commit with the message `feat(rings): mark nightly releases build-complete once their images verify`.

---

## PR 2 — promote, manual dispatch only (branch `feat/rings-promote`, based on PR 1)

### Task 4: `promote.py plan`, the decision

**Files:**
- Modify: `.github/scripts/personal-staging/promote.py`
- Modify: `.github/scripts/personal-staging/test_promote.py`

**Interfaces:**
- Consumes: `stage.git`, `stage.remote_ref`, `stage.pin_name`, `stage.PRODUCTION`, `stage.migration_touched`, `stage.assert_migration_graph`, `stage.assert_migration_history`, `stage.StageError` (all existing in `stage.py`).
- Produces:
  - `class GitHub` with the methods `statuses(sha) -> list[dict]` (newest first, API shape `{"context","state","creator":{"login"},"target_url"}`), `release_assets(tag) -> list[str] | None` (None = no release), and `release_json(tag, name) -> dict | None`.
  - `plan(cwd, fork, gh, *, trigger: str, sha: str | None, nightly: str | None, soak_login: str, approve_migration: str, allow_older: bool, date: str) -> dict`.
  - The returned plan dict always has `"action"` (one of `"promote"`, `"noop"`, `"blocked"`, `"refused"`, `"ignored"`) and `"reason"`. For promote, noop and blocked it also has `nightly`, `sha`, `prev_pin`, `prev_tag` and `gate`. For promote it also has `production_tag`, `tag_created`, `assets`, `images` and `source`.

Fixture to add to `test_promote.py` (real throwaway git repos, as `test_stage.py` uses):

```python
import subprocess
from pathlib import Path


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


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
    return {"context": context, "state": state, "creator": {"login": login}, "target_url": "https://soak/run/1"}


def complete_nightly(repo, gh, tag, sha, upstream_sha=None):
    """Tag *sha* as *tag* with a release carrying build-complete.json + merge report."""
    repo.tag(tag, sha)
    bc = {"schema": 1, "tag": tag, "sha": sha,
          "assets": {"omnigent-staging-debug.apk": "1" * 64, "merge-report.json": "2" * 64},
          "images": {"server": "sha256:" + "3" * 64, "host": "sha256:" + "4" * 64}}
    gh.releases[tag] = {"omnigent-staging-debug.apk": b"apk", "merge-report.json": {"upstream_sha": upstream_sha or repo.base},
                        "SHA256SUMS": b"", "build-complete.json": bc}


@pytest.fixture
def repo(tmp_path):
    return Repo(tmp_path)


def run_plan(repo, gh, **kw):
    args = dict(trigger="dispatch", sha=None, nightly=None, soak_login=LOGIN, approve_migration="",
                allow_older=False, date=DATE)
    args.update(kw)
    return promote.plan(repo.work, "origin", gh, **args)
```

- [ ] **Step 1: Write the failing tests.** Each one builds its scenario with `Repo` and `FakeGitHub`:

```python
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


@pytest.mark.parametrize("state,action", [("failure", "ignored"), ("error", "ignored"), ("pending", "ignored")])
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
    sha = repo.commit("omnigent/db/migrations/versions/0001_x.py", 'revision = "x"\ndown_revision = None\n')
    complete_nightly(repo, gh, "nightly-20261010", sha)
    blocked = run_plan(repo, gh, nightly="nightly-20261010")
    assert blocked["action"] == "blocked" and blocked["gate"]["blocked"] is True
    assert sha in blocked["gate"]["approval_hint"]
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration="f" * 40)["action"] == "blocked"
    assert run_plan(repo, gh, nightly="nightly-20261010", approve_migration=sha)["action"] == "promote"


def test_plan_refuses_shipped_migration_rewrite(repo):
    gh = FakeGitHub()
    path = "omnigent/db/migrations/versions/0001_x.py"
    shipped = repo.commit(path, 'revision = "x"\ndown_revision = None\n')
    _promoted(repo, gh, "production-20261009", shipped, "nightly-20261009")
    rewritten = repo.commit(path, 'revision = "x"\ndown_revision = None\n# edited\n')
    complete_nightly(repo, gh, "nightly-20261010", rewritten)
    p = run_plan(repo, gh, nightly="nightly-20261010", approve_migration=rewritten)
    assert p["action"] == "refused" and "append-only" in p["reason"] or "migration history" in p["reason"]
```

- [ ] **Step 2:** Run `pytest test_promote.py -q`. Expect FAILs (`promote.plan` missing).
- [ ] **Step 3: Implement** in `promote.py`. Add `import subprocess`, and import `stage` from the same directory (`sys.path.insert(0, str(Path(__file__).resolve().parent))`, then `import stage`):

```python
SOAK_CONTEXT = "soak/homelab"
SOURCE = "source.json"


class GitHub:
    """Read-only GitHub access through the gh CLI (injectable for tests)."""

    def __init__(self, repo: str):
        self.repo = repo

    def _gh(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["gh", *args], text=True, capture_output=True)

    def statuses(self, sha: str) -> list[dict]:
        r = self._gh("api", "--paginate", f"repos/{self.repo}/commits/{sha}/statuses")
        if r.returncode:
            raise PromoteError(f"cannot read statuses for {sha}: {r.stderr.strip()}")
        # --paginate concatenates JSON arrays; normalise "][" joins.
        return json.loads("[" + r.stdout.strip()[1:-1].replace("][", ",") + "]") if r.stdout.strip() else []

    def release_assets(self, tag: str) -> list[str] | None:
        r = self._gh("release", "view", tag, "-R", self.repo, "--json", "assets")
        if r.returncode:
            if "release not found" in r.stderr.lower():
                return None
            raise PromoteError(f"cannot read release {tag}: {r.stderr.strip()}")
        return sorted(a["name"] for a in json.loads(r.stdout)["assets"])

    def release_json(self, tag: str, name: str) -> dict | None:
        r = self._gh("release", "download", tag, "-R", self.repo, "-p", name, "-O", "-")
        if r.returncode:
            return None
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError as error:
            raise PromoteError(f"{tag}/{name} is not JSON: {error}") from error
        return doc if isinstance(doc, dict) else None


def _tags(cwd, fork) -> dict[str, str]:
    """name -> commit sha for every lightweight tag on *fork*."""
    out = {}
    for line in stage.git(cwd, "ls-remote", "--tags", fork).stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.startswith("refs/tags/") and not ref.endswith("^{}"):
            out[ref.removeprefix("refs/tags/")] = sha
    return out


def _latest(tags: dict[str, str], pattern) -> tuple[str, str] | tuple[None, None]:
    best = max(((tag_key(t, pattern), t) for t in tags if tag_key(t, pattern)), default=None)
    return (best[1], tags[best[1]]) if best else (None, None)


def _result(action: str, reason: str, **fields) -> dict:
    return {"action": action, "reason": reason, **fields}


def plan(cwd, fork, gh, *, trigger, sha, nightly, soak_login, approve_migration, allow_older, date) -> dict:
    tags = _tags(cwd, fork)
    verdict = None
    if trigger == "status":
        if not soak_login:
            return _result("refused", "status-triggered promotion is disabled (SOAK_STATUS_LOGIN unset)")
        mine = [s for s in gh.statuses(sha) if s.get("context") == SOAK_CONTEXT]
        if not mine:
            return _result("ignored", f"no {SOAK_CONTEXT} status on {sha}")
        newest = mine[0]
        if (newest.get("creator") or {}).get("login") != soak_login:
            return _result("refused", f"newest {SOAK_CONTEXT} status is not from {soak_login}")
        if newest.get("state") != "success":
            return _result("ignored", f"soak verdict is {newest.get('state')}")
        verdict = newest
        candidates = [t for t, s in tags.items() if s == sha and tag_key(t, NIGHTLY_RE)]
        if not candidates:
            return _result("refused", f"{sha} is not a nightly-* tag target")
        nightly = max(candidates, key=lambda t: tag_key(t, NIGHTLY_RE))
    else:
        if not nightly or tag_key(nightly, NIGHTLY_RE) is None or nightly not in tags:
            return _result("refused", f"not an existing nightly tag: {nightly!r}")
        sha = tags[nightly]

    assets = gh.release_assets(nightly)
    marker = gh.release_json(nightly, BUILD_COMPLETE) if assets else None
    if not marker:
        return _result("refused", f"{nightly} has no {BUILD_COMPLETE}; the nightly is incomplete")
    if marker.get("sha") != sha or marker.get("tag") != nightly or marker.get("schema") != 1:
        return _result("refused", f"{BUILD_COMPLETE} on {nightly} does not describe {sha}")
    missing = sorted(set(marker.get("assets", {})) - set(assets))
    if missing:
        return _result("refused", f"{nightly} release is missing {', '.join(missing)}")

    prev_tag, prev_sha = _latest(tags, PRODUCTION_RE)
    fields = {"nightly": nightly, "sha": sha, "prev_tag": prev_tag, "prev_pin": prev_sha}
    if prev_sha == sha:
        return _result("noop", f"production already points at {sha}", **fields, gate=None)
    if prev_tag:
        source = gh.release_json(prev_tag, SOURCE)
        floor = tag_key(source["source_tag"], NIGHTLY_RE) if source and source.get("source_tag") else None
        cand = tag_key(nightly, NIGHTLY_RE)
        older = cand <= floor if floor else cand[0] < tag_key(prev_tag, PRODUCTION_RE)[0]
        if older and not allow_older:
            return _result("refused", f"{nightly} is not newer than production's source", **fields, gate=None)

    stage.git(cwd, "fetch", "-q", fork, sha, *( [prev_sha] if prev_sha else [] ))
    try:
        stage.assert_migration_history(cwd, sha, prev_sha)
        stage.assert_migration_graph(cwd, sha)
    except stage.StageError as error:
        return _result("refused", str(error), **fields, gate=None)
    report = gh.release_json(nightly, "merge-report.json") or {}
    upstream_sha = report.get("upstream_sha") or prev_sha or sha
    touched = stage.migration_touched(cwd, sha, upstream_sha, prev_sha) if upstream_sha != sha else False
    gate = {"candidate": sha, "prev_pin": prev_sha, "blocked": bool(touched and approve_migration != sha)}
    if gate["blocked"]:
        gate["approval_hint"] = f"re-dispatch personal-promote.yml with nightly={nightly} approve_migration={sha}"
        return _result("blocked", "candidate touches migrations and is not approved", **fields, gate=gate)

    production_tag, created = stage.pin_name(cwd, fork, date, sha, stage.PRODUCTION)
    return _result(
        "promote", f"promote {nightly} to {production_tag}", **fields, gate=gate,
        production_tag=production_tag, tag_created=created,
        assets=marker["assets"], images=marker["images"],
        source={"source_tag": nightly, "sha": sha, "trigger": trigger,
                "soak_target_url": (verdict or {}).get("target_url"),
                "approve_migration": approve_migration or None, "allow_older": allow_older},
    )
```

  Add the `plan` subcommand to `main`:
  - arguments `--workdir .`, `--fork-remote origin`, `--repo` (required), `--trigger {status,dispatch}`, `--sha`, `--nightly`, `--soak-login` (default `""`), `--approve-migration` (default `""`), `--allow-older` (`store_true`), `--date` (default: today, UTC `YYYYMMDD`), `--out` (path);
  - it writes the plan JSON to `--out` and stdout and returns 0, also for refused, ignored and blocked plans. The workflow branches on `action`.
  - Return 1 only on `PromoteError` or an unexpected exception.
- [ ] **Step 4:** Run `pytest test_promote.py -q`. Expect all tests to PASS. Fix the implementation, not the tests, unless a test contradicts the spec. (Fix `test_plan_refuses_shipped_migration_rewrite`'s assertion precedence to `assert p["action"] == "refused" and ("append-only" in p["reason"] or "migration history" in p["reason"])`.) Run ruff check and ruff format.
- [ ] **Step 5:** Commit with the message `feat(rings): decide nightly promotion in a tested promote plan`.

### Task 5: `promote.py verify-assets` and `promote.py pin`

**Files:**
- Modify: `promote.py`, `test_promote.py`

**Interfaces:**
- Produces:
  - `verify_assets(manifest: dict, directory: Path) -> None`: raises `PromoteError` when the directory isn't exactly the manifest's assets, or when a sha256 differs.
  - `pin(cwd, fork, sha, date) -> tuple[str, bool]`: calls `stage.pin_name(cwd, fork, date, sha, stage.PRODUCTION)` and, when the tag is new, pushes it with `git push --atomic <fork> --force-with-lease=refs/tags/<tag>: <sha>:refs/tags/<tag>`.
  - CLI: `verify-assets --manifest FILE --dir DIR` and `pin --sha S --date D [--workdir --fork-remote]`, which prints the tag.

- [ ] **Step 1: Write the failing tests:**

```python
import hashlib


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


def test_pin_creates_tag(repo):
    sha = repo.commit("a.txt", "a\n")
    assert promote.pin(repo.work, "origin", sha, DATE) == ("production-20261011", True)
    assert git(repo.fork, "rev-parse", "refs/tags/production-20261011") == sha


def test_pin_reuses_existing_tag_for_same_sha(repo):
    sha = repo.commit("a.txt", "a\n")
    promote.pin(repo.work, "origin", sha, DATE)
    assert promote.pin(repo.work, "origin", sha, DATE) == ("production-20261011", False)


def test_pin_reruns_for_a_different_sha_same_day(repo):
    a = repo.commit("a.txt", "a\n")
    b = repo.commit("b.txt", "b\n")
    promote.pin(repo.work, "origin", a, DATE)
    assert promote.pin(repo.work, "origin", b, DATE) == ("production-20261011-rerun1", True)
```

- [ ] **Step 2:** Run the tests. Expect them to FAIL.
- [ ] **Step 3: Implement:**

```python
import hashlib


def verify_assets(manifest: dict, directory: Path) -> None:
    want = manifest.get("assets") or {}
    got = {p.name for p in directory.iterdir() if p.is_file()}
    if got != set(want):
        raise PromoteError(f"asset set differs: missing {sorted(set(want) - got)}, extra {sorted(got - set(want))}")
    for name, digest in want.items():
        actual = hashlib.sha256((directory / name).read_bytes()).hexdigest()
        if actual != digest:
            raise PromoteError(f"{name}: sha256 {actual} != build-complete {digest}")


def pin(cwd, fork, sha, date) -> tuple[str, bool]:
    tag, created = stage.pin_name(cwd, fork, date, sha, stage.PRODUCTION)
    if created:
        ref = f"refs/tags/{tag}"
        stage.git(cwd, "push", "--atomic", fork, f"--force-with-lease={ref}:", f"{sha}:{ref}")
    return tag, created
```

  Wire both subcommands into `main`.
- [ ] **Step 4:** Run `pytest test_promote.py -q` and ruff. Expect a PASS.
- [ ] **Step 5:** Commit with the message `feat(rings): verify promoted assets and pin production idempotently`.

### Task 6: `personal-promote.yml` (manual dispatch) and docs

**Files:**
- Create: `.github/workflows/personal-promote.yml`
- Modify: `.github/workflows/personal-staging.yml` and `personal-production.yml` (add `promote.py`/`test_promote.py` to nothing: `test-composer` already runs the whole directory)
- Modify: `.github/scripts/personal-staging/README.md` (add a "Soak and promote" section summarising the spec's flow, rules and hardware contract)

- [ ] **Step 1: Write the workflow.** It mirrors the existing trust shape (test gate, then privileged jobs that run only the trusted checkout):

```yaml
# Promote a soaked staging nightly to production by reusing its artifacts.
# Manual dispatch only until the cutover PR adds the soak/homelab status
# trigger. promote.py decides (see spec docs/superpowers/specs/
# 2026-10-09-ring-soak-promote-design.md); this workflow only moves bytes and
# refs the plan names. Never checks out or executes the nightly's code.
name: Personal Promote

on:
  workflow_dispatch:
    inputs:
      nightly:
        description: "nightly-YYYYMMDD[-rerunN] tag to promote"
        required: true
        type: string
      approve_migration:
        description: "Full candidate sha approving a migration-touching promotion (after the CNPG backup)"
        type: string
        default: ""
      allow_older:
        description: "Allow promoting a nightly older than production's source (rollback)"
        type: boolean
        default: false
      dry_run:
        description: "Only print the plan"
        type: boolean
        default: false

permissions:
  contents: read

concurrency:
  group: personal-promote
  cancel-in-progress: false

jobs:
  test-promote:
    if: github.repository == 'btli/omnigent' && github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          persist-credentials: false
      - uses: actions/setup-python@a309ff8b426b58ec0e2a45f0f869d46889d02405 # v6.2.0
        with:
          python-version-file: ".python-version"
      - uses: astral-sh/setup-uv@fac544c07dec837d0ccb6301d7b5580bf5edae39 # v8.2.0
        with:
          enable-cache: false
      - run: uv run --frozen --group dev python -m pytest .github/scripts/personal-staging/test_promote.py

  promote:
    needs: test-promote
    runs-on: ubuntu-latest
    environment: staging-push
    timeout-minutes: 30
    permissions:
      contents: write
      packages: write
    env:
      GH_TOKEN: ${{ github.token }}
    steps:
      - name: Checkout trusted main with tags
        uses: actions/checkout@9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0 # v7.0.0
        with:
          token: ${{ secrets.STAGING_PUSH_TOKEN || github.token }}
          fetch-depth: 0

      - name: Plan
        id: plan
        env:
          NIGHTLY: ${{ inputs.nightly }}
          APPROVE: ${{ inputs.approve_migration }}
          ALLOW_OLDER: ${{ inputs.allow_older }}
        run: |
          set -euo pipefail
          extra=()
          [ "$ALLOW_OLDER" = "true" ] && extra+=(--allow-older)
          python3 .github/scripts/personal-staging/promote.py plan --repo "$GITHUB_REPOSITORY" \
            --trigger dispatch --nightly "$NIGHTLY" --approve-migration "$APPROVE" "${extra[@]}" --out plan.json
          echo "action=$(jq -r .action plan.json)" >> "$GITHUB_OUTPUT"
          { echo "## Promote plan"; echo '```json'; cat plan.json; echo '```'; } >> "$GITHUB_STEP_SUMMARY"

      - name: Stop on refused plan
        if: steps.plan.outputs.action == 'refused'
        run: |
          echo "::error::$(jq -r .reason plan.json)"
          exit 1

      - name: Alert on blocked migration gate
        if: steps.plan.outputs.action == 'blocked'
        env:
          HA_NOTIFY_HMAC: ${{ secrets.HA_NOTIFY_HMAC }}
          RUN_URL: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}
        run: |
          # Same HMAC ha-notify block as personal-production.yml, message:
          # "omnigent promote BLOCKED <nightly> (migrations): $(jq -r .gate.approval_hint plan.json) $RUN_URL"

      - name: Copy and verify assets into a draft production release
        if: steps.plan.outputs.action == 'promote' && !inputs.dry_run
        id: draft
        run: |
          set -euo pipefail
          nightly=$(jq -r .nightly plan.json); tag=$(jq -r .production_tag plan.json); sha=$(jq -r .sha plan.json)
          rm -rf dl dist && mkdir dl dist
          jq '{assets: .assets}' plan.json > manifest.json
          for name in $(jq -r '.assets | keys[]' plan.json); do
            gh release download "$nightly" -p "$name" -D dl --clobber
          done
          python3 .github/scripts/personal-staging/promote.py verify-assets --manifest manifest.json --dir dl
          for f in dl/*; do b=$(basename "$f"); cp "$f" "dist/${b/omnigent-staging-/omnigent-production-}"; done
          rm -f dist/SHA256SUMS
          jq .source plan.json > dist/source.json
          (cd dist && sha256sum ./* > ../SHA256SUMS && mv ../SHA256SUMS .)
          gh release download "$nightly" -p merge-report.json -O report.json --clobber
          python3 .github/scripts/personal-staging/stage.py notes --report report.json --signed true --ring production > notes.md
          printf '\n\nPromoted from `%s` (`%s`).\n' "$nightly" "$sha" >> notes.md
          if gh release view "$tag" >/dev/null 2>&1; then
            gh release edit "$tag" --notes-file notes.md
          else
            gh release create "$tag" --draft --prerelease --target "$sha" \
              --title "Personal production $tag" --notes-file notes.md
          fi
          gh release upload "$tag" dist/* --clobber
          echo "tag=$tag" >> "$GITHUB_OUTPUT"

      - name: Delete the draft if copying failed
        if: failure() && steps.draft.outcome == 'failure'
        run: |
          tag=$(jq -r .production_tag plan.json)
          if gh release view "$tag" --json isDraft --jq .isDraft 2>/dev/null | grep -q true; then
            gh release delete "$tag" --yes
          fi

      - name: Retag images by digest
        if: steps.plan.outputs.action == 'promote' && !inputs.dry_run
        # login + imagetools create, as personal-staging-images.yml's promote-channel job does,
        # for ghcr.io/btli/omnigent-server@<images.server> and omnigent-host@<images.host>
        # -> :<production_tag> and :sha-<sha[:12]>
        ...

      - name: Pin
        if: steps.plan.outputs.action == 'promote' && !inputs.dry_run
        run: |
          python3 .github/scripts/personal-staging/promote.py pin --sha "$(jq -r .sha plan.json)" \
            --date "$(date -u +%Y%m%d)" --fork-remote origin

      - name: Switch production
        if: steps.plan.outputs.action == 'promote' && !inputs.dry_run
        env:
          GH_TOKEN: ${{ secrets.STAGING_PUSH_TOKEN || github.token }}
        run: |
          set -euo pipefail
          tag=$(jq -r .production_tag plan.json); sha=$(jq -r .sha plan.json)
          gh release edit "$tag" --draft=false --prerelease
          # production-nightly channel: imagetools create -t …:production-nightly <digests> (needs GHCR login; do it in the image step's job or log in again here)
          gh api -X PATCH "repos/$GITHUB_REPOSITORY/git/refs/tags/production-latest" -f sha="$sha" -F force=true \
            || gh api -X POST "repos/$GITHUB_REPOSITORY/git/refs" -f ref="refs/tags/production-latest" -f sha="$sha"
          gh release edit production-latest --prerelease --title "Personal production (latest — $tag)" --notes-file notes.md \
            || gh release create production-latest --prerelease --title "Personal production (latest — $tag)" --notes-file notes.md
          gh release upload production-latest dist/* --clobber

      - name: Alert on failure
        if: failure()
        # same HMAC ha-notify block: "omnigent promote FAILED — $RUN_URL"
```

  Replace every `…`/comment placeholder above with the concrete commands copied from the referenced workflow before committing. Place the `production-nightly` channel retag in the same step as the digest retag, but run it only after `Pin` succeeds: split it into a separate step after `Pin` with the GHCR login (`docker/login-action` with the same pin as `personal-staging-images.yml`).
- [ ] **Step 2:** Run `actionlint .github/workflows/personal-promote.yml`. Expect no errors.
- [ ] **Step 3:** Add the README section and fix the spec's wording (`stage.py promote` → `promote.py plan|pin|verify-assets`).
- [ ] **Step 4:** Commit with the message `feat(rings): manual-dispatch promote workflow reusing nightly artifacts`.

---

## PR 3 — cutover (branch `feat/rings-promote-cutover`, based on PR 2; left OPEN)

### Task 7: Status trigger, retire production compose, retention guard

**Files:**
- Modify: `.github/workflows/personal-promote.yml`, `.github/workflows/personal-production.yml`, `.github/scripts/personal-staging/prune_pins.py`, `.github/workflows/personal-pin-retention.yml`, `test_prune_pins.py`, `README.md`
- Delete: `.github/scripts/personal-staging/extras-production.txt`

- [ ] **Step 1: Status trigger.**
  - Add `status:` to `on:` in `personal-promote.yml`.
  - Gate the jobs with `if: github.event_name != 'status' || (github.event.context == 'soak/homelab' && vars.SOAK_STATUS_LOGIN != '')`.
  - For status events, call the plan with `--trigger status --sha "${{ github.event.sha }}" --soak-login "${{ vars.SOAK_STATUS_LOGIN }}"`, passing these through `env:`, never interpolated inline.
  - `ignored` plans exit 0 with a summary line. `failure`/`error` verdicts send an ha-notify alert (`soak FAILED for <sha>`).
- [ ] **Step 2: Retention guard.** `prune_pins.plan()` takes `protected: set[str]` (tag names never pruned). In `main`, read `production-latest`'s `source.json` through `gh release download production-latest -p source.json -O -` and protect its `source_tag`; if the file is missing, protect nothing. Add a test, `test_plan_never_prunes_protected_nightly`, in `test_prune_pins.py`, following that file's existing plan tests.
- [ ] **Step 3: Retire production compose.**
  - Reduce `personal-production.yml` to a stub with a `workflow_dispatch` that fails with "production is promoted by personal-promote.yml" and a header comment saying why. Alternatively delete the file and update every reference: `grep -rn personal-production .github docs`.
  - Delete `extras-production.txt`. `stage.py`'s PRODUCTION ring stays, because `pin_name` uses it; a missing manifest means no extras.
  - Remove the production `test-composer`/`publish-images` references to that manifest, if any.
- [ ] **Step 4:** Run the full suite: `pytest .github/scripts/personal-staging/ -q`, then actionlint on all changed workflows. Commit with the message `feat(rings): promote on soak/homelab statuses and retire production compose`.

---

## Live verification (coordinator, after merging PRs 1–2)

1. Dispatch `personal-staging.yml`. Expect verify, android-build (package id `.dev`), desktop-build and publish to succeed, then the image run to upload `build-complete.json`.
2. Dispatch `personal-promote.yml` with `nightly=<that tag>` and `dry_run=true`. Expect `action: promote`, unless migrations block it; a blocked plan is reported and needs the user's backup before approval.
3. Dispatch it again without `dry_run`. Expect `production-YYYYMMDD` at the nightly's sha, a release whose asset sha256s equal the nightly's, `production-latest` moved, and the image tags present.
