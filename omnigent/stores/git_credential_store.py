"""Encrypted-at-rest per-user git credentials.

The store encrypts the token on :meth:`create` and decrypts it *only* in
:meth:`resolve_lease` (called server-side when composing a launch/handoff).
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
from omnigent.db.enum_codecs import decode_git_credential_kind, encode_git_credential_kind
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
    kind: str
    username: str | None
    created_at: int
    updated_at: int


@dataclass(frozen=True, repr=False)
class CredentialLease:
    """A resolved, ready-to-use git credential.

    Uniform across credential ``kind`` (design §8.2) so the clone/egress
    consumer never branches on ``pat`` vs ``oauth``. ``expires_at`` is ``None``
    for a PAT (long-lived); an OAuth access token (P3) will carry its expiry.

    :param token: The decrypted bearer token/PAT. Never log, repr, or place it
        in an error message — hence the custom redacting ``__repr__``.
    :param expires_at: Unix epoch seconds after which the token is invalid, or
        ``None`` when it does not expire.
    """

    token: str
    expires_at: int | None

    def __repr__(self) -> str:
        """Redact the token so a lease can never leak via logs/tracebacks."""
        return f"CredentialLease(token=<redacted>, expires_at={self.expires_at!r})"


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


def _find_owned_row(
    session: Session,
    *,
    owner_user_id: str,
    host_id: str,
    credential_id: str,
) -> SqlGitCredential | None:
    """Look up a credential row scoped to its authorized owner and host.

    Unlike :func:`_find_row` (workspace-only), this filters the full
    authorization tuple ``(workspace, owner_user_id, host_id, id)``, so a
    caller who merely knows an id cannot address a row they do not own or a
    row bound to a different host. A malformed ``credential_id`` (not a
    32-char hex uuid) addresses no row and returns ``None`` — same tolerance
    as :func:`_find_row`.

    :param session: The active SQLAlchemy session.
    :param owner_user_id: The authenticated owner the row must belong to.
    :param host_id: The operator host id the row must be bound to.
    :param credential_id: Opaque credential slot id to look up.
    :returns: The matching row, or ``None`` if absent, malformed, or not owned.
    """
    try:
        return session.execute(
            select(SqlGitCredential).where(
                SqlGitCredential.workspace_id == current_workspace_id(),
                SqlGitCredential.owner_user_id == owner_user_id,
                SqlGitCredential.host_id == host_id,
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
        kind=decode_git_credential_kind(row.kind),
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
        kind: str = "pat",
    ) -> GitCredential:
        """Encrypt *token* and store a new credential.

        :raises ValueError: When this owner already has a credential labeled
            *label* for *host_id*.
        :raises RuntimeError: On any other database failure while inserting
            the row. The original error is not chained — its message can
            carry the INSERT parameters, including ``token_ciphertext``.
        """
        now = now_epoch()
        row = SqlGitCredential(
            id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            host_id=host_id,
            provider=provider,
            kind=encode_git_credential_kind(kind),
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
            except StatementError:
                # Any other statement error (e.g. an oversized value on MySQL)
                # carries the INSERT parameters — including token_ciphertext — in
                # its message. Drop it from the chain so the ciphertext can never
                # reach the 500 traceback log.
                raise RuntimeError("failed to store the git credential") from None
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
        """Fetch a credential's metadata by id, workspace-scoped only.

        Not an authorization boundary: ownership is enforced by the route
        layer. A handoff needing an owned, host-bound slot must use
        :meth:`resolve_lease`.
        """
        with self._session() as session:
            row = _find_row(session, credential_id)
            return _row_to_entity(row) if row is not None else None

    def delete(self, credential_id: str) -> None:
        """Delete a credential by id, workspace-scoped only.

        Not an authorization boundary: the route layer verifies the caller
        owns the credential before calling this. Tolerates a malformed id
        (matches no row).
        """
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

    def resolve_lease(
        self,
        *,
        owner_user_id: str,
        host_id: str,
        credential_id: str,
    ) -> CredentialLease | None:
        """Resolve an owned credential slot into a :class:`CredentialLease`.

        Authorization is unchanged from the former ``resolve_token``: the slot
        is resolved against the full tuple ``(workspace, owner_user_id,
        host_id, credential_id)`` and decrypted **only** on a complete match.
        A foreign owner/host, a foreign workspace (ambient, via
        :func:`current_workspace_id`), or a malformed id yields ``None`` —
        never a lease. ``credential_id`` is an identifier, not a capability.

        The lease is uniform across ``kind`` (design §8.2) so the clone/egress
        consumer never branches on ``pat`` vs ``oauth``. For a PAT
        ``expires_at`` is ``None`` (long-lived); OAuth expiry is a P3 extension.

        This is the only method that returns plaintext; call server-side only.

        :param owner_user_id: The authenticated owner the slot must belong to.
        :param host_id: The operator host id the slot must be bound to.
        :param credential_id: The opaque slot id to resolve.
        :returns: The lease, or ``None`` if no owned row matches.
        """
        with self._session() as session:
            row = _find_owned_row(
                session,
                owner_user_id=owner_user_id,
                host_id=host_id,
                credential_id=credential_id,
            )
            if row is None:
                return None
            # PAT has no expiry; oauth (P3) will compute expires_at from the
            # minted access token. The lease shape is uniform either way.
            return CredentialLease(
                token=self._cipher.decrypt(row.token_ciphertext), expires_at=None
            )
