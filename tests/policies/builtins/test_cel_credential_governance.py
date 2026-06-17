"""Governance example: a CEL policy gating on the seeded ``credential.*`` labels.

This exercises the "mechanism + policy" pairing — the policy-engine build seeds
``credential.kind`` from the session's bound account (see
:mod:`omnigent.subscription_tokens.labels`), and a guardrail policy governs on
it. The expression below is the documented example
(`docs/claude/MULTI_SUBSCRIPTION_PLAN.md`): require approval before any tool
call when the session runs on a personal *subscription* account, while api_key
(and unbound) sessions proceed. Keeping it in a test guards the example against
CEL-shape drift (e.g. the labels living at ``event.context.labels``).
"""

from __future__ import annotations

from typing import Any

from omnigent.policies.builtins.cel import cel_policy

# The shipped example expression. The `in` guard makes a session with no
# credential label (no pool configured) abstain to ALLOW rather than hit a
# missing-key eval error.
GOVERNANCE_EXPRESSION = (
    'event.type == "tool_call"'
    ' && "credential.kind" in event.context.labels'
    ' && event.context.labels["credential.kind"] == "subscription"'
    ' ? {"result": "ASK",'
    '    "reason": "Tool use on a personal subscription account requires approval."}'
    ' : {"result": "ALLOW"}'
)


def _tool_call_event(labels: dict[str, str]) -> dict[str, Any]:
    """Build a minimal ``tool_call`` PolicyEvent carrying *labels*."""
    return {
        "type": "tool_call",
        "target": "sys_os_shell",
        "data": {"name": "sys_os_shell", "arguments": {}},
        "context": {"labels": labels},
    }


def test_subscription_tool_call_asks() -> None:
    """A tool call on a subscription account is gated to ASK."""
    evaluate = cel_policy(expression=GOVERNANCE_EXPRESSION)
    result = evaluate(_tool_call_event({"credential.kind": "subscription"}))
    assert result is not None
    assert result["result"] == "ASK"
    assert "subscription" in result["reason"]


def test_api_key_tool_call_allows() -> None:
    """The same tool call on an api_key account proceeds (ALLOW)."""
    evaluate = cel_policy(expression=GOVERNANCE_EXPRESSION)
    result = evaluate(_tool_call_event({"credential.kind": "api_key"}))
    assert result == {"result": "ALLOW"}


def test_unbound_session_allows() -> None:
    """No credential label (no pool configured) → the `in` guard abstains to
    ALLOW instead of erroring on the missing key."""
    evaluate = cel_policy(expression=GOVERNANCE_EXPRESSION)
    result = evaluate(_tool_call_event({}))
    assert result == {"result": "ALLOW"}


def test_non_tool_call_phase_allows() -> None:
    """The example only gates tool calls: a plain request on a subscription
    account is not asked (so chat turns aren't gated)."""
    evaluate = cel_policy(expression=GOVERNANCE_EXPRESSION)
    result = evaluate(
        {
            "type": "request",
            "data": "hello",
            "context": {"labels": {"credential.kind": "subscription"}},
        }
    )
    assert result == {"result": "ALLOW"}
