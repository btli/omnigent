#!/usr/bin/env python3
"""Compose the personal staging ring on the fork.

Builds branch ``staging`` = upstream main + every open btli PR plus the
``extras.txt`` pins, merged sequentially (ascending PR number; conflicting
PRs are skipped and reported). The nightly mode then pins an immutable
``nightly-YYYYMMDD`` branch + tag and a PEP 440 ``vX.Y.Z.devYYYYMMDD`` tag
at the same commit; ``--staging-only`` (the hourly mode) pushes only
``staging`` and skips the push entirely when the composition is unchanged.
The only refs ever pushed are ``staging``, the ``nightly-*`` pin, and the
dev tag.

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
import time
from pathlib import Path

# Resolved and cached at module load, while sys.path[0] (this script's dir)
# still holds only trusted files — a merged PR dropping a tomllib.py shadow
# next to us mid-run must never be importable. Only the stage command needs
# it; the notes entry point tolerates python < 3.11.
try:
    import tomllib
except ImportError:
    tomllib = None

UPSTREAM_REPO = "omnigent-ai/omnigent"
PR_AUTHOR = "btli"
PR_LIST_LIMIT = 100
EXTRAS_FILE = Path(__file__).resolve().parent / "extras.txt"
# Two different answers about a pinned extra: only a ref confirmed absent
# invites editing the manifest, and only a failure to reach the remote blocks
# the push (dropping a required pin would silently regress staging).
EXTRA_MISSING = "extra unfetchable (likely deleted; remove from extras.txt)"
EXTRA_FETCH_FAILED = "extra fetch failed (cannot reach upstream; pin kept, staging not advanced)"
EXTRA_FETCH_ATTEMPTS = 3
EXTRA_FETCH_BACKOFF_S = 2
# Subject line minted below for every staging merge commit; also decoded to
# diff two compositions (what changed between the old and new staging).
STAGING_MERGE_RE = re.compile(r"staging: merge PR #(\d+) \((.+) @ ([0-9a-f]{12})\)")
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
    return check_not_truncated(json.loads(out))


def check_not_truncated(prs: list[dict]) -> list[dict]:
    if len(prs) >= PR_LIST_LIMIT:
        raise StageError(
            f"PR list has {len(prs)} entries (limit {PR_LIST_LIMIT}); "
            "results may be truncated — raise the limit"
        )
    return prs


def parse_extras(path: Path) -> list[int]:
    """PR numbers from the extras manifest: one per line, blank lines and
    ``#``-to-end-of-line comments allowed; a missing file means no extras.
    A non-numeric entry is a config error — fail loud, don't drop a pin."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    numbers: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        if not entry.isdigit():
            raise StageError(f"{path}:{lineno}: invalid PR number {entry!r}")
        numbers.append(int(entry))
    return numbers


def fetch_extra(cwd: str | Path, upstream: str, num: int) -> tuple[str, str]:
    """Resolve a pinned extra's frozen ``refs/pull/N/head`` to a commit oid,
    returning ``(oid, "")`` on success and ``("", reason)`` otherwise. A
    deleted ref and an unreachable remote are different answers: only
    ls-remote reporting the ref absent proves deletion, so only that outcome
    invites editing the manifest."""
    ref = f"refs/pull/{num}/head"
    for attempt in range(EXTRA_FETCH_ATTEMPTS):
        if git(cwd, "fetch", upstream, ref, check=False).returncode == 0:
            head = git(cwd, "rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}", check=False)
            if head.returncode == 0:
                return head.stdout.strip(), ""
        if attempt + 1 < EXTRA_FETCH_ATTEMPTS:
            time.sleep(EXTRA_FETCH_BACKOFF_S)
    probe = git(cwd, "ls-remote", upstream, ref, check=False)
    if probe.returncode == 0 and not probe.stdout.strip():
        return "", EXTRA_MISSING
    return "", EXTRA_FETCH_FAILED


def merge_stream(open_prs: list[dict], extras: list[int]) -> list[dict]:
    """Union of open PRs and extras, deduped by PR number (the open entry
    wins), ascending — the same ordering rule the composition always used."""
    open_nums = {p["number"] for p in open_prs}
    stream = [{**p, "source": "open"} for p in open_prs]
    stream += [{"number": n, "source": "extra"} for n in sorted(set(extras) - open_nums)]
    return sorted(stream, key=lambda p: p["number"])


def remote_ref(cwd: str | Path, remote: str, ref: str) -> str:
    out = git(cwd, "ls-remote", remote, ref).stdout.strip()
    return out.split("\t")[0] if out else ""


def conflict_paths(cwd: str | Path) -> list[str]:
    out = git(cwd, "diff", "--name-only", "--diff-filter=U").stdout
    return sorted(p for p in out.splitlines() if p)


def dev_version(cwd: str | Path, upstream_sha: str, datestamp: str) -> str:
    """Mirror nightly-release.yml's scheme, but read the version from the
    UPSTREAM commit — a merged PR must not control the tag we mint."""
    if tomllib is None:
        raise StageError("the stage command requires python >= 3.11 (tomllib)")
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
        num, source = pr["number"], pr.get("source", "open")
        if source == "extra":
            # No pinned head for an extra: its refs/pull/N/head is frozen by
            # GitHub after close, so the ref itself is the only pin.
            branch = f"pull/{num}/head"
            oid, reason = fetch_extra(cwd, upstream, num)
            if not oid:
                skipped.append(
                    {
                        "pr": num,
                        "branch": branch,
                        "conflict_paths": [],
                        "reason": reason,
                        "source": source,
                    }
                )
                continue
        else:
            branch, oid = pr["headRefName"], pr["headRefOid"]
            git(cwd, "fetch", upstream, f"refs/pull/{num}/head")
            # The listed head may have been force-pushed away between list and fetch.
            if git(cwd, "cat-file", "-e", f"{oid}^{{commit}}", check=False).returncode != 0:
                skipped.append(
                    {
                        "pr": num,
                        "branch": branch,
                        "conflict_paths": [],
                        "reason": "pinned head unreachable",
                        "source": source,
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
            skipped.append(
                {"pr": num, "branch": branch, "conflict_paths": paths, "source": source}
            )
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
        applied.append({"pr": num, "branch": branch, "oid": oid, "source": source})
    return applied, skipped


def composition_of(cwd: str | Path, sha: str) -> tuple[str, dict[int, tuple[str, str]]] | None:
    """Decode a pushed staging commit into (upstream base sha, {pr: (branch,
    head12)}) from its first-parent merge subjects, walking to the real base —
    the merge count is unbounded. None when the sha isn't readable."""
    if not sha or git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        return None
    merges: dict[int, tuple[str, str]] = {}
    out = git(cwd, "log", "--first-parent", "--format=%H%x09%s", sha).stdout
    for line in out.splitlines():
        commit, _, subject = line.partition("\t")
        m = STAGING_MERGE_RE.fullmatch(subject)
        if not m:
            return commit, merges
        merges[int(m.group(1))] = (m.group(2), m.group(3))
    return None


def push_causes(
    old: tuple[str, dict[int, tuple[str, str]]] | None,
    upstream_sha: str,
    applied: list[dict],
) -> list[str]:
    """One-line answer to "why did staging move": which composition inputs
    differ from the one the remote branch already carried."""
    if old is None:
        return ["no previous composition to compare"]
    old_base, old_merges = old
    new = {p["pr"]: (p["branch"], str(p["oid"])[:12], p["source"]) for p in applied}
    label = {"open": "open PR set", "extra": "extras"}
    causes: list[str] = []
    if old_base != upstream_sha:
        causes.append("upstream HEAD")
    dropped: list[str] = []
    for num in sorted(old_merges.keys() | new.keys()):
        entry = new.get(num)
        if entry is None:
            # No longer composed at all (unpinned extra, closed PR, skip) —
            # say so rather than guessing which input it used to come from.
            dropped.append(f"#{num}")
        elif old_merges.get(num) != entry[:2] and label[entry[2]] not in causes:
            causes.append(label[entry[2]])
    if dropped:
        causes.append("dropped PR " + ", ".join(dropped))
    return causes or ["composition changed (cause unknown)"]


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
    staging_only: bool = False,
) -> dict:
    datestamp = date.strftime("%Y%m%d")

    git(cwd, "fetch", upstream, "main")
    upstream_sha = git(cwd, "rev-parse", "FETCH_HEAD").stdout.strip()
    git(cwd, "checkout", "--detach", upstream_sha)

    applied, skipped = merge_prs(cwd, prs, upstream)
    staging_sha = git(cwd, "rev-parse", "HEAD").stdout.strip()

    # An extra we could not even reach is an infrastructure failure, not a
    # composition: publishing without it would silently regress staging.
    blocked = [p["pr"] for p in skipped if p.get("reason") == EXTRA_FETCH_FAILED]

    if staging_only:
        # Hourly mode: only refs/heads/staging moves — no pins, no tags.
        # Composition is byte-reproducible, so an identical remote sha means
        # nothing changed and the push is skipped outright.
        expected_staging = remote_ref(cwd, fork, "refs/heads/staging")
        report = {
            "date": datestamp,
            "upstream_sha": upstream_sha,
            "staging_sha": staging_sha,
            "staging_only": True,
            "blocked": blocked,
            "pushed": not blocked and expected_staging != staging_sha,
            "causes": [],
            "applied": applied,
            "skipped": skipped,
        }
        if not report["pushed"]:
            return report
        git(cwd, "fetch", fork, "refs/heads/staging", check=False)
        report["causes"] = push_causes(
            composition_of(cwd, expected_staging), upstream_sha, applied
        )
        git(
            cwd,
            "push",
            f"--force-with-lease=refs/heads/staging:{expected_staging}",
            fork,
            f"{staging_sha}:refs/heads/staging",
        )
        return report

    # The nightly publishes releases from this composition, and its downstream
    # jobs consume the report's sha/tag — so refuse loudly before any ref moves
    # rather than shipping artifacts that quietly omit a required pin.
    if blocked:
        raise StageError(
            "extras unreachable due to infrastructure failure (not deleted): "
            + ", ".join(f"#{n}" for n in blocked)
            + "; refusing to publish a composition without them"
        )

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
    else:
        # The tag already points here; the twin branch must agree. Repair a
        # missing branch, but a divergent one means someone moved an
        # immutable pin — fail rather than clobber.
        branch_sha = remote_ref(cwd, fork, f"refs/heads/{name}")
        if not branch_sha:
            refspecs.append(f"{staging_sha}:refs/heads/{name}")
        elif branch_sha != staging_sha:
            raise StageError(f"pin branch {name} points at {branch_sha}, expected {staging_sha}")
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
    if report.get("staging_only"):
        if report["blocked"]:
            result = (
                "**NOT pushed** — extras unreachable: "
                + ", ".join(f"#{n}" for n in report["blocked"])
                + " (staging left at its previous commit)"
            )
        elif report["pushed"]:
            result = f"pushed (changed: {', '.join(report['causes'])})"
        else:
            result = "unchanged — push skipped"
        rows = [
            "## Personal staging hourly",
            "",
            "| | |",
            "| --- | --- |",
            f"| Upstream main | `{report['upstream_sha']}` |",
            f"| Staging | `{report['staging_sha']}` |",
            f"| Result | {result} |",
            f"| Applied / skipped | {len(report['applied'])} / {len(report['skipped'])} |",
        ]
        rows += [f"| Skipped #{p['pr']} | {_skip_reason(p)} |" for p in report["skipped"]]
        return "\n".join(rows) + "\n"

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
    p_stage.add_argument(
        "--staging-only",
        action="store_true",
        help="push only refs/heads/staging; mint no nightly pins or dev tags",
    )
    p_stage.add_argument(
        "--extras",
        default=str(EXTRAS_FILE),
        help="extras manifest path (missing file == no extras)",
    )

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
    title = "Personal staging hourly" if args.staging_only else "Personal staging nightly"
    try:
        # Fail the stage path before any fetch/merge if the parser is absent.
        if tomllib is None:
            raise StageError("the stage command requires python >= 3.11 (tomllib)")
        prs = (
            check_not_truncated(json.loads(Path(args.prs_json).read_text()))
            if args.prs_json
            else list_prs()
        )
        # Extras are read here, before any merge touches the worktree, so the
        # manifest always comes from the trusted checkout.
        prs = merge_stream(prs, parse_extras(Path(args.extras)))
        report = stage(
            args.workdir,
            prs,
            date,
            upstream=args.upstream_remote,
            fork=args.fork_remote,
            staging_only=args.staging_only,
        )
    except Exception as e:
        # The step summary is the failure surface — never exit without one.
        append_summary(f"## {title}\n\n**FAILED:** {e}\n")
        raise
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    append_summary(summarize(report))
    if report.get("blocked"):
        print(
            "::warning::could not reach upstream for extras "
            + ", ".join(f"#{n}" for n in report["blocked"])
            + " — staging left unchanged; this is an infrastructure failure, not a deleted ref"
        )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
