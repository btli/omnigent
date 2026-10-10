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


def plan(changes: list[dict], committed_at: dict[str, int], now: int, settle_s: int) -> dict:
    """Rebuild once the newest changed head is ``settle_s`` old."""
    if not changes:
        return {"action": "noop", "reason": "open btli PRs match the newest nightly", "changed": [], "settle_at": None}
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
    return {"action": "rebuild", "reason": f"{summary}; settled", "changed": changes, "settle_at": settle_at}


def gh_json(*args: str):
    out = subprocess.run(["gh", *args], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def commit_time(oid: str) -> int:
    """Committer time of a PR head: a push, rebase or amend all set it anew."""
    doc = gh_json("api", f"repos/{stage.UPSTREAM_REPO}/commits/{oid}")
    stamp = doc["commit"]["committer"]["date"].replace("Z", "+00:00")
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
            ["git", "ls-remote", "--tags", f"https://github.com/{args.repo}.git", "refs/tags/nightly-*"],
            check=True, capture_output=True, text=True,
        ).stdout.splitlines()
        if "^{}" not in line
    ]
    nightly = newest_nightly(tags)
    if nightly is None:
        print(json.dumps({"action": "noop", "reason": "no nightly yet", "changed": [], "settle_at": None}))
        return 0
    report_raw = subprocess.run(
        ["gh", "release", "download", nightly, "-R", args.repo, "-p", "merge-report.json", "-O", "-"],
        capture_output=True, text=True,
    )
    if report_raw.returncode:
        # The newest nightly is still publishing; its own report is the baseline.
        print(json.dumps({"action": "wait", "reason": f"{nightly} has no merge report yet",
                          "changed": [], "settle_at": None}))
        return 0
    composed = composed_heads(json.loads(report_raw.stdout))
    # Same selection as the composer: open btli PRs minus exclude.txt.
    prs, _ = stage.apply_exclusions(stage.list_prs(), stage.parse_exclusions(Path(args.exclude)))
    current = {int(p["number"]): p["headRefOid"] for p in prs}
    changes = diff(current, composed)
    committed = {c["oid"]: commit_time(c["oid"]) for c in changes if c["oid"]}
    result = plan(changes, committed, int(time.time()), args.settle_minutes * 60)
    result["baseline"] = nightly
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
