"""Content-search strategy and legacy result parity across database backends."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from unittest.mock import patch

import pytest
from sqlalchemy import and_, event, func, select, update
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationItem,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.utils import build_search_snippet
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store import sqlalchemy_store as store_module
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _append(store: SqlAlchemyConversationStore, cid: str, texts: list[str]) -> None:
    store.append(
        cid,
        [
            NewConversationItem(
                type="message",
                response_id=f"response-{i}",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for i, text in enumerate(texts)
        ],
    )


@contextmanager
def _captured_sql(store: SqlAlchemyConversationStore) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())

    event.listen(store._conv_engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", capture)


def _legacy_snippets(session: Session, conversation_ids: list[str], query: str, **kwargs):
    """Reference the original grouped-position lookup independently of the seam."""
    earliest = (
        select(
            SqlConversationItem.conversation_id.label("cid"),
            func.min(SqlConversationItem.position).label("pos"),
        )
        .where(
            SqlConversationItem.workspace_id == current_workspace_id(),
            SqlConversationItem.conversation_id.in_(conversation_ids),
            SqlConversationItem.search_text.ilike(f"%{query.lower()}%"),
        )
        .group_by(SqlConversationItem.conversation_id)
        .subquery()
    )
    rows = session.execute(
        select(SqlConversationItem.conversation_id, SqlConversationItem.search_text).join(
            earliest,
            and_(
                SqlConversationItem.workspace_id == current_workspace_id(),
                SqlConversationItem.conversation_id == earliest.c.cid,
                SqlConversationItem.position == earliest.c.pos,
            ),
        )
    ).all()
    return {
        cid: snippet
        for cid, text in rows
        if text and (snippet := build_search_snippet(text, query)) is not None
    }


@pytest.mark.parametrize(
    ("query", "eligible"),
    [
        ("", False),
        ("a", False),
        ("ab", False),
        ("ABC", True),
        ("123", True),
        ("ab cd", False),
        ("ab%cd", False),
        ("ab_cd", False),
        (r"ab\cd", False),
        ("%abc_", True),
        (r"ab\cde", True),
        ("abc-def", True),
        ("日本語", False),
        ("naïve", False),
        ("éabé", False),
        ("café", True),
        ("日本abc語", True),
        ("１２３", False),
        ("ab\nc", False),
        ("İab", False),
        ("Kab", False),
    ],
)
def test_trigram_eligibility(query: str, eligible: bool) -> None:
    from omnigent.stores.conversation_store.content_search import is_trigram_eligible

    assert is_trigram_eligible(query) is eligible


@pytest.mark.parametrize("eligible", [False, True])
def test_selector_always_uses_complete_legacy(eligible: bool) -> None:
    from omnigent.stores.conversation_store.content_search import (
        legacy_content_matches,
        select_content_search,
    )

    strategy = select_content_search(_trigram_eligible=eligible)
    assert strategy is legacy_content_matches
    with Session() as session:
        matches = strategy(session, "abc")
    assert matches.completeness == "complete"
    assert matches.earliest_match_positions is None
    sql = str(matches.predicate)
    assert "EXISTS" in sql
    assert "conversation_items.conversation_id = conversations.id" in sql


def test_duplicate_position_snippets_keep_legacy_ties(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    cid = conversation_store.create_conversation().id
    _append(conversation_store, cid, ["first needle", "second needle", "unrelated", "late needle"])
    with conversation_store._conv_session("characterize_search_ties") as session:
        session.execute(
            update(SqlConversationItem)
            .where(SqlConversationItem.conversation_id == cid, SqlConversationItem.position < 3)
            .values(position=0)
        )
    with conversation_store._conv_session("characterize_search_ties") as session:
        expected = _legacy_snippets(session, [cid], "needle")
        assert expected[cid] in ("first needle", "second needle")
        seen = []
        original = store_module.build_search_snippet

        def record(text, query):
            seen.append(text)
            return original(text, query)

        with (
            patch.object(store_module, "build_search_snippet", record),
            patch.object(session, "execute", wraps=session.execute) as execute,
        ):
            actual = store_module._fetch_search_snippets(session, [cid], "needle")
        assert actual == expected
        assert sorted(seen) == ["first needle", "second needle", "unrelated"]
        assert "GROUP BY" not in str(execute.call_args.args[0])


@pytest.mark.parametrize("query", ["needle", "%", "_", r"a\b", "日本語", "naïve", "ab", "absent"])
@pytest.mark.parametrize("order", ["asc", "desc"])
@pytest.mark.parametrize("store_fixture", ["conversation_store", "split_db_conversation_store"])
def test_search_seam_parity(
    request: pytest.FixtureRequest,
    query: str,
    order: str,
    store_fixture: str,
) -> None:
    from omnigent.stores.conversation_store.content_search import (
        ContentMatches,
        is_trigram_eligible,
    )

    store: SqlAlchemyConversationStore = request.getfixturevalue(store_fixture)
    body = r"needle 100% under_score a\b 日本語 naïve ab"
    visible = [store.create_conversation(title="ordinary") for _ in range(4)]
    title_only = store.create_conversation(title=body)
    hidden = store.create_conversation(title=body)
    archived = store.create_conversation(title=body)
    child = store.create_conversation(parent_conversation_id=visible[0].id, title=body)
    for conv in [*visible, hidden, archived, child]:
        _append(store, conv.id, ["unrelated", body, f"later {body}"])
    with store._conv_session("seed_search_parity") as session:
        session.execute(
            update(SqlConversation).where(SqlConversation.id == archived.id).values(archived=True)
        )
    with store._session("seed_search_permissions") as session:
        session.add_all(
            [
                SqlSessionPermission(user_id="reader", conversation_id=conv.id, level=1)
                for conv in [*visible, title_only, archived, child]
            ]
        )

    def pages():
        result = []
        cursor = None
        while True:
            page = store.list_conversations(
                search_query=query, accessible_by="reader", order=order, limit=2, after=cursor
            )
            result.append(asdict(page))
            if not page.has_more:
                break
            cursor = page.last_id
        if cursor:
            result.append(
                asdict(
                    store.list_conversations(
                        search_query=query,
                        accessible_by="reader",
                        order=order,
                        limit=2,
                        before=cursor,
                    )
                )
            )
        return result

    selected = store_module.select_content_search
    with patch.object(store_module, "select_content_search", wraps=selected) as selector:
        actual = pages()
        assert selector.call_count == len(actual)
        assert all(
            call.kwargs == {"_trigram_eligible": is_trigram_eligible(query)}
            for call in selector.call_args_list
        )

    def legacy(session, query):
        return ContentMatches(
            predicate=select(SqlConversationItem.conversation_id)
            .where(
                SqlConversationItem.workspace_id == current_workspace_id(),
                SqlConversationItem.conversation_id == SqlConversation.id,
                SqlConversationItem.search_text.ilike(f"%{query.lower()}%"),
            )
            .exists(),
            completeness="complete",
        )

    with (
        patch.object(store_module, "select_content_search", return_value=legacy),
        patch.object(store_module, "_fetch_search_snippets", _legacy_snippets),
    ):
        expected = pages()
    assert actual == expected
    ids = {conv["id"] for page in actual for conv in page["data"]}
    assert not ids.intersection({hidden.id, archived.id, child.id})
    if query != "absent":
        assert title_only.id in ids
        assert all(
            conv["search_snippet"] is None
            for page in actual
            for conv in page["data"]
            if conv["id"] == title_only.id
        )


def test_snippet_lookup_stops_at_first_position(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    cid = conversation_store.create_conversation(title="needle").id
    _append(conversation_store, cid, ["unrelated", "early needle", "late needle"])
    title_id = conversation_store.create_conversation(title="needle").id
    with _captured_sql(conversation_store) as statements:
        with conversation_store._conv_session("test_search_snippets") as session:
            assert store_module._fetch_search_snippets(session, [cid, title_id], "needle") == {
                cid: "early needle"
            }
    sql = " ".join(statements)
    assert "group by" not in sql
    assert "order by conversation_items.position" in sql
    assert "limit" in sql
    assert "left outer join" in sql
    if conversation_store._conv_engine.dialect.name == "postgresql":
        assert "left outer join lateral" in sql
    else:
        assert "lateral" not in sql


@pytest.mark.parametrize("completeness", ["complete", "overflow"])
def test_candidates_locators_and_overflow(
    conversation_store: SqlAlchemyConversationStore, completeness: str
) -> None:
    from omnigent.stores.conversation_store.content_search import ContentMatches

    cid = conversation_store.create_conversation().id
    other = conversation_store.create_conversation().id
    title_id = conversation_store.create_conversation(title="needle").id
    _append(conversation_store, cid, ["unrelated", "early needle", "late needle"])
    _append(conversation_store, other, ["other needle"])

    def strategy(session, query):
        return ContentMatches(
            predicate=SqlConversation.id == cid,
            completeness=completeness,
            earliest_match_positions={cid: 1 if completeness == "complete" else 2},
        )

    with _captured_sql(conversation_store) as statements:
        with patch.object(store_module, "select_content_search", return_value=strategy):
            page = conversation_store.list_conversations(search_query="needle")
    by_id = {conv.id: conv.search_snippet for conv in page.data}
    assert by_id[cid] == "early needle"
    assert by_id[title_id] is None
    assert (other in by_id) == (completeness == "overflow")
    if completeness == "complete":
        item_sql = [sql for sql in statements if "conversation_items" in sql]
        assert len(item_sql) == 1
        assert "like" not in item_sql[0]
