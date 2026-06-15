"""Verify the native Claude forwarder feeds item text to subscription-token detection.

This is the wiring test for the reactive hook: ``_post_external_conversation_item``
must call ``integration.record_reactive_text`` with the item's text, the
``anthropic`` family, and the session id — independent of the subscription-token config
(the facade itself decides whether to act).
"""

from __future__ import annotations

import httpx
import pytest

import omnigent.subscription_tokens.integration as integration
from omnigent.claude_native_bridge import ClaudeTranscriptItem
from omnigent.claude_native_forwarder import _post_external_conversation_item


async def test_item_poster_invokes_reactive_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []

    def _capture(text: str, *, family: str, session_id: str) -> None:
        calls.append((text, family, session_id))

    monkeypatch.setattr(integration, "record_reactive_text", _capture)

    item = ClaudeTranscriptItem(
        source_id="abc:0:message",
        item_type="message",
        data={"role": "assistant", "content": "Claude AI usage limit reached"},
        response_id="resp_1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt_1"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await _post_external_conversation_item(client, session_id="conv_1", item=item)

    assert len(calls) == 1
    text, family, session_id = calls[0]
    assert "usage limit reached" in text.lower()
    assert family == "anthropic"
    assert session_id == "conv_1"


async def test_user_and_tool_items_are_not_scanned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user message (or tool output) quoting the limit phrase must NOT scan."""
    calls: list[str] = []
    monkeypatch.setattr(
        integration,
        "record_reactive_text",
        lambda text, *, family, session_id: calls.append(text),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt"})

    user_item = ClaudeTranscriptItem(
        source_id="u:0:message",
        item_type="message",
        data={"role": "user", "content": "what happens when Claude usage limit reached?"},
        response_id="resp_u",
    )
    tool_item = ClaudeTranscriptItem(
        source_id="t:0:function_call",
        item_type="function_call",
        data={"name": "fetch", "output": "Claude usage limit reached"},
        response_id="resp_t",
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await _post_external_conversation_item(client, session_id="conv_1", item=user_item)
        await _post_external_conversation_item(client, session_id="conv_1", item=tool_item)

    assert calls == []  # neither user nor tool content triggers detection


async def test_subagent_item_attributes_to_parent_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The sub-agent path must scan against the explicit subscription-token (parent) session."""
    captured: list[str] = []
    monkeypatch.setattr(
        integration,
        "record_reactive_text",
        lambda text, *, family, session_id: captured.append(session_id),
    )

    item = ClaudeTranscriptItem(
        source_id="s:0:message",
        item_type="message",
        data={"role": "assistant", "content": "Claude usage limit reached"},
        response_id="resp_s",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "evt"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await _post_external_conversation_item(
            client, session_id="conv_child", item=item, subtoken_session_id="conv_parent"
        )

    assert captured == ["conv_parent"]
