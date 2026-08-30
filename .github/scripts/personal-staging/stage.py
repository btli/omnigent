#!/usr/bin/env python3
"""Compose the personal staging ring on the fork.

Builds branch ``staging`` = upstream main + every open btli PR plus the
``extras.txt`` pins, merged sequentially (ascending PR number; conflicting
PRs are skipped and reported — but an open PR whose merge conflicts gets
one rebase rescue onto upstream main first, and a clean rescue is pushed
back to the PR's fork branch so the PR stays current). The nightly mode
then pins an immutable ``nightly-YYYYMMDD`` tag, a same-name compatibility
branch through v0.11.x,
and a PEP 440 ``vX.Y.Z.devYYYYMMDD`` tag at the same commit;
``--staging-only`` (the hourly mode) pushes only
``staging`` and skips the push entirely when the composition is unchanged.
The refs ever pushed are ``staging``, the ``nightly-*`` pin, the dev tag,
and (rescues only) the rescued PR's own fork branch. ``--ring production``
composes the production ring instead: open
non-draft PRs plus the ``extras-production.txt`` pins (a listed draft is
force-promoted past the draft gate), branch ``production`` + immutable
``production-YYYYMMDD`` pins, no dev tag, and no hourly mode
(``--staging-only`` is rejected for it). A production composition
that touches DB migrations is BLOCKED before any ref moves unless
``--migration-approval`` names the exact candidate sha.

Stdlib + git subprocess only; the gh CLI is used solely to list PRs and can
be bypassed with --prs-json (how the tests stay offline).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
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
PR_LIST_LIMIT = 300
EXTRAS_FILE = Path(__file__).resolve().parent / "extras.txt"
PRODUCTION_EXTRAS_FILE = Path(__file__).resolve().parent / "extras-production.txt"
EXCLUDE_FILE = Path(__file__).resolve().parent / "exclude.txt"
# Two different answers about a pinned extra: only a ref confirmed absent
# invites editing the manifest, and only a failure to reach the remote blocks
# the push (dropping a required pin would silently regress staging).
EXTRA_MISSING = "extra unfetchable (likely deleted; remove from extras.txt)"
EXTRA_FETCH_FAILED = "extra fetch failed (cannot reach remote; pin kept, staging not advanced)"
EXTRA_FETCH_ATTEMPTS = 3
EXTRA_FETCH_BACKOFF_S = 2
# Committed conflict resolutions in git's own rr-cache layout (one <40-hex>/
# directory holding preimage+postimage per recorded conflict). Seeded into the
# compose workspace so a PR whose conflicts are fully covered merges instead of
# being skipped. The seed is a composition input like extras.txt: identical
# seeds + identical heads reproduce identical staging bytes.
RR_CACHE_DIR = Path(__file__).resolve().parent / "rr-cache"


@dataclass(frozen=True)
class Ring:
    """A promotion ring the composer can build. Every branch-name, pin-tag
    prefix, and merge-subject string the composer mints or decodes is derived
    from here, so a second ring (e.g. production) reuses the same code path
    with different identity. ``use_extras``/``exclude_drafts``/``mint_dev_tag``
    are composition switches consumed by the CLI."""

    name: str
    branch: str
    merge_subject_prefix: str
    pin_prefix: str
    use_extras: bool
    exclude_drafts: bool
    mint_dev_tag: bool
    # Whether a conflicting open PR gets a rebase rescue onto upstream main
    # (and its fork branch force-with-lease pushed to the rescued head).
    # Rewriting PR branches is a staging-ring convenience only; production
    # composes what the PRs actually say.
    rebase_rescue: bool = False
    # The ring's extras manifest (consulted only when use_extras is set);
    # per-ring so a production pin never drags staging's pins along.
    extras_file: Path = EXTRAS_FILE
    # Whether ``branch:<name>`` pins are legal in the manifest. PR pins are
    # reviewable upstream refs; a raw fork branch is not, so production
    # refuses them.
    branch_extras: bool = True


# The staging ring — the composer's only ring today; every function defaults to
# it so existing callers (and main()) are unchanged.
STAGING = Ring(
    name="staging",
    branch="staging",
    merge_subject_prefix="staging",
    pin_prefix="nightly-",
    use_extras=True,
    exclude_drafts=False,
    mint_dev_tag=True,
    rebase_rescue=True,
)


# The production ring: upstream main + open NON-draft PRs — draft status is
# the promotion gate — plus the extras-production.txt pins (its own manifest,
# so nothing staged-only reaches prod by accident; a listed draft is
# force-promoted past the gate). No dev tag (nothing downstream consumes
# one), immutable production-YYYYMMDD pins.
PRODUCTION = Ring(
    name="production",
    branch="production",
    merge_subject_prefix="production",
    pin_prefix="production-",
    use_extras=True,
    exclude_drafts=True,
    mint_dev_tag=False,
    extras_file=PRODUCTION_EXTRAS_FILE,
    branch_extras=False,
)

RINGS = {r.name: r for r in (STAGING, PRODUCTION)}


# Subject line minted for every merge commit; also decoded to diff two
# compositions (what changed between the old and new tip). Built from the ring
# so the prefix follows the ring's identity.
def merge_re(ring: Ring = STAGING) -> re.Pattern[str]:
    return re.compile(
        re.escape(ring.merge_subject_prefix) + r": merge PR #(\d+) \((.+) @ ([0-9a-f]{12})\)"
    )


def branch_merge_re(ring: Ring = STAGING) -> re.Pattern[str]:
    return re.compile(
        re.escape(ring.merge_subject_prefix) + r": merge branch (.+) \((.+) @ ([0-9a-f]{12})\)"
    )


# Fixed per-ring identity, no gpg signature: the merge commit must be
# byte-reproducible so an unchanged same-day rerun lands on the identical sha
# (no-op detection). Staging's exact values are locked by the golden.
def commit_ident(ring: Ring = STAGING) -> list[str]:
    return [
        "-c",
        f"user.name=omnigent-{ring.name}",
        "-c",
        f"user.email={ring.name}@invalid",
        "-c",
        "commit.gpgsign=false",
    ]


class StageError(RuntimeError):
    pass


def _is_git_lock_failure(stderr: str) -> bool:
    error = stderr.casefold()
    return "merge_rr.lock" in error or ("unable to create" in error and "file exists" in error)


# Soft-failable ``git push --porcelain`` rejection reasons for refs/heads/main.
# Distinct from ``[remote rejected]`` (hooks/auth policy), which must stay loud.
_STALE_REF_REASONS = frozenset({"non-fast-forward", "fetch first", "stale info"})


def is_stale_ref_push_rejection(porcelain: str, *, ref: str = "refs/heads/main") -> bool:
    """True iff ``git push --porcelain`` shows a confirmed stale-ref race on ``ref``.

    Classifies from porcelain status lines only — never from free-form stderr
    prose — so a pre-receive hook that prints ``fetch first`` while emitting
    ``[remote rejected]`` cannot soft-fail. Soft-fail only when the target
    ref's line is flag ``!`` with summary ``[rejected]`` (not
    ``[remote rejected]``) and a non-fast-forward / fetch-first / stale-info
    reason. No matching status line (auth, transport, empty output) is a
    hard failure.
    """
    for line in porcelain.splitlines():
        if "\t" not in line:
            continue
        # Status lines are ``<flag>\\t<from>:<to>\\t<summary>``; ignore To/Done
        # and any hook noise that lacks this shape.
        flag, *fields = line.split("\t")
        if len(flag) != 1 or len(fields) < 2 or ":" not in fields[0]:
            continue
        dst = fields[0].rsplit(":", 1)[-1]
        if dst != ref:
            continue
        summary = fields[1]
        # Soft-fail ONLY canonical local rejection — never [remote rejected].
        if flag != "!" or not summary.startswith("[rejected]"):
            return False
        m = re.fullmatch(r"\[rejected\] \((.+)\)", summary)
        return bool(m and m.group(1) in _STALE_REF_REASONS)
    return False


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
            "number,headRefName,headRefOid,isDraft,author",
        ],
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    return own_prs(check_not_truncated(json.loads(out)))


def own_prs(prs: list[dict]) -> list[dict]:
    """Keep only records actually authored by ``PR_AUTHOR``, dropping the
    author field from the survivors.

    gh delegates ``--author`` to GitHub's search API; observed to come back
    UNFILTERED (a plain listing of every open PR), which below the truncation
    guard would silently compose other people's PRs into the ring. Filter
    locally on the record's own author instead of trusting the flag; a record
    with no author login is indeterminate and fails loud rather than passing
    as ours."""
    mine: list[dict] = []
    for p in prs:
        author = p.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(login, str) or not login:
            raise StageError(
                f"PR #{p.get('number')} carries no author login; "
                "refusing to guess whose PR this is"
            )
        if login.lower() != PR_AUTHOR.lower():
            continue
        mine.append({k: v for k, v in p.items() if k != "author"})
    return mine


def filter_drafts(prs: list[dict]) -> list[dict]:
    """Drop draft PRs — the production ring's promotion gate (decision 2:
    draft status, not a review/CI check, decides readiness). The gate fails
    CLOSED: a record whose isDraft is missing or non-boolean is indeterminate
    and raises rather than passing as ready."""
    for p in prs:
        if not isinstance(p.get("isDraft"), bool):
            raise StageError(
                f"PR #{p.get('number')} carries no boolean isDraft field; "
                "refusing to guess draft state for a production composition"
            )
    return [p for p in prs if p["isDraft"] is False]


def check_not_truncated(prs: list[dict]) -> list[dict]:
    if len(prs) >= PR_LIST_LIMIT:
        raise StageError(
            f"PR list has {len(prs)} entries (limit {PR_LIST_LIMIT}); "
            "results may be truncated — raise the limit"
        )
    return prs


def _validate_branch_pin(name: str, ring: Ring = STAGING) -> str | None:
    """Return a fail-loud reason, or None if ``name`` is a legal fork-branch pin.

    Reject ``-`` prefixes before any git argv so a crafted name cannot be
    parsed as an option; then ``git check-ref-format --branch`` (name is now
    a positional value, not a flag). ``main`` and the ring's own branch are the
    composition's own refs — pinning them would self-merge."""
    if not name:
        return "invalid branch name"
    if name.startswith("-"):
        return "branch name must not start with '-'"
    if name in {"main", ring.branch}:
        return f"branch {name!r} is a composition self-reference"
    # name is a single argv after --branch and does not start with '-'.
    proc = subprocess.run(
        ["git", "check-ref-format", "--branch", name],
        check=False,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0 or proc.stdout.strip() != name:
        return f"invalid branch name {name!r}"
    return None


def parse_extras(path: Path, ring: Ring = STAGING) -> tuple[list[int], list[str]]:
    """PR numbers and ``branch:<name>`` pins from the extras manifest: one
    per line, blank lines and ``#``-to-end-of-line comments allowed; a
    missing file means no extras. Anything else is a config error — fail
    loud, don't drop a pin."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return [], []
    numbers: list[int] = []
    branches: list[str] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.isdigit():
            numbers.append(int(entry))
            continue
        if entry.startswith("branch:"):
            if not ring.branch_extras:
                raise StageError(
                    f"{path}:{lineno}: branch pins are not valid {ring.name} extras {entry!r}"
                )
            name = entry[len("branch:") :]
            reason = _validate_branch_pin(name, ring)
            if reason:
                raise StageError(f"{path}:{lineno}: {reason} {entry!r}")
            branches.append(name)
            continue
        raise StageError(f"{path}:{lineno}: invalid extras entry {entry!r}")
    return numbers, branches


def parse_exclusions(path: Path) -> list[int]:
    """PR numbers from the exclusion manifest, one per line."""
    try:
        text = path.read_text()
    except FileNotFoundError:
        return []
    numbers: list[int] = []
    for lineno, raw in enumerate(text.splitlines(), 1):
        entry = raw.split("#", 1)[0].strip()
        if not entry:
            continue
        if entry.isdigit():
            numbers.append(int(entry))
            continue
        raise StageError(f"{path}:{lineno}: invalid exclude entry {entry!r}")
    return numbers


def fetch_extra(cwd: str | Path, remote: str, ref: str) -> tuple[str, str]:
    """Resolve a pinned extra ref to a commit oid, returning ``(oid, "")``
    on success and ``("", reason)`` otherwise. A deleted ref and an
    unreachable remote are different answers: only ls-remote reporting the
    ref absent proves deletion, so only that outcome invites editing the
    manifest. Refs are passed after ``--`` so a crafted name cannot be
    parsed as a git option."""
    for attempt in range(EXTRA_FETCH_ATTEMPTS):
        if git(cwd, "fetch", remote, "--", ref, check=False).returncode == 0:
            head = git(cwd, "rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}", check=False)
            if head.returncode == 0:
                return head.stdout.strip(), ""
        if attempt + 1 < EXTRA_FETCH_ATTEMPTS:
            time.sleep(EXTRA_FETCH_BACKOFF_S)
    probe = git(cwd, "ls-remote", remote, "--", ref, check=False)
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


def apply_exclusions(prs: list[dict], exclusions: list[int]) -> tuple[list[dict], list[dict]]:
    numbers = list(dict.fromkeys(exclusions))
    excluded_numbers = set(numbers)
    stream = [p for p in prs if p["number"] not in excluded_numbers]
    excluded = [{"pr": number, "reason": "held out via exclude.txt"} for number in numbers]
    return stream, excluded


def remote_ref(cwd: str | Path, remote: str, ref: str) -> str:
    out = git(cwd, "ls-remote", remote, ref).stdout.strip()
    return out.split("\t")[0] if out else ""


def conflict_paths(cwd: str | Path) -> list[str]:
    out = git(cwd, "diff", "--name-only", "--diff-filter=U").stdout
    return sorted(p for p in out.splitlines() if p)


def seed_rerere(cwd: str | Path, seed_dir: Path | None) -> int:
    """Copy committed conflict resolutions into the workspace's rr-cache.

    Returns the count of seeded entries. Must run BEFORE the compose detaches
    onto upstream HEAD: the seed lives on fork main and leaves the worktree at
    that point (the same reason extras are read early). A malformed entry
    fails loud — silently dropping one would turn a resolved PR back into a
    skip."""
    if seed_dir is None:
        return 0
    try:
        entries = sorted(p for p in seed_dir.iterdir() if p.is_dir())
    except (FileNotFoundError, NotADirectoryError):
        return 0
    git_dir = Path(git(cwd, "rev-parse", "--absolute-git-dir").stdout.strip())
    count = 0
    for entry in entries:
        if not re.fullmatch(r"[0-9a-f]{40}", entry.name):
            raise StageError(f"rr-cache seed {entry.name!r} is not a conflict-hash directory")
        if not (entry / "preimage").is_file() or not (entry / "postimage").is_file():
            raise StageError(f"rr-cache seed {entry.name} lacks a preimage/postimage pair")
        dest = git_dir / "rr-cache" / entry.name
        dest.mkdir(parents=True, exist_ok=True)
        for name in ("preimage", "postimage"):
            shutil.copyfile(entry / name, dest / name)
        count += 1
    return count


_CONFLICT_MARKER_RE = re.compile(r"^(<{7}( |$)|={7}$|>{7}( |$))", re.M)


def _rerere_resolved_paths(cwd: str | Path, paths: list[str]) -> list[str] | None:
    """*paths* iff rerere auto-resolved every conflict in the merge, else None.

    Trust is positive and layered: rerere itself must report nothing left
    (``rerere remaining``); every unmerged path must be a two-sided content
    conflict (rerere never handles delete/rename conflicts, and it stays
    SILENT about them — an empty ``remaining`` alone proves nothing); and the
    worktree copy must carry no conflict markers."""
    if git(cwd, "-c", "rerere.enabled=true", "rerere", "remaining").stdout.strip():
        return None
    stages: dict[str, set[int]] = {}
    for line in git(cwd, "ls-files", "-u", "-z").stdout.split("\0"):
        if not line:
            continue
        meta, _, path = line.partition("\t")
        stages.setdefault(path, set()).add(int(meta.split()[2]))
    if not stages or any(not {2, 3} <= present for present in stages.values()):
        return None
    for path in paths:
        try:
            text = (Path(cwd) / path).read_text(errors="replace")
        except OSError:
            return None
        if _CONFLICT_MARKER_RE.search(text):
            return None
    return paths


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


def _pin_label(p: dict) -> str:
    if p.get("source") == "extra-branch":
        return f"branch:{p['branch']}"
    return f"#{p['pr']}"


def rebase_rescue(cwd: str | Path, oid: str, upstream_sha: str, ring: Ring = STAGING) -> str:
    """Replay a conflicting PR head onto upstream main, commit by commit.

    A merge applies the branch's TOTAL diff against the merge base, so a PR
    partially landed upstream (merged in evolved form, cherry-picked, …)
    conflicts even though replaying its commits one by one succeeds — the
    rebase drops already-upstreamed patches by patch-id and ``--empty=drop``
    discards ones that replay to nothing. Runs in a throwaway worktree so the
    composition's detached HEAD never moves. ``--committer-date-is-author-date``
    plus the ring's fixed identity keeps the rewritten oids byte-reproducible:
    a same-day rerun (or a run after a failed push-back) rebuilds the
    identical head, so no-op detection still works.

    :returns: The rescued head oid, or ``""`` when the head contains merge
        commits (a linear replay would silently drop them), the rebase
        conflicts, or the head would not move (already based on upstream —
        the conflict is with the stack, and re-basing cannot help).
    """
    # A linear replay silently drops merge commits — their messages, DCO
    # trailers, and any changes introduced only in the merge resolution —
    # and the rescued head would replace the contributor's branch. Refuse
    # instead: a merge-containing head keeps its normal conflict skip.
    merges = git(cwd, "rev-list", "--merges", f"{upstream_sha}..{oid}", check=False)
    if merges.returncode != 0 or merges.stdout.strip():
        return ""
    scratch = Path(tempfile.mkdtemp(prefix="rebase-rescue-"))
    worktree = scratch / "wt"
    try:
        git(cwd, "worktree", "add", "--detach", str(worktree), oid)
        rebase = git(
            worktree,
            *commit_ident(ring),
            "rebase",
            "--empty=drop",
            "--committer-date-is-author-date",
            upstream_sha,
            check=False,
        )
        if rebase.returncode != 0:
            git(worktree, "rebase", "--abort", check=False)
            return ""
        new_oid = git(worktree, "rev-parse", "HEAD").stdout.strip()
        # No head movement means the conflict was with the stack; landing ON
        # upstream_sha means every commit already landed (blanking the PR
        # branch with upstream's own sha helps nobody) — neither is a rescue.
        return "" if new_oid in (oid, upstream_sha) else new_oid
    finally:
        git(cwd, "worktree", "remove", "--force", str(worktree), check=False)
        shutil.rmtree(scratch, ignore_errors=True)


def merge_prs(
    cwd: str | Path,
    prs: list[dict],
    upstream: str,
    fork: str = "origin",
    ring: Ring = STAGING,
    upstream_sha: str = "",
) -> tuple[list[dict], list[dict]]:
    applied: list[dict] = []
    skipped: list[dict] = []
    for pr in sorted(prs, key=lambda p: (p["number"] is None, p["number"])):
        num, source = pr.get("number"), pr.get("source", "open")
        if source == "extra-branch":
            branch = pr["headRefName"]
            oid, reason = fetch_extra(cwd, fork, f"refs/heads/{branch}")
            if not oid:
                skipped.append(
                    {
                        "pr": None,
                        "branch": branch,
                        "conflict_paths": [],
                        "reason": reason,
                        "source": source,
                    }
                )
                continue
        elif source == "extra":
            # No pinned head for an extra: its refs/pull/N/head is frozen by
            # GitHub after close, so the ref itself is the only pin.
            branch = f"pull/{num}/head"
            oid, reason = fetch_extra(cwd, upstream, f"refs/pull/{num}/head")
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
            git(cwd, "fetch", upstream, "--", f"refs/pull/{num}/head")
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
        rebased_from = ""
        merge = git(
            cwd,
            "-c",
            "rerere.enabled=true",
            "merge",
            "--no-ff",
            "--no-commit",
            "--",
            oid,
            check=False,
        )
        rerere_paths: list[str] = []
        if merge.returncode != 0:
            what = f"branch {branch}" if source == "extra-branch" else f"PR #{num}"
            if _is_git_lock_failure(merge.stderr):
                raise StageError(f"merge of {what} failed on a Git lock: {merge.stderr.strip()}")
            # Only a genuine content conflict (unmerged index entries) is
            # skippable; anything else is a broken workspace — fail loud.
            if not git(cwd, "ls-files", "-u").stdout.strip():
                raise StageError(
                    f"merge of {what} failed without conflicts: {merge.stderr.strip()}"
                )
            paths = conflict_paths(cwd)
            # A seeded rr-cache resolution (seed_rerere) may cover the whole
            # merge; anything short of full, verified coverage falls through
            # to the rebase rescue.
            resolved = _rerere_resolved_paths(cwd, paths)
            if resolved is not None:
                git(cwd, "add", "--", *resolved)
                rerere_paths = resolved
            else:
                git(cwd, "merge", "--abort")
                # One rescue per conflicting open PR: rebase its head onto
                # upstream main and retry the merge with the rescued head.
                # Extras stay verbatim — their refs are frozen pins.
                rescued = (
                    rebase_rescue(cwd, oid, upstream_sha, ring)
                    if source == "open" and upstream_sha and ring.rebase_rescue
                    else ""
                )
                retried = rescued and (
                    git(
                        cwd, "merge", "--no-ff", "--no-commit", "--", rescued, check=False
                    ).returncode
                    == 0
                )
                if not retried:
                    if rescued and git(cwd, "ls-files", "-u").stdout.strip():
                        git(cwd, "merge", "--abort")
                    skipped.append(
                        {"pr": num, "branch": branch, "conflict_paths": paths, "source": source}
                    )
                    continue
                rebased_from, oid = oid, rescued
        # --no-commit leaves MERGE_HEAD behind on a real merge; an
        # already-merged PR returns 0 with nothing to commit.
        minted = False
        if git(cwd, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode == 0:
            # Stamp the merge with the PR head's committer date: identical
            # inputs reproduce the exact staging sha, so a same-day rerun with
            # nothing new is detected as a no-op instead of minting -rerunN.
            when = git(cwd, "show", "-s", "--format=%cI", oid).stdout.strip()
            subject = (
                f"{ring.merge_subject_prefix}: merge branch {branch} ({branch} @ {oid[:12]})"
                if source == "extra-branch"
                else f"{ring.merge_subject_prefix}: merge PR #{num} ({branch} @ {oid[:12]})"
            )
            git(
                cwd,
                *commit_ident(ring),
                "commit",
                "--no-verify",
                "-m",
                subject,
                env={"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when},
            )
            minted = True
        entry = {"pr": num, "branch": branch, "oid": oid, "source": source, "minted": minted}
        if rerere_paths:
            entry["rerere_paths"] = rerere_paths
        if rebased_from:
            # Refresh the PR with its rescued head so the next run composes it
            # on the normal path. The lease pins the pre-rescue head: a branch
            # that moved meanwhile keeps its newer work (this run still ships
            # the rescue, and the next run rescues from the newer head).
            push = git(
                cwd,
                "push",
                f"--force-with-lease=refs/heads/{branch}:{rebased_from}",
                fork,
                f"{oid}:refs/heads/{branch}",
                check=False,
            )
            entry["rebased_from"] = rebased_from
            entry["pushed_back"] = push.returncode == 0
        applied.append(entry)
    return applied, skipped


def composition_of(
    cwd: str | Path, sha: str, ring: Ring = STAGING
) -> tuple[str, dict[int | str, tuple[str, str]]] | None:
    """Decode a pushed ring-tip commit into (upstream base sha, {pr: (branch,
    head12)}) from its first-parent merge subjects, walking to the real base —
    the merge count is unbounded. None when the sha isn't readable."""
    if not sha or git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode != 0:
        return None
    pr_re = merge_re(ring)
    br_re = branch_merge_re(ring)
    merges: dict[int | str, tuple[str, str]] = {}
    out = git(cwd, "log", "--first-parent", "--format=%H%x09%s", sha).stdout
    for line in out.splitlines():
        commit, _, subject = line.partition("\t")
        m = pr_re.fullmatch(subject)
        if m:
            merges[int(m.group(1))] = (m.group(2), m.group(3))
            continue
        b = br_re.fullmatch(subject)
        if b:
            merges[f"branch:{b.group(1)}"] = (b.group(2), b.group(3))
            continue
        return commit, merges
    return None


def _blocked_label(n: object) -> str:
    if isinstance(n, int):
        return f"#{n}"
    s = str(n)
    return s if s.startswith("branch:") else f"branch:{s}"


def push_causes(
    old: tuple[str, dict[int | str, tuple[str, str]]] | None,
    upstream_sha: str,
    applied: list[dict],
) -> list[str]:
    """One-line answer to "why did staging move": which composition inputs
    differ from the one the remote branch already carried."""
    if old is None:
        return ["no previous composition to compare"]
    old_base, old_merges = old
    # Only minted merges are comparable: the decoded old composition knows
    # merge subjects, and an already-merged PR mints none.
    new = {
        (f"branch:{p['branch']}" if p.get("source") == "extra-branch" else p["pr"]): (
            p["branch"],
            str(p["oid"])[:12],
            p["source"],
        )
        for p in applied
        if p.get("minted", True)
    }
    label = {"open": "open PR set", "extra": "extras", "extra-branch": "extras"}
    causes: list[str] = []
    if old_base != upstream_sha:
        causes.append("upstream HEAD")
    dropped: list[str] = []
    # ints (PR numbers) sort before str keys (branch: pins)
    for num in sorted(old_merges.keys() | new.keys(), key=lambda k: (isinstance(k, str), k)):
        entry = new.get(num)
        if entry is None:
            # No longer composed at all (unpinned extra, closed PR, skip) —
            # say so rather than guessing which input it used to come from.
            dropped.append(_blocked_label(num))
        elif old_merges.get(num) != entry[:2] and label[entry[2]] not in causes:
            causes.append(label[entry[2]])
    if dropped:
        causes.append("dropped PR " + ", ".join(dropped))
    return causes or ["composition changed (cause unknown)"]


def pin_name(
    cwd: str | Path, fork: str, datestamp: str, sha: str, ring: Ring = STAGING
) -> tuple[str, bool]:
    """Allocate a dual-namespace pin, failing closed on incomplete pairs."""
    n = 0
    while True:
        cand = f"{ring.pin_prefix}{datestamp}" + (f"-rerun{n}" if n else "")
        branch_sha = remote_ref(cwd, fork, f"refs/heads/{cand}")
        tag_sha = remote_ref(cwd, fork, f"refs/tags/{cand}")
        if not branch_sha and not tag_sha:
            return cand, True
        if branch_sha and not tag_sha:
            raise StageError(
                f"pin branch {cand} points at {branch_sha}, but its tag is absent; "
                "refusing to reuse the dated name"
            )
        if tag_sha and not branch_sha:
            if tag_sha != sha:
                raise StageError(
                    f"pin tag {cand} points at {tag_sha}, expected {sha}, "
                    "and its compatibility branch is absent"
                )
            return cand, False
        if branch_sha != tag_sha:
            raise StageError(
                f"pin branch {cand} points at {branch_sha}, but tag points at {tag_sha}"
            )
        if tag_sha == sha:
            return cand, False
        n += 1


def assert_production_identity(
    cwd: str | Path, candidate_sha: str, upstream_sha: str, applied: list[dict]
) -> None:
    """Fail closed unless a production candidate is the expected merge chain."""
    if git(
        cwd,
        "merge-base",
        "--is-ancestor",
        upstream_sha,
        candidate_sha,
        check=False,
    ).returncode:
        raise StageError("production identity: upstream is not an ancestor of the candidate")

    if any(p.get("source") == "extra-branch" for p in applied):
        raise StageError("production identity: branch extras are not valid production inputs")
    expected = [
        p for p in applied if p.get("source") in {"open", "extra"} and p.get("minted") is True
    ]
    commits = git(
        cwd,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{upstream_sha}..{candidate_sha}",
    ).stdout.splitlines()
    if len(commits) != len(expected):
        raise StageError(
            "production identity: first-parent merges do not match minted open PR entries"
        )

    expected_ident = "omnigent-production <production@invalid>"
    for commit, entry in zip(commits, expected, strict=True):
        fields = (
            git(
                cwd,
                "show",
                "-s",
                "--format=%P%x00%s%x00%an <%ae>%x00%cn <%ce>",
                commit,
            )
            .stdout.rstrip("\n")
            .split("\0")
        )
        parents, subject, author, committer = fields
        parent_shas = parents.split()
        if len(parent_shas) != 2:
            raise StageError(f"production identity: {commit} is not a two-parent merge")
        expected_subject = (
            f"production: merge PR #{entry['pr']} ({entry['branch']} @ {str(entry['oid'])[:12]})"
        )
        if subject != expected_subject or merge_re(PRODUCTION).fullmatch(subject) is None:
            raise StageError(f"production identity: nonconforming merge subject {subject!r}")
        if parent_shas[1] != entry["oid"]:
            raise StageError(
                f"production identity: {commit} does not merge applied PR #{entry['pr']}"
            )
        if author != expected_ident or committer != expected_ident:
            raise StageError(
                f"production identity: {commit} has author {author!r} and committer {committer!r}"
            )


# Alembic migrations live here; a composition that touches (or drops) a file
# under this prefix must not auto-promote to production (locked decision 11).
MIGRATIONS_PATH_PREFIX = "omnigent/db/migrations/versions/"


def migration_touched(
    cwd: str | Path,
    candidate_sha: str,
    upstream_sha: str,
    prev_pin_sha: str | None,
    prefix: str = MIGRATIONS_PATH_PREFIX,
) -> bool:
    """True when the candidate composition carries a schema change: either
    ``upstream..candidate`` touches the migrations path (a composed PR adds,
    edits, or deletes one) or ``prev_pin..candidate`` does (a migration-bearing
    PR *removed* between compositions — invisible to the upstream leg). With no
    previous pin only the upstream diff is consulted."""
    for base in (upstream_sha, prev_pin_sha):
        if not base:
            continue
        out = git(cwd, "diff", "--name-only", f"{base}..{candidate_sha}").stdout
        if any(line.startswith(prefix) for line in out.splitlines()):
            return True
    return False


def latest_pin_sha(cwd: str | Path, fork: str, ring: Ring) -> str | None:
    """Commit of the newest ``<prefix>YYYYMMDD[-rerunN]`` pin tag on the fork
    — the previous composition the migration gate diffs against. None when
    the ring has never pinned: a bootstrap compose is gated on the upstream
    leg only. Pins are lightweight tags (pushed as commit-sha refspecs), so
    the ls-remote sha IS the commit; a peeled ``^{}`` line never fullmatches."""
    pin_re = re.compile(re.escape(ring.pin_prefix) + r"(\d{8})(?:-rerun(\d+))?")
    latest: tuple[tuple[str, int], str] | None = None
    for line in git(cwd, "ls-remote", "--tags", fork).stdout.splitlines():
        sha, _, ref = line.partition("\t")
        m = pin_re.fullmatch(ref.removeprefix("refs/tags/"))
        if not m:
            continue
        key = (m.group(1), int(m.group(2) or 0))
        if latest is None or key > latest[0]:
            latest = (key, sha)
    return latest[1] if latest else None


def stage(
    cwd: str | Path,
    prs: list[dict],
    date: dt.date,
    upstream: str = "upstream",
    fork: str = "origin",
    staging_only: bool = False,
    ring: Ring = STAGING,
    migration_approval: str = "",
    rr_cache_dir: Path | None = None,
) -> dict:
    datestamp = date.strftime("%Y%m%d")

    if ring == PRODUCTION and any(p.get("source") == "extra-branch" for p in prs):
        raise StageError("production compositions do not accept branch extras")

    # Seed before the detach: the seed directory is part of the trusted fork
    # checkout and disappears from the worktree once HEAD moves to upstream.
    seed_rerere(cwd, rr_cache_dir)
    git(cwd, "config", "maintenance.rerere-gc.auto", "0")
    git(cwd, "fetch", upstream, "main")
    upstream_sha = git(cwd, "rev-parse", "FETCH_HEAD").stdout.strip()
    git(cwd, "checkout", "--detach", upstream_sha)

    try:
        applied, skipped = merge_prs(cwd, prs, upstream, fork, ring, upstream_sha)
    except StageError as error:
        if not staging_only or not _is_git_lock_failure(str(error)):
            raise
        staging_sha = git(cwd, "rev-parse", "HEAD").stdout.strip()
        return {
            "date": datestamp,
            "upstream_sha": upstream_sha,
            "staging_sha": staging_sha,
            "remote_staging_sha": remote_ref(cwd, fork, f"refs/heads/{ring.branch}"),
            "staging_only": True,
            "blocked": [],
            "infrastructure_error": str(error),
            "pushed": False,
            "causes": [],
            "applied": [],
            "skipped": [],
        }
    staging_sha = git(cwd, "rev-parse", "HEAD").stdout.strip()

    # An extra we could not even reach is an infrastructure failure, not a
    # composition: publishing without it would silently regress staging.
    blocked = [
        p["branch"] if p.get("source") == "extra-branch" else p["pr"]
        for p in skipped
        if p.get("reason") == EXTRA_FETCH_FAILED
    ]

    if ring == PRODUCTION:
        assert_production_identity(cwd, staging_sha, upstream_sha, applied)

    if staging_only:
        # Hourly mode: only refs/heads/staging moves — no pins, no tags.
        # Composition is byte-reproducible, so an identical remote sha means
        # nothing changed and the push is skipped outright.
        expected_staging = remote_ref(cwd, fork, f"refs/heads/{ring.branch}")
        report = {
            "date": datestamp,
            "upstream_sha": upstream_sha,
            "staging_sha": staging_sha,
            # Pre-push remote tip: distinct from the local candidate when the
            # push is blocked or a no-op, so the summary never labels an
            # unpublished composition as "Staging".
            "remote_staging_sha": expected_staging,
            "staging_only": True,
            "blocked": blocked,
            "pushed": not blocked and expected_staging != staging_sha,
            "causes": [],
            "applied": applied,
            "skipped": skipped,
        }
        if not report["pushed"]:
            return report
        git(cwd, "fetch", fork, f"refs/heads/{ring.branch}", check=False)
        report["causes"] = push_causes(
            composition_of(cwd, expected_staging, ring), upstream_sha, applied
        )
        git(
            cwd,
            "push",
            f"--force-with-lease=refs/heads/{ring.branch}:{expected_staging}",
            fork,
            f"{staging_sha}:refs/heads/{ring.branch}",
        )
        return report

    # The nightly publishes releases from this composition, and its downstream
    # jobs consume the report's sha/tag — so refuse loudly before any ref moves
    # rather than shipping artifacts that quietly omit a required pin.
    if blocked:
        raise StageError(
            "extras unreachable due to infrastructure failure (not deleted): "
            + ", ".join(_blocked_label(n) for n in blocked)
            + "; refusing to publish a composition without them"
        )

    # Migration gate — production pins only (locked decision 11): a candidate
    # touching the migrations path must not auto-publish. Checked BEFORE any
    # ref moves — the atomic push below is the only publish, so returning here
    # leaves the fork untouched. Approval is the exact candidate sha, minted
    # by an operator re-dispatch after the CNPG backup checkpoint; a drifted
    # composition mints a different sha and blocks again.
    gate: dict | None = None
    if ring.pin_prefix == "production-":
        prev_pin_sha = latest_pin_sha(cwd, fork, ring)
        gate = {
            "blocked": False,
            "candidate": staging_sha,
            "prev_pin": prev_pin_sha,
        }
        if (
            migration_touched(cwd, staging_sha, upstream_sha, prev_pin_sha)
            and migration_approval != staging_sha
        ):
            gate["blocked"] = True
            gate["approval_hint"] = f"re-dispatch with approve_migration={staging_sha}"
            return {
                "date": datestamp,
                "upstream_sha": upstream_sha,
                "staging_sha": staging_sha,
                "pushed": False,
                "migration_gate": gate,
                "applied": applied,
                "skipped": skipped,
            }

    dev_tag = dev_version(cwd, upstream_sha, datestamp) if ring.mint_dev_tag else None
    name, created = pin_name(cwd, fork, datestamp, staging_sha, ring)
    pin_branch_ref = f"refs/heads/{name}"
    pin_tag_ref = f"refs/tags/{name}"

    # One atomic push for every ref: a partial failure can't leave the fork
    # with mixed staging/pin/dev-tag state. Leases pin the values we just
    # observed so a concurrent push loses loudly instead of being clobbered.
    refspecs: list[str] = []
    leases: list[str] = []
    expected_staging = remote_ref(cwd, fork, f"refs/heads/{ring.branch}")
    if expected_staging != staging_sha:
        refspecs.append(f"{staging_sha}:refs/heads/{ring.branch}")
        leases.append(f"--force-with-lease=refs/heads/{ring.branch}:{expected_staging}")
    # Dated compatibility branches are removed in v0.12.0.
    if created:
        refspecs += [f"{staging_sha}:{pin_branch_ref}", f"{staging_sha}:{pin_tag_ref}"]
        leases += [
            f"--force-with-lease={pin_branch_ref}:",
            f"--force-with-lease={pin_tag_ref}:",
        ]
    # The dev tag floats within the day: a rerun repoints it (fork-local tag,
    # nothing downstream pins to it mid-day). Rings without one push nothing.
    if dev_tag:
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
        "pin_ref": pin_tag_ref,
        "audit": [
            {
                "ref": ref,
                "expected": staging_sha,
                "observed": remote_ref(cwd, fork, ref),
            }
            for ref in (pin_tag_ref, pin_branch_ref)
        ],
        **({"dev_tag": dev_tag} if dev_tag else {}),
        "pin_created": created,
        **({"migration_gate": gate} if gate else {}),
        "applied": applied,
        "skipped": skipped,
    }


def _skip_reason(p: dict) -> str:
    if p.get("reason"):
        return str(p["reason"])
    paths = ", ".join(md_code(c) for c in p["conflict_paths"])
    return f"merge conflict: {paths or 'unknown paths'}"


def notes(report: dict, signed: bool, ring: Ring = STAGING) -> str:
    lines = [
        f"Nightly personal {ring.name} ring for {report['date']} (UTC).",
        "",
        f"- Upstream main: `{report['upstream_sha']}`",
        f"- {ring.name.capitalize()} commit: `{report['staging_sha']}`",
        *([f"- Dev tag: `{report['dev_tag']}`"] if report.get("dev_tag") else []),
        "",
        f"## Applied PRs ({len(report['applied'])})",
    ]
    lines += [
        f"- {_pin_label(p)} {md_code(p['branch'])} @ `{str(p['oid'])[:12]}`"
        + (
            f" — rebased onto upstream from `{str(p['rebased_from'])[:12]}`"
            + ("" if p.get("pushed_back") else " (branch push-back FAILED; will re-rescue)")
            if p.get("rebased_from")
            else (
                " — conflicts resolved from rr-cache: "
                + ", ".join(md_code(x) for x in p["rerere_paths"])
                if p.get("rerere_paths")
                else ""
            )
        )
        for p in report["applied"]
    ] or ["- none"]
    lines += ["", f"## Skipped PRs ({len(report['skipped'])})"]
    lines += [
        f"- {_pin_label(p)} {md_code(p['branch'])} — {_skip_reason(p)}" for p in report["skipped"]
    ] or ["- none"]
    # Every ring ships a re-signed debug APK on its release, so the signing
    # section renders for all of them.
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


def summarize(report: dict, ring: Ring = STAGING) -> str:
    tier = ring.name.capitalize()
    if report.get("staging_only"):
        # A no-op hour produces the same ~19-row skip table every time; collapse
        # it to a one-line count so the few lines that matter stay visible.
        # Runs that push (or that block on unreachable extras) keep full detail.
        infrastructure_error = report.get("infrastructure_error")
        noop = not report["pushed"] and not report["blocked"] and not infrastructure_error
        if infrastructure_error:
            result = (
                "**NOT pushed** — composition infrastructure failure: "
                + md_code(infrastructure_error)
                + " (staging left at its previous commit)"
            )
        elif report["blocked"]:
            result = (
                "**NOT pushed** — extras unreachable: "
                + ", ".join(_blocked_label(n) for n in report["blocked"])
                + " (staging left at its previous commit)"
            )
        elif report["pushed"]:
            result = f"pushed (changed: {', '.join(report['causes'])})"
        else:
            result = (
                f"no-op: staging unchanged; {len(report['applied'])} PRs applied, "
                f"{len(report['skipped'])} skipped"
            )
        if report["pushed"]:
            sha_rows = [f"| {tier} | `{report['staging_sha']}` |"]
        else:
            remote = report.get("remote_staging_sha") or "(none)"
            sha_rows = [
                f"| candidate (not pushed) | `{report['staging_sha']}` |",
                f"| Remote {ring.name} | `{remote}` |",
            ]
        rows = [
            f"## Personal {ring.name} hourly",
            "",
            "| | |",
            "| --- | --- |",
            f"| Upstream main | `{report['upstream_sha']}` |",
            *sha_rows,
            f"| Result | {result} |",
        ]
        if noop or infrastructure_error:
            return "\n".join(rows) + "\n"
        rows += [
            f"| Applied / skipped | {len(report['applied'])} / {len(report['skipped'])} |",
        ]
        rows += [f"| Skipped {_pin_label(p)} | {_skip_reason(p)} |" for p in report["skipped"]]
        return "\n".join(rows) + "\n"

    # A migration-gated production run publishes nothing: no pin row, a loud
    # BLOCKED result carrying the exact approval instruction.
    gate = report.get("migration_gate") or {}
    if gate.get("blocked"):
        rows = [
            f"## Personal {ring.name} nightly",
            "",
            "| | |",
            "| --- | --- |",
            f"| Upstream main | `{report['upstream_sha']}` |",
            f"| candidate (not pushed) | `{report['staging_sha']}` |",
            f"| Previous pin | `{gate.get('prev_pin') or '(none)'}` |",
            (
                "| Result | **BLOCKED — migration gate**: the composition touches "
                f"`{MIGRATIONS_PATH_PREFIX}`; no refs were pushed. "
                f"{gate['approval_hint']} |"
            ),
            f"| Applied / skipped | {len(report['applied'])} / {len(report['skipped'])} |",
        ]
        rows += [f"| Skipped {_pin_label(p)} | {_skip_reason(p)} |" for p in report["skipped"]]
        return "\n".join(rows) + "\n"

    pin_note = "" if report["pin_created"] else " (already pinned — rerun no-op)"
    rows = [
        f"## Personal {ring.name} nightly",
        "",
        "| | |",
        "| --- | --- |",
        f"| Upstream main | `{report['upstream_sha']}` |",
        f"| {tier} | `{report['staging_sha']}` |",
        f"| Pin | `{report['tag']}`{pin_note} |",
        *([f"| Dev tag | `{report['dev_tag']}` |"] if report.get("dev_tag") else []),
        f"| Applied / skipped | {len(report['applied'])} / {len(report['skipped'])} |",
    ]
    rows += [f"| Skipped {_pin_label(p)} | {_skip_reason(p)} |" for p in report["skipped"]]
    return "\n".join(rows) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_stage = sub.add_parser("stage", help="build and push a promotion ring")
    p_stage.add_argument("--workdir", default=".")
    p_stage.add_argument(
        "--ring",
        choices=list(RINGS),
        default=STAGING.name,
        help="which ring to compose (default: staging)",
    )
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
        default=None,
        help="extras manifest path (default: the ring's own manifest; missing file == no extras)",
    )
    p_stage.add_argument(
        "--exclude",
        default=str(EXCLUDE_FILE),
        help="exclusion manifest path (missing file == no exclusions)",
    )
    p_stage.add_argument(
        "--rr-cache",
        default=str(RR_CACHE_DIR),
        help="committed rr-cache seed directory (missing dir == no resolutions)",
    )
    p_stage.add_argument(
        "--migration-approval",
        default="",
        metavar="SHA",
        help="full candidate sha approving a migration-touching production "
        "composition (only meaningful with --ring production)",
    )

    p_notes = sub.add_parser("notes", help="render release notes from a merge report")
    p_notes.add_argument("--report", default="merge-report.json")
    p_notes.add_argument("--signed", choices=["true", "false"], required=True)
    p_notes.add_argument(
        "--ring",
        choices=list(RINGS),
        default=STAGING.name,
        help="ring whose notes variant to render (default: staging)",
    )

    p_stale = sub.add_parser(
        "is-stale-ref-rejection",
        help="exit 0 iff stdin is git-push --porcelain for a main stale-ref race",
    )
    p_stale.add_argument(
        "--ref",
        default="refs/heads/main",
        help="destination ref to classify (default: refs/heads/main)",
    )

    args = parser.parse_args(argv)

    if args.cmd == "is-stale-ref-rejection":
        return 0 if is_stale_ref_push_rejection(sys.stdin.read(), ref=args.ref) else 1

    if args.cmd == "notes":
        report = json.loads(Path(args.report).read_text())
        sys.stdout.write(notes(report, signed=args.signed == "true", ring=RINGS[args.ring]))
        return 0

    if args.date:
        date = dt.date(int(args.date[:4]), int(args.date[4:6]), int(args.date[6:8]))
    else:
        date = dt.datetime.now(dt.timezone.utc).date()
    ring = RINGS[args.ring]
    # The hourly mode force-pushes the ring branch with none of the nightly's
    # gates (production's migration gate above all) — it exists for staging
    # only. Rejected here, before any fetch or push can move a ref.
    if args.staging_only and ring is not STAGING:
        parser.error("--staging-only is only valid for --ring staging")
    title = f"Personal {ring.name} " + ("hourly" if args.staging_only else "nightly")
    try:
        # Fail the stage path before any fetch/merge if the parser is absent.
        if tomllib is None:
            raise StageError("the stage command requires python >= 3.11 (tomllib)")
        prs = (
            check_not_truncated(json.loads(Path(args.prs_json).read_text()))
            if args.prs_json
            else list_prs()
        )
        if ring.exclude_drafts:
            prs = filter_drafts(prs)
        exclusions = parse_exclusions(Path(args.exclude))
        # Extras are read here, before any merge touches the worktree, so the
        # manifest always comes from the trusted checkout. A ring without
        # extras never opens the manifest at all.
        extras_path = Path(args.extras) if args.extras else ring.extras_file
        pr_extras, branch_extras = parse_extras(extras_path, ring) if ring.use_extras else ([], [])
        prs = merge_stream(prs, pr_extras)
        for name in dict.fromkeys(branch_extras):
            prs.append({"source": "extra-branch", "headRefName": name, "number": None})
        prs, excluded = apply_exclusions(prs, exclusions)
        report = stage(
            args.workdir,
            prs,
            date,
            upstream=args.upstream_remote,
            fork=args.fork_remote,
            staging_only=args.staging_only,
            ring=ring,
            migration_approval=args.migration_approval,
            rr_cache_dir=Path(args.rr_cache),
        )
        report["excluded"] = excluded
    except Exception as e:
        # The step summary is the failure surface — never exit without one.
        append_summary(f"## {title}\n\n**FAILED:** {e}\n")
        raise
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    append_summary(summarize(report, ring))
    if report.get("infrastructure_error"):
        error = str(report["infrastructure_error"]).replace("\n", " ")
        print(f"::warning::{error} — staging left unchanged")
    elif report.get("blocked"):
        print(
            "::warning::could not reach remote for extras "
            + ", ".join(_blocked_label(n) for n in report["blocked"])
            + " — staging left unchanged; this is an infrastructure failure, not a deleted ref"
        )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
