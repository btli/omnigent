"""Detect staging composition regressions from consecutive merge reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def compare_reports(
    previous: dict[str, Any], current: dict[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """Return applied-to-skipped regressions and first-run skipped extras."""
    previously_applied = {entry["pr"]: entry for entry in previous.get("applied", [])}
    previously_seen = {
        entry["pr"] for section in ("applied", "skipped") for entry in previous.get(section, [])
    }

    regressions = []
    new_pins = []
    for entry in current.get("skipped", []):
        pr = entry["pr"]
        if pr in previously_applied:
            regressions.append(
                {
                    "pr": pr,
                    "previous_rerere_paths": previously_applied[pr].get("rerere_paths", []),
                    "current": entry,
                }
            )
        if entry.get("source") == "extra" and pr not in previously_seen:
            new_pins.append({"pr": pr, "current": entry})

    return {"regressions": regressions, "new_pins": new_pins}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("previous", type=Path)
    parser.add_argument("current", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    result = compare_reports(
        json.loads(args.previous.read_text()),
        json.loads(args.current.read_text()),
    )
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(rendered)
    else:
        print(rendered, end="")


if __name__ == "__main__":
    main()
