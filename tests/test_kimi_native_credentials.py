"""Tests for the per-session KIMI_CODE_HOME builder."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

from omnigent.harnesses.kimi_native.credentials import (
    KIMI_CODE_HOME_ENV_VAR,
    build_kimi_session_home,
    render_kimi_hooks_toml,
    with_kimi_ci_request_tags,
)


def _fake_user_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``resolve_user_kimi_home`` at a populated fake global home."""
    user_home = tmp_path / "user-kimi"
    user_home.mkdir()
    (user_home / "config.toml").write_text(
        'default_model = "kimi-code/x"\n[providers."managed"]\ntype = "kimi"\n', encoding="utf-8"
    )
    (user_home / "oauth").mkdir()
    (user_home / "oauth" / "token").write_text("secret", encoding="utf-8")
    (user_home / "sessions").mkdir()
    (user_home / "sessions" / "wd_g").mkdir()
    (user_home / "session_index.jsonl").write_text('{"sessionDir": "x"}\n', encoding="utf-8")
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(user_home))
    return user_home


def test_render_hooks_toml_is_valid_and_complete() -> None:
    toml = render_kimi_hooks_toml(bridge_dir=Path("/tmp/b r"), python_executable="/py")
    parsed = tomllib.loads(toml)
    events = {h["event"] for h in parsed["hooks"]}
    assert events == {"PreToolUse", "PermissionRequest"}
    for hook in parsed["hooks"]:
        assert "omnigent.harnesses.kimi_native.hook" in hook["command"]
        assert "/tmp/b r" in hook["command"]  # space-bearing path round-trips
        # ``-I`` (isolated mode) is mandatory: kimi runs the hook with cwd set to
        # the session workspace, so without it a workspace containing its own
        # ``omnigent/`` shadows the install and the hook dies on ImportError
        # before publishing the approval card.
        assert " -I -m omnigent.harnesses.kimi_native.hook" in hook["command"]
        # Pinned above kimi's 30s default so the permission hook survives a slow
        # web verdict (else the injected keystroke never lands); 600 is kimi's
        # ceiling.
        assert hook["timeout"] == 600


def test_build_session_home_preserves_user_config_and_appends_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    env = build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    assert env == {KIMI_CODE_HOME_ENV_VAR: str(session_home)}
    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    # User config preserved …
    assert parsed["default_model"] == "kimi-code/x"
    assert "managed" in parsed["providers"]
    # … and the Omnigent hooks appended.
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}


def test_build_session_home_symlinks_auth_but_not_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    # oauth is symlinked through to the user's tokens (auth keeps working) …
    oauth_link = session_home / "oauth"
    assert oauth_link.is_symlink()
    assert (oauth_link / "token").read_text(encoding="utf-8") == "secret"
    # … but config.toml is a real file (we own its content), not a symlink.
    assert not (session_home / "config.toml").is_symlink()


def test_build_session_home_keeps_sessions_store_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sessions store must not be shared, or parallel kimi sessions launched
    near-simultaneously can adopt each other's wire logs (crossed transcripts)."""
    _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    sessions = session_home / "sessions"
    assert sessions.is_dir() and not sessions.is_symlink()
    assert list(sessions.iterdir()) == []  # empty private store, not the global one
    assert not (session_home / "session_index.jsonl").exists()


def test_build_session_home_drops_stale_global_sessions_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session home built before the store went private gets its global
    ``sessions`` / index symlinks replaced by a private store on rebuild."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    session_home = tmp_path / "session-home"
    session_home.mkdir()
    (session_home / "sessions").symlink_to(user_home / "sessions")
    (session_home / "session_index.jsonl").symlink_to(user_home / "session_index.jsonl")
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    sessions = session_home / "sessions"
    assert sessions.is_dir() and not sessions.is_symlink()
    assert not (session_home / "session_index.jsonl").exists()
    # The user's global store is untouched.
    assert (user_home / "sessions" / "wd_g").is_dir()
    assert (user_home / "session_index.jsonl").read_text(encoding="utf-8")


def _relay_session_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return ``(bridge_dir, session_home)`` under a production-shaped kimi bridge root."""
    from omnigent.harnesses.kimi_native import bridge as kimi_bridge

    monkeypatch.setattr(kimi_bridge, "_BRIDGE_ROOT", tmp_path / "omnigent-test" / "kimi-native")
    bridge_dir = kimi_bridge.bridge_dir_for_session_id("conv_relay")
    bridge_dir.mkdir(mode=0o700, parents=True)
    return bridge_dir, bridge_dir / "kimi-code-home"


def _relay_servers(bridge_dir: Path) -> dict[str, dict[str, object]]:
    return {"omnigent": {"command": "/py", "args": ["serve-mcp", "--bridge-dir", str(bridge_dir)]}}


def test_build_session_home_replaces_stale_global_mcp_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A home built before relay wiring links ``mcp.json`` to the global file;
    enabling the relay must replace the link, never write through it."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    user_mcp = user_home / "mcp.json"
    user_mcp.write_text('{"mcpServers": {"docs": {"command": "docs-mcp"}}}', encoding="utf-8")
    user_mcp_before = user_mcp.read_bytes()
    bridge_dir, session_home = _relay_session_home(tmp_path, monkeypatch)
    build_kimi_session_home(session_home, bridge_dir=bridge_dir)
    assert (session_home / "mcp.json").is_symlink()

    build_kimi_session_home(
        session_home, bridge_dir=bridge_dir, omnigent_mcp_servers=_relay_servers(bridge_dir)
    )

    session_mcp = session_home / "mcp.json"
    assert not session_mcp.is_symlink()
    assert session_mcp.stat().st_mode & 0o777 == 0o600
    assert set(json.loads(session_mcp.read_text(encoding="utf-8"))["mcpServers"]) == {
        "docs",
        "omnigent",
    }
    assert user_mcp.read_bytes() == user_mcp_before


def test_build_session_home_writes_relay_only_mcp_config_for_invalid_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unreadable global ``mcp.json`` is skipped with a warning that names the
    path but never its contents."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    (user_home / "mcp.json").write_text('{"mcpServers": {"x": "user-secret"', encoding="utf-8")
    bridge_dir, session_home = _relay_session_home(tmp_path, monkeypatch)

    build_kimi_session_home(
        session_home, bridge_dir=bridge_dir, omnigent_mcp_servers=_relay_servers(bridge_dir)
    )

    payload = json.loads((session_home / "mcp.json").read_text(encoding="utf-8"))
    assert payload == {"mcpServers": _relay_servers(bridge_dir)}
    assert str(user_home / "mcp.json") in caplog.text
    assert "JSONDecodeError" in caplog.text
    assert "user-secret" not in caplog.text


@pytest.mark.parametrize("global_mcp", [True, False])
def test_build_session_home_restores_global_mcp_when_relay_is_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, global_mcp: bool
) -> None:
    """Without relay wiring, a previously generated ``mcp.json`` gives way to the
    pre-relay state: the global symlink, or no file at all."""
    user_home = _fake_user_home(tmp_path, monkeypatch)
    if global_mcp:
        (user_home / "mcp.json").write_text('{"mcpServers": {}}', encoding="utf-8")
    bridge_dir, session_home = _relay_session_home(tmp_path, monkeypatch)
    session_home.mkdir()
    (session_home / "mcp.json").write_text(
        json.dumps({"mcpServers": _relay_servers(bridge_dir)}), encoding="utf-8"
    )

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    session_mcp = session_home / "mcp.json"
    if global_mcp:
        assert session_mcp.is_symlink()
        assert session_mcp.resolve() == (user_home / "mcp.json").resolve()
    else:
        assert not session_mcp.exists() and not session_mcp.is_symlink()


def test_build_session_home_without_user_home_writes_hooks_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(KIMI_CODE_HOME_ENV_VAR, str(tmp_path / "does-not-exist"))
    session_home = tmp_path / "session-home"
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    build_kimi_session_home(session_home, bridge_dir=bridge_dir)

    parsed = tomllib.loads((session_home / "config.toml").read_text(encoding="utf-8"))
    assert {h["event"] for h in parsed["hooks"]} == {"PreToolUse", "PermissionRequest"}


@pytest.mark.parametrize("context", [None, "invalid", '{"pipeline":"repro-agent","run_id":"123"}'])
@pytest.mark.parametrize(
    "provider_config",
    [
        pytest.param(
            """[providers.databricks]
type = "openai_responses"
base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1"
api_key = "test-key"
[providers.databricks.custom_headers]
X-Existing = "kept"
Databricks-Ai-Gateway-Request-Tags = '{"team":"ci"}'
""",
            id="table-with-headers",
        ),
        pytest.param(
            'providers.databricks = {type = "openai_responses", '
            'base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1", '
            'api_key = "test-key"}\n',
            id="inline-without-headers",
        ),
        pytest.param(
            'providers.databricks = {type = "openai_responses", '
            'base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1", '
            'api_key = "test-key", customHeaders = {X-Existing = "kept", '
            'Databricks-Ai-Gateway-Request-Tags = \'{"team":"ci"}\'}}\n',
            id="inline-with-headers",
        ),
    ],
)
def test_ci_headers_survive_kimi_private_home(
    tmp_path, monkeypatch, caplog, context, provider_config
) -> None:
    """Native setup adds CI headers without changing provider settings or user files."""
    from omnigent.databricks_ai_gateway import REQUEST_TAGS_HEADER

    monkeypatch.delenv("OMNIGENT_CI_PIPELINE", raising=False)
    monkeypatch.delenv("OMNIGENT_CI_CONTEXT", raising=False)
    if context is not None:
        monkeypatch.setenv("OMNIGENT_CI_CONTEXT", context)
    if context in (None, "invalid"):
        monkeypatch.setattr(
            "omnigent.databricks_ai_gateway.urlparse",
            lambda _: pytest.fail("Disabled telemetry must not parse URLs"),
        )
    user_home = _fake_user_home(tmp_path, monkeypatch)
    original = (
        'default_model = "test"\n'
        + provider_config
        + """[providers.external]
type = "openai_responses"
base_url = "https://example.com/v1"
[providers.unused]
base_url = "https://[invalid"
[models.test]
provider = "databricks"
model = "test-model"
max_context_size = 200000
"""
    )
    (user_home / "config.toml").write_text(original)
    original_config = tomllib.loads(original)
    session_home = tmp_path / "session"
    env = build_kimi_session_home(session_home, bridge_dir=tmp_path / "bridge")
    configured = tomllib.loads((Path(env[KIMI_CODE_HOME_ENV_VAR]) / "config.toml").read_text())
    provider = configured["providers"]["databricks"]
    original_provider = original_config["providers"]["databricks"]
    header_key = "customHeaders" if "customHeaders" in original_provider else "custom_headers"
    original_headers = original_provider.get(header_key, {})
    headers = provider.get(header_key, {})
    assert {key: value for key, value in provider.items() if key != header_key} == {
        key: value for key, value in original_provider.items() if key != header_key
    }
    assert {key: value for key, value in headers.items() if key != REQUEST_TAGS_HEADER} == {
        key: value for key, value in original_headers.items() if key != REQUEST_TAGS_HEADER
    }
    expected = json.loads(original_headers.get(REQUEST_TAGS_HEADER, "{}"))
    if context and context != "invalid":
        expected.update(json.loads(context))
    assert json.loads(headers.get(REQUEST_TAGS_HEADER, "{}")) == expected
    assert configured["providers"]["external"] == original_config["providers"]["external"]
    assert configured["providers"]["unused"] == original_config["providers"]["unused"]
    assert ("malformed provider URL" in caplog.text) == (context not in (None, "invalid"))
    assert configured["models"] == original_config["models"]
    assert len(configured["hooks"]) == 2
    assert (session_home / "oauth").is_symlink()
    assert (user_home / "config.toml").read_text() == original


@pytest.mark.parametrize("failure", ["exception", "invalid-output"])
def test_kimi_serialization_failure_preserves_original_config(
    tmp_path, monkeypatch, caplog, failure
) -> None:
    import tomlkit
    from tomlkit.exceptions import ConvertError

    user_home = _fake_user_home(tmp_path, monkeypatch)
    original = """[providers.databricks]
base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1"
api_key = "test-key"
"""
    (user_home / "config.toml").write_text(original)
    monkeypatch.setenv("OMNIGENT_CI_CONTEXT", '{"pipeline":"repro-agent"}')

    def fail_serialization(config):
        if failure == "exception":
            raise ConvertError("test serialization failure")
        return "invalid toml ["

    monkeypatch.setattr(tomlkit, "dumps", fail_serialization)
    assert with_kimi_ci_request_tags(original) == original
    session_home = tmp_path / "session"
    build_kimi_session_home(session_home, bridge_dir=tmp_path / "bridge")
    written = (session_home / "config.toml").read_text()
    assert written.startswith(original)
    configured = tomllib.loads(written)
    assert configured["providers"] == tomllib.loads(original)["providers"]
    assert len(configured["hooks"]) == 2
    assert (session_home / "oauth").is_symlink()
    assert (user_home / "config.toml").read_text() == original
    assert "Skipping CI tags: could not serialize Kimi provider config" in caplog.text


@pytest.mark.parametrize(
    "config",
    [
        "not valid toml [",
        """[providers.databricks]
base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1"
[models.external]
provider = "databricks"
base_url = "https://example.com/v1"
""",
        """[providers.legacy]
base_url = "https://test.cloud.databricks.com/serving-endpoints"
""",
        """[providers.databricks]
base_url = "https://test.cloud.databricks.com/ai-gateway/codex/v1"
[models.unused]
provider = "databricks"
base_url = "https://[invalid"
""",
    ],
)
@pytest.mark.parametrize("opted_in", [False, True])
def test_kimi_does_not_tag_invalid_legacy_or_mixed_route_config(
    monkeypatch, caplog, config, opted_in
) -> None:
    monkeypatch.delenv("OMNIGENT_CI_PIPELINE", raising=False)
    monkeypatch.delenv("OMNIGENT_CI_CONTEXT", raising=False)
    if opted_in:
        monkeypatch.setenv("OMNIGENT_CI_CONTEXT", '{"pipeline":"repro-agent"}')
    assert with_kimi_ci_request_tags(config) == config
    if "https://[invalid" in config:
        assert ("malformed provider URL" in caplog.text) == opted_in
