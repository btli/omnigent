#!/usr/bin/env python3
"""Compose the nightly personal staging ring on the fork.

Builds branch ``staging`` = upstream main + every open btli PR merged
sequentially (ascending PR number; conflicting PRs are skipped and reported),
then pins an immutable ``nightly-YYYYMMDD`` branch + tag and a PEP 440
``vX.Y.Z.devYYYYMMDD`` tag at the same commit. The only refs ever pushed are
``staging``, the ``nightly-*`` pin, and the dev tag.

Stdlib + git subprocess only; the gh CLI is used solely to list PRs and can
be bypassed with --prs-json (how the tests stay offline).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import tomllib

UPSTREAM_REPO = "omnigent-ai/omnigent"
PR_AUTHOR = "btli"
PR_LIST_LIMIT = 100
# Fixed identity, no gpg signature: the merge commit must be byte-reproducible
# so an unchanged same-day rerun lands on the identical sha (no-op detection).
COMMIT_IDENT = [
    "-c",
    "user.name=omnigent-staging",
    "-c",
    "user.email=staging@invalid",
    "-c",
    "commit.gpgsign=false",
]


class StageError(RuntimeError):
    pass


def git(
    cwd: str | Path, *args: str, check: bool = True, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, **env} if env else None,
    )
    if check and proc.returncode != 0:
        raise StageError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc


def md_code(text: object) -> str:
    """Render untrusted text (branch names, paths) as an inert code span so a
    crafted PR branch name can't inject markdown into notes or summaries."""
    clean = str(text).replace("`", "'").replace("|", "/").replace("\n", " ")
    return f"`{clean}`"


def append_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text)


def list_prs() -> list[dict]:
    out = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            UPSTREAM_REPO,
            "--author",
            PR_AUTHOR,
            "--state",
            "open",
            "--limit",
            str(PR_LIST_LIMIT),
            "--json",
            "number,headRefName,headRefOid",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    prs = json.loads(out)
    if len(prs) >= PR_LIST_LIMIT:
        raise StageError(
            f"gh pr list returned {len(prs)} PRs (limit {PR_LIST_LIMIT}); "
            "results may be truncated — raise the limit"
        )
    return prs


def remote_ref(cwd: str | Path, remote: str, ref: str) -> str:
    out = git(cwd, "ls-remote", remote, ref).stdout.strip()
    return out.split("\t")[0] if out else ""


def conflict_paths(cwd: str | Path) -> list[str]:
    out = git(cwd, "diff", "--name-only", "--diff-filter=U").stdout
    return sorted(p for p in out.splitlines() if p)


def dev_version(cwd: str | Path, upstream_sha: str, datestamp: str) -> str:
    """Mirror nightly-release.yml's scheme, but read the version from the
    UPSTREAM commit — a merged PR must not control the tag we mint."""
    text = git(cwd, "show", f"{upstream_sha}:pyproject.toml").stdout
    version = tomllib.loads(text).get("project", {}).get("version", "")
    m = re.fullmatch(r"(\d+\.\d+\.\d+)\.dev0", version)
    if not m:
        raise StageError(
            f"upstream pyproject.toml version is {version!r}, expected X.Y.Z.dev0; "
            "refusing to derive a dev tag"
        )
    return f"v{m.group(1)}.dev{datestamp}"


def merge_prs(cwd: str | Path, prs: list[dict], upstream: str) -> tuple[list[dict], list[dict]]:
    applied: list[dict] = []
    skipped: list[dict] = []
    for pr in sorted(prs, key=lambda p: p["number"]):
        num, branch, oid = pr["number"], pr["headRefName"], pr["headRefOid"]
        git(cwd, "fetch", upstream, f"refs/pull/{num}/head")
        # The listed head may have been force-pushed away between list and fetch.
        if git(cwd, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode != 0:
            skipped.append(
                {
                    "pr": num,
                    "branch": branch,
                    "conflict_paths": [],
                    "reason": "pinned head unreachable",
                }
            )
            continue
        merge = git(cwd, "merge", "--no-ff", "--no-commit", oid, check=False)
        if merge.returncode != 0:
            # Only a genuine content conflict (unmerged index entries) is
            # skippable; anything else is a broken workspace — fail loud.
            if not git(cwd, "ls-files", "-u").stdout.strip():
                raise StageError(
                    f"merge of PR #{num} failed without conflicts: {merge.stderr.strip()}"
                )
            paths = conflict_paths(cwd)
            git(cwd, "merge", "--abort")
            skipped.append({"pr": num, "branch": branch, "conflict_paths": paths})
            continue
        # --no-commit leaves MERGE_HEAD behind on a real merge; an
        # already-merged PR returns 0 with nothing to commit.
        if git(cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            # Stamp the merge with the PR head's committer date: identical
            # inputs reproduce the exact staging sha, so a same-day rerun with
            # nothing new is detected as a no-op instead of minting -rerunN.
            when = git(cwd, "show", "-s", "--format=%cI", oid).stdout.strip()
            git(
                cwd,
                *COMMIT_IDENT,
                "commit",
                "--no-verify",
                "-m",
                f"staging: merge PR #{num} ({branch} @ {oid[:12]})",
                env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
            )
        applied.append({"pr": num, "branch": branch, "oid": oid})
    return applied, skipped


def pin_name(cwd: str | Path, fork: str, datestamp: str, sha: str) -> tuple[str, bool]:
    """Immutable name for tonight's pin: nightly-YYYYMMDD, -rerunN if the day
    already has a pin at a different commit, no-op if it already points here."""
    n = 0
    while True:
        cand = f"nightly-{datestamp}" + (f"-rerun{n}" if n else "")
        existing = remote_ref(cwd, fork, f"refs/tags/{cand}")
        if not existing:
            return cand, True
        if existing == sha:
            return cand, False
        n += 1


def stage(
    cwd: str | Path,
    prs: list[dict],
    date: dt.date,
    upstream: str = "upstream",
    fork: str = "origin",
) -> dict:
    datestamp = date.strftime("%Y%m%d")

    git(cwd, "fetch", upstream, "main")
    upstream_sha = git(cwd, "rev-parse", "FETCH_HEAD").stdout.strip()
    git(cwd, "checkout", "--detach", upstream_sha)

    applied, skipped = merge_prs(cwd, prs, upstream)
    staging_sha = git(cwd, "rev-parse", "HEAD").stdout.strip()
    dev_tag = dev_version(cwd, upstream_sha, datestamp)
    name, created = pin_name(cwd, fork, datestamp, staging_sha)

    # One atomic push for every ref: a partial failure can't leave the fork
    # with mixed staging/pin/dev-tag state. Leases pin the values we just
    # observed so a concurrent push loses loudly instead of being clobbered.
    refspecs: list[str] = []
    leases: list[str] = []
    expected_staging = remote_ref(cwd, fork, "refs/heads/staging")
    if expected_staging != staging_sha:
        refspecs.append(f"{staging_sha}:refs/heads/staging")
        leases.append(f"--force-with-lease=refs/heads/staging:{expected_staging}")
    if created:
        refspecs += [f"{staging_sha}:refs/heads/{name}", f"{staging_sha}:refs/tags/{name}"]
    # The dev tag floats within the day: a rerun repoints it (fork-local tag,
    # nothing downstream pins to it mid-day).
    expected_dev = remote_ref(cwd, fork, f"refs/tags/{dev_tag}")
    if expected_dev != staging_sha:
        refspecs.append(f"{staging_sha}:refs/tags/{dev_tag}")
        leases.append(f"--force-with-lease=refs/tags/{dev_tag}:{expected_dev}")
    if refspecs:
        git(cwd, "push", "--atomic", *leases, fork, *refspecs)

    return {
        "date": datestamp,
        "upstream_sha": upstream_sha,
        "staging_sha": staging_sha,
        "branch": name,
        "tag": name,
        "dev_tag": dev_tag,
        "pin_created": created,
        "applied": applied,
        "skipped": skipped,
    }


def _skip_reason(p: dict) -> str:
    if p.get("reason"):
        return str(p["reason"])
    paths = ", ".join(md_code(c) for c in p["conflict_paths"])
    return f"merge conflict: {paths or 'unknown paths'}"


def notes(report: dict, signed: bool) -> str:
    lines = [
        f"Nightly personal staging ring for {report['date']} (UTC).",
        "",
        f"- Upstream main: `{report['upstream_sha']}`",
        f"- Staging commit: `{report['staging_sha']}`",
        f"- Dev tag: `{report['dev_tag']}`",
        "",
        f"## Applied PRs ({len(report['applied'])})",
    ]
    lines += [
        f"- #{p['pr']} {md_code(p['branch'])} @ `{str(p['oid'])[:12]}`" for p in report["applied"]
    ] or ["- none"]
    lines += ["", f"## Skipped PRs ({len(report['skipped'])})"]
    lines += [
        f"- #{p['pr']} {md_code(p['branch'])} — {_skip_reason(p)}" for p in report["skipped"]
    ] or ["- none"]
    lines += ["", "## APK signing"]
    if signed:
        lines.append(
            "Debug APK signed with the shared debug keystore — installs upgrade in place."
        )
    else:
        lines.append(
            "Debug APK signed with a runner-ephemeral keystore — "
            "in-place upgrade across nightlies will FAIL (uninstall first). "
            "Set the OMNIGENT_DEBUG_KEYSTORE_B64 secret to fix this."
        )
    return "\n".join(lines) + "\n"


def summarize(report: dict) -> str:
    pin_note = "" if report["pin_created"] else " (already pinned — rerun no-op)"
    rows = [
        "## Personal staging nightly",
        "",
        "| | |",
        "| --- | --- |",
        f"| Upstream main | `{report['upstream_sha']}` |",
        f"| Staging | `{report['staging_sha']}` |",
        f"| Pin | `{report['tag']}`{pin_note} |",
        f"| Dev tag | `{report['dev_tag']}` |",
        f"| Applied / skipped | {len(report['applied'])} / {len(report['skipped'])} |",
    ]
    rows += [f"| Skipped #{p['pr']} | {_skip_reason(p)} |" for p in report["skipped"]]
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser("stage", help="build and push the staging ring")
    p_stage.add_argument("--workdir", default=".")
    p_stage.add_argument("--upstream-remote", default="upstream")
    p_stage.add_argument("--fork-remote", default="origin")
    p_stage.add_argument("--date", help="UTC datestamp YYYYMMDD (default: today)")
    p_stage.add_argument("--prs-json", help="read PRs from this JSON file instead of gh")
    p_stage.add_argument("--report", default="merge-report.json")

    p_notes = sub.add_parser("notes", help="render release notes from a merge report")
    p_notes.add_argument("--report", default="merge-report.json")
    p_notes.add_argument("--signed", choices=["true", "false"], required=True)

    args = parser.parse_args(argv)

    if args.cmd == "notes":
        report = json.loads(Path(args.report).read_text())
        sys.stdout.write(notes(report, signed=args.signed == "true"))
        return 0

    if args.date:
        date = dt.date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:8]))
    else:
        date = dt.datetime.now(dt.timezone.utc).date()
    try:
        prs = json.loads(Path(args.prs_json).read_text()) if args.prs_json else list_prs()
        report = stage(
            args.workdir, prs, date, upstream=args.upstream_remote, fork=args.fork_remote
        )
    except Exception as e:
        # The step summary is the failure surface — never exit without one.
        append_summary(f"## Personal staging nightly\n\n**FAILED:** {e}\n")
        raise
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    append_summary(summarize(report))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
