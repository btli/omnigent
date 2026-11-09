from __future__ import annotations

from datetime import datetime

import pytest
from recover_images import plan

NOW = datetime.fromisoformat("2026-11-09T12:00:00+00:00").timestamp()
TAG = "nightly-20261109"


def release(tag=TAG, complete=False):
    return {
        "tag_name": tag,
        "published_at": "2026-11-09T10:00:00Z",
        "assets": [{"name": "build-complete.json"}] if complete else [],
    }


def run(**overrides):
    return {
        "id": 42,
        "display_title": f"Publish personal ring images ({TAG}) by @btli",
        "status": "completed",
        "conclusion": "failure",
        "run_attempt": 1,
        "updated_at": "2026-11-09T11:00:00Z",
    } | overrides


def test_complete_nightly_never_rebuilt():
    assert plan([release(complete=True)], [run()], NOW)["action"] == "noop"


def test_missing_dispatch_recovered_after_grace_period():
    assert plan([release()], [], NOW)["action"] == "dispatch"
    assert plan([release()], [], NOW - 6000)["action"] == "wait"


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "startup_failure"])
def test_failed_run_retried_without_new_tag(conclusion):
    result = plan([release()], [run(conclusion=conclusion)], NOW)
    assert result["action"] == "retry"
    assert result["run_id"] == 42
    assert result["tag"] == TAG


def test_success_without_marker_requires_investigation():
    assert plan([release()], [run(conclusion="success")], NOW)["action"] == "exhausted"


def test_attempt_budget_includes_manual_runs_and_reruns():
    assert plan([release()], [run(run_attempt=3)], NOW)["action"] == "exhausted"
    assert plan([release()], [run(run_attempt=2), run(id=43)], NOW)["action"] == "exhausted"


def test_recent_failure_and_active_builds_wait():
    assert plan([release()], [run(updated_at="2026-11-09T11:45:00Z")], NOW)["action"] == "wait"
    assert plan([release()], [run(status="queued")], NOW)["action"] == "wait"
    assert (
        plan([release()], [run(status="in_progress", display_title="another image build")], NOW)[
            "action"
        ]
        == "wait"
    )


def test_only_latest_nightly_matters_and_drafts_are_ignored():
    releases = [
        release("nightly-20261109-rerun2"),
        release("nightly-20261109-rerun10", complete=True),
    ]
    assert plan(releases, [run()], NOW)["action"] == "noop"
    assert plan([release() | {"draft": True}], [], NOW)["action"] == "noop"
    assert plan([release("production-20261109")], [], NOW)["action"] == "noop"


def test_old_candidate_run_does_not_spend_new_candidate_budget():
    assert (
        plan([release("nightly-20261109-rerun1")], [run(run_attempt=3)], NOW)["action"]
        == "dispatch"
    )
