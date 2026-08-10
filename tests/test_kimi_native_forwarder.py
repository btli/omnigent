"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (turn.prompt + content.part + usage.record + llm.request), the
line-offset state round-trip, workspace/recency-based session discovery, and
the usage/model mirroring sync. The live POST loop is exercised by the e2e
gate, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.kimi_native_forwarder import (
    KimiWireItem,
    _discover_wire,
    _ForwardState,
    _KimiUsageSync,
    _read_state,
    _row_to_item,
    _write_state,
    clear_kimi_bridge_state,
    read_kimi_wire_items,
)


class TestRowToItem:
    def test_turn_prompt_is_user(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "what is in this repo?"}],
            "origin": {"kind": "user"},
        }
        item = _row_to_item(4, row)
        assert item is not None
        assert item.role == "user"
        assert item.text == "what is in this repo?"
        assert item.response_id == "kimi:turn:4"

    def test_content_part_text_is_assistant(self) -> None:
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "67ce67f7",
                "part": {"type": "text", "text": "This is **Omnigent**."},
            },
        }
        item = _row_to_item(9, row)
        assert item is not None
        assert item.role == "assistant"
        assert item.text == "This is **Omnigent**."
        assert item.response_id == "kimi:67ce67f7"

    def test_think_part_is_reasoning(self) -> None:
        # Reasoning lives in part["think"] (not part["text"]) and is mirrored as a
        # reasoning item, not skipped — the kimi analogue of codex-native #1254.
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "abc123",
                "part": {"type": "think", "think": "Let me reason about this."},
            },
        }
        item = _row_to_item(5, row)
        assert item is not None
        assert item.kind == "reasoning"
        assert item.role == "assistant"
        assert item.text == "Let me reason about this."
        assert item.response_id == "kimi:abc123"

    def test_tool_call_and_metadata_skipped(self) -> None:
        for row in (
            {"type": "context.append_loop_event", "event": {"type": "tool.call", "name": "Read"}},
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
        ):
            assert _row_to_item(0, row) is None

    def test_step_end_with_end_turn_is_turn_end(self) -> None:
        """``end_turn`` is the edge that reports terminal status to the parent."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 3,
                "finishReason": "end_turn",
            },
        }
        item = _row_to_item(28, row)
        assert item is not None
        assert item.kind == "turn_end"
        assert item.response_id == "kimi:turn_end:28"

    def test_step_end_with_tool_use_is_skipped(self) -> None:
        """A step that stopped to call a tool is mid-turn, not a completion."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 1,
                "finishReason": "tool_use",
            },
        }
        assert _row_to_item(12, row) is None

    def test_non_user_turn_prompt_skipped(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "x"}],
            "origin": {"kind": "system"},
        }
        assert _row_to_item(0, row) is None

    def test_usage_record_maps_counts(self) -> None:
        """Pinned Kimi Code 0.34.0 ``usage.record`` shape → a usage item."""
        row = {
            "type": "usage.record",
            "model": "kimi-k3-databricks",
            "usage": {
                "inputOther": 2975,
                "output": 76,
                "inputCacheRead": 17920,
                "inputCacheCreation": 0,
            },
            "usageScope": "turn",
            "time": 1786275843173,
        }
        item = _row_to_item(7, row)
        assert item is not None
        assert item.kind == "usage"
        assert item.usage == {
            "input_other": 2975,
            "output": 76,
            "cache_read": 17920,
            "cache_creation": 0,
        }
        assert item.model == "kimi-k3-databricks"
        assert item.response_id == "kimi:usage:7"

    def test_usage_record_non_turn_scope_skipped(self) -> None:
        """Only turn-scoped records carry the session's own spend."""
        row = {
            "type": "usage.record",
            "usage": {"inputOther": 10, "output": 1, "inputCacheRead": 0, "inputCacheCreation": 0},
            "usageScope": "aggregate",
        }
        assert _row_to_item(0, row) is None

    def test_usage_record_malformed_counts_default_to_zero(self) -> None:
        """Non-int / negative fields count as 0 — tolerant, never crash."""
        row = {
            "type": "usage.record",
            "usage": {"inputOther": "lots", "output": -5, "inputCacheRead": 7},
            "usageScope": "turn",
        }
        item = _row_to_item(0, row)
        assert item is not None
        assert item.usage == {
            "input_other": 0,
            "output": 0,
            "cache_read": 7,
            "cache_creation": 0,
        }

    def test_llm_request_prefers_provider_resolved_model(self) -> None:
        """Pinned 0.34.0 ``llm.request`` shape → a model item on the resolved id."""
        row = {
            "type": "llm.request",
            "kind": "loop",
            "provider": "openai",
            "model": "system.ai.kimi-k3",
            "modelAlias": "kimi-k3-databricks",
            "maxTokens": 65536,
            "time": 1786190562670,
        }
        item = _row_to_item(2, row)
        assert item is not None
        assert item.kind == "model"
        assert item.model == "system.ai.kimi-k3"

    def test_llm_request_falls_back_to_alias(self) -> None:
        row: dict[str, object] = {"type": "llm.request", "modelAlias": "kimi-k3-databricks"}
        item = _row_to_item(0, row)
        assert item is not None
        assert item.model == "kimi-k3-databricks"

    def test_llm_request_without_any_model_skipped(self) -> None:
        assert _row_to_item(0, {"type": "llm.request", "kind": "loop"}) is None


class TestReadNewItems:
    def _wire(self, tmp_path: Path) -> Path:
        def _part(uuid: str, part_type: str, text: str) -> dict[str, object]:
            return {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "uuid": uuid,
                    "part": {"type": part_type, "text": text},
                },
            }

        rows = [
            {"type": "metadata", "protocol_version": 1},
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "hi"}],
                "origin": {"kind": "user"},
            },
            _part("u1", "think", "…"),
            _part("u2", "text", "hello!"),
        ]
        p = tmp_path / "wire.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def test_parses_user_and_assistant_only(self, tmp_path: Path) -> None:
        items = read_kimi_wire_items(self._wire(tmp_path), 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi"), ("assistant", "hello!")]

    def test_offset_skips_already_seen(self, tmp_path: Path) -> None:
        wire = self._wire(tmp_path)
        # last_line past the user prompt (line 1) → only the assistant text (line 3).
        items = read_kimi_wire_items(wire, 2)
        assert [(i.role, i.text) for i in items] == [("assistant", "hello!")]
        assert items[0].line_no == 3

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_kimi_wire_items(tmp_path / "nope.jsonl", 0) == []


class TestState:
    def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        assert _read_state(tmp_path) is None
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7))
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        assert loaded.usage_totals == {}
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None

    def test_round_trip_preserves_usage_totals(self, tmp_path: Path) -> None:
        """Cumulative sums survive a forwarder restart (server usage is monotonic,
        so a zero-reset would silently drop all post-restart usage)."""
        totals = {"input_other": 100, "output": 20, "cache_read": 50, "cache_creation": 5}
        _write_state(tmp_path, _ForwardState("/x/wire.jsonl", 3, totals))
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.usage_totals == totals

    def test_legacy_state_without_totals_still_loads(self, tmp_path: Path) -> None:
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 4}), encoding="utf-8"
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.last_line == 4
        assert loaded.usage_totals == {}


class _RecordingClient:
    """Async httpx-client stub that records POST bodies and returns HTTP 200."""

    def __init__(self, status_code: int = 200) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._status_code = status_code
        self.fail_with: Exception | None = None

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        del headers
        if self.fail_with is not None:
            raise self.fail_with
        self.posts.append((url, json))
        return httpx.Response(self._status_code, request=httpx.Request("POST", url))


def _usage_item(
    line_no: int,
    *,
    input_other: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    model: str | None = None,
) -> KimiWireItem:
    row: dict[str, object] = {
        "type": "usage.record",
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": "turn",
    }
    if model is not None:
        row["model"] = model
    item = _row_to_item(line_no, row)
    assert item is not None
    return item


def _sync(totals: dict[str, int] | None = None) -> _KimiUsageSync:
    return _KimiUsageSync(base_url="http://ap", headers={}, session_id="conv_k", totals=totals)


def _no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep flush() off the real model-catalog/litellm lookup."""
    from omnigent.llms import context_window

    monkeypatch.setattr(context_window, "find_model_context_window", lambda _m: None)


class TestUsageSync:
    @pytest.mark.asyncio
    async def test_accumulates_cumulative_totals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Per-call records sum into cumulative SET-semantics fields.

        ``cumulative_input_tokens`` is INCLUSIVE of cache reads (the server
        splits ``cumulative_cache_read_input_tokens`` back out to price them
        at the cache-read rate) and folds cache-creation into input (no
        dedicated server field). ``context_tokens`` is the LATEST record's
        occupancy, not a sum.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync()

        sync.record(_usage_item(1, input_other=20559, output=160))
        await sync.flush(client)
        sync.record(_usage_item(2, input_other=2975, output=76, cache_read=17920))
        await sync.flush(client)

        assert len(client.posts) == 2
        url, body = client.posts[1]
        assert url == "http://ap/v1/sessions/conv_k/events"
        assert body["type"] == "external_session_usage"
        assert body["data"] == {
            "cumulative_input_tokens": 20559 + 2975 + 17920,
            "cumulative_cache_read_input_tokens": 17920,
            "cumulative_output_tokens": 160 + 76,
            "context_tokens": 2975 + 17920,
        }

    @pytest.mark.asyncio
    async def test_model_rides_along_on_every_usage_post(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The effective model is attached to every token post (the server
        reprices cumulative totals per post and needs it each time)."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync()
        await sync.sync_model(client, "system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1))
        await sync.flush(client)
        sync.record(_usage_item(2, input_other=20, output=2))
        await sync.flush(client)

        usage_posts = [b for _u, b in client.posts if b["type"] == "external_session_usage"]
        assert len(usage_posts) == 2
        assert all(b["data"]["model"] == "system.ai.kimi-k3" for b in usage_posts)

    @pytest.mark.asyncio
    async def test_usage_record_alias_fills_model_until_llm_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync()

        sync.record(_usage_item(1, input_other=10, output=1, model="kimi-k3-databricks"))
        await sync.flush(client)

        assert client.posts[0][1]["data"]["model"] == "kimi-k3-databricks"

    @pytest.mark.asyncio
    async def test_flush_dedupes_unchanged_totals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync()

        sync.record(_usage_item(1, input_other=10, output=1))
        await sync.flush(client)
        await sync.flush(client)

        assert len(client.posts) == 1

    @pytest.mark.asyncio
    async def test_model_change_posts_once_and_is_not_seeded_at_spawn(self) -> None:
        """The FIRST llm.request mirrors the spawn model (baseline never
        seeded — the codex pattern, so the cost gate sees the real model),
        and an unchanged model is not re-posted."""
        client = _RecordingClient()
        sync = _sync()

        await sync.sync_model(client, "system.ai.kimi-k3")
        await sync.sync_model(client, "system.ai.kimi-k3")

        assert client.posts == [
            (
                "http://ap/v1/sessions/conv_k/events",
                {"type": "external_model_change", "data": {"model": "system.ai.kimi-k3"}},
            )
        ]

    @pytest.mark.asyncio
    async def test_model_change_posts_again_on_switch(self) -> None:
        client = _RecordingClient()
        sync = _sync()

        await sync.sync_model(client, "system.ai.kimi-k3")
        await sync.sync_model(client, "system.ai.kimi-k3-mini")

        models = [b["data"]["model"] for _u, b in client.posts]
        assert models == ["system.ai.kimi-k3", "system.ai.kimi-k3-mini"]

    @pytest.mark.asyncio
    async def test_context_window_included_when_resolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnigent.llms import context_window

        monkeypatch.setattr(context_window, "find_model_context_window", lambda _m: 262_144)
        client = _RecordingClient()
        sync = _sync()
        await sync.sync_model(client, "system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1))
        await sync.flush(client)

        usage_post = client.posts[-1][1]
        assert usage_post["data"]["context_window"] == 262_144

    @pytest.mark.asyncio
    async def test_context_window_omitted_when_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unknown model must OMIT the window — a guessed default would
        draw a wrong context ring."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync()
        await sync.sync_model(client, "system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1))
        await sync.flush(client)

        assert "context_window" not in client.posts[-1][1]["data"]

    @pytest.mark.asyncio
    async def test_post_failure_is_swallowed_and_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed usage post never raises (it must not stall transcript
        mirroring); the next flush re-posts the cumulative totals."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        client.fail_with = httpx.ConnectError("boom")
        sync = _sync()

        sync.record(_usage_item(1, input_other=10, output=1))
        await sync.flush(client)
        assert client.posts == []

        client.fail_with = None
        await sync.flush(client)
        assert len(client.posts) == 1
        assert client.posts[0][1]["data"]["cumulative_input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_totals_seeded_from_persisted_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restarted forwarder resumes the cumulative counters instead of
        resetting to zero (which the server's monotonic clamp would ignore)."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(
            totals={"input_other": 100, "output": 20, "cache_read": 50, "cache_creation": 5}
        )

        sync.record(_usage_item(9, input_other=10, output=1))
        await sync.flush(client)

        data = client.posts[0][1]["data"]
        assert data["cumulative_input_tokens"] == 100 + 50 + 5 + 10
        assert data["cumulative_cache_read_input_tokens"] == 50
        assert data["cumulative_output_tokens"] == 21


class TestDiscoverWire:
    def _make_session(
        self, home: Path, session_dir_name: str, work_dir: str, *, mtime: float
    ) -> Path:
        wire = home / "sessions" / "wd_x" / session_dir_name / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text("{}\n", encoding="utf-8")
        import os

        os.utime(wire, (mtime, mtime))
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        idx = home / "session_index.jsonl"
        index_row = {"sessionDir": str(wire.parent.parent.parent), "workDir": work_dir}
        with idx.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(index_row) + "\n")
        return wire

    def test_picks_newest_matching_workspace(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_old", "/ws", mtime=1000.0)
        newest = self._make_session(home, "session_new", "/ws", mtime=2000.0)
        self._make_session(home, "session_other", "/different", mtime=3000.0)
        found = _discover_wire(home, "/ws", launch_epoch_ms=0)
        assert found == newest

    def test_none_before_any_session(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        assert _discover_wire(home, "/ws", launch_epoch_ms=0) is None

    def test_ignores_sessions_before_launch(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_stale", "/ws", mtime=1000.0)
        # launch far in the future (ms) → the 1000s-mtime session is below the floor.
        assert _discover_wire(home, "/ws", launch_epoch_ms=9_000_000_000_000) is None
