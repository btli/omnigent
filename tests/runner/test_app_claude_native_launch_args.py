"""Tests for the runner's claude-native base-args assembly.

``_build_claude_native_base_args`` is the pure seam that turns a
session's persisted launch config (reasoning_effort, model_override,
terminal_launch_args) into the base ``claude`` CLI args a
daemon/server-spawned runner launches with — before
``augment_claude_args`` layers on the bridge/MCP/hook/AP wiring. The
invariants under test (order, model precedence, ignore-unknown-effort)
are what make a host-spawned launch match what the CLI would have
passed. See designs/NATIVE_RUNNER_SERVER_LAUNCH.md.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import click
import pytest

from omnigent import claude_native, model_catalog_store
from omnigent.claude_native import (
    ClaudeNativeUcodeConfig,
    build_native_claude_terminal_env,
)
from omnigent.runner.app import _build_claude_native_base_args, _claude_terminal_env_unset
from omnigent.runner.native.orchestration import (
    _ROUTED_SPAWN_ALLOWED_TOOLS,
    _claude_launch_metadata_from_envelope,
    _load_legacy_claude_launch_metadata,
    _log_claude_launch_catalog_unavailable,
    _routed_spawn_launch_args,
    _select_authoritative_claude_launch_model,
)
from omnigent.runner.subagent_routing import AUTO_HARNESS_LABEL_KEY

_GATEWAY_ROWS = [
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-4-6[1m]",
        "displayName": "Sonnet 4.6",
    },
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-8[1m]",
        "displayName": "Opus 4.8",
        "isDefault": True,
    },
    {
        "id": "haiku",
        "model": "system.ai.claude-haiku-4-5",
        "displayName": "Haiku 4.5",
    },
]


def _gateway_config(*, databricks: bool = True) -> ClaudeNativeUcodeConfig:
    env = {"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"}
    if databricks:
        env.update(
            {
                "CLAUDE_CODE_USE_GATEWAY": "1",
                "ANTHROPIC_CUSTOM_HEADERS": "x-databricks-use-coding-agent-mode: true",
            }
        )
    return ClaudeNativeUcodeConfig(env=env)


async def _fresh_catalog_result(
    rows: list[dict[str, object]] = _GATEWAY_ROWS,
    *,
    fingerprint: str = "selector",
) -> model_catalog_store.CatalogResult:
    async def _resolve() -> list[dict[str, object]]:
        return rows

    return await model_catalog_store.ensure_authoritative_catalog_result(
        "claude-native", fingerprint, _resolve
    )


async def _failed_catalog_result(
    kind: model_catalog_store.CatalogRefreshFailureKind,
    message: str,
    *,
    with_cache: bool = True,
) -> model_catalog_store.CatalogResult:
    if with_cache:
        model_catalog_store.write_catalog("claude-native", "selector", _GATEWAY_ROWS)
        path = model_catalog_store.catalog_path("claude-native", "selector")
        stale_time = time.time() - model_catalog_store.CATALOG_STALE_AFTER_S - 1.0
        os.utime(path, (stale_time, stale_time))

    async def _fail() -> list[dict[str, object]]:
        raise model_catalog_store.CatalogRefreshError(kind, message)

    return await model_catalog_store.ensure_authoritative_catalog_result(
        "claude-native", "selector", _fail
    )


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("databricks-claude-sonnet-4-6", "system.ai.claude-sonnet-4-6[1m]"),
        ("system.ai.claude-sonnet-4-6[1m]", "system.ai.claude-sonnet-4-6[1m]"),
        ("sonnet", "sonnet"),
        ("databricks-claude-opus-4-8", "system.ai.claude-opus-4-8[1m]"),
        ("system.ai.claude-opus-4-8[1m]", "system.ai.claude-opus-4-8[1m]"),
        ("opus", "opus"),
        ("databricks-claude-haiku-4-5", "system.ai.claude-haiku-4-5"),
        ("system.ai.claude-haiku-4-5", "system.ai.claude-haiku-4-5"),
        ("haiku", "haiku"),
    ],
)
async def test_explicit_gateway_model_resolves_to_catalog_vocabulary(
    requested: str,
    expected: str,
) -> None:
    catalog = await _fresh_catalog_result()
    selected, _notice = _select_authoritative_claude_launch_model(
        explicit_model=requested,
        configured_model=None,
        claude_config=_gateway_config(),
        catalog=catalog,
    )

    assert selected == expected


async def test_fresh_catalog_miss_lists_launchable_ids_without_auth_advice() -> None:
    catalog = await _fresh_catalog_result()
    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-fable-5",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert str(exc_info.value) == (
        "the requested model 'databricks-claude-fable-5' is not in this host's current "
        "model list — it may have changed since the pick. Launchable model ids: 'haiku', "
        "'opus', 'sonnet', 'system.ai.claude-haiku-4-5', "
        "'system.ai.claude-opus-4-8[1m]', 'system.ai.claude-sonnet-4-6[1m]'. Pick again "
        "from the model menu."
    )


async def test_fresh_catalog_does_not_substitute_a_different_generation() -> None:
    catalog = await _fresh_catalog_result()
    with pytest.raises(click.ClickException, match="not in this host's current model list"):
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-5",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )


async def test_ambiguous_normalized_catalog_match_fails_closed() -> None:
    catalog = await _fresh_catalog_result(
        [
            {"id": "opus", "model": "system.ai.claude-opus-4-8"},
            {"id": "opus[1m]", "model": "system.ai.claude-opus-4-8[1m]"},
        ],
        fingerprint="ambiguous-selector",
    )

    with pytest.raises(click.ClickException, match="not in this host's current model list"):
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-4-8",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )


async def test_auth_failed_catalog_refresh_gives_databricks_repair_for_absent_pin() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.AUTH,
        "Claude model catalog authentication failed",
    )
    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-fable-5",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert str(exc_info.value) == (
        "the requested model 'databricks-claude-fable-5' could not be validated against "
        "a fresh model list (Claude model catalog authentication failed). Restore provider "
        "credentials and retry. For Databricks, run "
        "`databricks auth login --profile <PROFILE>`."
    )


async def test_auth_failed_catalog_refresh_omits_databricks_command_for_other_gateway() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.AUTH,
        "Claude model catalog authentication failed",
    )
    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="gateway-claude-fable-5",
            configured_model=None,
            claude_config=_gateway_config(databricks=False),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "Restore provider credentials and retry" in message
    assert "databricks auth login" not in message


async def test_non_auth_refresh_failure_uses_neutral_retry_guidance() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
        "Claude model catalog refresh timed out",
    )
    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-fable-5",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert str(exc_info.value) == (
        "the requested model 'databricks-claude-fable-5' could not be validated against "
        "a fresh model list (Claude model catalog refresh timed out). Retry after checking "
        "Claude CLI availability and provider connectivity."
    )


async def test_gateway_pin_uses_stale_rows_when_authoritative_refresh_fails() -> None:
    catalog = model_catalog_store.CatalogResult(
        _GATEWAY_ROWS,
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
            "Claude model catalog refresh timed out",
        ),
    )

    launch_model, notice = _select_authoritative_claude_launch_model(
        explicit_model="databricks-claude-opus-4-8",
        configured_model=None,
        claude_config=_gateway_config(),
        catalog=catalog,
    )
    assert launch_model == "system.ai.claude-opus-4-8[1m]"
    # The stale fold is as visible as the fresh one: the launch spelling
    # carries a different [1m] context marker than the request.
    assert notice is not None
    assert "context marker" in notice


async def test_configured_pin_survives_a_stale_row_outage_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stale rows that neither fold nor serve the configured pin never drop it."""
    catalog = model_catalog_store.CatalogResult(
        [{"id": "opus", "model": "system.ai.claude-opus-4-8[1m]", "isDefault": True}],
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
            "Claude model catalog refresh timed out",
        ),
    )

    with caplog.at_level("WARNING", logger="omnigent.runner.app"):
        selection = _select_authoritative_claude_launch_model(
            explicit_model=None,
            configured_model="databricks-claude-fable-5",
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert selection[0] == "databricks-claude-fable-5"
    assert selection[1] is not None
    assert "could not be verified" in selection[1]
    assert "without fresh-catalog verification" in caplog.text


async def test_canonical_configured_pin_survives_a_stale_row_outage_silently(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A canonical configured pin passes the outage without the unverified WARN."""
    catalog = model_catalog_store.CatalogResult(
        [{"id": "haiku", "model": "claude-haiku-4-5", "isDefault": True}],
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
            "Claude model catalog refresh timed out",
        ),
    )
    claude_config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"},
        model="claude-fable-5",
    )

    with caplog.at_level("WARNING", logger="omnigent.runner.app"):
        selection = _select_authoritative_claude_launch_model(
            explicit_model=None,
            configured_model="claude-fable-5",
            claude_config=claude_config,
            catalog=catalog,
        )

    assert selection == ("claude-fable-5", None)
    assert "without fresh-catalog verification" not in caplog.text


async def test_default_launch_goes_bare_when_only_stale_rows_survive() -> None:
    """A stale default is yesterday's answer: a no-pin launch passes no --model."""
    catalog = model_catalog_store.CatalogResult(
        _GATEWAY_ROWS,
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
            "Claude model catalog refresh timed out",
        ),
    )

    assert _select_authoritative_claude_launch_model(
        explicit_model=None,
        configured_model=None,
        claude_config=_gateway_config(),
        catalog=catalog,
    ) == (None, None)


def test_canonical_pin_fail_closes_on_established_auth_probe_failure() -> None:
    """`Please run /login` in probe stderr is an AUTH failure, not fail-open."""
    refresh_error = claude_native._claude_probe_process_error(b"Please run /login")
    catalog = model_catalog_store.CatalogResult(
        None,
        model_catalog_store.CatalogFreshness.MISSING,
        refresh_error,
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="claude-opus-4-8",
            configured_model=None,
            claude_config=None,
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "Restore provider credentials" in message


async def test_gateway_pin_does_not_use_stale_rows_when_auth_refresh_fails() -> None:
    catalog = model_catalog_store.CatalogResult(
        _GATEWAY_ROWS,
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.AUTH,
            "Claude model catalog authentication failed",
        ),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-4-8",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "Restore provider credentials and retry" in message
    assert "databricks auth login" in message


async def test_gateway_pin_does_not_use_stale_rows_after_authoritative_empty() -> None:
    catalog = model_catalog_store.CatalogResult(
        _GATEWAY_ROWS,
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.EMPTY,
            "Claude model catalog refresh returned no launchable models",
        ),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-4-8",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "is not available" in message
    assert "Pick again from the model menu" in message
    assert "provider credentials" not in message


async def test_configured_gateway_model_does_not_use_stale_rows_when_auth_refresh_fails() -> None:
    catalog = model_catalog_store.CatalogResult(
        _GATEWAY_ROWS,
        model_catalog_store.CatalogFreshness.STALE,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.AUTH,
            "Claude model catalog authentication failed",
        ),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model=None,
            configured_model="databricks-claude-opus-4-8",
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "Restore provider credentials and retry" in message
    assert "databricks auth login" in message


@pytest.mark.parametrize(
    "pin",
    [
        # A genuinely canonical pin: the outage pass-through would accept it,
        # so this locks CLI_ABSENT failing closed BEFORE the pass-through.
        "claude-opus-4-8",
        "system.ai.claude-opus-4-8[1m]",
    ],
)
def test_canonical_gateway_model_does_not_bypass_cli_absent_failure(pin: str) -> None:
    from omnigent.claude_native import is_canonical_claude_pin

    assert is_canonical_claude_pin("claude-opus-4-8")
    claude_config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}
    )
    catalog = model_catalog_store.CatalogResult(
        None,
        model_catalog_store.CatalogFreshness.MISSING,
        model_catalog_store.CatalogRefreshError(
            model_catalog_store.CatalogRefreshFailureKind.CLI_ABSENT,
            "Claude model catalog could not launch the CLI",
        ),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model=pin,
            configured_model=None,
            claude_config=claude_config,
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "could not launch the CLI" in message
    assert "Claude CLI availability and provider connectivity" in message


def test_forbidden_catalog_failure_advises_reauthentication() -> None:
    refresh_error = claude_native._claude_probe_process_error(b"HTTP 403 Forbidden quota exceeded")
    catalog = model_catalog_store.CatalogResult(
        None,
        model_catalog_store.CatalogFreshness.MISSING,
        refresh_error,
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-fable-5",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "Restore provider credentials" in message
    assert "databricks auth login" in message


def test_catalog_catch_all_log_does_not_include_exception_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    payload = "Authorization: Bearer super-secret response-body=private"

    with caplog.at_level("WARNING", logger="omnigent.runner.app"):
        try:
            raise RuntimeError(payload)
        except RuntimeError:
            _log_claude_launch_catalog_unavailable("session-123")

    assert "claude launch catalog unavailable for session=session-123" in caplog.text
    assert payload not in caplog.text
    assert "RuntimeError" not in caplog.text


async def test_probe_failure_message_suppresses_provider_secrets_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _UnauthorizedProcess:
        returncode = 1

        async def communicate(self) -> tuple[bytes, bytes]:
            return (
                b"",
                b"HTTP 401 Authorization: Bearer super-secret https://user:pass@gw.example",
            )

    async def _fake_exec(*args: object, **kwargs: object) -> _UnauthorizedProcess:
        del args, kwargs
        return _UnauthorizedProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", _fake_exec)
    config = _gateway_config()
    catalog = await model_catalog_store.ensure_authoritative_catalog_result(
        "claude-native",
        "secret-suppression",
        lambda: claude_native.claude_model_catalog(config),
    )

    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-4-8",
            configured_model=None,
            claude_config=config,
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "authentication failed" in message
    assert "super-secret" not in message
    assert "Authorization" not in message
    assert "user:pass" not in message
    assert "super-secret" not in caplog.text
    assert "Authorization" not in caplog.text
    assert "user:pass" not in caplog.text


async def test_missing_empty_catalog_does_not_blame_credentials() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.EMPTY,
        "Claude model catalog enumeration returned no models",
        with_cache=False,
    )
    with pytest.raises(click.ClickException) as exc_info:
        _select_authoritative_claude_launch_model(
            explicit_model="databricks-claude-opus-4-8",
            configured_model=None,
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    message = str(exc_info.value)
    assert "is not available" in message
    assert "Pick again from the model menu" in message
    assert "provider credentials" not in message
    assert "databricks auth login" not in message


async def test_direct_login_alias_fails_open_when_refresh_fails() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
        "Claude model catalog refresh timed out",
        with_cache=False,
    )

    assert _select_authoritative_claude_launch_model(
        explicit_model="sonnet",
        configured_model=None,
        claude_config=None,
        catalog=catalog,
    ) == ("sonnet", None)


async def test_fresh_direct_login_catalog_still_rejects_an_unlisted_family() -> None:
    catalog = await _fresh_catalog_result()

    with pytest.raises(click.ClickException, match="not in this host's current model list"):
        _select_authoritative_claude_launch_model(
            explicit_model="claude-fable-5",
            configured_model=None,
            claude_config=None,
            catalog=catalog,
        )


async def test_configured_gateway_model_folds_to_fresh_catalog_vocabulary(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = await _fresh_catalog_result()

    with caplog.at_level("WARNING", logger="omnigent.claude_native"):
        selection = _select_authoritative_claude_launch_model(
            explicit_model=None,
            configured_model="databricks-claude-opus-4-8",
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert selection[0] == "system.ai.claude-opus-4-8[1m]"
    assert selection[1] is not None
    assert "different [1m] context marker" in selection[1]
    assert "substituting requested model" in caplog.text


async def test_unserved_configured_gateway_model_uses_fresh_default_visibly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    catalog = await _fresh_catalog_result()

    with caplog.at_level("WARNING", logger="omnigent.runner.app"):
        selection = _select_authoritative_claude_launch_model(
            explicit_model=None,
            configured_model="system.ai.claude-opus-4-7",
            claude_config=_gateway_config(),
            catalog=catalog,
        )

    assert selection[0] == "system.ai.claude-opus-4-8[1m]"
    assert selection[1] is not None
    assert "is unavailable on this host" in selection[1]
    assert "using fresh catalog default" in caplog.text


async def test_unserved_configured_gateway_model_uses_bare_fresh_launch_visibly() -> None:
    catalog = await _fresh_catalog_result(
        [{"id": "sonnet", "model": "system.ai.claude-sonnet-4-6"}],
        fingerprint="configured-without-default",
    )

    selection = _select_authoritative_claude_launch_model(
        explicit_model=None,
        configured_model="system.ai.claude-opus-4-7",
        claude_config=_gateway_config(),
        catalog=catalog,
    )

    assert selection[0] is None
    assert selection[1] is not None
    assert "fresh catalog has no default" in selection[1]


async def test_default_gateway_launch_fails_open_when_refresh_fails() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.OTHER,
        "Claude model catalog probe exited unsuccessfully",
        with_cache=False,
    )

    assert _select_authoritative_claude_launch_model(
        explicit_model=None,
        configured_model=None,
        claude_config=_gateway_config(),
        catalog=catalog,
    ) == (None, None)


async def test_configured_gateway_pin_surfaces_a_cold_catalog_outage() -> None:
    catalog = await _failed_catalog_result(
        model_catalog_store.CatalogRefreshFailureKind.TIMEOUT,
        "Claude model catalog refresh timed out",
        with_cache=False,
    )

    selection = _select_authoritative_claude_launch_model(
        explicit_model=None,
        configured_model="databricks-claude-opus-4-8",
        claude_config=_gateway_config(),
        catalog=catalog,
    )

    assert selection[0] == "databricks-claude-opus-4-8"
    assert selection[1] is not None
    assert "could not be verified" in selection[1]


async def test_absent_configured_resolution_continues_to_fresh_catalog_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = await _fresh_catalog_result()
    monkeypatch.setattr(
        "omnigent.claude_native.resolve_claude_native_model_selection",
        lambda model, config: None,
    )

    selection = _select_authoritative_claude_launch_model(
        explicit_model=None,
        configured_model="unresolved-provider-default",
        claude_config=_gateway_config(),
        catalog=catalog,
    )

    assert selection[0] == "system.ai.claude-opus-4-8[1m]"
    assert selection[1] is not None
    assert "is unavailable on this host" in selection[1]


@pytest.mark.parametrize(
    ("reasoning_effort", "model_override", "terminal_launch_args", "expected"),
    [
        # Effort only → "--effort <value>"; nothing else contributed.
        ("high", None, None, ("--effort", "high")),
        # Pass-through flags are included verbatim; model_override is
        # appended as a default --model because the user gave no --model.
        (
            None,
            "claude-opus-4-7",
            ["--dangerously-skip-permissions"],
            ("--dangerously-skip-permissions", "--model", "claude-opus-4-7"),
        ),
        # Explicit --model in pass-through args WINS over model_override
        # (space form): the override default must not be appended.
        (None, "claude-opus-4-7", ["--model", "sonnet"], ("--model", "sonnet")),
        # Explicit --model in pass-through args WINS (joined form): the
        # ``--model=X`` spelling must also suppress the override default.
        (None, "claude-opus-4-7", ["--model=sonnet"], ("--model=sonnet",)),
        # Full ordering: effort prefix, then pass-through, then the
        # model default last. A different order would mean the assembly
        # logic changed and the launch command no longer matches the CLI.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            ("--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # Nothing persisted → no args (Claude uses its settings.json
        # defaults). A non-empty result here would mean we injected a
        # phantom flag.
        (None, None, None, ()),
        # An empty pass-through list behaves like None — contributes
        # nothing, but the model default still applies.
        (None, "claude-opus-4-7", [], ("--model", "claude-opus-4-7")),
        # An unrecognised effort is dropped (not a Claude effort), so it
        # never reaches the CLI as a bogus ``--effort`` value.
        ("bogus-effort", None, None, ()),
    ],
    ids=[
        "effort-only",
        "model-default-appended",
        "explicit-model-space-wins",
        "explicit-model-joined-wins",
        "full-ordering",
        "all-none",
        "empty-passthrough-still-adds-model",
        "unknown-effort-dropped",
    ],
)
def test_build_claude_native_base_args(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    expected: tuple[str, ...],
) -> None:
    """
    Assemble base args from persisted launch config.

    Each case pins one invariant; the expected tuple is the exact arg
    vector the runner must hand to ``augment_claude_args``. A mismatch
    means a daemon/server-spawned claude launch would diverge from the
    CLI's command (wrong order, missing pass-through flag, or the model
    override clobbering an explicit user ``--model``).
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("reasoning_effort", "model_override", "terminal_launch_args", "resume", "expected"),
    [
        # Resume alone → just the --resume prefix.
        (None, None, None, "sid-123", ("--resume", "sid-123")),
        # --resume comes FIRST, before effort / pass-through / model —
        # mirroring the CLI's (*cold_resume_args, *claude_args) order.
        (
            "high",
            "claude-opus-4-7",
            ["--verbose"],
            "sid-123",
            ("--resume", "sid-123", "--effort", "high", "--verbose", "--model", "claude-opus-4-7"),
        ),
        # No resume id → no --resume (fresh launch, or no local
        # transcript could be synthesized).
        (None, None, ["--verbose"], None, ("--verbose",)),
    ],
    ids=["resume-only", "resume-first-ordering", "no-resume"],
)
def test_build_claude_native_base_args_resume_prefix(
    reasoning_effort: str | None,
    model_override: str | None,
    terminal_launch_args: list[str] | None,
    resume: str | None,
    expected: tuple[str, ...],
) -> None:
    """
    A cold-resume session id is prepended as ``--resume <sid>`` ahead of
    every other arg.

    The ordering matters: Claude applies ``--resume`` to pick the
    transcript, and the runner-side launch must match the CLI's
    long-standing ``--resume``-first arg vector. A wrong position (or a
    missing prefix when an id is supplied) would mean a daemon/web-UI
    resume silently starts a fresh Claude session instead of reopening
    the prior transcript.
    """
    assert (
        _build_claude_native_base_args(
            reasoning_effort=reasoning_effort,
            model_override=model_override,
            terminal_launch_args=terminal_launch_args,
            resume_external_session_id=resume,
        )
        == expected
    )


def test_claude_terminal_env_unset_masks_key_with_api_key_helper() -> None:
    """An apiKeyHelper launch strips the raw key + nested-session marker.

    When the credential reaches Claude Code via an ``apiKeyHelper``, a raw
    ``ANTHROPIC_API_KEY`` in the child env makes Claude open its custom-API-
    key confirmation menu, and the first web message is typed into that menu
    instead of the chat composer. The key must not leak into the terminal
    child; ``CLAUDECODE`` is stripped alongside it to avoid a nested-session
    error. ``DATABRICKS_CONFIG_PROFILE`` is always dropped.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    env_unset = _claude_terminal_env_unset(config)
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "CLAUDECODE" in env_unset
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset


def test_claude_terminal_env_unset_without_helper_keeps_key() -> None:
    """No apiKeyHelper preserves the raw key but strips nested-session state.

    Claude's own-login path (``None`` config) and a Bedrock-style config have
    no ``apiKeyHelper``, so this helper does not strip ``ANTHROPIC_API_KEY``.
    ``CLAUDECODE`` must still be absent because Claude Code rejects nested
    launches in every auth mode.
    """
    expected = ["DATABRICKS_CONFIG_PROFILE", "CLAUDECODE"]
    own_login_env_unset = _claude_terminal_env_unset(None)
    assert own_login_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in own_login_env_unset
    bedrock_like = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BEDROCK_BASE_URL": "https://bedrock.example"},
        api_key_helper=None,
        model="us.anthropic.claude-opus-4-5-20251101-v1:0",
    )
    bedrock_env_unset = _claude_terminal_env_unset(bedrock_like)
    assert bedrock_env_unset == expected
    assert "ANTHROPIC_API_KEY" not in bedrock_env_unset


def test_native_launch_passes_synthesized_model_as_flag() -> None:
    """A synthesized gateway model reaches the launch as ``--model``.

    ``_auto_create_claude_terminal`` feeds ``claude_config.model`` (populated
    by ambient Anthropic synthesis from ``ANTHROPIC_MODEL``) into the base
    args as the model default. This pins the end-to-end contract: a gateway
    model resolves to ``--model <id>`` so Claude Code doesn't launch with its
    own default that the gateway rejects.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://gateway.example/anthropic"},
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    # Mirrors the runner's precedence: session override wins, else the
    # provider/ucode gateway model becomes the --model default.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=None or config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "gateway-served-claude")


def test_routed_launch_model_reaches_the_terminal_env_as_the_custom_slot() -> None:
    """A routed exact id is launchable AND switchable back to mid-session.

    Mirrors the runner's composition: the session override becomes
    ``--model`` and the same value is pinned into Claude Code's custom picker
    slot, which is the only spelling ``/model`` accepts for an id no family
    alias points at (``opus`` here resolves to the newer generation).
    """
    from omnigent.claude_model_vocabulary import claude_model_command_arg
    from omnigent.claude_native import claude_config_with_launch_model_pinned

    config = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-5",
        },
        api_key_helper="printf %s sk-gateway",
        model="databricks-claude-opus-5",
    )
    session_model_override = "databricks-claude-opus-4-8"

    launched = claude_config_with_launch_model_pinned(config, session_model_override)
    assert launched is not None
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=session_model_override,
        terminal_launch_args=None,
    )
    terminal_env = build_native_claude_terminal_env(launched)

    assert args == ("--model", "databricks-claude-opus-4-8")
    assert terminal_env["ANTHROPIC_CUSTOM_MODEL_OPTION"] == "databricks-claude-opus-4-8"
    assert (
        claude_model_command_arg(session_model_override, terminal_env)
        == "databricks-claude-opus-4-8"
    )


def test_build_native_claude_terminal_env_rejects_raw_key_on_helper_path() -> None:
    """The env-build seam fails loud if a raw key rides the apiKeyHelper path.

    ``_claude_terminal_env_unset`` strips the raw key from the terminal child
    only because ``build_native_claude_terminal_env`` never emits one on the
    helper path. Pin that invariant mechanically: if a future config injects a
    raw ``ANTHROPIC_API_KEY`` into the terminal env while an ``apiKeyHelper`` is
    configured, the build must raise rather than silently reintroduce Claude
    Code's custom-API-key menu hang.
    """
    leaking = ClaudeNativeUcodeConfig(
        env={
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "ANTHROPIC_API_KEY": "sk-leaked",
        },
        api_key_helper="printf %s sk-gateway",
        model="gateway-served-claude",
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_native_claude_terminal_env(leaking)


def test_claude_terminal_env_databricks_gateway_helper_path() -> None:
    """The Databricks ucode/profile gateway session, end to end through the env seams.

    A Databricks-gateway session has an ``apiKeyHelper`` (the Databricks auth
    command), ``ANTHROPIC_BASE_URL`` at the Databricks endpoint, a ucode model,
    and NO raw ``ANTHROPIC_API_KEY``; the runner also carries an ambient
    ``DATABRICKS_CONFIG_PROFILE``. This pins the real-user shape (not just a
    generic gateway): the terminal child must drop the Databricks profile and
    the raw key / nested-session marker, while ``apiKeyHelper`` +
    ``ANTHROPIC_BASE_URL`` + the model override survive so Claude Code still
    authenticates against Databricks via the helper.
    """
    config = ClaudeNativeUcodeConfig(
        env={"ANTHROPIC_BASE_URL": "https://dbc-example.cloud.databricks.com/anthropic"},
        api_key_helper="databricks auth token --host https://dbc-example.cloud.databricks.com",
        model="databricks-claude-opus-4-8",
    )

    # The terminal child strips the ambient Databricks profile plus the raw
    # key / nested-session marker on the helper path.
    env_unset = _claude_terminal_env_unset(config)
    assert "DATABRICKS_CONFIG_PROFILE" in env_unset
    assert "ANTHROPIC_API_KEY" in env_unset
    assert "CLAUDECODE" in env_unset

    # The built terminal env preserves the gateway endpoint and never emits a
    # raw key (routing is via ANTHROPIC_BASE_URL + apiKeyHelper); the Databricks
    # profile is dropped via env_unset, not the built env.
    terminal_env = build_native_claude_terminal_env(config)
    assert terminal_env["ANTHROPIC_BASE_URL"] == (
        "https://dbc-example.cloud.databricks.com/anthropic"
    )
    assert "ANTHROPIC_API_KEY" not in terminal_env
    assert "DATABRICKS_CONFIG_PROFILE" not in terminal_env

    # The gateway model reaches the launch as ``--model`` so Claude Code
    # doesn't start on an Anthropic-direct default the gateway rejects; the
    # apiKeyHelper survives on the config for augment_claude_args to register.
    args = _build_claude_native_base_args(
        reasoning_effort=None,
        model_override=config.model,
        terminal_launch_args=None,
    )
    assert args == ("--model", "databricks-claude-opus-4-8")
    assert config.api_key_helper


@pytest.fixture
def bridge_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """
    Yield a bridge dir the claude-native bridge accepts.

    ``augment_claude_args`` validates the bridge dir against the real
    ``$TMPDIR/omnigent-<uid>/claude-native`` root, so a raw ``tmp_path`` is
    rejected. Point the bridge root and its trusted parent at the test's temp
    dir the way ``tests/test_claude_native_bridge.py`` does.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: Bridge dir under the patched bridge root.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)
    return tmp_path


def _augmented(bridge_dir: Path, *, auto_harness: bool) -> list[str]:
    """Run the runner's own claude-native argv composition for one session shape."""
    from omnigent.claude_native_bridge import augment_claude_args

    note, allowed = _routed_spawn_launch_args(auto_harness)
    return augment_claude_args(
        ("--model", "databricks-claude-sonnet-5"),
        bridge_dir=bridge_dir,
        python_executable="/venv/bin/python",
        append_system_prompt=note,
        allowed_tools=allowed,
    )


def test_auto_harness_launch_names_the_routed_spawn_tool_and_preapproves_it(
    bridge_dir: Path,
) -> None:
    """An auto-harness Claude launch carries the note AND the tool allowlist.

    Both halves of the live failure: the model reported
    ``mcp__omnigent__sys_session_create`` as nonexistent (no note, and the
    schema is deferred behind tool search), and Claude Code's don't-ask mode
    denied the Omnigent MCP call outright (no ``--allowedTools``).
    """
    args = _augmented(bridge_dir, auto_harness=True)

    note = args[args.index("--append-system-prompt") + 1]
    assert "mcp__omnigent__sys_session_create" in note
    assert "mcp__omnigent__sys_agent_list" in note
    # Bare spellings would send the model looking for a tool Claude does not
    # advertise, which is the bug.
    assert "`sys_session_create`" not in note
    allowed = args[args.index("--allowedTools") + 1].split(",")
    assert "mcp__omnigent__sys_session_create" in allowed
    assert "mcp__omnigent__sys_agent_list" in allowed
    assert "mcp__omnigent__sys_session_send" in allowed
    assert set(_ROUTED_SPAWN_ALLOWED_TOOLS) <= set(allowed)


def test_pinned_harness_launch_argv_is_unchanged(bridge_dir: Path) -> None:
    """A pinned session's argv must stay byte-identical to the pre-change one.

    The routed-spawn note and the tool allowlist are additions for auto-harness
    sessions only; leaking either into a pinned launch would change every
    non-routed native session's command line.
    """
    from omnigent.claude_native_bridge import augment_claude_args

    baseline = augment_claude_args(
        ("--model", "databricks-claude-sonnet-5"),
        bridge_dir=bridge_dir,
        python_executable="/venv/bin/python",
    )

    assert _augmented(bridge_dir, auto_harness=False) == baseline
    assert "--append-system-prompt" not in baseline
    assert "--allowedTools" not in baseline


def test_routed_spawn_launch_args_gate_is_off_without_auto_harness() -> None:
    assert _routed_spawn_launch_args(False) == (None, ())
    note, allowed = _routed_spawn_launch_args(True)
    assert note
    assert allowed == _ROUTED_SPAWN_ALLOWED_TOOLS


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({AUTO_HARNESS_LABEL_KEY: "1"}, None, True),
        # The sentinel is replaced once first-message routing resolves a
        # harness, so a session still carrying it is auto-harness too.
        ({}, "auto", True),
        ({}, "claude-native", False),
        ({AUTO_HARNESS_LABEL_KEY: "0"}, None, False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "label-off", "neither"],
)
def test_envelope_metadata_reads_the_auto_harness_flag(
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    from omnigent.runner.session_init_protocol import (
        SESSION_INIT_PROTOCOL_VERSION,
        RunnerSessionInitEnvelope,
    )

    envelope = RunnerSessionInitEnvelope(
        protocol_version=SESSION_INIT_PROTOCOL_VERSION,
        server_version="test",
        session_id="conv_abc",
        agent_id="agent",
        snapshot={
            "created_at": 0,
            "updated_at": 0,
            "labels": labels,
            "harness_override": harness_override,
        },
    )

    assert _claude_launch_metadata_from_envelope(envelope).auto_harness is expected


@pytest.mark.parametrize(
    ("labels", "harness_override", "expected"),
    [
        ({AUTO_HARNESS_LABEL_KEY: "1"}, None, True),
        ({}, "auto", True),
        ({}, "claude-native", False),
        ({}, None, False),
    ],
    ids=["label", "sentinel", "pinned", "neither"],
)
async def test_legacy_metadata_loader_reads_the_auto_harness_flag(
    labels: dict[str, str],
    harness_override: str | None,
    expected: bool,
) -> None:
    """The removable legacy snapshot path must parse the flag too.

    A server predating the init envelope still answers ``GET /v1/sessions``, and
    an auto-harness session launched through it needs the same note.
    """
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"labels": labels, "harness_override": harness_override},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://runner"
    ) as client:
        metadata = await _load_legacy_claude_launch_metadata(client, "conv_abc")

    assert metadata.auto_harness is expected
