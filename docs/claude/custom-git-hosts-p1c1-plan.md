# Custom git hosts — Plan 3 (P1c-1): encrypted-at-rest user credentials

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user register a per-host git credential (a token/PAT) that is **encrypted at rest** with a rotatable key, stored as an opaque server-owned slot, bound to an operator-defined host — the persistence foundation the P1c-2 server→runner fetch/push handoff will consume.

**Architecture:** A new `omnigent/git_hosts/crypto.py` wraps a `MultiFernet` key list (env-provided, rotatable). A new `SqlGitCredential(OmnigentBase)` table mirrors `SqlHost` (composite `(workspace_id, id)` PK, `Uuid16`, no FK, workspace-scoped) with a `token_ciphertext` column. `GitCredentialStore` encrypts on write and decrypts **only** in an explicit `resolve_token()`; the `GitCredential` entity deliberately omits the secret so it can never ride a list/get response. `create_git_credentials_router` exposes CRUD: the user supplies only a `host_id` (validated against `app.state.git_hosts`) and the token; every authority field (owner, workspace, provider) is server-derived.

**Tech Stack:** Python 3.13-compatible, SQLAlchemy + Alembic (auto-run at startup), `cryptography.fernet` (new explicit dep), FastAPI, pytest.

## Global Constraints

- **Package manager:** `uv` only. `uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`. Never `pip`. Add deps with `uv add`.
- **No linter suppressions:** never `# noqa` / `# type: ignore`. Fix root causes. Avoid blind `except Exception` (ruff BLE001) — catch the specific exception.
- **Style:** `from __future__ import annotations` first; frozen dataclasses for value types where the codebase does (note: existing store entities like `Host` are plain `@dataclass`, not frozen — match the neighbor you mirror); Sphinx `:param:` docstrings; comments describe the scenario, not the change.
- **Security invariants (design §8.2, §8.3 — enforce, don't just document):**
  - The row **`id` is the opaque credential slot**. `owner_user_id` and `workspace_id` come from the authenticated request; `provider` is derived from the operator host config for `host_id`. The route **rejects** any client-supplied owner/workspace/provider.
  - `host_id` **must** reference a host in `app.state.git_hosts`; attaching a credential to an unknown host is a 4xx.
  - The token is **encrypted immediately** on write and **decrypted only** in `GitCredentialStore.resolve_token()` (called server-side by the future handoff). `create`/`list`/`get` responses and the `GitCredential` entity **never** contain the token or ciphertext.
  - Fernet **key list** from `OMNIGENT_GIT_CREDENTIAL_KEYS` (comma-separated; the **first** key encrypts, **all** keys decrypt → rotation). Unset → the credential store is disabled (router not mounted), mirroring how `host_store` is optional.
  - **No DB foreign keys** (Rule R032); no partial indexes; app-enforced uniqueness via a real `UniqueConstraint`.
- **Migrations:** adding the model is **not** enough — on an already-migrated prod DB the `create_all` safety net does not fire. A hand-written Alembic migration (new head chained off `z7a2b3c4d5e6`) is **required**.
- **Backward compatibility:** with `OMNIGENT_GIT_CREDENTIAL_KEYS` unset, nothing changes — the router isn't mounted, no new required env, existing behavior byte-identical.
- **Commit discipline:** one commit per task; `pre-commit run --files <changed>` before each.

---

## File Structure

- `omnigent/git_hosts/crypto.py` — NEW: `GitCredentialCipher` (MultiFernet wrapper) + `load_cipher_from_env`.
- `omnigent/db/db_models.py` — add `SqlGitCredential(OmnigentBase)`.
- `omnigent/db/migrations/versions/z8a2b3c4d5e6_add_git_credentials_table.py` — NEW migration (head).
- `omnigent/stores/git_credential_store.py` — NEW: `GitCredential` dataclass + `GitCredentialStore`.
- `omnigent/server/routes/git_credentials.py` — NEW: `create_git_credentials_router`.
- `omnigent/server/app.py`, `omnigent/cli.py`, `deploy/docker/entrypoint.py` — construct + inject `git_credential_store`, mount the router.
- `pyproject.toml` — add `cryptography`.
- Tests: `tests/git_hosts/test_crypto.py`, `tests/stores/test_git_credential_store.py`, `tests/server/test_git_credentials_route.py`.

Controller note: line numbers are anchors from commit `cb4e7615` — implementers locate the quoted text, never trust raw numbers. Tasks 1 and 3 are haiku-able (self-contained, full code given); Tasks 2, 4, 5 touch large existing files / migrations — sonnet.

---

### Task 1: `cryptography` dependency + the cipher module

**Files:**
- Modify: `pyproject.toml`
- Create: `omnigent/git_hosts/crypto.py`
- Test: `tests/git_hosts/test_crypto.py`

**Interfaces:**
- Produces: `GitCredentialCipher(keys: list[str])` with `.encrypt(plaintext: str) -> str` and `.decrypt(token: str) -> str` (raises `ValueError` on empty key list; `.decrypt` raises `cryptography.fernet.InvalidToken` on a token no key can decrypt); `load_cipher_from_env(env: Mapping[str, str] | None = None) -> GitCredentialCipher | None` (`None` when `OMNIGENT_GIT_CREDENTIAL_KEYS` is unset/empty; raises `RuntimeError` naming the env var when a key is malformed).

- [ ] **Step 1: Add the dependency**

Run: `uv add 'cryptography>=43'`
Then confirm it landed in `[project] dependencies` in `pyproject.toml` (match the existing pin style there). **Rationale (document in the report):** `cryptography` was only a latent transitive dep (imported unguarded in `omnigent/inner/egress/ca.py`); this makes it explicit for the at-rest cipher. Latest stable is 49.0.0, but the `databricks` extra pulls `mlflow`, which caps `cryptography<49` — a documented compatibility ceiling — so the floor is `>=43` (mlflow's own floor; Fernet predates it by years; the lock already resolves 48.0.1). Do **not** use `>=49` (it has no overlap with the mlflow cap and fails resolution).

- [ ] **Step 2: Write the failing test**

Create `tests/git_hosts/test_crypto.py`:

```python
"""Tests for :mod:`omnigent.git_hosts.crypto`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken

from omnigent.git_hosts.crypto import GitCredentialCipher, load_cipher_from_env


def test_encrypt_decrypt_roundtrip() -> None:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    token = cipher.encrypt("ghp_secret")
    assert token != "ghp_secret"  # ciphertext, not plaintext
    assert cipher.decrypt(token) == "ghp_secret"


def test_empty_key_list_rejected() -> None:
    with pytest.raises(ValueError, match="at least one"):
        GitCredentialCipher([])


def test_key_rotation_old_key_still_decrypts() -> None:
    old = Fernet.generate_key().decode()
    new = Fernet.generate_key().decode()
    old_cipher = GitCredentialCipher([old])
    token = old_cipher.encrypt("s3cret")
    # New primary key first, old key retained -> can still decrypt the old token,
    # and new writes use the new key.
    rotated = GitCredentialCipher([new, old])
    assert rotated.decrypt(token) == "s3cret"
    fresh = rotated.encrypt("s3cret")
    assert GitCredentialCipher([new]).decrypt(fresh) == "s3cret"


def test_decrypt_unknown_token_raises() -> None:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    other = GitCredentialCipher([Fernet.generate_key().decode()]).encrypt("x")
    with pytest.raises(InvalidToken):
        cipher.decrypt(other)


def test_load_cipher_from_env_absent_returns_none() -> None:
    assert load_cipher_from_env({}) is None
    assert load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": "  "}) is None


def test_load_cipher_from_env_parses_key_list() -> None:
    k1, k2 = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    cipher = load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": f"{k1}, {k2}"})
    assert cipher is not None
    assert cipher.decrypt(cipher.encrypt("v")) == "v"


def test_load_cipher_from_env_malformed_key_raises() -> None:
    with pytest.raises(RuntimeError, match="OMNIGENT_GIT_CREDENTIAL_KEYS"):
        load_cipher_from_env({"OMNIGENT_GIT_CREDENTIAL_KEYS": "not-a-valid-fernet-key"})
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/git_hosts/test_crypto.py -v`
Expected: FAIL — no module `omnigent.git_hosts.crypto`.

- [ ] **Step 4: Implement**

Create `omnigent/git_hosts/crypto.py`:

```python
"""Encrypt/decrypt per-user git credentials at rest with a rotatable key list.

Uses :class:`cryptography.fernet.MultiFernet`: the first key in the list
encrypts new tokens, and any key in the list can decrypt — so rotating a key
means prepending a new key while retaining the old one until re-encryption.
The key list is operator-provided out-of-band (``OMNIGENT_GIT_CREDENTIAL_KEYS``);
this module never persists a key.
"""

from __future__ import annotations

from collections.abc import Mapping
import os

from cryptography.fernet import Fernet, MultiFernet

_ENV_VAR = "OMNIGENT_GIT_CREDENTIAL_KEYS"


class GitCredentialCipher:
    """Symmetric encrypt/decrypt over a rotatable Fernet key list.

    :param keys: One or more urlsafe-base64 Fernet keys; the first encrypts.
    :raises ValueError: When *keys* is empty or a key is malformed.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("at least one Fernet key is required")
        # Fernet(...) raises ValueError on a malformed key.
        self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        """Return the ciphertext token for *plaintext* (encrypted with the first key)."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Return the plaintext for *token*.

        :raises cryptography.fernet.InvalidToken: When no key can decrypt it.
        """
        return self._fernet.decrypt(token.encode()).decode()


def load_cipher_from_env(env: Mapping[str, str] | None = None) -> GitCredentialCipher | None:
    """Build a cipher from ``OMNIGENT_GIT_CREDENTIAL_KEYS``, or ``None`` if unset.

    :param env: Environment mapping (defaults to ``os.environ``).
    :returns: A :class:`GitCredentialCipher`, or ``None`` when the var is unset/blank
        (the credential store is then disabled).
    :raises RuntimeError: When the var is set but contains a malformed key.
    """
    source = os.environ if env is None else env
    raw = source.get(_ENV_VAR, "").strip()
    if not raw:
        return None
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        return GitCredentialCipher(keys)
    except ValueError as exc:
        raise RuntimeError(
            f"{_ENV_VAR} contains an invalid Fernet key; each comma-separated entry must be "
            "a urlsafe-base64 32-byte key (generate with "
            "`python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`)"
        ) from exc
```

- [ ] **Step 5: Run to verify pass**

Run: `uv run pytest tests/git_hosts/test_crypto.py -v`
Expected: PASS (7 tests).

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add pyproject.toml uv.lock omnigent/git_hosts/crypto.py tests/git_hosts/test_crypto.py
pre-commit run --files pyproject.toml omnigent/git_hosts/crypto.py tests/git_hosts/test_crypto.py
git commit -m "feat(git-hosts): rotatable Fernet cipher for credential-at-rest"
```

---

### Task 2: `SqlGitCredential` model + Alembic migration

**Files:**
- Modify: `omnigent/db/db_models.py` (add the class near `SqlHost` ~:1063-1150)
- Create: `omnigent/db/migrations/versions/z8a2b3c4d5e6_add_git_credentials_table.py`
- Test: `tests/db/test_git_credentials_schema.py`

**Interfaces:**
- Produces: `SqlGitCredential(OmnigentBase)`, `__tablename__ = "git_credentials"`, PK `(workspace_id, id)`, columns: `owner_user_id: String(256)`, `host_id: String(256)`, `provider: String(32)`, `username: String(256) | None`, `token_ciphertext: Text`, `created_at`/`updated_at: Integer`; `UniqueConstraint("workspace_id","owner_user_id","host_id", name="uq_git_credentials_workspace_owner_host")`; no FK. Migration `revision="z8a2b3c4d5e6"`, `down_revision="z7a2b3c4d5e6"`.

- [ ] **Step 1: Write the failing test**

Create `tests/db/test_git_credentials_schema.py` (mirror an existing `tests/db/` or `tests/stores/` schema test for the engine fixture; if none, use `get_or_create_engine` against a temp sqlite path from `tmp_path`):

```python
"""The git_credentials table is created and round-trips a row."""

from __future__ import annotations

from sqlalchemy import select

from omnigent.db.db_models import SqlGitCredential
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch


def test_git_credentials_table_roundtrips(tmp_path) -> None:
    engine = get_or_create_engine(f"sqlite:///{tmp_path}/t.db")
    session_maker = make_managed_session_maker(engine)
    with session_maker() as session:
        session.add(
            SqlGitCredential(
                id="0123456789abcdef0123456789abcdef",
                owner_user_id="alice@example.com",
                host_id="acme-forgejo",
                provider="forgejo",
                username="alice",
                token_ciphertext="gAAAA-fake-ciphertext",
                created_at=now_epoch(),
                updated_at=now_epoch(),
            )
        )
    with session_maker() as session:
        row = session.execute(
            select(SqlGitCredential).where(SqlGitCredential.host_id == "acme-forgejo")
        ).scalar_one()
        assert row.owner_user_id == "alice@example.com"
        assert row.token_ciphertext == "gAAAA-fake-ciphertext"
        assert row.workspace_id == 0  # single-tenant default
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/db/test_git_credentials_schema.py -v`
Expected: FAIL — `ImportError: cannot import name 'SqlGitCredential'`.

- [ ] **Step 3: Implement the model** (in `db_models.py`, after `SqlHost`; reuse the file's existing imports — `Uuid16`, `current_workspace_id`, `BigInteger`, `String`, `Text`, `Integer`, `UniqueConstraint`, `mapped_column`, `Mapped`)

```python
class SqlGitCredential(OmnigentBase):
    """A per-user, per-host git credential, encrypted at rest.

    The ``id`` is an opaque server-minted slot; ``owner_user_id`` and
    ``workspace_id`` come from the authenticated request, and ``provider`` is a
    validated snapshot of the operator host config. ``token_ciphertext`` is a
    Fernet token (see :mod:`omnigent.git_hosts.crypto`) — the plaintext is never
    stored. No foreign key (Rule R032); uniqueness is application-declared.
    """

    __tablename__ = "git_credentials"

    workspace_id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        nullable=False,
        server_default="0",
        default=current_workspace_id,
    )
    id: Mapped[str] = mapped_column(Uuid16(), primary_key=True)
    owner_user_id: Mapped[str] = mapped_column(String(256), nullable=False)
    host_id: Mapped[str] = mapped_column(String(256), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    username: Mapped[str | None] = mapped_column(String(256), nullable=True)
    token_ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[int] = mapped_column(Integer)

    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "owner_user_id",
            "host_id",
            name="uq_git_credentials_workspace_owner_host",
        ),
    )
```

- [ ] **Step 4: Write the migration** — read `omnigent/db/migrations/versions/z6a2b3c4d5e6_add_scheduled_tasks_tables.py` first and copy its exact `op.create_table` idiom (imports, `Uuid16()` column type, `PrimaryKeyConstraint`, docstring citing no-FK). Create `omnigent/db/migrations/versions/z8a2b3c4d5e6_add_git_credentials_table.py`:

```python
"""add git_credentials table

Revision ID: z8a2b3c4d5e6
Revises: z7a2b3c4d5e6
Create Date: (leave the alembic-generated placeholder or omit)

Per-user, per-host git credentials, encrypted at rest. No foreign keys
(Rule R032); the application enforces the (workspace_id, owner_user_id,
host_id) uniqueness declared here.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "z8a2b3c4d5e6"
down_revision = "z7a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "git_credentials",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("owner_user_id", sa.String(length=256), nullable=False),
        sa.Column("host_id", sa.String(length=256), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("username", sa.String(length=256), nullable=True),
        sa.Column("token_ciphertext", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=True),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "owner_user_id",
            "host_id",
            name="uq_git_credentials_workspace_owner_host",
        ),
    )


def downgrade() -> None:
    op.drop_table("git_credentials")
```

Match the `Uuid16` import path to how the sibling migration imports it (it may import from `omnigent.db.db_models` or a types module — mirror exactly).

- [ ] **Step 5: Run to verify pass** (fresh sqlite in the test → full migration run → table exists)

Run: `uv run pytest tests/db/test_git_credentials_schema.py -v`
Expected: PASS. Also verify the migration chain is linear: `uv run python -c "from alembic.config import Config; from alembic.script import ScriptDirectory; from omnigent.db.utils import _build_alembic_config; s=ScriptDirectory.from_config(_build_alembic_config('sqlite:///:memory:')); print('head:', s.get_current_head())"` prints `z8a2b3c4d5e6` (adjust the helper name if `_build_alembic_config` differs; the goal is: assert a single head, and it is our new revision).

- [ ] **Step 6: Commit**

```bash
uv run ruff check --fix omnigent/db tests/db && uv run ruff format omnigent/db tests/db
git add omnigent/db/db_models.py omnigent/db/migrations/versions/z8a2b3c4d5e6_add_git_credentials_table.py tests/db/test_git_credentials_schema.py
pre-commit run --files omnigent/db/db_models.py omnigent/db/migrations/versions/z8a2b3c4d5e6_add_git_credentials_table.py tests/db/test_git_credentials_schema.py
git commit -m "feat(git-hosts): SqlGitCredential table + migration (encrypted-at-rest)"
```

---

### Task 3: `GitCredential` entity + `GitCredentialStore`

**Files:**
- Create: `omnigent/stores/git_credential_store.py`
- Test: `tests/stores/test_git_credential_store.py`

**Interfaces:**
- Consumes: `SqlGitCredential` (Task 2); `GitCredentialCipher` (Task 1); `get_or_create_engine`, `make_managed_session_maker`, `now_epoch` (`omnigent.db.utils`); `current_workspace_id` (`omnigent.db.db_models`).
- Produces: `GitCredential` dataclass (`id`, `owner_user_id`, `host_id`, `provider`, `username`, `created_at`, `updated_at` — **no token/ciphertext field**); `GitCredentialStore(storage_location: str, cipher: GitCredentialCipher)` with `create(*, owner_user_id, host_id, provider, username, token) -> GitCredential` (raises `ValueError` on duplicate `(owner_user_id, host_id)`), `list_for_owner(owner_user_id) -> list[GitCredential]`, `get(credential_id) -> GitCredential | None`, `delete(credential_id) -> None`, `resolve_token(*, owner_user_id, host_id) -> str | None` (decrypts; the only method that returns plaintext).

- [ ] **Step 1: Write the failing test**

Create `tests/stores/test_git_credential_store.py`:

```python
"""Tests for :class:`omnigent.stores.git_credential_store.GitCredentialStore`."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from omnigent.git_hosts.crypto import GitCredentialCipher
from omnigent.stores.git_credential_store import GitCredential, GitCredentialStore


def _store(tmp_path) -> GitCredentialStore:
    cipher = GitCredentialCipher([Fernet.generate_key().decode()])
    return GitCredentialStore(f"sqlite:///{tmp_path}/creds.db", cipher)


def test_create_returns_entity_without_secret(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice", host_id="acme-forgejo", provider="forgejo",
        username="alice", token="ghp_secret",
    )
    assert isinstance(cred, GitCredential)
    assert cred.host_id == "acme-forgejo"
    assert cred.provider == "forgejo"
    # The entity must not expose the secret in any field.
    assert "ghp_secret" not in repr(cred)
    assert not hasattr(cred, "token")
    assert not hasattr(cred, "token_ciphertext")


def test_resolve_token_roundtrips(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(owner_user_id="alice", host_id="h", provider="forgejo", username=None, token="tok")
    assert store.resolve_token(owner_user_id="alice", host_id="h") == "tok"
    assert store.resolve_token(owner_user_id="bob", host_id="h") is None


def test_list_is_owner_scoped(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(owner_user_id="alice", host_id="h1", provider="forgejo", username=None, token="a")
    store.create(owner_user_id="bob", host_id="h2", provider="gitea", username=None, token="b")
    alice = store.list_for_owner("alice")
    assert [c.host_id for c in alice] == ["h1"]


def test_duplicate_owner_host_rejected(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(owner_user_id="alice", host_id="h", provider="forgejo", username=None, token="a")
    with pytest.raises(ValueError, match="already"):
        store.create(owner_user_id="alice", host_id="h", provider="forgejo", username=None, token="b")


def test_delete_then_absent(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(owner_user_id="alice", host_id="h", provider="forgejo", username=None, token="a")
    store.delete(cred.id)
    assert store.get(cred.id) is None
    assert store.resolve_token(owner_user_id="alice", host_id="h") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/stores/test_git_credential_store.py -v`
Expected: FAIL — no module `omnigent.stores.git_credential_store`.

- [ ] **Step 3: Implement** — read `omnigent/stores/host_store.py` (`HostStore.__init__`, `_row_to_host`, the `current_workspace_id()` filter idiom) and `omnigent/stores/agent_store/sqlalchemy_store.py` (the create-with-uniqueness-pre-check) first, then mirror. Mint `id` the same way the agent store mints its `Uuid16` id (find its id-minting call — likely `uuid.uuid4().hex` — and use the identical form).

```python
"""Encrypted-at-rest per-user git credentials.

The store encrypts the token on :meth:`create` and decrypts it *only* in
:meth:`resolve_token` (called server-side when composing a launch/handoff).
The :class:`GitCredential` entity deliberately omits the secret so it can never
be serialized into an API response.
"""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from sqlalchemy import delete as sa_delete, select

from omnigent.db.db_models import SqlGitCredential, current_workspace_id
from omnigent.db.utils import get_or_create_engine, make_managed_session_maker, now_epoch
from omnigent.git_hosts.crypto import GitCredentialCipher


@dataclass
class GitCredential:
    """A stored git credential's non-secret metadata."""

    id: str
    owner_user_id: str
    host_id: str
    provider: str
    username: str | None
    created_at: int
    updated_at: int


def _row_to_entity(row: SqlGitCredential) -> GitCredential:
    return GitCredential(
        id=row.id,
        owner_user_id=row.owner_user_id,
        host_id=row.host_id,
        provider=row.provider,
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
        username: str | None,
        token: str,
    ) -> GitCredential:
        """Encrypt *token* and store a new credential.

        :raises ValueError: When this owner already has a credential for *host_id*.
        """
        now = now_epoch()
        row = SqlGitCredential(
            id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            host_id=host_id,
            provider=provider,
            username=username,
            token_ciphertext=self._cipher.encrypt(token),
            created_at=now,
            updated_at=now,
        )
        with self._session() as session:
            # (workspace_id, owner_user_id, host_id) is unique; MySQL has no
            # partial index, so the store checks before insert.
            existing = session.execute(
                select(SqlGitCredential.id).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.owner_user_id == owner_user_id,
                    SqlGitCredential.host_id == host_id,
                )
            ).first()
            if existing is not None:
                raise ValueError(
                    f"a git credential for host {host_id!r} already exists for this user"
                )
            session.add(row)
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

    def get(self, credential_id: str) -> GitCredential | None:
        with self._session() as session:
            row = session.execute(
                select(SqlGitCredential).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.id == credential_id,
                )
            ).scalar_one_or_none()
            return _row_to_entity(row) if row is not None else None

    def delete(self, credential_id: str) -> None:
        with self._session() as session:
            session.execute(
                sa_delete(SqlGitCredential).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.id == credential_id,
                )
            )

    def resolve_token(self, *, owner_user_id: str, host_id: str) -> str | None:
        """Decrypt and return the token for (owner, host), or ``None`` if absent.

        The only method that returns plaintext; call server-side only.
        """
        with self._session() as session:
            row = session.execute(
                select(SqlGitCredential).where(
                    SqlGitCredential.workspace_id == current_workspace_id(),
                    SqlGitCredential.owner_user_id == owner_user_id,
                    SqlGitCredential.host_id == host_id,
                )
            ).scalar_one_or_none()
            if row is None:
                return None
            return self._cipher.decrypt(row.token_ciphertext)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/stores/test_git_credential_store.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/stores tests/stores && uv run ruff format omnigent/stores tests/stores
git add omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py
pre-commit run --files omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py
git commit -m "feat(git-hosts): GitCredentialStore — encrypt on write, decrypt only on resolve"
```

---

### Task 4: `create_git_credentials_router`

**Files:**
- Create: `omnigent/server/routes/git_credentials.py`
- Test: `tests/server/test_git_credentials_route.py`

**Interfaces:**
- Consumes: `GitCredentialStore` (Task 3); `app.state.git_hosts` (from P1b — a `tuple[HostConfig, ...]`); the auth helper the other routes use (`require_user` / `_require_user`); `OmnigentError`/`ErrorCode` or `HTTPException` per the neighboring route's convention.
- Produces: `create_git_credentials_router(git_credential_store, git_hosts, *, auth_provider=None) -> APIRouter` mounting `POST /v1/git-credentials`, `GET /v1/git-credentials`, `DELETE /v1/git-credentials/{credential_id}`. Request model carries only `host_id` + `token` (+ optional `username`); the response model has **no** token field. `provider` is derived from the matching `HostConfig`; an unknown `host_id` → 4xx; the token is encrypted via the store immediately; ownership is enforced on delete.

- [ ] **Step 1: Write the failing test** — read `omnigent/server/routes/default_policies.py` (POST/DELETE shape, error codes) and `tests/server/test_app_git_hosts.py` (how to build a real `create_app` with stores) first. Mount the router on a minimal app with a `GitCredentialStore` and a one-host `git_hosts`. Assert: POST with a configured `host_id` returns 200/201 with the credential metadata and **no** token; POST with an unknown `host_id` returns 4xx; the created credential appears in GET; a second POST for the same host returns a conflict; DELETE removes it. Also assert the response body never contains the submitted token string.

(Write the concrete test using the real fixtures — model the app construction on `tests/server/test_app_git_hosts.py`'s `app_factory`, extended to also pass a `git_credential_store` and mount `create_git_credentials_router`.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/server/test_git_credentials_route.py -v`
Expected: FAIL — no module / router.

- [ ] **Step 3: Implement** — mirror `default_policies.py`'s router factory, request/response Pydantic models (define them in this module or `schemas.py` following the neighbor), the `IntegrityError`/`ValueError` → `CONFLICT` mapping, and the ownership 404/403 idiom from `hosts.py:385-391`. Derive `provider` by finding the `HostConfig` in `git_hosts` whose `id == body.host_id`; if none, raise the validation error. Encrypt happens in `store.create`. Off-thread store calls via `asyncio.to_thread` as the neighbors do. The response model exposes `id, host_id, provider, username, created_at` — never the token.

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/server/test_git_credentials_route.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/git_credentials.py tests/server/test_git_credentials_route.py omnigent/server/schemas.py
pre-commit run --files omnigent/server/routes/git_credentials.py tests/server/test_git_credentials_route.py
git commit -m "feat(git-hosts): /v1/git-credentials CRUD router (token never returned)"
```

---

### Task 5: Startup wiring

**Files:**
- Modify: `omnigent/server/app.py` (`create_app` param + `app.state` + router mount, mirroring `host_store`)
- Modify: `omnigent/cli.py`, `deploy/docker/entrypoint.py` (construct the store when the cipher env is set)
- Test: `tests/server/test_app_git_hosts.py` (extend)

**Interfaces:**
- Consumes: `load_cipher_from_env` (Task 1), `GitCredentialStore` (Task 3), `create_git_credentials_router` (Task 4), the resolved `db_uri` used for `HostStore`.
- Produces: `create_app(..., git_credential_store: GitCredentialStore | None = None)` → `app.state.git_credential_store`; when non-`None`, mount `create_git_credentials_router(git_credential_store, git_hosts, auth_provider=...)`. In `cli.py`/`entrypoint.py`: `cipher = load_cipher_from_env(); git_credential_store = GitCredentialStore(db_uri, cipher) if cipher is not None else None`, passed to `create_app`.

- [ ] **Step 1: Write the failing test** (extend `tests/server/test_app_git_hosts.py`)

```python
def test_git_credential_store_absent_by_default(app_factory) -> None:
    app = app_factory()
    assert getattr(app.state, "git_credential_store", None) is None


def test_git_credential_router_mounted_when_store_present(app_factory) -> None:
    from cryptography.fernet import Fernet

    from omnigent.git_hosts.crypto import GitCredentialCipher
    from omnigent.stores.git_credential_store import GitCredentialStore

    store = GitCredentialStore("sqlite://", GitCredentialCipher([Fernet.generate_key().decode()]))
    app = app_factory(git_credential_store=store)
    assert app.state.git_credential_store is store
    assert any(r.path == "/v1/git-credentials" for r in app.routes)
```

(If an in-memory `sqlite://` engine is shared across sessions problematically in the fixture, use a `tmp_path` file URI instead — match how the neighboring store tests construct one.)

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/server/test_app_git_hosts.py -v -k git_credential`
Expected: FAIL.

- [ ] **Step 3: Implement** the three wiring edits, mirroring the `host_store` param/assignment/conditional-mount exactly.

- [ ] **Step 4: Run to verify pass + no regression**

Run: `uv run pytest tests/server/test_app_git_hosts.py -q && uv run pytest tests/server/test_managed_hosts.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/app.py omnigent/cli.py deploy/docker/entrypoint.py tests/server/test_app_git_hosts.py
pre-commit run --files omnigent/server/app.py omnigent/cli.py deploy/docker/entrypoint.py tests/server/test_app_git_hosts.py
git commit -m "feat(git-hosts): wire git_credential_store + router at startup (opt-in via key env)"
```

---

### Task 6: Full-suite gate

**Files:** none (verification only).

- [ ] **Step 1:** `uv run pytest tests/git_hosts tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py tests/server/test_git_credentials_route.py tests/server/test_app_git_hosts.py -q` — all pass.
- [ ] **Step 2:** `uv run pytest tests/server/test_managed_hosts.py tests/server/integration/test_host_session_binding.py -q` — no P1b regression.
- [ ] **Step 3:** `uv run ruff check omnigent/git_hosts omnigent/stores/git_credential_store.py omnigent/server/routes/git_credentials.py omnigent/db && uv run ruff format --check omnigent/git_hosts omnigent/stores/git_credential_store.py` — clean; `grep -rn "noqa\|type: ignore" omnigent/git_hosts omnigent/stores/git_credential_store.py omnigent/server/routes/git_credentials.py` empty.
- [ ] **Step 4:** Migration linearity: confirm a single Alembic head and it is `z8a2b3c4d5e6` (per Task 2 Step 5).
- [ ] **Step 5:** Secret-hygiene grep: `grep -rn "token" omnigent/server/routes/git_credentials.py` — confirm the token appears only in the request model / the `store.create(token=...)` call, never in a response model or log line.

---

## What this plan does NOT do (next plans)

- **P1c-2:** the authenticated confidential server→runner fetch/push handoff that calls `resolve_token()` and delivers the credential to the runner parent (design §8.5) — the consumer of this store; the credential-host-IDs-in-scope persistence and the reconciliation sweeper for operator-removed hosts.
- **P1c-3:** k8s init-container Secret delivery + the §8.4 askpass/secret-env channel replacing the exec env-prefix interim mechanism.
- **P1c-4:** commit identity = session starter; the session-sharing credential notice (§8.6/§8.7).
- Carried minors from P1a/P1b remain ledgered.

## Self-Review

- **Spec coverage:** §8.2 encrypted-at-rest user creds (Fernet key list) → Tasks 1-3; §8.3 opaque-slot id + server-derived authority + host-must-exist → Tasks 3-4; portability (no FK, real UniqueConstraint, Uuid16, workspace-scoped, migration) → Task 2; opt-in/backward-compat (unset key → disabled) → Tasks 1,5. The token-decrypt-only-on-resolve and never-returned invariants are enforced in Task 3 (entity omits secret) and Task 4 (response omits token) and gated in Task 6 Step 5.
- **Placeholder scan:** Tasks 4's test/impl and Task 5's fixture say "mirror the neighbor" for app-construction boilerplate that is repo-specific — every novel unit (crypto, model, migration, store) ships complete code. No TBDs.
- **Type consistency:** `GitCredentialCipher.encrypt/decrypt: str->str` used by the store (Task 3); `GitCredentialStore(storage_location, cipher)` signature identical in Tasks 3/5; `create(*, owner_user_id, host_id, provider, username, token)` identical in Task 3 impl and Task 4 route; `SqlGitCredential` column names identical across model (Task 2), migration (Task 2), and store (Task 3).
