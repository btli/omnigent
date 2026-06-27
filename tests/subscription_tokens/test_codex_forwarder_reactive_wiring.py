"""Verify the native Codex forwarder feeds item text to subscription-token detection.

The OpenAI analogue of ``test_forwarder_reactive_wiring``: ``_post_external_item``
must scan an assistant/system message and call ``integration.record_reactive_text``
with the message text, the ``openai`` family, and the session id — and must NOT
scan a user message (so a prompt quoting a quota phrase never fails over).
"""

from __future__ import annotations

import httpx
import pytest

import omnigent.subscription_tokens.integration as integration
from omnigent.codex_native_forwarder import _post_external_item


def _ok_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"id": "evt"})


async def test_codex_assistant_message_invokes_openai_reactive_detection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        integration,
        "record_reactive_text",
        lambda text, *, family, session_id: calls.append((text, family, session_id)),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_ok_handler), base_url="http://ap"
    ) as client:
        await _post_external_item(
            client,
            "conv_codex",
            item_type="message",
            item_data={
                "role": "assistant",
                "content": [{"type": "output_text", "text": "OpenAI rate limit reached"}],
            },
            response_id="resp_1",
        )

    assert len(calls) == 1
    text, family, session_id = calls[0]
    assert "rate limit reached" in text.lower()
    assert family == "openai"
    assert session_id == "conv_codex"


async def test_codex_user_message_is_not_scanned(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        integration,
        "record_reactive_text",
        lambda text, *, family, session_id: calls.append(text),
    )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_ok_handler), base_url="http://ap"
    ) as client:
        await _post_external_item(
            client,
            "conv_codex",
            item_type="message",
            item_data={
                "role": "user",
                "content": [{"type": "input_text", "text": "what if openai rate limit reached?"}],
            },
            response_id="resp_u",
        )

    assert calls == []  # a user prompt quoting the phrase must not fail over
