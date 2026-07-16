"""Session lifecycle invariants introduced by the Projects schema."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from omnigent.db.db_models import (
    SqlConversationMetadata,
    SqlSessionProjectSnapshot,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_projectless_create_flow_is_unchanged(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)

    conversation = store.create_conversation()

    assert conversation.project_id is None
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        metadata = session.get(SqlConversationMetadata, (0, conversation.id))
        snapshot = session.get(SqlSessionProjectSnapshot, (0, conversation.id))
    assert metadata is not None and metadata.project_id is None
    assert snapshot is None


async def test_session_delete_removes_project_snapshot_with_metadata(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation = store.create_conversation()
    engine = get_or_create_engine(db_uri)
    with Session(engine) as session:
        metadata = session.get(SqlConversationMetadata, (0, conversation.id))
        assert metadata is not None
        metadata.project_id = "proj_legacy"
        session.add(
            SqlSessionProjectSnapshot(
                session_id=conversation.id,
                project_id="proj_legacy",
                snapshot_origin="backfill",
                project_row_version=None,
                defaults_schema_version=1,
                defaults_json="{}",
                created_at=1,
            )
        )
        session.commit()

    assert await store.delete_conversation(conversation.id) is True

    with Session(engine) as session:
        assert (
            session.execute(
                select(SqlConversationMetadata).where(
                    SqlConversationMetadata.id == conversation.id
                )
            ).scalar_one_or_none()
            is None
        )
        assert (
            session.execute(
                select(SqlSessionProjectSnapshot).where(
                    SqlSessionProjectSnapshot.session_id == conversation.id
                )
            ).scalar_one_or_none()
            is None
        )
