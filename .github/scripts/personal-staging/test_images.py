"""Ring tags select independent channels and native checks gate promotion."""

import os
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / "workflows/personal-staging-images.yml"


@pytest.mark.parametrize(
    ("tag", "channel"),
    [
        ("nightly-20260905", "staging-nightly"),
        ("production-20260905-rerun1", "production-nightly"),
    ],
)
def test_ring_channel(tag: str, channel: str) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["build-and-push"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "source")
    with tempfile.NamedTemporaryFile() as output:
        subprocess.run(
            ["bash", "-c", script],
            env={**os.environ, "INPUT_REF": tag, "GITHUB_OUTPUT": output.name},
            check=True,
            capture_output=True,
        )
        values = Path(output.name).read_text()
    assert f"channel={channel}\n" in values
    assert f"omnigent-host:{tag}" in values


@pytest.mark.parametrize("tag", ["main", "production-latest", "nightly-../../main"])
def test_reject_noncanonical_source_tag(tag: str) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["build-and-push"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "source")
    result = subprocess.run(
        ["bash", "-c", script],
        env={**os.environ, "INPUT_REF": tag},
        capture_output=True,
    )
    assert result.returncode != 0


def test_promotion_requires_native_host_checks() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert "native-host-check" in jobs["promote-channel"]["needs"]
    machines = jobs["native-host-check"]["strategy"]["matrix"]["include"]
    assert {entry["machine"] for entry in machines} == {"x86_64", "aarch64"}
