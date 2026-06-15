"""Verify the native Claude forwarder feeds item text to cswap detection.

This is the wiring test for the reactive hook: ``_post_external_conversation_item``
must call ``integration.record_reactive_text`` with the item's text, the
``anthropic`` family, and the session id — independent of the cswap config
(the facade itself decides whether to act).
"""

from __future__ import annotations

import httpx
import pytest

import omnigent.cswap.integration as integration
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
