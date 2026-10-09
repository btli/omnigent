#!/usr/bin/env python3
"""Decide whether a staging composition merged every PR and pin.

The nightly builds only when ``skipped`` is empty. Entries held out through
exclude.txt are intentional (``excluded``) and never count as failures.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from stage import md_code

HEADING = "Staging composition incomplete — nightly not built"


def _identity(entry: dict) -> str:
    pr = entry.get("pr")
    return f"#{pr}" if pr is not None else md_code(entry.get("branch", "unknown"))


def _issue(entry: dict) -> str:
    paths = entry.get("conflict_paths") or []
    if paths:
        return ", ".join(md_code(p) for p in paths)
    return md_code(entry.get("reason") or "unknown")


def composition_report(report: dict) -> tuple[bool, str]:
    """Return ``(complete, markdown)`` for a merge report."""
    skipped = report["skipped"]
    if not skipped:
        return (
            True,
            f"Staging composition complete: all {len(report['applied'])} entries merged.\n",
        )
    rows = [
        f"| {_identity(e)} | {md_code(e.get('source', 'unknown'))} "
        f"| {md_code(e.get('branch', 'unknown'))} | {_issue(e)} |"
        for e in skipped
    ]
    lines = [
        f"## {HEADING}",
        "",
        f"{len(skipped)} entries failed to merge.",
        "",
        "| PR | Source | Branch | Issue |",
        "| --- | --- | --- | --- |",
        *rows,
    ]
    return False, "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--summary", required=True, type=Path)
    args = parser.parse_args(argv)
    complete, markdown = composition_report(json.loads(args.report.read_text()))
    args.summary.write_text(markdown)
    print(f"complete={'true' if complete else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
