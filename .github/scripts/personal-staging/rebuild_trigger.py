"""Decide whether updated btli PRs warrant a staging nightly rerun.

The staging nightly (personal-staging.yml) builds once a day. Between nightlies
the open btli PRs keep moving; this compares them with what the newest nightly
actually composed (its release's merge-report.json ``applied`` list) and asks
for a rerun only once the change has SETTLED: every changed PR head commit must
be at least ``--settle-minutes`` old. A burst of pushes keeps resetting the
clock, so a dozen force-pushes in an afternoon yield one rebuild, not twelve.

No state store: the newest nightly's report is the baseline, and a rerun mints
``nightly-YYYYMMDD-rerunN`` whose report becomes the next baseline. A rerun
already queued or running is respected by the caller (workflow concurrency +
an in-flight check), never doubled.

Prints one JSON object:
  {"action": "rebuild" | "wait" | "noop", "reason": ..., "changed": [...],
   "settle_at": <unix seconds or null>}
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import stage

NIGHTLY_RE = re.compile(r"nightly-(\d{8})(?:-rerun(\d+))?")


def newest_nightly(tags: list[str]) -> str | None:
    keyed = [(m.group(1), int(m.group(2) or 0), t) for t in tags if (m := NIGHTLY_RE.fullmatch(t))]
    return max(keyed)[2] if keyed else None


def composed_heads(report: dict) -> dict[int, str]:
    """PR number -> head oid, for PRs the nightly composed from the open stream."""
    return {
        int(item["pr"]): item["oid"]
        for item in report.get("applied", [])
        if item.get("source") == "open" and item.get("pr") and item.get("oid")
    }


def diff(current: dict[int, str], composed: dict[int, str]) -> list[dict]:
    """Every PR whose composed head differs: updated, newly opened, or gone."""
    changes = []
    for number in sorted(set(current) | set(composed)):
        now, then = current.get(number), composed.get(number)
        if now == then:
            continue
        kind = "opened" if then is None else "closed" if now is None else "updated"
        changes.append({"pr": number, "kind": kind, "oid": now})
    return changes


def plan(
    changes: list[dict],
    committed_at: dict[str, int],
    now: int,
    settle_s: int,
    attempted_at: int = 0,
) -> dict:
    """Rebuild once the newest changed head is ``settle_s`` old.

    ``attempted_at`` is when a newer nightly than the baseline was last
    composed, one whose composition gate failed (no release). Heads committed
    before it were already tried; rebuilding again would only repeat the same
    merge conflict, so only a later push re-arms the trigger.
    """
    if (
        attempted_at
        and changes
        and all(c["oid"] and committed_at[c["oid"]] <= attempted_at for c in changes)
    ):
        summary = ", ".join(f"#{c['pr']} {c['kind']}" for c in changes)
        return {
            "action": "noop",
            "reason": f"{summary}; already attempted by the newest nightly (composition failed)",
            "changed": changes,
            "settle_at": None,
        }
    if not changes:
        return {
            "action": "noop",
            "reason": "open btli PRs match the newest nightly",
            "changed": [],
            "settle_at": None,
        }
    # A closed PR has no head to age; its removal is settled the moment it closes,
    # so it alone rebuilds immediately on the next poll.
    times = [committed_at[c["oid"]] for c in changes if c["oid"]]
    newest = max(times) if times else now - settle_s
    settle_at = newest + settle_s
    summary = ", ".join(f"#{c['pr']} {c['kind']}" for c in changes)
    if now < settle_at:
        wait = (settle_at - now + 59) // 60
        return {
            "action": "wait",
            "reason": f"{summary}; settling ({wait} min left of {settle_s // 60})",
            "changed": changes,
            "settle_at": settle_at,
        }
    return {
        "action": "rebuild",
        "reason": f"{summary}; settled",
        "changed": changes,
        "settle_at": settle_at,
    }


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def commit_time(oid: str) -> int:
    """Committer time of a PR head: a push, rebase or amend all set it anew."""
    doc = gh_json("api", f"repos/{stage.UPSTREAM_REPO}/commits/{oid}")
    stamp = doc["commit"]["committer"]["date"].replace("Z", "+00:00")
    return int(datetime.fromisoformat(stamp).timestamp())


def release_report(repo: str, tag: str) -> dict | None:
    """The nightly's merge-report.json, or None when it has no release/report."""
    out = subprocess.run(
        ["gh", "release", "download", tag, "-R", repo, "-p", "merge-report.json", "-O", "-"],
        capture_output=True,
        text=True,
    )
    if out.returncode:
        return None
    return json.loads(out.stdout)


def last_attempt_time(repo: str) -> int:
    """When the newest staging nightly run started (unix seconds), or 0.

    A tag's commit time is no use here: the composer stamps merges with each PR
    head's own commit time for reproducibility. The run start is when the
    composer read the PR heads, so anything committed before it was attempted.
    """
    runs = gh_json(
        "run",
        "list",
        "-R",
        repo,
        "--workflow",
        "personal-staging.yml",
        "--limit",
        "1",
        "--json",
        "createdAt",
    )
    if not runs:
        return 0
    stamp = runs[0]["createdAt"].replace("Z", "+00:00")
    return int(datetime.fromisoformat(stamp).timestamp())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", default="btli/omnigent")
    parser.add_argument("--settle-minutes", type=int, default=30)
    parser.add_argument("--exclude", default=str(stage.EXCLUDE_FILE))
    args = parser.parse_args(argv)

    tags = [
        line.split("refs/tags/", 1)[1]
        for line in subprocess.run(
            [
                "git",
                "ls-remote",
                "--tags",
                f"https://github.com/{args.repo}.git",
                "refs/tags/nightly-*",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        if "^{}" not in line
    ]
    newest = newest_nightly(tags)
    if newest is None:
        print(
            json.dumps(
                {"action": "noop", "reason": "no nightly yet", "changed": [], "settle_at": None}
            )
        )
        return 0
    # A nightly whose composition gate failed keeps its tag but never gets a
    # release, so its report is missing for good: the baseline is the newest
    # nightly that published one. A freshly minted nightly still publishing is
    # the same shape, and is covered by the caller's in-flight check.
    ordered = sorted(
        (t for t in tags if NIGHTLY_RE.fullmatch(t)),
        key=lambda t: (
            NIGHTLY_RE.fullmatch(t).group(1),
            int(NIGHTLY_RE.fullmatch(t).group(2) or 0),
        ),
        reverse=True,
    )
    nightly, report = None, None
    for candidate in ordered[:10]:
        report = release_report(args.repo, candidate)
        if report is not None:
            nightly = candidate
            break
    if nightly is None:
        print(
            json.dumps(
                {
                    "action": "wait",
                    "reason": "no recent nightly has a merge report",
                    "changed": [],
                    "settle_at": None,
                }
            )
        )
        return 0
    attempted_at = last_attempt_time(args.repo) if newest != nightly else 0
    composed = composed_heads(report)
    # Same selection as the composer: open btli PRs minus exclude.txt.
    prs, _ = stage.apply_exclusions(stage.list_prs(), stage.parse_exclusions(Path(args.exclude)))
    current = {int(p["number"]): p["headRefOid"] for p in prs}
    changes = diff(current, composed)
    committed = {c["oid"]: commit_time(c["oid"]) for c in changes if c["oid"]}
    result = plan(changes, committed, int(time.time()), args.settle_minutes * 60, attempted_at)
    result["baseline"] = nightly
    if newest != nightly:
        result["newest_tag"] = newest
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
