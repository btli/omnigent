"""Unit tests for ``NativeInterruptRunner`` (PR 1.6 interrupt/stop seam).

These drive the runner class directly with lightweight fakes, complementing the
HTTP-path tests in ``test_app_sessions_native_events_lifecycle.py`` /
``test_app_sessions_native_supervision.py`` (which POST to ``/events`` and patch
the bridge-module control functions). The focus here is the registry dispatch
and the descriptor-collapsed uniform handlers: which harnesses route where, the
opencode interrupt (native ``/interrupt``) dispatch, the antigravity no-handler
fall-through contract, and the 503 mapping.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from fastapi.responses import Response

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.harnesses.codex_native import app_server as codex_app_server
from omnigent.harnesses.codex_native.bridge import CodexNativeBridgeState
from omnigent.harnesses.opencode_native import app_server as opencode_app_server
from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from omnigent.harnesses.opencode_native.client import OpenCodeClientError
from omnigent.runner import create_runner_app, subagent_work
from omnigent.runner.native import orchestration
from omnigent.runner.native.interrupt import NativeInterruptRunner
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.native_helpers import _harness_spec


@dataclass
class _FakeAck:
    """Stand-in for ``_SubagentDeliveryAck``."""

    delivered: bool = True
    entry: object | None = None
    reason: str = "delivered"


class _FakeTerminalRegistry:
    def __init__(self) -> None:
        self.closed: list[str] = []

    def list_for_conversation(self, conv_id: str) -> list[Any]:
        return []


class _FakeResourceRegistry:
    def __init__(self) -> None:
        self.terminal_registry = _FakeTerminalRegistry()

    async def close_terminal(self, conv_id: str, terminal_id: str) -> bool:
        return True


def _make_runner(**overrides: Any) -> tuple[NativeInterruptRunner, dict[str, Any]]:
    """Build a runner with recording fakes; return it plus a capture dict."""
    captured: dict[str, Any] = {
        "published": [],
        "wakes": [],
        "wake_calls": [],
        "superseded": [],
        # The work_id the runner's ``subagent_work_id_for_session`` callback
        # reports; flip it to simulate a newer send replacing the dispatch.
        "current_work_id": None,
    }

    def _publish(conv_id: str, event: dict[str, Any]) -> None:
        captured["published"].append((conv_id, event))

    def _mark_and_wake(
        child_session_id: str,
        *,
        status: str,
        output: str | None,
        only_if_work_id: str | None = None,
        confirmed_cancellation: bool = False,
    ) -> _FakeAck:
        if only_if_work_id is not None and only_if_work_id != captured["current_work_id"]:
            # A newer dispatch replaced the entry: drop the delayed op untouched.
            captured["superseded"].append((child_session_id, status, only_if_work_id))
            return _FakeAck(delivered=False, reason="superseded_dispatch")
        captured["wakes"].append((child_session_id, status, output))
        captured["wake_calls"].append(
            {
                "child_session_id": child_session_id,
                "status": status,
                "output": output,
                "only_if_work_id": only_if_work_id,
                "confirmed_cancellation": confirmed_cancellation,
            }
        )
        return _FakeAck()

    async def _codex_bridge_state(
        conv_id: str, *, action: str, **_kw: Any
    ) -> tuple[Any | None, Path]:
        return None, Path("unused-bridge-dir")

    def _client_safe(exc: BaseException, *, context: str) -> str:
        return f"safe:{context}"

    def _work_id_for_session(conv_id: str) -> str | None:
        return captured["current_work_id"]

    kwargs: dict[str, Any] = {
        "server_client": SimpleNamespace(),
        "resource_registry": _FakeResourceRegistry(),
        "publish_event": _publish,
        "mark_subagent_terminal_and_wake": _mark_and_wake,
        "session_sub_agent_names": {},
        "codex_bridge_state_for_session": _codex_bridge_state,
        "client_safe_error_detail": _client_safe,
        "logger": logging.getLogger("test.interrupt"),
        "subagent_work_id_for_session": _work_id_for_session,
    }
    kwargs.update(overrides)
    return NativeInterruptRunner(**kwargs), captured


def test_native_cancel_capability_follows_stop_registry() -> None:
    """Parent cancel capability must track ``_UNIFORM_STOP``, Claude, and dispatched Codex."""
    from omnigent.native.native_coding_agents import NATIVE_CODING_AGENTS
    from omnigent.runner.native.interrupt import (
        _UNIFORM_STOP,
        native_cancel_capability,
    )

    for agent in NATIVE_CODING_AGENTS:
        capability = native_cancel_capability(agent.wrapper_label)
        if agent.key in ("claude", "codex") or agent.key in _UNIFORM_STOP:
            assert capability == "stop", agent.key
        else:
            assert capability == "best_effort", agent.key
        if agent.subagent_wrapper_label:
            # A Codex-spawned thread shares its parent's app-server: nothing to kill.
            expected = "best_effort" if agent.key == "codex" else capability
            assert native_cancel_capability(agent.subagent_wrapper_label) == expected

    assert native_cancel_capability(None) == "inprocess"
    assert native_cancel_capability("not-a-native-wrapper") == "inprocess"


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["antigravity-native", "claude-sdk", None])
async def test_no_handler_harnesses_return_none(harness: str | None) -> None:
    """Harnesses without an interrupt/stop handler return None (caller falls through)."""
    runner, _ = _make_runner()
    assert await runner.interrupt(harness, "conv_x") is None
    assert await runner.stop(harness, "conv_x") is None


_OPENCODE_STATE = SimpleNamespace(
    server_base_url="http://127.0.0.1:9999/",
    opencode_session_id="ses_oc",
    auth_secret="secret",
    workspace="/ws",
)


class _FakeOpenCodeClient:
    """Records the native interrupt, raising *error* when one is given."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.interrupt_calls: list[str] = []
        self.closed = False

    async def interrupt(self, session_id: str) -> bool:
        self.interrupt_calls.append(session_id)
        if self.error is not None:
            raise self.error
        return True

    async def aclose(self) -> None:
        self.closed = True


def _patch_opencode_bridge(
    monkeypatch: pytest.MonkeyPatch, *, state: Any, client: _FakeOpenCodeClient
) -> dict[str, Any]:
    """Serve *state* as the bridge state and *client* as its client; return the routing kwargs."""
    routed: dict[str, Any] = {}

    def _client_for_state(**kwargs: Any) -> _FakeOpenCodeClient:
        routed.update(kwargs)
        return client

    monkeypatch.setattr(opencode_bridge, "read_bridge_state", lambda bridge_dir: state)
    monkeypatch.setattr(opencode_app_server, "client_for_state", _client_for_state)
    return routed


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["interrupt", "stop"])
async def test_opencode_interrupt_stops_native_turn(
    monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    """Interrupt and stop end the turn on OpenCode's own server and record it for the dispatch.

    The pending record lets a bare idle or the grace timer settle the dispatch as
    cancelled; the handler itself wakes nothing.
    """
    client = _FakeOpenCodeClient()
    routed = _patch_opencode_bridge(monkeypatch, state=_OPENCODE_STATE, client=client)
    runner, captured = _make_runner()
    captured["current_work_id"] = "work_oc"

    resp = await getattr(runner, method)("opencode-native", "conv_oc")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert client.interrupt_calls == ["ses_oc"] and client.closed
    assert routed == {
        "base_url": "http://127.0.0.1:9999/",
        "auth_secret": "secret",
        "directory": "/ws",
    }
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_oc") == (True, "work_oc")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "error", "expected_status"),
    [
        pytest.param(None, None, None, id="no-bridge-state"),
        pytest.param(_OPENCODE_STATE, OpenCodeClientError("boom"), 503, id="client-error"),
        pytest.param(_OPENCODE_STATE, httpx.ReadTimeout("slow"), 503, id="transport-error"),
    ],
)
async def test_opencode_interrupt_fallbacks(
    monkeypatch: pytest.MonkeyPatch,
    state: Any,
    error: Exception | None,
    expected_status: int | None,
) -> None:
    """Missing bridge state falls through; a failed interrupt is a 503."""
    client = _FakeOpenCodeClient(error)
    _patch_opencode_bridge(monkeypatch, state=state, client=client)
    runner, captured = _make_runner()

    resp = await runner.interrupt("opencode-native", "conv_oc")

    if expected_status is None:
        assert resp is None
    else:
        assert resp is not None and resp.status_code == expected_status
        assert json.loads(bytes(resp.body))["error"] == "opencode_native_interrupt_failed"
    assert client.closed is (state is not None)
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_oc")[0] is False


@pytest.mark.asyncio
async def test_uniform_interrupt_defers_parent_wake_until_outcome_known(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt injects the Escape but must not report a terminal status yet.

    Injecting an Escape does not confirm the agent stopped, so no ``cancelled``
    may be delivered until the harness's terminal edge or the grace timer
    resolves the outcome.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    calls: list[Any] = []

    def _inject(bridge_dir: Any, *, timeout_s: float) -> None:
        calls.append((bridge_dir, timeout_s))

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", _inject)

    runner, captured = _make_runner()
    resp = await runner.interrupt("goose-native", "conv_g")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == [("dir/conv_g", 1.0)]
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_g")[0] is True
    # Consumed: a second take finds nothing and the grace timer is disarmed.
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_interrupt_grace_timer_delivers_unconfirmed_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no terminal edge after the interrupt, the grace timer reports cancelled."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_g"  # a live dispatch to bind the cancel to
    resp = await runner.interrupt("goose-native", "conv_g")
    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == []

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert runner.take_pending_interrupt("conv_g")[0] is False


@pytest.mark.asyncio
async def test_grace_timer_does_not_cancel_superseded_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An old interrupt's grace timer must not cancel a newer dispatch.

    Interrupt turn A, then a new send registers turn B on the same child before
    A's timer fires. The timer is bound to A's ``work_id``, so when it fires the
    newer dispatch B is left untouched and can still complete — instead of B
    being cancelled and its result lost.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # defers, bound to work_A
    # A new send reuses the child session before the timer fires.
    captured["current_work_id"] = "work_B"

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "the newer dispatch must not be cancelled"
    assert captured["superseded"] == [("conv_g", "cancelled", "work_A")]


@pytest.mark.asyncio
async def test_resolve_pending_interrupt_only_consumes_matching_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pending interrupt resolves only for its own dispatch, else is dropped.

    An idle for a NEWER dispatch (the interrupted one exited or was superseded)
    must not consume the stale pending — for a legacy harness that idle is the
    new dispatch's completion. ``resolve_pending_interrupt`` reports pending
    only on a work_id match; otherwise it drops the stale record and reports
    not-pending so the caller handles the idle normally.
    """
    import omnigent.harnesses.goose_native.bridge as goose_bridge

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # pending bound to work_A

    # An idle arrives for a NEWER dispatch: the pending is stale and must be
    # dropped, letting the idle be handled as work_B's own outcome.
    resolved, work_id = runner.resolve_pending_interrupt("conv_g", "work_B")
    assert (resolved, work_id) == (False, None)
    assert runner.take_pending_interrupt("conv_g")[0] is False  # stale record dropped

    # A matching dispatch's idle DOES resolve its interrupt.
    captured["current_work_id"] = "work_C"
    await runner.interrupt("goose-native", "conv_h")
    resolved, work_id = runner.resolve_pending_interrupt("conv_h", "work_C")
    assert (resolved, work_id) == (True, "work_C")


@pytest.mark.asyncio
async def test_repeated_interrupt_on_reused_child_gives_new_dispatch_its_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale pending interrupt must not deny a new dispatch its cancellation.

    Interrupt A and leave its pending record (its launch was reaped before the
    timer fired). Reuse the child for B and interrupt it: B must get its OWN
    pending record and grace timer — the earlier record for A is replaced, not
    dedup-suppressed — so B still has a cancellation fallback and its parent is
    not left waiting indefinitely.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.05)

    runner, captured = _make_runner()
    captured["current_work_id"] = "work_A"
    await runner.interrupt("goose-native", "conv_g")  # A's pending (work_A) lingers
    # The child is reused for dispatch B and interrupted before A's timer fires.
    captured["current_work_id"] = "work_B"
    await runner.interrupt("goose-native", "conv_g")  # must replace A with work_B

    await asyncio.sleep(0.2)
    # B receives its OWN grace-period cancellation (bound to work_B, delivered),
    # rather than being suppressed by A's stale record.
    assert captured["wakes"] == [("conv_g", "cancelled", None)]
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_grace_timer_skips_cancel_when_dispatch_unbound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An interrupt with no bound dispatch must not cancel anything on timeout.

    If no work entry existed when the interrupt fired (e.g. a runner restart had
    not recovered it), the timer captures ``None`` and cannot bind. Delivering a
    cancel then could settle a newer send that reused the child, so the timer
    must skip entirely — the restart recovery scan owns unbound children.
    """
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    captured["current_work_id"] = None  # no dispatch to bind to
    await runner.interrupt("goose-native", "conv_g")

    await asyncio.sleep(0.1)
    assert captured["wakes"] == [], "an unbound interrupt must deliver no cancel"
    assert captured["superseded"] == []


@pytest.mark.asyncio
async def test_resolved_interrupt_disarms_grace_timer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A terminal edge that consumed the pending interrupt silences the timer."""
    import asyncio

    import omnigent.harnesses.goose_native.bridge as goose_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    monkeypatch.setattr(goose_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(goose_bridge, "inject_interrupt", lambda bridge_dir, *, timeout_s: None)
    monkeypatch.setattr(interrupt_mod, "_NATIVE_INTERRUPT_CANCEL_GRACE_S", 0.02)

    runner, captured = _make_runner()
    await runner.interrupt("goose-native", "conv_g")
    assert runner.take_pending_interrupt("conv_g")[0] is True

    await asyncio.sleep(0.1)
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_pi_interrupt_uses_enqueue_without_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's uniform interrupt uses enqueue_interrupt with no timeout kwarg."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[Any] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.interrupt("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert calls == ["dir/conv_p"]


@pytest.mark.asyncio
async def test_uniform_interrupt_bridge_error_returns_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A RuntimeError from the bridge inject maps to a 503 with the error code."""
    import json

    import omnigent.harnesses.qwen_native.bridge as qwen_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(qwen_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(qwen_bridge, "inject_interrupt", _boom)

    runner, captured = _make_runner()
    resp = await runner.interrupt("qwen-native", "conv_q")

    assert resp is not None and resp.status_code == 503
    body = json.loads(bytes(resp.body))
    assert body["error"] == "qwen_native_interrupt_failed"
    assert body["detail"] == "safe:qwen-native interrupt"
    # No parent wake on failure.
    assert captured["wakes"] == []


@pytest.mark.asyncio
async def test_uniform_stop_kills_tears_down_and_goes_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A uniform stop kills the bridge, publishes idle, and wakes the parent."""
    import omnigent.harnesses.cursor_native.bridge as cursor_bridge

    killed: list[Any] = []
    monkeypatch.setattr(cursor_bridge, "bridge_dir_for_session_id", lambda conv: f"dir/{conv}")
    monkeypatch.setattr(
        cursor_bridge,
        "kill_session",
        lambda bridge_dir, *, timeout_s: killed.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    # A stop that follows an unresolved interrupt settles it: the kill is
    # confirmed, so no grace-timer cancel may fire later.
    runner._pending_interrupts["conv_c"] = 0.0
    resp = await runner.stop("cursor-native", "conv_c")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert runner.take_pending_interrupt("conv_c")[0] is False
    assert killed == [("dir/conv_c", 1.0)]
    idle = [e for _, e in captured["published"] if e.get("status") == "idle"]
    assert idle == [{"type": "session.status", "status": "idle"}]
    assert captured["wakes"] == [("conv_c", "cancelled", None)]
    assert captured["wake_calls"][-1]["confirmed_cancellation"] is True


@pytest.mark.asyncio
async def test_uniform_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed kill returns 503 and does NOT publish idle (no lie to the UI)."""
    import json

    import omnigent.harnesses.hermes_native.bridge as hermes_bridge

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        raise RuntimeError("tmux target is not advertised")

    monkeypatch.setattr(hermes_bridge, "bridge_dir_for_session_id", lambda conv: "d")
    monkeypatch.setattr(hermes_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("hermes-native", "conv_h")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "hermes_native_stop_failed"
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_codex_and_pi_stop_route_to_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """codex/pi have no distinct stop — stop() routes to their interrupt handler."""
    import omnigent.harnesses.pi_native.bridge as pi_bridge

    calls: list[str] = []
    monkeypatch.setattr(pi_bridge, "bridge_dir_for_session_id", lambda conv: conv)
    monkeypatch.setattr(
        pi_bridge, "enqueue_interrupt", lambda bridge_dir: calls.append(bridge_dir)
    )

    runner, _ = _make_runner()
    resp = await runner.stop("pi-native", "conv_p")

    assert isinstance(resp, Response) and resp.status_code == 204
    # The interrupt path ran (enqueue_interrupt), not a kill_session.
    assert calls == ["conv_p"]


@pytest.mark.asyncio
async def test_codex_interrupt_noop_when_no_bridge_state() -> None:
    """codex interrupt returns 204 when there is no live bridge state."""
    runner, _ = _make_runner()  # default codex_bridge_state returns None
    resp = await runner.interrupt("codex-native", "conv_cx")
    assert isinstance(resp, Response) and resp.status_code == 204


@pytest.mark.asyncio
async def test_claude_stop_is_idempotent_without_advertised_tmux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An already-absent Claude pane still completes stop teardown."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _absent(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise claude_bridge.TmuxSessionNotAdvertised("not advertised")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _absent)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert captured["wakes"] == [("conv_cn", "cancelled", None)]
    assert captured["wake_calls"][-1]["confirmed_cancellation"] is True


@pytest.mark.asyncio
async def test_claude_stop_kill_failure_returns_503_without_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Claude kill ``RuntimeError`` is 503; only ``TmuxSessionNotAdvertised`` is 204."""
    import json

    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        del server_client, session_id
        return "bridge123"

    def _boom(bridge_dir: Any, *, timeout_s: float) -> None:
        del bridge_dir, timeout_s
        raise RuntimeError("tmux kill-session failed: connection refused")

    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bridge_id: bridge_id)
    monkeypatch.setattr(claude_bridge, "kill_session", _boom)

    runner, captured = _make_runner()
    resp = await runner.stop("claude-native", "conv_cn")

    assert resp is not None and resp.status_code == 503
    assert json.loads(bytes(resp.body))["error"] == "claude_native_stop_failed"
    assert captured["wakes"] == []
    assert [e for _, e in captured["published"] if e.get("status") == "idle"] == []


@pytest.mark.asyncio
async def test_claude_interrupt_resolves_bridge_id_and_injects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """claude interrupt resolves the bridge id and injects, deferring the wake."""
    import omnigent.harnesses.claude_native.bridge as claude_bridge
    from omnigent.runner.native import interrupt as interrupt_mod

    async def _fake_bridge_id(*, server_client: Any, session_id: str) -> str:
        return f"bid-{session_id}"

    injected: list[Any] = []
    monkeypatch.setattr(interrupt_mod, "_claude_native_bridge_id_for_session", _fake_bridge_id)
    monkeypatch.setattr(claude_bridge, "bridge_dir_for_bridge_id", lambda bid: f"dir/{bid}")
    monkeypatch.setattr(
        claude_bridge,
        "inject_interrupt",
        lambda bridge_dir, *, timeout_s: injected.append((bridge_dir, timeout_s)),
    )

    runner, captured = _make_runner()
    resp = await runner.interrupt("claude-native", "conv_cl")

    assert isinstance(resp, Response) and resp.status_code == 204
    assert injected == [("dir/bid-conv_cl", 1.0)]
    # No optimistic 'cancelled': the outcome is unknown until an edge lands.
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt("conv_cl")[0] is True


class _RecordingCodexClient:
    """Codex app-server client double that records JSON-RPC requests."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    async def connect(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        return {}

    async def close(self) -> None:
        return None


class _RecordingAppServer:
    """Registered Codex app-server double; ``close`` is the process teardown."""

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


class _OneTerminalList(_FakeTerminalRegistry):
    """Terminal registry listing the codex tmux terminal for one session."""

    def __init__(self, conv_id: str) -> None:
        super().__init__()
        self._conv_id = conv_id

    def list_for_conversation(self, conv_id: str) -> list[Any]:
        if conv_id != self._conv_id:
            return []
        return [SimpleNamespace(terminal_name="codex", session_key="main")]


class _OneTerminalRegistry(_FakeResourceRegistry):
    """Resource registry exposing the codex tmux terminal for one session."""

    def __init__(self, conv_id: str) -> None:
        super().__init__()
        self.closed_terminals: list[str] = []
        self.terminal_registry = _OneTerminalList(conv_id)

    async def close_terminal(self, conv_id: str, terminal_id: str) -> bool:
        self.closed_terminals.append(terminal_id)
        return True


def _codex_runner_with_live_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    conv_id: str,
    *,
    sub_agent: bool,
) -> tuple[NativeInterruptRunner, dict[str, Any], _RecordingCodexClient, _OneTerminalRegistry]:
    """Build a runner whose codex session has an active turn and a registered app-server."""
    state = CodexNativeBridgeState(
        session_id=conv_id,
        socket_path="ws://127.0.0.1:43299",
        thread_id="thread_child",
        codex_home=str(tmp_path / "codex-home"),
        active_turn_id="turn_child",
    )

    async def _bridge_state(
        conv: str, *, action: str, **_kw: Any
    ) -> tuple[CodexNativeBridgeState | None, Path]:
        return (state if conv == conv_id else None), tmp_path

    client = _RecordingCodexClient()
    monkeypatch.setattr(codex_app_server, "client_for_transport", lambda *_a, **_k: client)
    registry = _OneTerminalRegistry(conv_id)
    runner, captured = _make_runner(
        codex_bridge_state_for_session=_bridge_state,
        resource_registry=registry,
        session_sub_agent_names={conv_id: "codex_impl"} if sub_agent else {},
    )
    return runner, captured, client, registry


@pytest.mark.asyncio
async def test_codex_subagent_stop_interrupts_turn_then_kills_app_server_and_terminal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Journey: a parent's sys_cancel_task hard-stops its codex-native sub-agent.

    The stop must interrupt the live turn, terminate the Codex app-server and its
    tmux terminal, and hand the parent a confirmed ``cancelled`` outcome.
    """
    conv_id = "conv_codex_child"
    app_server = _RecordingAppServer()
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, conv_id, app_server)
    runner, captured, client, registry = _codex_runner_with_live_turn(
        monkeypatch, tmp_path, conv_id, sub_agent=True
    )

    resp = await runner.stop("codex-native", conv_id)

    assert isinstance(resp, Response) and resp.status_code == 204
    assert client.requests == [
        ("turn/interrupt", {"threadId": "thread_child", "turnId": "turn_child"})
    ]
    assert app_server.closed is True
    assert conv_id not in orchestration._AUTO_CODEX_APP_SERVERS
    assert registry.closed_terminals == [terminal_resource_id("codex", "main")]
    assert {"type": "session.status", "status": "idle"} in [e for _, e in captured["published"]]
    assert captured["wakes"] == [(conv_id, "cancelled", None)]
    assert captured["wake_calls"][-1]["confirmed_cancellation"] is True
    # The kill settled the outcome, so no grace-timer cancel stays armed.
    assert runner.take_pending_interrupt(conv_id)[0] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event", "sub_agent"),
    [
        pytest.param("interrupt", True, id="ui-stop-on-subagent"),
        pytest.param("interrupt", False, id="ui-stop-top-level"),
        pytest.param("stop", False, id="stop-session-top-level"),
    ],
)
async def test_codex_ui_stop_and_top_level_stop_only_interrupt_the_turn(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    event: str,
    sub_agent: bool,
) -> None:
    """Journey: a user presses Stop on a codex-native chat; only the turn is interrupted."""
    conv_id = "conv_codex_ui"
    app_server = _RecordingAppServer()
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, conv_id, app_server)
    runner, captured, client, registry = _codex_runner_with_live_turn(
        monkeypatch, tmp_path, conv_id, sub_agent=sub_agent
    )

    dispatch = runner.interrupt if event == "interrupt" else runner.stop
    resp = await dispatch("codex-native", conv_id)

    assert isinstance(resp, Response) and resp.status_code == 204
    assert client.requests == [
        ("turn/interrupt", {"threadId": "thread_child", "turnId": "turn_child"})
    ]
    assert app_server.closed is False
    assert registry.closed_terminals == []
    assert captured["wakes"] == []
    assert runner.take_pending_interrupt(conv_id)[0] is True


def _empty_ok_server_client() -> httpx.AsyncClient:
    """Server client answering every request with an empty 200, like ``NullServerClient``."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
        base_url="http://server",
    )


async def _grok_runner_events(
    *,
    sub_agent: bool,
    event_type: str,
    turn_in_flight: bool,
) -> tuple[
    _FakeProcessManager, _ScriptedHarnessClient, int, subagent_work._SubagentWorkEntry | None
]:
    """POST one control event to a runner hosting a grok ACP session."""
    conv_id = "conv_grok_child" if sub_agent else "conv_grok_top"
    spec = _harness_spec("grok")

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        return spec

    harness_client = _ScriptedHarnessClient([])
    pm = _FakeProcessManager(harness_client)
    server_client = _empty_ok_server_client()
    app = create_runner_app(
        # The fake implements the process-manager calls the runner makes.
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_resolver,
        server_client=server_client,
    )
    if sub_agent:
        subagent_work.register_subagent_work(
            parent_session_id="conv_grok_parent",
            child_session_id=conv_id,
            agent="grok_impl",
            title="work",
        )
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "agent_grok"}
            )
            assert created.status_code == 201, created.text
            await pm.get_client(conv_id, "grok")
            if turn_in_flight:
                # A stream-mode turn: the harness owns the live ACP prompt.
                app.state.active_turns[conv_id] = None
            resp = await client.post(
                f"/v1/sessions/{conv_id}/events", json={"type": event_type, "data": {}}
            )
        entry = subagent_work.get_subagent_work(conv_id)
    finally:
        subagent_work.unregister_subagent_work(conv_id)
        await server_client.aclose()
    return pm, harness_client, resp.status_code, entry


@pytest.mark.asyncio
@pytest.mark.parametrize("turn_in_flight", [True, False], ids=["turn-in-flight", "idle"])
async def test_grok_subagent_stop_cancels_turn_and_terminates_acp_process(
    turn_in_flight: bool,
) -> None:
    """Journey: a parent hard-stops its grok ACP sub-agent, mid-turn or idle.

    A live turn gets ``session/cancel`` (the harness interrupt); the resident
    ACP process is terminated either way and the parent gets a confirmed cancel.
    """
    pm, harness_client, status_code, entry = await _grok_runner_events(
        sub_agent=True, event_type="stop_session", turn_in_flight=turn_in_flight
    )

    assert status_code == 204
    interrupts = [body for body in harness_client.patched_events if body == {"type": "interrupt"}]
    assert interrupts == ([{"type": "interrupt"}] if turn_in_flight else [])
    assert pm.released == ["conv_grok_child"]
    assert entry is not None
    assert entry.status == "cancelled"
    assert entry.cancellation_confirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("event_type", "sub_agent"),
    [
        pytest.param("interrupt", True, id="ui-stop-on-subagent"),
        pytest.param("interrupt", False, id="ui-stop-top-level"),
        pytest.param("stop_session", False, id="stop-session-top-level"),
    ],
)
async def test_grok_ui_stop_and_top_level_stop_keep_the_acp_process(
    event_type: str,
    sub_agent: bool,
) -> None:
    """Journey: a user presses Stop on a grok chat; the turn ends, the process stays."""
    pm, harness_client, status_code, entry = await _grok_runner_events(
        sub_agent=sub_agent, event_type=event_type, turn_in_flight=True
    )

    assert status_code == 204
    assert {"type": "interrupt"} in harness_client.patched_events
    assert pm.released == []
    if sub_agent:
        assert entry is not None and entry.cancellation_confirmed is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("harness", "expected_event"),
    [
        pytest.param("grok", "stop_session", id="grok-acp-hard-stop"),
        pytest.param("claude-sdk", "interrupt", id="non-acp-in-process-interrupt"),
    ],
)
async def test_sys_cancel_task_routes_in_process_child_by_runner_harness(
    harness: str, expected_event: str
) -> None:
    """Journey: a parent cancels an in-process sub-agent with ``sys_cancel_task``.

    The child runner's own harness decides the route: a grok ACP child gets the
    ``stop_session`` hard-stop, any other in-process child keeps ``interrupt``.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_cancel_parent"
    child_id = f"conv_cancel_child_{harness}"
    spec = _harness_spec(harness)
    # The in-process SDK harness needs a model to accept the session.
    spec = dataclasses.replace(
        spec, executor=dataclasses.replace(spec.executor, model="claude-sonnet-4-5")
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        return spec

    runner_server = _empty_ok_server_client()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager, _FakeProcessManager(_ScriptedHarnessClient([]))
        ),
        spec_resolver=_resolver,
        server_client=runner_server,
    )
    posted: list[dict[str, Any]] = []

    def _parent_server(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == f"/v1/sessions/{child_id}/events":
            posted.append(json.loads(request.content))
            return httpx.Response(204)
        return httpx.Response(404)

    subagent_work.register_subagent_work(
        parent_session_id=parent_id, child_session_id=child_id, agent="worker", title="work"
    )
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": child_id, "agent_id": "agent_worker"}
            )
            assert created.status_code == 201, created.text
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(_parent_server), base_url="http://server"
        ) as parent_server:
            await execute_tool(
                tool_name="sys_cancel_task",
                arguments=json.dumps({"task_id": child_id}),
                server_client=parent_server,
                conversation_id=parent_id,
                session_async_tasks={},
            )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        await runner_server.aclose()

    assert posted == [{"type": expected_event, "data": {}}]


class _HeldReleaseProcessManager(_FakeProcessManager):
    """Process manager whose ``release`` waits on a gate, like a slow ACP shutdown."""

    def __init__(self, client: _ScriptedHarnessClient) -> None:
        super().__init__(client)
        self.release_started = asyncio.Event()
        self.release_gate = asyncio.Event()

    async def release(self, conversation_id: str, **kwargs: Any) -> None:
        self.release_started.set()
        await self.release_gate.wait()
        await super().release(conversation_id, **kwargs)


class _HeldCloseAppServer(_RecordingAppServer):
    """Codex app-server double whose ``close`` waits on a gate."""

    def __init__(self) -> None:
        super().__init__()
        self.close_started = asyncio.Event()
        self.close_gate = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.close_gate.wait()
        await super().close()


async def _grok_stop_while_new_work_arrives() -> tuple[
    str, subagent_work._SubagentWorkEntry | None
]:
    conv_id = "conv_grok_reused"
    spec = _harness_spec("grok")

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        return spec

    pm = _HeldReleaseProcessManager(_ScriptedHarnessClient([]))
    server_client = _empty_ok_server_client()
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_resolver,
        server_client=server_client,
    )
    subagent_work.register_subagent_work(
        parent_session_id="conv_reuse_parent",
        child_session_id=conv_id,
        agent="grok_impl",
        title="work",
        work_id="work_stopped",
    )
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "agent_grok"}
            )
            assert created.status_code == 201, created.text
            await pm.get_client(conv_id, "grok")
            stop = asyncio.create_task(
                client.post(
                    f"/v1/sessions/{conv_id}/events", json={"type": "stop_session", "data": {}}
                )
            )
            await asyncio.wait_for(pm.release_started.wait(), timeout=5)
            subagent_work.register_subagent_work(
                parent_session_id="conv_reuse_parent",
                child_session_id=conv_id,
                agent="grok_impl",
                title="work",
                work_id="work_replacement",
            )
            pm.release_gate.set()
            assert (await stop).status_code == 204
        return conv_id, subagent_work.get_subagent_work(conv_id)
    finally:
        subagent_work.unregister_subagent_work(conv_id)
        await server_client.aclose()


async def _codex_stop_while_new_work_arrives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> list[tuple[str, str, str]]:
    conv_id = "conv_codex_reused"
    app_server = _HeldCloseAppServer()
    monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, conv_id, app_server)
    runner, captured, _client, _registry = _codex_runner_with_live_turn(
        monkeypatch, tmp_path, conv_id, sub_agent=True
    )
    captured["current_work_id"] = "work_stopped"
    stop = asyncio.create_task(runner.stop("codex-native", conv_id))
    await asyncio.wait_for(app_server.close_started.wait(), timeout=5)
    captured["current_work_id"] = "work_replacement"
    app_server.close_gate.set()
    resp = await stop
    assert isinstance(resp, Response) and resp.status_code == 204
    assert app_server.closed is True
    assert captured["superseded"] == [(conv_id, "cancelled", "work_stopped")]
    return captured["wakes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("harness", ["grok", "codex"])
async def test_subagent_hard_stop_leaves_work_sent_during_teardown_running(
    harness: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Journey: a parent stops a sub-agent, then reuses it before the kill finishes.

    The stop's confirmed ``cancelled`` belongs to the dispatch it was raised for;
    new work registered on the same child while the process is torn down must
    not be marked cancelled.
    """
    if harness == "grok":
        _conv_id, entry = await _grok_stop_while_new_work_arrives()
        assert entry is not None
        assert entry.work_id == "work_replacement"
        assert entry.status != "cancelled"
        assert entry.cancellation_confirmed is False
    else:
        wakes = await _codex_stop_while_new_work_arrives(monkeypatch, tmp_path)
        assert wakes == []


@pytest.mark.asyncio
async def test_codex_subagent_stop_during_backend_launch_tears_down_the_launched_backend(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Journey: a parent cancels its codex sub-agent while the child's backend is starting.

    The stop must wait for the in-flight launch, tear down the app-server it
    registers, and only then report the cancellation as confirmed.
    """
    from omnigent.harnesses.codex_native import bridge as codex_bridge
    from omnigent.runner import app as runner_app_module

    async def _fail_launch_config(**kwargs: Any) -> None:
        raise RuntimeError("launch config disabled in test")

    # Keep the session-create auto-launch from touching the host's codex install.
    monkeypatch.setattr(runner_app_module, "_codex_native_launch_config", _fail_launch_config)
    monkeypatch.setattr(codex_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    conv_id = "conv_codex_launching"
    spec = _harness_spec("codex-native")

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        return spec

    server_client = _empty_ok_server_client()
    app = create_runner_app(
        process_manager=cast(
            HarnessProcessManager, _FakeProcessManager(_ScriptedHarnessClient([]))
        ),
        spec_resolver=_resolver,
        server_client=server_client,
    )
    subagent_work.register_subagent_work(
        parent_session_id="conv_launch_parent",
        child_session_id=conv_id,
        agent="codex_impl",
        title="work",
        wrapper_label="codex-native-ui",
    )
    app_server = _RecordingAppServer()
    try:
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": conv_id, "agent_id": "agent_codex"}
            )
            assert created.status_code == 201, created.text
            launch_lock = app.state.native_terminal_ensure_locks["codex"].setdefault(
                conv_id, asyncio.Lock()
            )
            async with launch_lock:
                stop = asyncio.create_task(
                    client.post(
                        f"/v1/sessions/{conv_id}/events",
                        json={"type": "stop_session", "data": {}},
                    )
                )
                await asyncio.sleep(0.2)
                assert not stop.done(), "stop confirmed before the launch was dealt with"
                entry = subagent_work.get_subagent_work(conv_id)
                assert entry is not None and entry.status != "cancelled"
                # The launch completes by registering its backend.
                monkeypatch.setitem(orchestration._AUTO_CODEX_APP_SERVERS, conv_id, app_server)
            resp = await stop
        entry = subagent_work.get_subagent_work(conv_id)
    finally:
        subagent_work.unregister_subagent_work(conv_id)
        await server_client.aclose()

    assert resp.status_code == 204
    assert app_server.closed is True
    assert conv_id not in orchestration._AUTO_CODEX_APP_SERVERS
    assert entry is not None
    assert entry.status == "cancelled"
    assert entry.cancellation_confirmed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("parent_view", ["completed", "drained"])
async def test_sys_cancel_task_hard_stops_idle_grok_child_with_resident_process(
    parent_view: str,
) -> None:
    """Journey: a parent cancels a grok sub-agent whose turn already ended.

    The child's ACP process stays resident after the turn, so the cancel must
    still reach the runner's hard-stop and release it, whether the parent's
    work entry is ``completed`` or was evicted after the parent read the result.
    """
    from omnigent.runner.tool_dispatch import execute_tool

    parent_id = "conv_idle_grok_parent"
    child_id = "conv_idle_grok_child"
    spec = _harness_spec("grok")

    async def _resolver(agent_id: str, session_id: str | None = None) -> Any:
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    runner_server = _empty_ok_server_client()
    app = create_runner_app(
        process_manager=cast(HarnessProcessManager, pm),
        spec_resolver=_resolver,
        server_client=runner_server,
    )
    posted: list[dict[str, Any]] = []
    try:
        async with _runner_client(app) as runner:

            async def _parent_server(request: httpx.Request) -> httpx.Response:
                if request.method == "GET" and request.url.path == f"/v1/sessions/{child_id}":
                    return httpx.Response(
                        200, json={"id": child_id, "parent_session_id": parent_id, "labels": {}}
                    )
                if request.method == "POST" and request.url.path.endswith("/events"):
                    body = json.loads(request.content)
                    posted.append(body)
                    forwarded = await runner.post(request.url.path, json=body)
                    return httpx.Response(forwarded.status_code)
                return httpx.Response(404)

            created = await runner.post(
                "/v1/sessions",
                json={"session_id": child_id, "agent_id": "agent_grok", "sub_agent_name": "grok"},
            )
            assert created.status_code == 201, created.text
            await pm.get_client(child_id, "grok")
            entry = subagent_work.register_subagent_work(
                parent_session_id=parent_id, child_session_id=child_id, agent="grok", title="work"
            )
            entry.status = "completed"
            if parent_view == "drained":
                subagent_work.unregister_subagent_work(child_id)
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(_parent_server), base_url="http://server"
            ) as parent_server:
                result = await execute_tool(
                    tool_name="sys_cancel_task",
                    arguments=json.dumps({"task_id": child_id}),
                    server_client=parent_server,
                    conversation_id=parent_id,
                    session_async_tasks={},
                )
    finally:
        subagent_work.unregister_subagent_work(child_id)
        await runner_server.aclose()

    assert not result.startswith("Error"), result
    assert posted == [{"type": "stop_session", "data": {}}]
    assert pm.released == [child_id]
