"""E2E: a mid-turn steer must steer the running Kimi turn, not queue behind it.

Regression guard for kimi-native steering. Omnigent injects a steer into the
Kimi TUI via :func:`omnigent.kimi_native_bridge.inject_user_message`. An UNFIXED
build commits it with ``Enter`` only; mid-turn, Kimi QUEUES a bare ``Enter``
(shown as ``↑ to edit · ctrl-s to steer immediately``) and ignores it until the
turn ends. The fix also sends ``Ctrl-S``, which flushes the draft into the
running turn (and no-ops while idle).

Real seam: ``KimiNativeExecutor.run_turn`` yields ``TurnComplete`` right after
the paste, so the executor-adapter tears down ``_watch_injections`` immediately
— a steer arriving while Kimi streams lands as a FRESH ``run_turn``, not through
``enqueue_session_message``. The test drives the steer through a fresh
``run_turn`` (the real path) and, parametrized, through
``enqueue_session_message`` (the brief initial-paste window).

Oracle: Kimi merges a steer locally and fires NO new model request until the
blocked turn releases — identically on fixed and unfixed builds — so the mock
cannot tell steered from queued. The queue affordance is therefore the sole
discriminator (see the discriminator assertion, which must not be simplified
away). The test first calibrates that the affordance both appears (a mid-turn
Enter-only submit queues) and disappears (Ctrl-S flushes) on this binary, then
requires it absent after the production steer, then confirms the steer reached
the model conversation.

Opt in with ``OMNIGENT_E2E_KIMI=1``; needs the ``kimi`` CLI (>= 0.41.0, where
``Ctrl-S`` steers) and ``tmux``. Excluded from default ``pytest`` runs
(``--ignore=tests/e2e``)::

    OMNIGENT_E2E_KIMI=1 uv run --no-sync pytest \
        tests/e2e/test_kimi_native_steering_e2e.py -v --timeout=600
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from functools import cache
from pathlib import Path

import httpx
import pytest

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.executor import ExecutorError
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.kimi_native_bridge import write_tmux_target
from tests.e2e.conftest import (
    configure_mock_llm,
    release_mock_gate,
    reset_mock_llm,
    set_fallback_mock_llm,
)

# Kimi >= 0.41.0 binds Ctrl-S to "steer the running turn"; older builds don't,
# so the fix is a no-op there and this test would false-fail a correct tree.
_MIN_KIMI_VERSION = (0, 41, 0)


def _kimi_binary() -> str | None:
    """Resolve the ``kimi`` binary the harness would launch, or ``None``."""
    explicit = resolve_harness_path("kimi")
    if explicit:
        return explicit if Path(explicit).exists() else None
    return shutil.which("kimi")


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _e2e_opt_in() -> bool:
    """Sibling real-CLI convention: opt in with ``OMNIGENT_E2E_KIMI=1`` + tools present."""
    if os.environ.get("OMNIGENT_E2E_KIMI") != "1":
        return False
    return _kimi_binary() is not None and _tmux_available()


_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


@cache
def _kimi_version() -> tuple[tuple[int, int, int] | None, str]:
    """Return ``(release_tuple, "")`` from ``kimi --version``, or ``(None, why)``.

    Rejects a nonzero exit (and OS/timeout failure) BEFORE parsing, so a version
    string can't be scraped out of an error response; *why* carries the diagnostic.
    """
    binary = _kimi_binary()
    if binary is None:
        return None, "kimi binary could not be resolved"
    try:
        proc = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"`{binary} --version` did not run: {exc}"
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        return None, f"`{binary} --version` exited {proc.returncode}: {detail}"
    match = _VERSION_RE.search(proc.stdout) or _VERSION_RE.search(proc.stderr)
    if match is None:
        return None, f"no version in `{binary} --version` output: {proc.stdout.strip()!r}"
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))), ""


pytestmark = [
    pytest.mark.timeout(600, method="signal"),
    pytest.mark.skipif(
        not _e2e_opt_in(),
        reason=(
            "Real-binary e2e: requires OMNIGENT_E2E_KIMI=1 plus the ``kimi`` (or "
            "OMNIGENT_KIMI_PATH) binary and ``tmux`` on PATH. Install kimi via "
            "`curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash`, then re-run "
            "with OMNIGENT_E2E_KIMI=1."
        ),
    ),
]

# Unique token in the initiating prompt so the mock routes this turn to our own
# (blocking) queue regardless of the model string Kimi sends.
_MATCH_TOKEN = "KIMI_MIDTURN_STEER_REPRO_TOKEN"
# The steer marker, kept distinct from the routing token so the "did the steer
# reach the model?" check keys on the steer text, not the echoed prompt.
_STEER_MARK = "APPLY_GUIDANCE_NOW_MARK"
_STEER_TEXT = f"{_STEER_MARK} stop everything and only reply BANANA"
# Calibration marker: a mid-turn Enter-only submission used only to prove the
# queue affordance is live for this Kimi build (distinct from the steer marker).
_CONTROL_MARK = "STEER_ORACLE_CALIBRATION_MARK"
_CONTROL_TEXT = f"{_CONTROL_MARK} calibration only"

# Kimi's queue-pane affordance, shown ONLY for a message QUEUED mid-turn (i.e.
# NOT steered). Distinct from the footer spinner tip about Ctrl-S, which rotates
# in regardless of queue state — so keying on "to steer immediately" tests
# queue-vs-steer, not the mere presence of a Ctrl-S hint.
_QUEUE_AFFORDANCE = "to steer immediately"

_BOOT_TIMEOUT_S = 45.0
_INPUT_READY_TIMEOUT_S = 45.0
_MIDTURN_TIMEOUT_S = 60.0
_AFFORDANCE_TIMEOUT_S = 20.0
_STEER_LLM_TIMEOUT_S = 30.0
_SETTLE_S = 0.5
_POLL_S = 0.5
# Per-command tmux budget; a tmux server starved by a parallel boot can stall
# past 5s while healthy.
_TMUX_TIMEOUT_S = 10.0


def _run_tmux(socket_path: str, *args: str) -> None:
    """Invoke tmux, raising ``RuntimeError`` on a nonzero exit or timeout."""
    label = args[0] if args else "tmux"
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, *args],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        raise RuntimeError(f"tmux {label} failed: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "<no output>"
        raise RuntimeError(f"tmux {label} failed (rc={proc.returncode}): {detail}")


def _capture(socket_path: str, target: str) -> str:
    """Return the visible pane text; ``""`` on any tmux failure (treated as not-ready)."""
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket_path, "capture-pane", "-t", target, "-p"],
            check=False,
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _send_keys(socket_path: str, target: str, *keys: str) -> None:
    _run_tmux(socket_path, "send-keys", "-t", target, *keys)


def _await_pane(
    socket_path: str, target: str, predicate: Callable[[str], bool], timeout_s: float
) -> tuple[bool, str]:
    """Poll captures until *predicate* holds on a NON-EMPTY pane.

    ``_capture`` returns ``""`` on any tmux failure (timeout, OSError, nonzero
    exit), so an empty capture is never fed to *predicate*. A "the affordance is
    gone" check therefore can't be flipped true by a transient tmux hiccup — it
    keeps polling and times out loudly instead. Returns
    ``(matched, last_non_empty_pane)`` so the caller reports what it actually saw.
    """
    deadline = time.monotonic() + timeout_s
    last = ""
    while time.monotonic() < deadline:
        pane = _capture(socket_path, target)
        if pane:
            last = pane
            if predicate(pane):
                return True, pane
        time.sleep(_POLL_S)
    return False, last


def _queue_via_enter_only(socket_path: str, target: str, bridge_dir: Path, text: str) -> None:
    """Submit *text* mid-turn exactly as an UNFIXED build would: clear, paste, Enter (no C-s)."""
    _send_keys(socket_path, target, "C-a")
    _send_keys(socket_path, target, "C-k")
    paste_path = bridge_dir / "calibration_paste.bin"
    paste_path.write_bytes((text + "\n").encode("utf-8"))
    _run_tmux(socket_path, "load-buffer", "-b", "omni-calibration", str(paste_path))
    _run_tmux(socket_path, "paste-buffer", "-p", "-d", "-b", "omni-calibration", "-t", target)
    time.sleep(_SETTLE_S)
    _send_keys(socket_path, target, "Enter")


def _wait_for(predicate: Callable[[], bool], timeout_s: float, *, poll_s: float = _POLL_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def _gate_pending(client: httpx.Client, mock_url: str) -> bool:
    try:
        return bool(client.get(f"{mock_url}/gate/pending").json().get("pending"))
    except httpx.HTTPError:
        return False


def _steer_reached_llm(client: httpx.Client, mock_url: str, mark: str) -> bool:
    """Whether any request the mock captured carries *mark* in its body."""
    try:
        requests = client.get(f"{mock_url}/mock/requests").json().get("requests", [])
    except httpx.HTTPError:
        return False
    return any(mark in json.dumps(req) for req in requests)


async def _drain_run_turn(executor: KimiNativeExecutor, text: str) -> list[object]:
    """Drive one ``run_turn`` to completion, collecting its events."""
    events: list[object] = []
    async for event in executor.run_turn(
        messages=[{"role": "user", "content": text}],
        tools=[],
        system_prompt="",
    ):
        events.append(event)
    return events


@pytest.fixture
def proxy_blind_client() -> Iterator[httpx.Client]:
    """Proxy-blind client: CI forces an egress proxy that must not intercept the loopback mock."""
    with httpx.Client(trust_env=False, timeout=10.0) as client:
        yield client


@pytest.fixture
def kimi_tui(
    mock_llm_server_url: str | None, tmp_path: Path
) -> Iterator[tuple[str, str, Path, str, tuple[int, int, int]]]:
    """Launch the real Kimi TUI in tmux, wired to this session's mock LLM.

    Yields ``(socket_path, tmux_target, bridge_dir, mock_url, kimi_version)``. Kimi
    talks OpenAI Chat Completions to the mock via the CLI's ``KIMI_MODEL_*`` env
    overlay — no real Kimi login or network egress required.
    """
    kimi_bin = _kimi_binary()
    if kimi_bin is None:
        pytest.skip("kimi CLI (or OMNIGENT_KIMI_PATH) is required for the kimi-native steer repro")
    if not _tmux_available():
        pytest.skip("tmux is required for the kimi-native steer repro")
    if mock_llm_server_url is None:
        pytest.skip("mock LLM server required (run without --llm-api-key)")

    version, version_detail = _kimi_version()
    if version is None:
        pytest.skip(f"kimi version undeterminable: {version_detail}")
    if version < _MIN_KIMI_VERSION:
        observed = ".".join(str(part) for part in version)
        minimum = ".".join(str(part) for part in _MIN_KIMI_VERSION)
        pytest.skip(f"kimi Ctrl-S steer needs >= {minimum}; found {observed}")

    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    # Model is supplied entirely by the KIMI_MODEL_* env overlay below.
    (kimi_home / "config.toml").write_text("# model provided via KIMI_MODEL_* env overlay\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    # The tmux socket lives in a short-named temp dir, NOT under the deep pytest
    # tmp_path: an AF_UNIX path over ~104 chars (which the nested pytest path plus
    # the parametrize suffix blows past) fails to bind with "File name too long".
    socket_dir = Path(tempfile.mkdtemp(prefix="okimi-"))
    socket_path = str(socket_dir / "s")
    target = "kimi:0.0"

    env = {
        **os.environ,
        # KIMI_CODE_HOME isolates kimi's own state (config, trust, sessions);
        # HOME is left inherited so binary-resolving launcher shims still find
        # their installed binary.
        "KIMI_CODE_HOME": str(kimi_home),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # KIMI_MODEL_* synthesizes an in-memory provider + model alias and sets it
        # as default_model, so the TUI speaks OpenAI Chat Completions to the mock
        # without a config-file model entry or a real login.
        "KIMI_MODEL_NAME": "mock-kimi-steer",
        "KIMI_MODEL_API_KEY": "mock-key",
        "KIMI_MODEL_PROVIDER_TYPE": "openai",
        "KIMI_MODEL_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(proxy_var, None)
    # Drop any inherited TMUX so this fresh, private tmux server isn't refused as
    # a nested session (``new-session`` exits 1 when $TMUX points elsewhere).
    for tmux_var in ("TMUX", "TMUX_PANE"):
        env.pop(tmux_var, None)

    write_tmux_target(bridge_dir, socket_path=Path(socket_path), tmux_target=target)

    # Enter the cleanup scope BEFORE launching so a failed/partial ``new-session``
    # (timeout, OSError, nonzero exit) still tears down any half-created server and
    # removes the socket dir instead of leaking them.
    try:
        launch = subprocess.run(
            [
                "tmux",
                "-S",
                socket_path,
                "new-session",
                "-d",
                "-s",
                "kimi",
                "-x",
                "200",
                "-y",
                "50",
                kimi_bin,
            ],
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
        if launch.returncode != 0:
            detail = launch.stderr.strip() or launch.stdout.strip() or "<no output>"
            raise RuntimeError(
                f"tmux new-session for kimi failed (rc={launch.returncode}): {detail}"
            )

        # First-run "Trust this folder?" gate: the trust option is pre-selected,
        # so Enter accepts it and mounts the input box. Only send Enter once we
        # positively see the prompt, so a changed startup UI can't eat a stray
        # Enter (an already-mounted input, seen via "context:", needs no Enter).
        saw_gate, pane = _await_pane(
            socket_path,
            target,
            lambda p: "Trust this" in p or "context:" in p,
            _BOOT_TIMEOUT_S,
        )
        assert saw_gate, f"Kimi TUI never showed its trust prompt or input box.\nPane:\n{pane}"
        if "Trust this" in pane:
            _send_keys(socket_path, target, "Enter")
        ready, pane = _await_pane(
            socket_path, target, lambda p: "context:" in p, _INPUT_READY_TIMEOUT_S
        )
        assert ready, f"Kimi TUI never mounted its input box.\nPane:\n{pane}"
        yield socket_path, target, bridge_dir, mock_llm_server_url, version
    finally:
        # kill-server in its own guard so a teardown timeout/OSError can't skip the
        # socket-dir removal or mask the real test result.
        try:
            subprocess.run(
                ["tmux", "-S", socket_path, "kill-server"],
                capture_output=True,
                timeout=_TMUX_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError):
            pass
        finally:
            shutil.rmtree(socket_dir, ignore_errors=True)


@pytest.mark.parametrize("steer_via", ["fresh_run_turn", "enqueue_session_message"])
def test_midturn_steer_is_applied_not_queued(
    kimi_tui: tuple[str, str, Path, str, tuple[int, int, int]],
    proxy_blind_client: httpx.Client,
    steer_via: str,
) -> None:
    """A steer sent mid-turn must steer the running Kimi turn, not queue behind it.

    ``fresh_run_turn`` is the real mid-turn path; ``enqueue_session_message`` covers
    the initial-paste window. Both go through ``inject_user_message`` (paste + Enter
    + Ctrl-S), so narrowing Ctrl-S to the enqueue path alone would fail the
    ``fresh_run_turn`` row here.
    """
    socket_path, target, bridge_dir, mock_url, version = kimi_tui
    version_str = ".".join(str(part) for part in version)

    # The initiating turn's answer BLOCKS (held open server-side) so the steer
    # arrives while the turn is genuinely mid-stream. A default fallback keeps any
    # incidental request answered so nothing else hangs.
    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, "default", "ok")
    configure_mock_llm(
        mock_url,
        [{"text": "REPRO_TURN_DONE", "block": True}],
        match=_MATCH_TOKEN,
    )

    executor = KimiNativeExecutor(bridge_dir=bridge_dir)

    # run_turn injects the initiating user message via the same tmux paste + Enter
    # + Ctrl-S path the web UI uses, then returns (Ctrl-S no-ops while idle).
    asyncio.run(_drain_run_turn(executor, f"Write a very long detailed essay. {_MATCH_TOKEN}"))

    gate_released = False
    try:
        # The turn must be genuinely in flight before we steer; a steer that
        # arrives idle proves nothing about mid-turn behavior.
        midturn = _wait_for(
            lambda: _gate_pending(proxy_blind_client, mock_url), _MIDTURN_TIMEOUT_S
        )
        assert midturn, (
            "Kimi turn never reached the mock LLM / mid-stream state; cannot test a mid-turn "
            f"steer.\nPane:\n{_capture(socket_path, target)}"
        )

        # Calibrate the oracle against THIS live build: a mid-turn Enter-only submit
        # (what an unfixed build sends) must show the queue affordance...
        _queue_via_enter_only(socket_path, target, bridge_dir, _CONTROL_TEXT)
        calibrated, cal_pane = _await_pane(
            socket_path,
            target,
            lambda p: _QUEUE_AFFORDANCE in p and _CONTROL_MARK in p,
            _AFFORDANCE_TIMEOUT_S,
        )
        assert calibrated, (
            "Kimi did not show its queue affordance for a mid-turn Enter-only message, so "
            f"the queued-vs-steered oracle can't be trusted for kimi {version_str} (the "
            f"affordance {_QUEUE_AFFORDANCE!r} may have drifted).\nPane:\n{cal_pane}"
        )

        # ...and a direct Ctrl-S must clear that single queued draft. This proves the
        # affordance both appears and disappears here, AND leaves the queue empty, so
        # the discriminator below only ever depends on Ctrl-S flushing a SINGLE draft
        # — never on a whole-queue flush the fix does not promise. This send is direct
        # tmux, so removing the production Ctrl-S does not affect it.
        _send_keys(socket_path, target, "C-s")
        cleared, cal_pane = _await_pane(
            socket_path, target, lambda p: _QUEUE_AFFORDANCE not in p, _AFFORDANCE_TIMEOUT_S
        )
        assert cleared, (
            f"Direct Ctrl-S did not clear a single queued draft for kimi {version_str}; the "
            f"queue-flush the fix relies on has drifted.\nPane:\n{cal_pane}"
        )

        # Steer mid-turn through the production path (queue now holds one draft).
        if steer_via == "fresh_run_turn":
            events = asyncio.run(_drain_run_turn(executor, _STEER_TEXT))
            steered = not any(isinstance(event, ExecutorError) for event in events)
        else:
            steered = asyncio.run(executor.enqueue_session_message("main", _STEER_TEXT))
        assert steered, f"steer injection via {steer_via} reported failure"

        # DISCRIMINATOR (load-bearing): the queue affordance must be gone. This is the
        # ONLY assertion that separates steered from queued: Kimi merges the steer
        # locally and fires no new model request until the blocked turn releases, so
        # the mock sees identical requests on fixed and unfixed builds and cannot
        # discriminate. Do NOT replace this with a mock-request check. ``_await_pane``
        # requires a NON-EMPTY capture, so a tmux failure can't read as "gone".
        applied, pane = _await_pane(
            socket_path, target, lambda p: _QUEUE_AFFORDANCE not in p, _AFFORDANCE_TIMEOUT_S
        )
        assert applied, (
            "Mid-turn steer was QUEUED, not applied to the running turn: Kimi's queue-pane "
            f"affordance {_QUEUE_AFFORDANCE!r} is still present for kimi {version_str}. Omnigent "
            "committed the steer with Enter and never sent the CLI's Ctrl-S steer key.\n"
            f"Pane:\n{pane}"
        )

        # Corroborate the inject physically happened (guards an absent affordance from
        # meaning a silently-dropped inject rather than a real steer): after releasing
        # the turn, the steer marker must reach the model. This does NOT discriminate
        # steered-vs-queued — the discriminator above is the only leg that does.
        release_mock_gate(mock_url)
        gate_released = True
        reached = _wait_for(
            lambda: _steer_reached_llm(proxy_blind_client, mock_url, _STEER_MARK),
            _STEER_LLM_TIMEOUT_S,
        )
        assert reached, (
            "Steer text never reached the model conversation after the turn released; the "
            f"injection did not join the running turn.\nPane:\n{_capture(socket_path, target)}"
        )
    finally:
        # Always release so a failure mid-flight can't leave the gate pending and
        # contaminate later e2e tests.
        if not gate_released:
            release_mock_gate(mock_url)
