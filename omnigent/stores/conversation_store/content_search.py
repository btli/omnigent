"""Internal content-match selection for session listings."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversation, SqlConversationItem, current_workspace_id


@dataclass(frozen=True)
class ContentMatches:
    """Candidate predicate, completeness, and optional exact earliest positions.

    Only complete results may filter a listing. Overflow requires a fallback
    before pagination; a capped item set remains overflow after deduplication.
    When supplied, locators cover all content matches, keyed by conversation id;
    missing ids are title-only matches. Positions must follow an exact recheck.
    """

    predicate: ColumnElement[bool]
    completeness: Literal["complete", "overflow"]
    earliest_match_positions: Mapping[str, int] | None = None


def is_trigram_eligible(query: str) -> bool:
    """Require a literal run of three ASCII alphanumerics; LIKE syntax splits runs."""
    return re.search(r"[a-zA-Z0-9]{3}", query) is not None


def legacy_content_matches(_session: Session, query: str) -> ContentMatches:
    """Probe each candidate session with the existing case-insensitive LIKE pattern."""
    # Correlated EXISTS scopes probes; raw ILIKE avoids matching a lower(search_text) GIN index.
    # Guarded by test_search_predicate_avoids_the_trigram_index_expression.
    return ContentMatches(
        predicate=(
            select(SqlConversationItem.conversation_id)
            .where(
                SqlConversationItem.workspace_id == current_workspace_id(),
                SqlConversationItem.conversation_id == SqlConversation.id,
                SqlConversationItem.search_text.ilike(f"%{query.lower()}%"),
            )
            .exists()
        ),
        completeness="complete",
    )


def select_content_search(
    *,
    trigram_eligible: bool,
) -> Callable[[Session, str], ContentMatches]:
    """Select legacy search; eligibility is ignored while it is the only strategy."""
    del trigram_eligible
    return legacy_content_matches
