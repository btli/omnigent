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
import sys
from pathlib import Path

NIGHTLY_RE = re.compile(r"nightly-(\d{8})(?:-rerun(\d+))?")
PRODUCTION_RE = re.compile(r"production-(\d{8})(?:-rerun(\d+))?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
SUMS_LINE_RE = re.compile(r"([0-9a-f]{64})\s+\*?(?:\./)?([A-Za-z0-9._-]+)")
BUILD_COMPLETE = "build-complete.json"


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_bc = sub.add_parser("build-complete", help="render a nightly's build-complete.json")
    p_bc.add_argument("--tag", required=True)
    p_bc.add_argument("--sha", required=True)
    p_bc.add_argument("--sums", required=True, type=Path)
    p_bc.add_argument("--server-digest", required=True)
    p_bc.add_argument("--host-digest", required=True)
    args = parser.parse_args(argv)
    if args.cmd == "build-complete":
        doc = build_complete(
            args.tag, args.sha, args.sums.read_text(), args.server_digest, args.host_digest
        )
        print(json.dumps(doc, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
