"""E2E: a mid-turn steer must be applied to the running Kimi turn, not queued.

Guards against a mid-turn steer regression: steering a running
``kimi`` (Kimi Code, ``kimi-native``) session from Omnigent does not take
effect immediately. Omnigent injects the steer text into the Kimi CLI's TUI
via :func:`omnigent.kimi_native_bridge.inject_user_message`, which commits the
message with ``Enter`` and *never* sends the CLI's steer key (``Ctrl-S``).
Mid-turn, Kimi treats ``Enter`` as "queue this message for after the turn" and
only ``Ctrl-S`` steers the in-flight turn -- so the steer lands in Kimi's queue
(``↑ to edit · ctrl-s to steer immediately``) instead of steering the running
turn, and is ignored until the current turn ends.

The reproduction drives the REAL user path, not a mock of it:

1. launch the real ``kimi`` TUI in a private tmux pane (exactly as the runner
   does), pointed at this session's mock LLM via the ``KIMI_MODEL_*`` env
   custom-provider overlay,
2. start a turn through the real :class:`~omnigent.inner.kimi_native_executor.
   KimiNativeExecutor` (``run_turn`` -> the same tmux bracketed-paste + Enter
   injection the web UI uses),
3. hold that turn open mid-stream with a ``block``ing mock response, and
4. steer it through the real live-steer entry point
   (``KimiNativeExecutor.enqueue_session_message`` -- what the runner's
   ``_watch_injections`` calls for an in-flight injection),

then assert on what a user sees in the embedded Kimi terminal: the steer must
be applied to the in-flight turn, evidenced by the absence of Kimi's queue-pane
steer affordance (``to steer immediately``). On the buggy build the steer is
queued (the affordance is present) and this test fails; once the bridge also
sends the CLI's ``Ctrl-S`` steer key mid-turn, the queued text flushes into the
running turn and the affordance is gone.

Real-binary e2e, skipped unless the ``kimi`` CLI (0.41.0+) and ``tmux`` are
available. Excluded from default ``pytest`` runs (``--ignore=tests/e2e``).
Invoke with::

    pytest tests/e2e/test_kimi_native_steering_e2e.py -v --timeout=600
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.harness_startup_config import resolve_harness_path
from omnigent.inner.kimi_native_executor import KimiNativeExecutor
from omnigent.kimi_native_bridge import write_tmux_target
from tests.e2e.conftest import (
    configure_mock_llm,
    release_mock_gate,
    reset_mock_llm,
    set_fallback_mock_llm,
)

pytestmark = [pytest.mark.timeout(600, method="signal")]

# A deliberately-unique token in the initiating prompt so the mock routes this
# turn to our own (blocking) queue regardless of what model string Kimi sends.
_MATCH_TOKEN = "KIMI_MIDTURN_STEER_REPRO_TOKEN"
# A steer marker DISTINCT from the routing token: the "did the steer reach the
# TUI?" guard must key on the steer message specifically, not on the echoed
# initiating prompt (which also carries the routing token). Kept free of
# substrings that overlap the routing token so neither check aliases the other.
_STEER_MARK = "APPLY_GUIDANCE_NOW_MARK"
_STEER_TEXT = f"{_STEER_MARK} stop everything and only reply BANANA"

# Kimi's queue-pane affordance, shown ONLY when a message is QUEUED mid-turn
# (i.e. NOT steered). Distinct from the footer spinner tip "ctrl-s to add
# guidance without waiting for the turn to finish", which rotates in regardless
# of queue state -- so keying on "to steer immediately" tests queue-vs-steer,
# not the mere presence of a Ctrl-S hint.
_QUEUE_AFFORDANCE = "to steer immediately"

_BOOT_TIMEOUT_S = 45.0
_INPUT_READY_TIMEOUT_S = 45.0
_MIDTURN_TIMEOUT_S = 60.0
_STEER_VISIBLE_TIMEOUT_S = 20.0
_STEER_SETTLE_S = 2.0
_POLL_S = 0.5

# Proxy-blind client: CI forces an egress proxy that must not intercept the
# loopback mock server.
_client = httpx.Client(trust_env=False, timeout=10.0)


def _kimi_binary() -> str | None:
    """Resolve the ``kimi`` binary the harness would launch, or ``None``."""
    explicit = resolve_harness_path("kimi")
    if explicit:
        return explicit if Path(explicit).exists() else None
    return shutil.which("kimi")


def _tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _capture(socket_path: str, target: str) -> str:
    """Return the visible text of the Kimi tmux pane."""
    return subprocess.run(
        ["tmux", "-S", socket_path, "capture-pane", "-t", target, "-p"],
        capture_output=True,
        text=True,
    ).stdout


def _send_keys(socket_path: str, target: str, *keys: str) -> None:
    subprocess.run(
        ["tmux", "-S", socket_path, "send-keys", "-t", target, *keys],
        check=False,
    )


def _wait_for(predicate, timeout_s: float, *, poll_s: float = _POLL_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_s)
    return False


def _gate_pending(mock_url: str) -> bool:
    try:
        return bool(_client.get(f"{mock_url}/gate/pending").json().get("pending"))
    except httpx.HTTPError:
        return False


@pytest.fixture
def kimi_tui(
    mock_llm_server_url: str | None, tmp_path: Path
) -> Iterator[tuple[str, str, Path, str]]:
    """Launch the real Kimi TUI in tmux, wired to this session's mock LLM.

    Yields ``(socket_path, tmux_target, bridge_dir, mock_url)``. The Kimi
    process is configured through the CLI's ``KIMI_MODEL_*`` env overlay so it
    speaks OpenAI Chat Completions to the mock server -- no real Kimi login or
    network egress required.
    """
    kimi_bin = _kimi_binary()
    if kimi_bin is None:
        pytest.skip("kimi CLI (or OMNIGENT_KIMI_PATH) is required for the kimi-native steer repro")
    if not _tmux_available():
        pytest.skip("tmux is required for the kimi-native steer repro")
    if mock_llm_server_url is None:
        pytest.skip("mock LLM server required (run without --llm-api-key)")

    kimi_home = tmp_path / "kimi-home"
    kimi_home.mkdir()
    # Model is supplied entirely by the KIMI_MODEL_* env overlay below.
    (kimi_home / "config.toml").write_text("# model provided via KIMI_MODEL_* env overlay\n")
    workspace = tmp_path / "ws"
    workspace.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    socket_path = str(tmp_path / "tmux.sock")
    target = "kimi:0.0"

    env = {
        **os.environ,
        "KIMI_CODE_HOME": str(kimi_home),
        "HOME": str(home),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # KIMI_MODEL_* synthesizes an in-memory provider + model alias and sets
        # it as default_model, so the TUI talks OpenAI Chat Completions to the
        # mock without a config-file model entry or a real login.
        "KIMI_MODEL_NAME": "mock-kimi-steer",
        "KIMI_MODEL_API_KEY": "mock-key",
        "KIMI_MODEL_PROVIDER_TYPE": "openai",
        "KIMI_MODEL_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    for proxy_var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(proxy_var, None)

    write_tmux_target(bridge_dir, socket_path=Path(socket_path), tmux_target=target)

    subprocess.run(
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
        check=True,
    )
    try:
        # First-run "Trust this folder?" gate: the "Trust" option is
        # pre-selected, so Enter accepts it and mounts the input box.
        _wait_for(lambda: "Trust this" in _capture(socket_path, target), _BOOT_TIMEOUT_S)
        _send_keys(socket_path, target, "Enter")
        ready = _wait_for(
            lambda: "context:" in _capture(socket_path, target), _INPUT_READY_TIMEOUT_S
        )
        assert ready, (
            f"Kimi TUI never mounted its input box.\nPane:\n{_capture(socket_path, target)}"
        )
        yield socket_path, target, bridge_dir, mock_llm_server_url
    finally:
        subprocess.run(["tmux", "-S", socket_path, "kill-server"], capture_output=True)


def test_midturn_steer_is_applied_not_queued(
    kimi_tui: tuple[str, str, Path, str],
    mock_llm_server_url: str | None,
) -> None:
    """A steer sent mid-turn must steer the running Kimi turn, not queue behind it.

    Drives the real Omnigent steer path against a live Kimi TUI held mid-stream
    and asserts the steer is applied to the in-flight turn (Kimi's queue-pane
    steer affordance is absent). Fails on the buggy build, where Omnigent
    commits the steer with Enter -- which Kimi queues -- and never sends the
    CLI's ``Ctrl-S`` steer key.
    """
    socket_path, target, bridge_dir, mock_url = kimi_tui

    # The initiating turn's answer BLOCKS (held open server-side) so the steer
    # arrives while the turn is genuinely mid-stream. A fallback keeps any
    # incidental request answered so nothing else hangs.
    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, "default", "ok")
    configure_mock_llm(
        mock_url,
        [{"text": "REPRO_TURN_DONE", "block": True}],
        match=_MATCH_TOKEN,
    )

    executor = KimiNativeExecutor(bridge_dir=bridge_dir)

    async def _start_turn() -> None:
        # run_turn injects the initiating user message via the same tmux
        # bracketed-paste + Enter path the web UI uses, then returns.
        async for _ in executor.run_turn(
            messages=[
                {
                    "role": "user",
                    "content": f"Write a very long detailed essay. {_MATCH_TOKEN}",
                }
            ],
            tools=[],
            system_prompt="",
        ):
            pass

    asyncio.run(_start_turn())

    # The turn must be genuinely in flight before we steer, otherwise a steer
    # that arrives idle proves nothing about mid-turn behavior.
    midturn = _wait_for(lambda: _gate_pending(mock_url), _MIDTURN_TIMEOUT_S)
    assert midturn, (
        "Kimi turn never reached the mock LLM / mid-stream state; cannot test "
        f"a mid-turn steer.\nPane:\n{_capture(socket_path, target)}"
    )

    # Steer mid-turn through the real live-steer entry point (what the runner's
    # _watch_injections calls for an in-flight injection).
    steered = asyncio.run(executor.enqueue_session_message("main", _STEER_TEXT))
    assert steered, "enqueue_session_message reported the steer injection failed"

    # The steer text must reach the pane at all (proves the inject path ran),
    # whether it lands queued (bug) or steered (fixed).
    landed = _wait_for(
        lambda: _STEER_MARK in _capture(socket_path, target),
        _STEER_VISIBLE_TIMEOUT_S,
    )
    assert landed, (
        "Steer text never appeared in the Kimi pane; the injection did not "
        f"reach the TUI.\nPane:\n{_capture(socket_path, target)}"
    )
    time.sleep(_STEER_SETTLE_S)

    pane = _capture(socket_path, target)
    try:
        # BUG: Omnigent commits the steer with Enter, which Kimi queues
        # mid-turn -- the pane shows the queue affordance
        # "↑ to edit · ctrl-s to steer immediately" and the steer is NOT applied
        # to the running turn. The fix additionally sends the CLI's Ctrl-S steer
        # key, which flushes the queued text into the in-flight turn, so the
        # affordance disappears.
        assert _QUEUE_AFFORDANCE not in pane, (
            "Mid-turn steer was QUEUED, not applied to the running turn: Kimi's "
            f"queue-pane affordance {_QUEUE_AFFORDANCE!r} is present. Omnigent "
            "committed the steer with Enter and never sent the CLI's Ctrl-S "
            f"steer key.\nPane:\n{pane}"
        )
    finally:
        # Let the held-open turn drain so teardown is clean.
        release_mock_gate(mock_url)
