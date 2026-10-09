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
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stage

NIGHTLY_RE = re.compile(r"nightly-(\d{8})(?:-rerun(\d+))?")
PRODUCTION_RE = re.compile(r"production-(\d{8})(?:-rerun(\d+))?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SUMS_LINE_RE = re.compile(r"([0-9a-f]{64})\s+\*?(?:\./)?([A-Za-z0-9._-]+)")
BUILD_COMPLETE = "build-complete.json"
SOAK_CONTEXT = "soak/homelab"
SOURCE = "source.json"
MERGE_REPORT = "merge-report.json"


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


def build_complete(
    tag: str, sha: str, sums_text: str, server_digest: str, host_digest: str
) -> dict:
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


class GitHub:
    """Read-only GitHub access through the gh CLI (injectable for tests)."""

    def __init__(self, repo: str):
        self.repo = repo

    def _gh(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["gh", *args], text=True, capture_output=True)

    def statuses(self, sha: str) -> list[dict]:
        """Commit statuses, newest first (the API's order); one page is enough."""
        r = self._gh("api", f"repos/{self.repo}/commits/{sha}/statuses?per_page=100")
        if r.returncode:
            raise PromoteError(f"cannot read statuses for {sha}: {r.stderr.strip()}")
        try:
            doc = json.loads(r.stdout or "[]")
        except json.JSONDecodeError as error:
            raise PromoteError(f"statuses for {sha} are not JSON: {error}") from error
        if not isinstance(doc, list):
            raise PromoteError(f"unexpected statuses payload for {sha}")
        return doc

    def release_assets(self, tag: str) -> list[str] | None:
        """Asset names of the release for *tag*; None when there is no release."""
        r = self._gh("release", "view", tag, "-R", self.repo, "--json", "assets")
        if r.returncode:
            if "release not found" in r.stderr.lower():
                return None
            raise PromoteError(f"cannot read release {tag}: {r.stderr.strip()}")
        return sorted(a["name"] for a in json.loads(r.stdout)["assets"])

    def release_json(self, tag: str, name: str) -> dict | None:
        """A JSON-object asset; None when it is not an object. Raises when unreadable."""
        r = self._gh("release", "download", tag, "-R", self.repo, "-p", name, "-O", "-")
        if r.returncode:
            raise PromoteError(f"cannot download {tag}/{name}: {r.stderr.strip()}")
        try:
            doc = json.loads(r.stdout)
        except json.JSONDecodeError as error:
            raise PromoteError(f"{tag}/{name} is not JSON: {error}") from error
        return doc if isinstance(doc, dict) else None


def _tags(cwd, fork) -> dict[str, str]:
    """name -> commit sha for every tag on *fork* (annotated tags are peeled)."""
    plain: dict[str, str] = {}
    peeled: dict[str, str] = {}
    for line in stage.git(cwd, "ls-remote", "--tags", fork).stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if not ref.startswith("refs/tags/"):
            continue
        name = ref.removeprefix("refs/tags/")
        if name.endswith("^{}"):
            peeled[name.removesuffix("^{}")] = sha
        else:
            plain[name] = sha
    return {**plain, **peeled}


def _latest(tags: dict[str, str], pattern: re.Pattern[str]) -> tuple[str, str] | tuple[None, None]:
    """The newest ``(tag, sha)`` of *pattern*'s family, or ``(None, None)``."""
    keyed = [(key, tag) for tag in tags if (key := tag_key(tag, pattern))]
    if not keyed:
        return None, None
    tag = max(keyed)[1]
    return tag, tags[tag]


def _have_commit(cwd, sha: str) -> bool:
    return stage.git(cwd, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _ensure_commits(cwd, fork, wanted: dict[str, str]) -> None:
    """Fetch the history behind each missing ``tag -> sha`` (a clone may lack it)."""
    for tag, sha in wanted.items():
        if not _have_commit(cwd, sha):
            stage.git(cwd, "fetch", "-q", fork, f"+refs/tags/{tag}:refs/promote/{tag}")


def _result(action: str, reason: str, **fields) -> dict:
    return {"action": action, "reason": reason, **fields}


def _check_marker(marker: dict, nightly: str, sha: str, assets: list[str]) -> str | None:
    """Why the completion marker is unusable, or None when it is sound."""
    if marker.get("schema") != 1 or marker.get("tag") != nightly or marker.get("sha") != sha:
        return f"{BUILD_COMPLETE} on {nightly} does not describe {sha}"
    listed, images = marker.get("assets"), marker.get("images")
    if not isinstance(listed, dict) or not listed:
        return f"{BUILD_COMPLETE} on {nightly} lists no assets"
    if not isinstance(images, dict) or not all(
        isinstance(images.get(k), str) and DIGEST_RE.fullmatch(images[k])
        for k in ("server", "host")
    ):
        return f"{BUILD_COMPLETE} on {nightly} has no valid image digests"
    missing = sorted(set(listed) - set(assets))
    if missing:
        return f"{nightly} release is missing {', '.join(missing)}"
    return None


def plan(
    cwd,
    fork,
    gh,
    *,
    trigger: str,
    sha: str | None,
    nightly: str | None,
    soak_login: str,
    approve_migration: str,
    allow_older: bool,
    date: str,
) -> dict:
    """Decide whether a nightly may be promoted; the workflow only carries out the result."""
    if trigger not in ("status", "dispatch"):
        raise PromoteError(f"unknown trigger: {trigger!r}")
    if not re.fullmatch(r"\d{8}", date or ""):
        raise PromoteError(f"date must be YYYYMMDD: {date!r}")
    tags = _tags(cwd, fork)
    verdict = None
    if trigger == "status":
        if not soak_login:
            return _result(
                "refused", "status-triggered promotion is disabled (SOAK_STATUS_LOGIN unset)"
            )
        if not sha or not SHA_RE.fullmatch(sha):
            raise PromoteError(f"status trigger needs a full commit sha: {sha!r}")
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

    assets = gh.release_assets(nightly) or []
    marker = gh.release_json(nightly, BUILD_COMPLETE) if BUILD_COMPLETE in assets else None
    if not marker:
        return _result("refused", f"{nightly} has no {BUILD_COMPLETE}; the nightly is incomplete")
    if bad := _check_marker(marker, nightly, sha, assets):
        return _result("refused", bad)

    prev_tag, prev_sha = _latest(tags, PRODUCTION_RE)
    fields = {"nightly": nightly, "sha": sha, "prev_tag": prev_tag, "prev_pin": prev_sha}
    if prev_sha == sha:
        return _result("noop", f"production already points at {sha}", **fields, gate=None)
    if prev_tag:
        cand = tag_key(nightly, NIGHTLY_RE)
        prev_assets = gh.release_assets(prev_tag) or []
        if SOURCE in prev_assets:
            source = gh.release_json(prev_tag, SOURCE) or {}
            floor = tag_key(str(source.get("source_tag")), NIGHTLY_RE)
            if floor is None:
                return _result(
                    "refused", f"{prev_tag}/{SOURCE} has no valid source_tag", **fields, gate=None
                )
            older = cand <= floor
        else:  # legacy composed pin: only its date is known
            older = cand[0] < tag_key(prev_tag, PRODUCTION_RE)[0]
        if older and not allow_older:
            return _result(
                "refused", f"{nightly} is not newer than production's source", **fields, gate=None
            )

    try:
        _ensure_commits(cwd, fork, {nightly: sha, **({prev_tag: prev_sha} if prev_tag else {})})
        stage.assert_migration_history(cwd, sha, prev_sha)
        stage.assert_migration_graph(cwd, sha)
        report = gh.release_json(nightly, MERGE_REPORT) if MERGE_REPORT in assets else None
        upstream_sha = (report or {}).get("upstream_sha")
        if not isinstance(upstream_sha, str) or not SHA_RE.fullmatch(upstream_sha):
            return _result(
                "refused",
                f"{nightly} {MERGE_REPORT} has no valid upstream_sha",
                **fields,
                gate=None,
            )
        if not _have_commit(cwd, upstream_sha):
            stage.git(cwd, "fetch", "-q", fork, upstream_sha)
        touched = stage.migration_touched(cwd, sha, upstream_sha, prev_sha)
    except stage.StageError as error:
        return _result("refused", str(error), **fields, gate=None)
    gate = {
        "candidate": sha,
        "prev_pin": prev_sha,
        "blocked": bool(touched and approve_migration != sha),
    }
    if gate["blocked"]:
        gate["approval_hint"] = (
            f"re-dispatch personal-promote.yml with nightly={nightly} approve_migration={sha}"
        )
        return _result(
            "blocked", "candidate touches migrations and is not approved", **fields, gate=gate
        )

    production_tag, created = stage.pin_name(cwd, fork, date, sha, stage.PRODUCTION)
    return _result(
        "promote",
        f"promote {nightly} to {production_tag}",
        **fields,
        gate=gate,
        production_tag=production_tag,
        tag_created=created,
        assets=marker["assets"],
        images=marker["images"],
        source={
            "source_tag": nightly,
            "sha": sha,
            "trigger": trigger,
            "soak_target_url": (verdict or {}).get("target_url"),
            "approve_migration": approve_migration or None,
            "allow_older": allow_older,
        },
    )


def _run_plan(args: argparse.Namespace) -> int:
    out = {"action": "error", "reason": "plan did not run"}
    code = 1
    try:
        out = plan(
            args.workdir,
            args.fork_remote,
            GitHub(args.repo),
            trigger=args.trigger,
            sha=args.sha,
            nightly=args.nightly,
            soak_login=args.soak_login,
            approve_migration=args.approve_migration,
            allow_older=args.allow_older,
            date=args.date,
        )
        code = 0
    except PromoteError as error:
        out = {"action": "error", "reason": str(error)}
    except Exception as error:  # noqa: BLE001 - always leave a plan for the workflow
        out = {"action": "error", "reason": f"unexpected {type(error).__name__}: {error}"}
    text = json.dumps(out, indent=2, sort_keys=True)
    if args.out:
        try:
            args.out.write_text(text + "\n")
        except OSError as error:
            print(f"cannot write {args.out}: {error}", file=sys.stderr)
            code = 1
    print(text)
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_bc = sub.add_parser("build-complete", help="render a nightly's build-complete.json")
    p_bc.add_argument("--tag", required=True)
    p_bc.add_argument("--sha", required=True)
    p_bc.add_argument("--sums", required=True, type=Path)
    p_bc.add_argument("--server-digest", required=True)
    p_bc.add_argument("--host-digest", required=True)
    p_plan = sub.add_parser("plan", help="decide whether a nightly may be promoted")
    p_plan.add_argument("--workdir", type=Path, default=Path("."))
    p_plan.add_argument("--fork-remote", default="origin")
    p_plan.add_argument("--repo", required=True, help="owner/name for the gh API")
    p_plan.add_argument("--trigger", required=True, choices=["status", "dispatch"])
    p_plan.add_argument("--sha")
    p_plan.add_argument("--nightly")
    p_plan.add_argument("--soak-login", default="")
    p_plan.add_argument("--approve-migration", default="")
    p_plan.add_argument("--allow-older", action="store_true")
    p_plan.add_argument(
        "--date", default=datetime.now(timezone.utc).strftime("%Y%m%d"), help="promote date (UTC)"
    )
    p_plan.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if args.cmd == "build-complete":
        doc = build_complete(
            args.tag, args.sha, args.sums.read_text(), args.server_digest, args.host_digest
        )
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    if args.cmd == "plan":
        return _run_plan(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
