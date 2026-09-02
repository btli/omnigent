#!/usr/bin/env python3
"""Retire aged personal-ring pins on the fork.

The staging and production nightlies each mint an immutable dated pin — a
``<ring>-YYYYMMDD[-rerunN]`` tag, a same-name branch, and a dated
prerelease — and staging also mints a PEP 440 ``vX.Y.Z.devYYYYMMDD`` tag.
Nothing resolves a pin once the homelab has built it (fork CI re-fetches
the canonical tag set, and dev versions are derived from pyproject, not
from tags), so they accumulate forever. This prunes every pin whose
datestamp is older than the retention window, keeping a floor of the
newest few per family so a pipeline outage can never strip the repo bare.

Deletion order within a pin is branch -> release -> tag, because
``stage.py``'s ``pin_name()`` fails closed on a half-pin: a pin branch
whose tag is absent is a hard error, while a tag whose branch is absent
is tolerated. A run that dies mid-pin therefore leaves the tolerated
state, and a stray half-pin branch is itself a prune candidate.

Only the two dated pin families and the dev tags are matched at all, so
floating refs (``production-latest``, ``nightly-latest``), ``pr-demos-*``,
release tags (``v0.1.0``) and ``archive/*`` can never be candidates.

Age comes from the datestamp in the name, not the ref's creation time:
that is the same string the composer allocates pins from, so a same-day
rerun and its parent always age out together.

Stdlib only; the gh CLI is the sole API surface (injected in tests).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Protocol

# Retention defaults. The floor is per family, so a stalled ring keeps its
# last few pins no matter how old they get.
KEEP_DAYS = 14
KEEP_PER_FAMILY = 5
# Circuit breaker: a plan larger than this aborts before any ref moves.
# Sized well above one nightly's worth of drift so only a genuine anomaly
# (a bad clock, a regex that suddenly matches everything) trips it.
MAX_DELETE = 60

RING_PIN_RE = re.compile(
    r"^(?P<family>production|nightly)-(?P<date>\d{8})(?:-rerun(?P<rerun>\d+))?$"
)
DEV_TAG_RE = re.compile(r"^v(?P<version>\d+\.\d+\.\d+)\.dev(?P<date>\d{8})$")
DEV_FAMILY = "dev"
# Families that mint a compatibility branch alongside the tag.
BRANCHED_FAMILIES = ("production", "nightly")


class PruneError(RuntimeError):
    """Refuse to prune; the caller reports it and exits non-zero."""


@dataclass(frozen=True)
class Pin:
    """One dated pin, with whichever of its refs actually exist."""

    family: str
    name: str
    date: dt.date
    # Newest-last sort key within the family: reruns after their parent,
    # and same-day dev tags ordered by version.
    order: tuple[object, ...]
    has_tag: bool = False
    has_branch: bool = False
    release_id: int | None = None

    @property
    def is_half(self) -> bool:
        return self.has_branch and not self.has_tag


@dataclass
class Plan:
    delete: list[Pin] = field(default_factory=list)
    keep_recent: list[Pin] = field(default_factory=list)
    keep_floor: list[Pin] = field(default_factory=list)
    cutoff: dt.date = dt.date.min

    @property
    def ref_count(self) -> int:
        return sum(p.has_branch + p.has_tag for p in self.delete)

    @property
    def release_count(self) -> int:
        return sum(p.release_id is not None for p in self.delete)


def _date(stamp: str) -> dt.date | None:
    try:
        return dt.date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8]))
    except ValueError:
        return None


def parse_pin(name: str) -> tuple[str, dt.date, tuple[object, ...]] | None:
    """Classify a ref name into (family, date, sort key), or None if it is
    not a dated pin this script is allowed to touch."""
    m = RING_PIN_RE.match(name)
    if m:
        date = _date(m.group("date"))
        if date is None:
            return None
        return m.group("family"), date, (date, int(m.group("rerun") or 0))
    m = DEV_TAG_RE.match(name)
    if m:
        date = _date(m.group("date"))
        if date is None:
            return None
        version = tuple(int(p) for p in m.group("version").split("."))
        return DEV_FAMILY, date, (date, version)
    return None


def collect(tags: list[str], branches: list[str], releases: list[dict]) -> list[Pin]:
    """Fold the fork's tags, branches and releases into one pin per name."""
    release_ids = {r["tag_name"]: r["id"] for r in releases}
    found: dict[str, Pin] = {}

    def note(name: str, *, tag: bool) -> None:
        parsed = parse_pin(name)
        if parsed is None:
            return
        family, date, order = parsed
        # A dev tag has no branch namespace; a branch by that name is
        # someone's own work, not a pin.
        if not tag and family not in BRANCHED_FAMILIES:
            return
        prev = found.get(name)
        found[name] = Pin(
            family=family,
            name=name,
            date=date,
            order=order,
            has_tag=tag or bool(prev and prev.has_tag),
            has_branch=(not tag) or bool(prev and prev.has_branch),
            release_id=release_ids.get(name) if family in BRANCHED_FAMILIES else None,
        )

    for name in tags:
        note(name, tag=True)
    for name in branches:
        note(name, tag=False)
    return sorted(found.values(), key=lambda p: (p.family, p.order))


def plan(
    pins: list[Pin],
    today: dt.date,
    keep_days: int = KEEP_DAYS,
    keep_per_family: int = KEEP_PER_FAMILY,
) -> Plan:
    """Split pins into delete / kept-recent / kept-by-floor buckets."""
    if keep_days < 1 or keep_per_family < 1:
        raise PruneError("keep_days and keep_per_family must both be >= 1")
    cutoff = today - dt.timedelta(days=keep_days)
    out = Plan(cutoff=cutoff)
    families: dict[str, list[Pin]] = {}
    for pin in pins:
        families.setdefault(pin.family, []).append(pin)
    for members in families.values():
        newest_first = sorted(members, key=lambda p: p.order, reverse=True)
        floor = {p.name for p in newest_first[:keep_per_family]}
        for pin in newest_first:
            if pin.date >= cutoff:
                out.keep_recent.append(pin)
            elif pin.name in floor:
                out.keep_floor.append(pin)
            else:
                out.delete.append(pin)
    out.delete.sort(key=lambda p: (p.family, p.order))
    return out


class PinDeleter(Protocol):
    """The three deletions execute() performs; faked in the tests."""

    def delete_branch(self, name: str) -> None: ...

    def delete_tag(self, name: str) -> None: ...

    def delete_release(self, release_id: int) -> None: ...


class GhClient:
    """The gh CLI, one method per call this script makes."""

    def __init__(self, repo: str, dry_run: bool = False) -> None:
        self.repo = repo
        self.dry_run = dry_run

    def _lines(self, path: str, jq: str) -> list[str]:
        proc = subprocess.run(
            ["gh", "api", path, "--paginate", "--jq", jq],
            check=True,
            text=True,
            capture_output=True,
        )
        return [line for line in proc.stdout.splitlines() if line.strip()]

    def tags(self) -> list[str]:
        refs = self._lines(f"repos/{self.repo}/git/refs/tags", ".[] | .ref")
        return [r.removeprefix("refs/tags/") for r in refs]

    def branches(self) -> list[str]:
        refs = self._lines(f"repos/{self.repo}/git/refs/heads", ".[] | .ref")
        return [r.removeprefix("refs/heads/") for r in refs]

    def releases(self) -> list[dict]:
        out = self._lines(f"repos/{self.repo}/releases", '.[] | "\\(.id)\\t\\(.tag_name)"')
        rows = []
        for line in out:
            rid, _, tag = line.partition("\t")
            rows.append({"id": int(rid), "tag_name": tag})
        return rows

    def _delete(self, path: str) -> None:
        if self.dry_run:
            return
        subprocess.run(
            ["gh", "api", "-X", "DELETE", path],
            check=True,
            text=True,
            capture_output=True,
        )

    def delete_branch(self, name: str) -> None:
        self._delete(f"repos/{self.repo}/git/refs/heads/{name}")

    def delete_tag(self, name: str) -> None:
        self._delete(f"repos/{self.repo}/git/refs/tags/{name}")

    def delete_release(self, release_id: int) -> None:
        self._delete(f"repos/{self.repo}/releases/{release_id}")


def execute(pruned: Plan, client: PinDeleter, max_delete: int = MAX_DELETE) -> list[str]:
    """Delete the planned pins, branch -> release -> tag so a crash can only
    ever leave the half-pin state stage.py tolerates."""
    if len(pruned.delete) > max_delete:
        raise PruneError(
            f"plan would delete {len(pruned.delete)} pins, above the {max_delete} "
            "circuit breaker; re-run with --max-delete once the plan looks right"
        )
    done: list[str] = []
    for pin in pruned.delete:
        if pin.has_branch:
            client.delete_branch(pin.name)
            done.append(f"branch {pin.name}")
        if pin.release_id is not None:
            client.delete_release(pin.release_id)
            done.append(f"release {pin.name}")
        if pin.has_tag:
            client.delete_tag(pin.name)
            done.append(f"tag {pin.name}")
    return done


def summarize(pruned: Plan, *, dry_run: bool, keep_per_family: int) -> str:
    verb = "Would delete" if dry_run else "Deleted"
    lines = [
        "## Personal pin retention",
        "",
        f"Cutoff `{pruned.cutoff.isoformat()}` — pins dated before it are retired, "
        f"keeping the newest {keep_per_family} per family regardless of age.",
        "",
        f"- {verb}: **{len(pruned.delete)}** pins "
        f"({pruned.ref_count} refs, {pruned.release_count} releases)",
        f"- Kept (within window): {len(pruned.keep_recent)}",
        f"- Kept (floor): {len(pruned.keep_floor)}",
        "",
    ]
    if pruned.delete:
        lines += ["| pin | family | refs | release |", "| --- | --- | --- | --- |"]
        for pin in pruned.delete:
            refs = "+".join(
                part for part, on in (("tag", pin.has_tag), ("branch", pin.has_branch)) if on
            )
            lines.append(
                f"| `{pin.name}` | {pin.family} | {refs} | "
                f"{'yes' if pin.release_id is not None else '—'} |"
            )
        lines.append("")
    halves = [p.name for p in pruned.delete if p.is_half]
    if halves:
        lines += [f"Half-pins swept (branch without tag): {', '.join(halves)}", ""]
    if pruned.keep_floor:
        lines += [
            "Kept by the floor despite age: "
            + ", ".join(f"`{p.name}`" for p in pruned.keep_floor),
            "",
        ]
    return "\n".join(lines)


def append_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text if text.endswith("\n") else text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "btli/omnigent"))
    parser.add_argument("--keep-days", type=int, default=KEEP_DAYS)
    parser.add_argument("--keep-per-family", type=int, default=KEEP_PER_FAMILY)
    parser.add_argument("--max-delete", type=int, default=MAX_DELETE)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="plan and report without deleting anything",
    )
    parser.add_argument("--date", help="override today as YYYYMMDD (testing/backfill)")
    parser.add_argument("--report", help="write the plan as JSON here")
    args = parser.parse_args(argv)

    today = _date(args.date) if args.date else dt.datetime.now(dt.timezone.utc).date()
    if today is None:
        parser.error(f"--date {args.date!r} is not a YYYYMMDD calendar date")

    try:
        client = GhClient(args.repo, dry_run=args.dry_run)
        pins = collect(client.tags(), client.branches(), client.releases())
        pruned = plan(pins, today, args.keep_days, args.keep_per_family)
        execute(pruned, client, args.max_delete)
    except Exception as e:
        append_summary(f"## Personal pin retention\n\n**FAILED:** {e}\n")
        raise

    text = summarize(pruned, dry_run=args.dry_run, keep_per_family=args.keep_per_family)
    append_summary(text)
    sys.stdout.write(text + "\n")
    if args.report:
        report = {
            "cutoff": pruned.cutoff.isoformat(),
            "dry_run": args.dry_run,
            "deleted": [p.name for p in pruned.delete],
            "kept_recent": [p.name for p in pruned.keep_recent],
            "kept_floor": [p.name for p in pruned.keep_floor],
        }
        with open(args.report, "w") as fh:
            json.dump(report, fh, indent=2)
            fh.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
