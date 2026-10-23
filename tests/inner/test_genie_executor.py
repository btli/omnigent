"""Tests for GenieExecutor and the genie harness registration.

Genie is Databricks' fork of the codex CLI; :class:`GenieExecutor` is a thin
:class:`CodexExecutor` subclass that only swaps the binary (``genie``) and the
on-disk home (``GENIE_HOME`` / ``~/.genie``). These tests pin those two seams
and the gateway-config inheritance, plus the ``genie`` harness registry entry.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.codex_executor import CodexExecutor
from omnigent.inner.genie_executor import (
    GENIE_HOME_DIR_NAME,
    GENIE_HOME_ENV_VAR,
    GenieExecutor,
    _find_genie_cli,
)


def _genie_executor(**overrides: object) -> GenieExecutor:
    """Build a GenieExecutor with the genie binary resolution stubbed."""
    with patch(
        "omnigent.inner.genie_executor._find_genie_cli",
        return_value="/usr/bin/genie",
    ):
        return GenieExecutor(**overrides)  # type: ignore[arg-type]


class TestGenieExecutor:
    def test_is_codex_executor_subclass(self) -> None:
        # The whole design: reuse the codex app-server driver, don't fork it.
        assert issubclass(GenieExecutor, CodexExecutor)

    def test_forces_genie_home(self) -> None:
        ex = _genie_executor()
        assert ex._home_env_var == GENIE_HOME_ENV_VAR == "GENIE_HOME"
        assert ex._home_dir_name == GENIE_HOME_DIR_NAME == ".genie"

    def test_home_override_cannot_be_smuggled_in(self) -> None:
        # Even if a caller passes codex's home, genie forces its own.
        ex = _genie_executor(home_env_var="CODEX_HOME", home_dir_name=".codex")
        assert ex._home_env_var == "GENIE_HOME"
        assert ex._home_dir_name == ".genie"

    def test_resolves_genie_binary(self) -> None:
        with patch(
            "omnigent.inner.genie_executor._find_genie_cli",
            return_value="/opt/genie",
        ):
            ex = GenieExecutor()
        assert ex._codex_path == "/opt/genie"

    def test_missing_binary_raises_with_genie_hint(self) -> None:
        with patch("omnigent.inner.genie_executor._find_genie_cli", return_value=None):
            with pytest.raises(ImportError, match="OMNIGENT_GENIE_PATH"):
                GenieExecutor()

    def test_find_genie_cli_uses_genie_name_and_env(self) -> None:
        with patch("omnigent.inner.genie_executor.resolve_cli_binary") as resolve:
            resolve.return_value = "/usr/bin/genie"
            assert _find_genie_cli() == "/usr/bin/genie"
        resolve.assert_called_once_with("genie", env_var="OMNIGENT_GENIE_PATH")

    def test_inherits_databricks_gateway_config(self) -> None:
        # Genie routes through the same Databricks Codex Responses gateway
        # (/ai-gateway/codex/v1) as codex; the gateway -c overrides come from
        # the shared CodexExecutor path unchanged.
        ex = _genie_executor(
            gateway=True,
            gateway_host="https://example.databricks.com",
            databricks_profile="eng-ml-agent-platform",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            gateway_auth_command="printf token",
            model="databricks-gpt-5-4-mini",
        )
        joined = "\n".join(ex._codex_config_overrides)
        assert "/ai-gateway/codex/v1" in joined
        assert 'model="databricks-gpt-5-4-mini"' in joined


class TestGenieRegistration:
    def test_registered_as_valid_harness(self) -> None:
        from omnigent import harness_plugins as hp

        assert "genie" in hp.valid_harnesses()
        assert hp.harness_modules()["genie"] == "omnigent.inner.genie_harness"
        assert hp.model_env_keys()["genie"] == "HARNESS_GENIE_MODEL"

    def test_consumes_openai_family(self) -> None:
        from omnigent.onboarding.provider_config import _HARNESS_FAMILY, OPENAI_FAMILY

        assert _HARNESS_FAMILY["genie"] == OPENAI_FAMILY

    def test_workflow_gateway_and_profile_maps(self) -> None:
        from omnigent.runtime import workflow as w

        assert w._HARNESS_GATEWAY_FLAG["genie"] == "HARNESS_GENIE_GATEWAY"
        assert w._HARNESS_DATABRICKS_PROFILE["genie"] == "HARNESS_GENIE_DATABRICKS_PROFILE"
        assert w._PROVIDER_HARNESS_FAMILY["genie"] == "openai"
        assert "genie" in w._UCODE_HARNESS_CONFIGS


@pytest.mark.parametrize("gateway_provider", [False, True])
def test_genie_launch_receives_spec_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gateway_provider: bool
) -> None:
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
    from omnigent.runner.app import _build_spawn_env_from_spec
    from omnigent.spec import AgentSpec, ExecutorSpec
    from tests._helpers.providers import isolate_provider_resolution

    isolate_provider_resolution(monkeypatch, tmp_path)
    if gateway_provider:
        monkeypatch.setenv("GENIE_TEST_KEY", "test-genie-key")
        (tmp_path / "config.yaml").write_text(
            "providers:\n"
            "  gateway:\n"
            "    kind: gateway\n"
            "    default: true\n"
            "    openai:\n"
            "      base_url: https://gateway.example/v1\n"
            "      api_key_ref: env:GENIE_TEST_KEY\n"
            "      wire_api: responses\n"
            "      models:\n"
            "        default: gpt-5.4\n"
        )
    spec = AgentSpec(
        spec_version=1,
        name="genie",
        executor=ExecutorSpec(type="omnigent", config={"harness": "genie"}),
        skills_filter="none",
        os_env=OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="none")),
    )
    env = _build_spawn_env_from_spec(
        spec, "genie", cwd=tmp_path, workdir=tmp_path, model_override="gpt-5.4"
    )
    assert env is not None, "Genie has no spawn environment builder"
    assert env["HARNESS_GENIE_MODEL"] == "gpt-5.4"
    if gateway_provider:
        assert env["HARNESS_GENIE_GATEWAY"] == "true"
        assert env["HARNESS_GENIE_GATEWAY_BASE_URL"] == "https://gateway.example/v1"
        assert "HARNESS_CODEX_GATEWAY_BASE_URL" not in env
    assert env["HARNESS_GENIE_CWD"] == str(tmp_path)
    assert env["HARNESS_GENIE_BUNDLE_DIR"] == str(tmp_path)
    assert json.loads(env["HARNESS_GENIE_SKILLS_FILTER"]) == "none"
    assert json.loads(env["HARNESS_GENIE_OS_ENV"])["sandbox"]["type"] == "none"


def test_genie_private_home_uses_genie_config_and_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from omnigent.inner import codex_executor

    monkeypatch.setenv("HOME", str(tmp_path))
    source = tmp_path / "custom-genie"
    source.mkdir()
    (source / "auth.json").write_text('{"token": "genie"}')
    skill = source / "skills" / "genie-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("Genie skill")
    monkeypatch.setenv("GENIE_HOME", str(source))
    captured: list[Path] = []

    async def capture_spawn(*args: str, **kwargs: object) -> None:
        process_env = kwargs["env"]
        assert isinstance(process_env, dict)
        private_home = Path(process_env["GENIE_HOME"])
        captured.append(private_home)
        assert private_home != source
        assert private_home.parent != tmp_path
        assert (private_home / "auth.json").read_text() == '{"token": "genie"}'
        assert (private_home / "skills" / "genie-skill" / "SKILL.md").read_text() == "Genie skill"
        raise RuntimeError("captured launch")

    monkeypatch.setattr(codex_executor, "_create_subprocess_exec", capture_spawn)
    session = codex_executor._CodexAppServerSession(
        codex_path="/usr/bin/genie",
        cwd=str(tmp_path),
        env={},
        tool_executor=None,
        home_env_var="GENIE_HOME",
        home_dir_name=".genie",
    )
    with pytest.raises(RuntimeError, match="captured launch"):
        asyncio.run(session.start())
    assert len(captured) == 1
    assert not captured[0].exists()
    assert (source / "auth.json").read_text() == '{"token": "genie"}'


def test_genie_decodes_nested_sandbox_bindings(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.inner.genie_harness import _resolve_os_env

    monkeypatch.setenv(
        "HARNESS_GENIE_OS_ENV",
        json.dumps(
            {
                "type": "caller_process",
                "sandbox": {
                    "type": "linux_bwrap",
                    "credential_proxy": {
                        "entries": [],
                        "databricks": {"profiles": [{"profile": "test"}]},
                    },
                },
            }
        ),
    )
    os_env = _resolve_os_env()
    assert os_env.sandbox is not None
    assert os_env.sandbox.credential_proxy is not None
    assert os_env.sandbox.credential_proxy.databricks is not None
    assert os_env.sandbox.credential_proxy.databricks.profiles[0].profile == "test"


@pytest.mark.parametrize("session_skills", [False, True])
def test_genie_harness_uses_session_skills_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session_skills: bool
) -> None:
    from omnigent.inner.genie_harness import _build_genie_executor

    skills_dir = tmp_path / "session-skills"
    monkeypatch.setenv("HARNESS_GENIE_GATEWAY", "0")
    monkeypatch.setenv("HARNESS_GENIE_SKILLS_DIR", str(skills_dir) if session_skills else "")
    monkeypatch.setenv("OMNIGENT_GENIE_PATH", "/usr/bin/genie")
    executor = _build_genie_executor()
    assert executor._skills_dir == (skills_dir if session_skills else None)
