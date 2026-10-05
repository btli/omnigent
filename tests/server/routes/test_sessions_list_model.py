"""Session-list model metadata comes directly from the persisted row."""

import pytest

from omnigent.entities import Conversation
from omnigent.server.routes._sessions.orchestration import _build_session_list_item


@pytest.mark.parametrize(
    ("reported", "override", "expected"),
    [
        ("opus[1m]", "sonnet", "opus[1m]"),
        (None, "sonnet", "sonnet"),
        (None, None, None),
        ("<synthetic>", "sonnet", "sonnet"),
        ("<synthetic>", None, None),
    ],
)
@pytest.mark.parametrize("harness_override", [None, "claude-sdk", "codex"])
def test_session_list_model(
    reported: str | None,
    override: str | None,
    expected: str | None,
    harness_override: str | None,
) -> None:
    conv = Conversation(
        id="conv_model",
        agent_id="ag_model",
        created_at=100,
        updated_at=200,
        root_conversation_id="conv_model",
        reported_model=reported,
        model_override=override,
        harness_override=harness_override,
    )
    item = _build_session_list_item(
        conv,
        agent_names_by_id={},
        grants=[],
        user_id=None,
        user_is_admin=False,
        permissions_enabled=False,
        pending_count=0,
        child_session_ids=[],
        comments_fingerprint=None,
    )
    assert item.llm_model == expected
    assert item.model_dump()["llm_model"] == expected
    assert item.harness_override == harness_override
    assert item.model_dump()["harness_override"] == harness_override
