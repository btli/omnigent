"""The git_credentials table is created and round-trips a row."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from omnigent.db.db_models import SqlGitCredential
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch


def test_git_credentials_table_roundtrips(tmp_path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path}/t.db")
    session_maker = make_managed_session_maker(engine)
    with session_maker() as session:
        # Two labeled identities for the same (owner, host) coexist (0..n).
        for label in ("personal", "work"):
            session.add(
                SqlGitCredential(
                    id=uuid.uuid4().hex,
                    owner_user_id="alice@example.com",
                    host_id="acme-forgejo",
                    provider="forgejo",
                    label=label,
                    username="alice",
                    token_ciphertext=f"gAAAA-fake-{label}",
                    created_at=now_epoch(),
                    updated_at=now_epoch(),
                )
            )
    with session_maker() as session:
        rows = (
            session.execute(
                select(SqlGitCredential).where(SqlGitCredential.host_id == "acme-forgejo")
            )
            .scalars()
            .all()
        )
        assert {r.label for r in rows} == {"personal", "work"}
        assert all(r.owner_user_id == "alice@example.com" for r in rows)
        assert all(r.workspace_id == 0 for r in rows)  # single-tenant default
