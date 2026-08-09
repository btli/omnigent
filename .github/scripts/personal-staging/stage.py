#!/usr/bin/env python3
"""Compose the nightly personal staging ring on the fork.

Builds branch ``staging`` = upstream main + every open btli PR merged
sequentially (ascending PR number; conflicting PRs are skipped and reported),
then pins an immutable ``nightly-YYYYMMDD`` branch + tag and a PEP 440
``vX.Y.Z.devYYYYMMDD`` tag at the same commit. Never touches branch
``testing`` (the manually composed homelab deploy branch).

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

UPSTREAM_REPO = "omnigent-ai/omnigent"
PR_AUTHOR = "btli"
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
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=check,
        text=True,
        capture_output=True,
        env={**os.environ, **env} if env else None,
    )


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
            "100",
            "--json",
            "number,headRefName,headRefOid",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    return json.loads(out)


def remote_ref(cwd: str | Path, remote: str, ref: str) -> str:
    out = git(cwd, "ls-remote", remote, ref).stdout.strip()
    return out.split("\t")[0] if out else ""


def conflict_paths(cwd: str | Path) -> list[str]:
    out = git(cwd, "diff", "--name-only", "--diff-filter=U").stdout
    return sorted(p for p in out.splitlines() if p)


def dev_version(cwd: str | Path, datestamp: str) -> str:
    """Mirror nightly-release.yml: main carries X.Y.Z.dev0, we stamp the date."""
    text = (Path(cwd) / "pyproject.toml").read_text()
    m = re.search(r'^version = "(\d+\.\d+\.\d+)\.dev0"$', text, re.MULTILINE)
    if not m:
        raise StageError("pyproject.toml version is not X.Y.Z.dev0; refusing to derive a dev tag")
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
            paths = conflict_paths(cwd)
            git(cwd, "merge", "--abort", check=False)
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
    dev_tag = dev_version(cwd, datestamp)

    # Push staging with a lease against the value we just observed, so a
    # concurrent push loses loudly instead of being clobbered.
    expected = remote_ref(cwd, fork, "refs/heads/staging")
    git(
        cwd,
        "push",
        f"--force-with-lease=refs/heads/staging:{expected}",
        fork,
        f"{staging_sha}:refs/heads/staging",
    )

    name, created = pin_name(cwd, fork, datestamp, staging_sha)
    if created:
        git(
            cwd,
            "push",
            fork,
            f"{staging_sha}:refs/heads/{name}",
            f"{staging_sha}:refs/tags/{name}",
        )

    # The dev tag floats within the day: a rerun repoints it (fork-local tag,
    # nothing downstream pins to it mid-day).
    if remote_ref(cwd, fork, f"refs/tags/{dev_tag}") != staging_sha:
        git(cwd, "push", "--force", fork, f"{staging_sha}:refs/tags/{dev_tag}")

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
    lines += [f"- #{p['pr']} {p['branch']} @ `{p['oid'][:12]}`" for p in report["applied"]] or [
        "- none"
    ]
    lines += ["", f"## Skipped PRs ({len(report['skipped'])})"]
    for p in report["skipped"]:
        why = p.get("reason") or "merge conflict: " + (
            ", ".join(p["conflict_paths"]) or "unknown paths"
        )
        lines.append(f"- #{p['pr']} {p['branch']} — {why}")
    if not report["skipped"]:
        lines.append("- none")
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
    for p in report["skipped"]:
        why = p.get("reason") or ", ".join(p["conflict_paths"])
        rows.append(f"| Skipped #{p['pr']} | {why} |")
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
    prs = json.loads(Path(args.prs_json).read_text()) if args.prs_json else list_prs()
    report = stage(args.workdir, prs, date, upstream=args.upstream_remote, fork=args.fork_remote)
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a") as f:
            f.write(summarize(report))
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
