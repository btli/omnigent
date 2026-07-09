"""Provider-agnostic tests for the :class:`SandboxLauncher` base behavior.

The exec-model defaults (``run_background`` / ``start_host``) are shared by
every provider whose sandbox is a bare box the server execs into (Modal,
Daytona, E2B, Boxlite, Islo, …), so they are tested once here against a
minimal recording launcher rather than per provider.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest
import yaml

from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
    render_host_config_write_command,
)


class _RecordingLauncher(SandboxLauncher):
    """Minimal exec-model launcher that records every ``run`` command."""

    provider: ClassVar[str] = "recording"

    def __init__(self, home: str = "/root") -> None:
        self.commands: list[str] = []
        self.backgrounded: list[str] = []
        self._home = home

    def prepare(self) -> None:  # pragma: no cover - unused preflight stub
        pass

    def provision(self, name: str) -> str:  # pragma: no cover - unused stub
        return "sb-1"

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        # start_host probes $HOME first; everything else returns empty.
        stdout = self._home if command == 'printf %s "$HOME"' else ""
        return RemoteCommandResult(returncode=0, stdout=stdout, stderr="")

    def run_background(
        self, sandbox_id: str, command: str, *, log_path: str = "/tmp/omnigent-host.log"
    ) -> RemoteCommandResult:
        # Capture the raw (pre-wrap) command so a test can prove a real shell
        # honors its env prefix, independent of the setsid/nohup wrapper.
        self.backgrounded.append(command)
        return super().run_background(sandbox_id, command, log_path=log_path)


def test_run_background_wraps_command_in_sh_c() -> None:
    """
    ``run_background`` must wrap the command in ``sh -c`` so env-var prefixes
    survive ``nohup``. ``nohup ENV=val cmd`` makes nohup try to exec a program
    literally named ``ENV=val`` ("No such file or directory") — re-parsing under
    ``sh -c`` lets the inner shell apply the assignment before running ``cmd``.
    Regression: managed Daytona/Modal hosts never came online because the
    in-sandbox ``omnigent host`` launch died on its ``OMNIGENT_HOST_TOKEN=…``
    prefix.
    """
    launcher = _RecordingLauncher()

    launcher.run_background("sb-1", "FOO=bar omnigent host --server https://srv")

    [cmd] = launcher.commands
    assert cmd == (
        "setsid nohup sh -c 'FOO=bar omnigent host --server https://srv' "
        "> /tmp/omnigent-host.log 2>&1 < /dev/null & echo launched"
    )


def test_start_host_env_prefix_is_honored_by_a_real_shell() -> None:
    """
    The env-prefixed command ``start_host`` hands to ``run_background`` must
    apply its ``OMNIGENT_HOST_*`` assignments when re-parsed by a shell — the
    exact thing the ``sh -c`` wrapper restores. Run the raw command through a
    real ``sh -c`` (the inner shell of the wrapper) with ``omnigent host``
    swapped for a probe that echoes the injected vars; the broken bare-``nohup``
    form would never reach this assignment-honoring shell.
    """
    launcher = _RecordingLauncher()

    workspace = launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )
    assert workspace == "/root/workspace"

    [raw] = launcher.backgrounded
    # A nested `sh -c` reads the *inherited* env (a bare `$VAR` in the same
    # simple command would expand in the parent shell, before the temporary
    # assignment takes effect — and print empty).
    probe = raw.replace(
        "omnigent host --server https://srv",
        "sh -c 'printf %s:%s:%s "
        '"$OMNIGENT_HOST_TOKEN" "$OMNIGENT_HOST_ID" "$OMNIGENT_HOST_NAME"\'',
    )
    out = subprocess.run(
        ["sh", "-c", probe], capture_output=True, text=True, check=True
    ).stdout.strip()
    assert out == "tok-123:host_abc:managed-abc"


# ── host_config materialization ────────────────────────────

_GATEWAY_HOST_CONFIG: dict[str, object] = {
    "providers": {
        "litellm": {
            "kind": "gateway",
            "default": ["pi"],
            "openai": {
                "base_url": "http://litellm.litellm.svc.cluster.local/v1",
                "api_key_ref": "env:LITELLM_API_KEY",
                "wire_api": "chat",
            },
        }
    }
}


def _materialize(command: str, home: Path) -> dict[str, object]:
    """Run the rendered write command through a real shell + python3."""
    subprocess.run(
        ["sh", "-c", command],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    )
    with open(home / ".omnigent" / "config.yaml") as f:
        return yaml.safe_load(f)


def test_render_host_config_write_command_creates_config_from_scratch(tmp_path: Path) -> None:
    """A fresh sandbox (no ~/.omnigent at all) gets the injected config verbatim."""
    written = _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)
    assert written == _GATEWAY_HOST_CONFIG


def test_render_host_config_write_command_merges_providers_and_replaces_other_keys(
    tmp_path: Path,
) -> None:
    """
    The merge mirrors cli.py's ``deep_merge_keys=("providers",)``: sibling
    provider entries survive, an injected entry of the same name wins
    wholesale, other top-level keys replace, untouched keys persist.
    """
    (tmp_path / ".omnigent").mkdir()
    (tmp_path / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "anthropic": {"kind": "key"},
                    "litellm": {"kind": "gateway", "default": True},
                },
                "server": "https://old.example.com",
                "host": {"name": "keep-me"},
            }
        )
    )

    injected = {**_GATEWAY_HOST_CONFIG, "server": "https://new.example.com"}
    written = _materialize(render_host_config_write_command(injected), tmp_path)

    providers = written["providers"]
    assert providers["anthropic"] == {"kind": "key"}  # sibling survives
    # Same-name entry replaced wholesale (no per-entry merge), injected wins.
    assert providers["litellm"] == _GATEWAY_HOST_CONFIG["providers"]["litellm"]
    assert written["server"] == "https://new.example.com"
    assert written["host"] == {"name": "keep-me"}


def test_render_host_config_write_command_survives_hostile_yaml_content(tmp_path: Path) -> None:
    """
    Quotes, ``$VAR``-looking strings, backticks, newlines, and unicode round-trip
    byte-exact: the payload rides base64 through the shell/python layers, so no
    operator YAML can break out of the quoting.
    """
    hostile: dict[str, object] = {
        "providers": {
            'we\'ird "name"': {
                "kind": "gateway",
                "note": "line1\nline2 `tick` $HOME 'single' — ünïcode ✓",
            }
        }
    }
    written = _materialize(render_host_config_write_command(hostile), tmp_path)
    assert written == hostile


def test_materialized_config_routes_pi_to_the_gateway(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The point of the injection: a host booted with the materialized config
    resolves the gateway as pi's provider through the REAL config loader and
    harness-routing chain — before any ambient env credential is consulted.
    """
    _materialize(render_host_config_write_command(_GATEWAY_HOST_CONFIG), tmp_path)

    from omnigent.onboarding.provider_config import default_provider_for_harness, load_config

    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / ".omnigent"))
    entry = default_provider_for_harness(load_config(), "pi")

    assert entry is not None
    assert entry.name == "litellm"
    assert entry.kind == "gateway"


def test_start_host_writes_host_config_before_launching_the_host() -> None:
    """The config write runs via ``run`` strictly before the host is backgrounded."""
    launcher = _RecordingLauncher()

    launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
        host_config=_GATEWAY_HOST_CONFIG,
    )

    write_index = launcher.commands.index(render_host_config_write_command(_GATEWAY_HOST_CONFIG))
    # run_background funnels through run(), so the wrapped host launch is
    # also in `commands` — the write must precede it.
    host_index = next(
        i for i, cmd in enumerate(launcher.commands) if "omnigent host --server" in cmd
    )
    assert write_index < host_index


def test_start_host_without_host_config_writes_nothing() -> None:
    """No host_config → no config-write command reaches the sandbox."""
    launcher = _RecordingLauncher()

    launcher.start_host(
        "sb-1",
        token="tok-123",
        host_id="host_abc",
        host_name="managed-abc",
        server_url="https://srv",
    )

    assert not any(cmd.startswith("python3 -c") for cmd in launcher.commands)
