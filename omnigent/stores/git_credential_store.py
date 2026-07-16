"""Encrypted-at-rest per-user git credentials.

The store encrypts the token on :meth:`create` and decrypts it *only* in
:meth:`resolve_token` (called server-side when composing a launch/handoff).
The :class:`GitCredential` entity deliberately omits the secret so it can never
be serialized into an API response.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete as sa_delete
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from omnigent.db.db_models import InvalidUuidError, SqlGitCredential, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.git_hosts.crypto import GitCredentialCipher


@dataclass
class GitCredential:
    """A stored git credential's non-secret metadata."""

    id: str
    owner_user_id: str
    host_id: str
    provider: str
    label: str
    username: str | None
    created_at: int
    updated_at: int


def _find_row(session: Session, credential_id: str) -> SqlGitCredential | None:
    """Look up a credential row by its opaque id, tolerating a malformed id.

    ``id`` is a ``Uuid16`` column: binding a value that isn't a 32-char hex
    uuid raises :class:`InvalidUuidError` wrapped in ``StatementError``
    (see ``omnigent/server/app.py``'s ``StatementError`` handler for the
    same convention at the HTTP layer). Such an id cannot address any row,
    so it is treated as not-found rather than propagating.

    :param session: The active SQLAlchemy session.
    :param credential_id: Opaque credential id to look up.
    :returns: The matching row, or ``None`` if absent or malformed.
    """
    try:
        return session.execute(
            select(SqlGitCredential).where(
                SqlGitCredential.workspace_id == current_workspace_id(),
                SqlGitCredential.id == credential_id,
            )
        ).scalar_one_or_none()
    except StatementError as exc:
        if isinstance(exc.orig, InvalidUuidError):
            return None
        raise


def _row_to_entity(row: SqlGitCredential) -> GitCredential:
    return GitCredential(
        id=row.id,
        owner_user_id=row.owner_user_id,
        host_id=row.host_id,
        provider=row.provider,
        label=row.label,
        username=row.username,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class GitCredentialStore:
    """CRUD + resolution for encrypted per-user git credentials."""

    def __init__(self, storage_location: str, cipher: GitCredentialCipher) -> None:
        self._engine = get_or_create_engine(storage_location)
        self._session = make_managed_session_maker(self._engine)
        self._cipher = cipher

    def create(
        self,
        *,
        owner_user_id: str,
        host_id: str,
        provider: str,
        label: str,
        username: str | None,
        token: str,
    ) -> GitCredential:
        """Encrypt *token* and store a new credential.

        :raises ValueError: When this owner already has a credential labeled
            *label* for *host_id*.
        """
        now = now_epoch()
        row = SqlGitCredential(
            id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            host_id=host_id,
            provider=provider,
            label=label,
            username=username,
            token_ciphertext=self._cipher.encrypt(token),
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            # (workspace_id, owner_user_id, host_id, label) is unique; MySQL has
            # no partial index. The pre-check gives a friendly error in the common
            # case; flush() makes the DB unique constraint the authoritative,
            # race-free guard for a concurrent insert.
            existing = session.execute(
                select(SqlGitCredential.id).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.owner_user_id == owner_user_id,
                    SqlGitCredential.host_id == host_id,
                    SqlGitCredential.label == label,
                )
            ).first()
            if existing is not None:
                raise ValueError(
                    f"a git credential labeled {label!r} for host {host_id!r} "
                    "already exists for this user"
                )
            session.add(row)
            try:
                session.flush()
            except IntegrityError:
                # A concurrent insert won the unique-constraint race. Translate to
                # the documented ValueError and drop the DB error from the chain —
                # its ``[parameters: …]`` carry the ciphertext.
                raise ValueError(
                    f"a git credential labeled {label!r} for host {host_id!r} "
                    "already exists for this user"
                ) from None
            return _row_to_entity(row)

    def list_for_owner(self, owner_user_id: str) -> list[GitCredential]:
        with self._session() as session:
            rows = session.execute(
                select(SqlGitCredential).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.owner_user_id == owner_user_id,
                )
            ).scalars()
            return [_row_to_entity(r) for r in rows]

    def list_for_owner_host(self, owner_user_id: str, host_id: str) -> list[GitCredential]:
        """The owner's candidate identities on *host_id* (a future selector picks one)."""
        with self._session() as session:
            rows = session.execute(
                select(SqlGitCredential).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.owner_user_id == owner_user_id,
                    SqlGitCredential.host_id == host_id,
                )
            ).scalars()
            return [_row_to_entity(r) for r in rows]

    def get(self, credential_id: str) -> GitCredential | None:
        with self._session() as session:
            row = _find_row(session, credential_id)
            return _row_to_entity(row) if row is not None else None

    def delete(self, credential_id: str) -> None:
        with self._session() as session:
            try:
                session.execute(
                    sa_delete(SqlGitCredential).where(
                        SqlGitCredential.workspace_id == current_workspace_id(),
                        SqlGitCredential.id == credential_id,
                    )
                )
            except StatementError as exc:
                # A malformed opaque id matches no row; see _find_row.
                if not isinstance(exc.orig, InvalidUuidError):
                    raise

    def resolve_token(self, credential_id: str) -> str | None:
        """Decrypt and return the token for the slot *credential_id*, or ``None``.

        Resolution is by opaque id (not ``(owner, host)``, which is ambiguous with
        multiple labeled identities). The only method that returns plaintext; call
        server-side only.
        """
        with self._session() as session:
            row = _find_row(session, credential_id)
            if row is None:
                return None
            return self._cipher.decrypt(row.token_ciphertext)
