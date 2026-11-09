"""Recover the newest incomplete nightly without rebuilding completed artifacts."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from rebuild_trigger import newest_nightly

WORKFLOW = "personal-staging-images.yml"


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def plan(releases: list[dict], runs: list[dict], now: float) -> dict:
    releases = [release for release in releases if not release.get("draft")]
    tag = newest_nightly([release["tag_name"] for release in releases])
    if tag is None:
        return {"action": "noop", "reason": "no nightly release"}
    release = next(release for release in releases if release["tag_name"] == tag)
    result = {"tag": tag}
    if any(asset["name"] == "build-complete.json" for asset in release["assets"]):
        return result | {"action": "noop", "reason": "newest nightly is complete"}
    # Serialize with every image publication, including a manually dispatched one.
    if any(run["status"] != "completed" for run in runs):
        return result | {"action": "wait", "reason": "image publication in flight"}
    matching = [
        run
        for run in runs
        if run["display_title"].startswith(f"Publish personal ring images ({tag}) by @")
    ]
    attempts = sum(run.get("run_attempt", 1) for run in matching)
    if attempts >= 3:
        return result | {
            "action": "exhausted",
            "reason": "three image attempts failed; operator repair required",
        }
    latest = max(matching, key=lambda run: run["id"]) if matching else None
    last_activity = latest["updated_at"] if latest else release["published_at"]
    if now - timestamp(last_activity) < 1800:
        return result | {
            "action": "wait",
            "reason": "waiting 30 minutes after publication or last attempt",
        }
    if latest:
        if latest["conclusion"] not in {"failure", "cancelled", "timed_out", "startup_failure"}:
            return result | {
                "action": "exhausted",
                "reason": "image run completed without marker; inspect publication",
            }
        return result | {
            "action": "retry",
            "run_id": latest["id"],
            "reason": f"retrying incomplete image build (attempt {attempts + 1}/3)",
        }
    return result | {"action": "dispatch", "reason": "nightly published without an image run"}


def gh_json(endpoint: str):
    result = subprocess.run(
        ["gh", "api", "--paginate", "--slurp", endpoint],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return json.loads(result.stdout)


def main() -> int:
    repo = os.environ["GITHUB_REPOSITORY"]
    if repo != "btli/omnigent":
        raise ValueError("image recovery is fork-only")
    releases = [
        release for page in gh_json(f"repos/{repo}/releases?per_page=100") for release in page
    ]
    runs = [
        run
        for page in gh_json(f"repos/{repo}/actions/workflows/{WORKFLOW}/runs?per_page=100")
        for run in page["workflow_runs"]
    ]
    result = plan(releases, runs, datetime.now(UTC).timestamp())
    print(json.dumps(result, indent=2))
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
        summary.write(
            f"## Nightly image recovery\n\n```json\n{json.dumps(result, indent=2)}\n```\n"
        )
    if result["action"] == "retry":
        subprocess.run(
            ["gh", "run", "rerun", str(result["run_id"]), "-R", repo, "--failed"],
            check=True,
            timeout=60,
        )
    elif result["action"] == "dispatch":
        subprocess.run(
            [
                "gh",
                "workflow",
                "run",
                WORKFLOW,
                "-R",
                repo,
                "--ref",
                "main",
                "-f",
                f"ref={result['tag']}",
            ],
            check=True,
            timeout=60,
        )
    elif result["action"] == "exhausted":
        print(f"::error::{result['tag']}: {result['reason']}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
