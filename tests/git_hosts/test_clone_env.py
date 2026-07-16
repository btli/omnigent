"""Per-clone credential env delivery in the exec launcher model."""

from __future__ import annotations

from typing import ClassVar

import click
import pytest

from omnigent.onboarding.sandboxes.base import RemoteCommandResult, SandboxLauncher


class _CaptureLauncher(SandboxLauncher):
    provider: ClassVar[str] = "capture"

    def __init__(self, *, fail: bool = False) -> None:
        self.commands: list[str] = []
        self._fail = fail

    def prepare(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def provision(self, name: str) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        if self._fail:
            raise click.ClickException(
                "boom: GIT_TOKEN='s3c ret' GIT_USERNAME=oauth2 git clone ..."
            )
        return RemoteCommandResult(returncode=0, stdout="", stderr="")


def test_clone_without_env_is_unchanged() -> None:
    launcher = _CaptureLauncher()
    launcher.materialize_workspace(
        "sb",
        workspace="/root/workspace",
        repo_url="https://git.acme.com/t/p",
        repo_branch=None,
        repo_name="p",
    )
    assert launcher.commands == ["git clone -- https://git.acme.com/t/p /root/workspace/p"]


def test_clone_env_is_prefixed_and_quoted() -> None:
    launcher = _CaptureLauncher()
    launcher.materialize_workspace(
        "sb",
        workspace="/root/workspace",
        repo_url="https://git.acme.com/t/p",
        repo_branch="main",
        repo_name="p",
        clone_env={"GIT_TOKEN": "s3c ret", "GIT_USERNAME": "oauth2"},
    )
    (cmd,) = launcher.commands
    assert cmd.startswith("GIT_TOKEN='s3c ret' GIT_USERNAME=oauth2 git clone ")
    assert "--branch main --single-branch" in cmd


def test_failed_clone_redacts_secret_values_from_the_error() -> None:
    launcher = _CaptureLauncher(fail=True)
    with pytest.raises(click.ClickException) as exc_info:
        launcher.materialize_workspace(
            "sb",
            workspace="/root/workspace",
            repo_url="https://git.acme.com/t/p",
            repo_branch=None,
            repo_name="p",
            clone_env={"GIT_TOKEN": "s3c ret", "GIT_USERNAME": "oauth2"},
        )
    message = exc_info.value.message
    assert "***" in message
    assert "s3c ret" not in message
    assert "'s3c ret'" not in message


def test_clone_env_bad_key_raises_before_running() -> None:
    launcher = _CaptureLauncher()
    with pytest.raises(click.ClickException, match="not a valid environment variable name"):
        launcher.materialize_workspace(
            "sb",
            workspace="/root/workspace",
            repo_url="https://git.acme.com/t/p",
            repo_branch=None,
            repo_name="p",
            clone_env={"GIT TOKEN": "x"},
        )
