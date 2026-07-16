# P1c-3 Implementation Plan — owner-aware resolver + binding persistence + `launch_generation` + `kind`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the managed-session launch path *owner-aware and drift-safe*: add a `kind`
discriminator + a uniform **credential lease** to the store; widen `RepoWorkspace` to carry the
non-secret `ClonePlan` fields P1c-4 needs; select the owner's credential **slot** at session create
and persist a re-validatable **relaunch binding**; add the monotonic **`launch_generation`**
anti-replay anchor; and consume the selected slot's lease at launch time — all while keeping the
github.com + ambient-`GIT_TOKEN` path byte-identical when no git-hosts / no credential store is
configured.

**Architecture:** Six thin, reviewable seams. (1) `SqlGitCredential.kind` (SmallInteger enum codec,
default `pat`) + `resolve_token` → `resolve_lease() -> CredentialLease`. (2) `resolve_repo_workspace`
copies through the `ClonePlan` fields it drops today. (3) the same resolver gains owner-aware
**slot selection** (§12.3 precedence) driven by a new optional create-request `git_credential_label`.
(4) a create-time **binding** (`host_config_id` + topology hash + canonical URL + slot id) is
persisted as server-owned labels and **re-authorized on every relaunch**, refusing only on
semantic rebind or a lost slot. (5)
`launch_generation` is a new monotonic column on `conversations`, bumped in `_run_managed_launch`
(create + relaunch), never on wake. (6) `_build_clone_env` resolves the owner's slot lease at launch,
failing closed. The secret never enters `RepoWorkspace`, a log, an error message, or a repr.
Relaunch refusal is reserved for semantic rebind / lost authorization; same-host config drift is
logged and deliberately takes effect (design §9), with the persisted binding refreshed.

**Tech Stack:** Python 3.13, SQLAlchemy 2 + Alembic (batch migrations, SQLite/MySQL/Postgres),
`cryptography.fernet` (existing), FastAPI, pytest. Package manager `uv` (prefix every uv command with
`env -u NODE_ENV`).

## Global Constraints

- **Package manager:** `uv` only, every command prefixed `env -u NODE_ENV` (a stray `NODE_ENV` breaks
  the web test path). `env -u NODE_ENV uv run pytest`, `env -u NODE_ENV uv run ruff check --fix`,
  `env -u NODE_ENV uv run ruff format`. Never `pip`.
- **No linter suppressions:** never `# noqa` / `# type: ignore`. Fix root causes. No blind
  `except Exception` (ruff BLE001) — catch the specific exception.
- **Security calls are keyword-only.** `resolve_lease` keeps the P1c-2 keyword-only signature
  (`*, owner_user_id, host_id, credential_id`) so a positional `(owner, host)` transposition is
  impossible. No client-supplied authority fields (owner/workspace/provider/slot are server-derived).
- **The secret is never observable.** The lease token must not appear in a log line, an exception
  message, a repr, or telemetry. `CredentialLease` uses `repr=False` + a redacting `__repr__`.
  `_build_clone_env`'s fail-closed error names no token.
- **DB portability (design §12, §15):** **no foreign keys** (Rule R032); **no partial indexes**;
  workspace scoping is **ambient** via `current_workspace_id()` (never a parameter); enum columns are
  `SmallInteger` + a stable int codec (`omnigent/db/enum_codecs.py`) + a `CheckConstraint`; slot ids
  are `Uuid16`. Adding a model class/column is **not** enough — every schema change ships a
  hand-written Alembic migration chained off the real current head (verified: **`z8a2b3c4d5e6`**).
- **Enum codes are STABLE and append-only** — never renumber a shipped code.
- **Backward compatibility (explicit test, Task 6):** with **no `git_hosts` configured and no
  credential store** (`app.state.git_credential_store is None`), behavior is byte-identical to today —
  github.com resolves to the built-in default, `credential_slot_id` stays `None`, and the ambient
  `GIT_TOKEN` path is untouched. Owner-aware selection and binding persistence engage **only** when a
  credential store is present (opt-in).
- **Commit discipline:** one commit per task; `env -u NODE_ENV uv run ruff check --fix <changed> &&
  env -u NODE_ENV uv run ruff format <changed>` then `pre-commit run --files <changed>` before each
  commit.
- **Out of scope (do NOT drift into these — later slices):** the sealed/ACKed `deliver_credential`
  and `invalidate_credential` tunnel frames, host-parent egress-proxy swap, repo-path-scoped rewrite
  rule, egress-rule auto-merge, ACK gating (**all P1c-4**); k8s in-Pod proxy, init-container clone
  Secret, SSH ssh-agent, tmux terminal swap (**P1c-5**); commit identity (`GIT_AUTHOR_*`) + the
  session-sharing notice (**P1c-6**); OAuth refresh/mint flows (**P3** — only the `kind` column + the
  uniform lease shape land now, `expires_at` is always `None` for the sole `pat` kind).

---

## File Structure

- `omnigent/db/enum_codecs.py` — add `GIT_CREDENTIAL_KIND` + encode/decode (Task 1).
- `omnigent/db/db_models.py` — `SqlGitCredential.kind` + CheckConstraint (Task 1); `SqlConversation.launch_generation` (Task 5).
- `omnigent/db/migrations/versions/z9a2b3c4d5e6_add_kind_to_git_credentials.py` — NEW (Task 1).
- `omnigent/db/migrations/versions/za1b2c3d4e5f_add_launch_generation_to_conversations.py` — NEW (Task 5).
- `omnigent/stores/git_credential_store.py` — `GitCredential.kind`, `create(kind=)`, `CredentialLease`, `resolve_lease` replacing `resolve_token` (Task 1).
- `omnigent/server/managed_hosts.py` — widen `RepoWorkspace` + `resolve_repo_workspace` (Task 2); `_select_credential_slot` + `CredentialSelectionError` (Task 3); binding labels + `host_config_hash` + `build_relaunch_binding_labels` + `reauthorize_relaunch_binding` + `RelaunchBindingError` (Task 4); owner-aware `_build_clone_env` + `credential_store` threading (Task 6).
- `omnigent/server/schemas.py` — `SessionCreateRequest.git_credential_label` (Task 3).
- `omnigent/server/routes/sessions.py` — create-gate wiring (Task 3); binding persist + relaunch re-auth (Task 4); `increment_launch_generation` call + `credential_store` threading (Tasks 5, 6).
- `omnigent/entities/conversation.py` — `Conversation.launch_generation` (Task 5).
- `omnigent/stores/conversation_store/__init__.py` — abstract `increment_launch_generation` (Task 5).
- `omnigent/stores/conversation_store/sqlalchemy_store.py` — `_to_conversation` mapping + `increment_launch_generation` impl (Task 5).
- Tests: `tests/stores/test_git_credential_store.py`, `tests/db/test_git_credentials_schema.py`, `tests/server/test_managed_hosts.py`, `tests/stores/test_conversation_store*` (extend the launch-generation there), `tests/server/test_app_git_hosts.py` (route no-regression).

Controller note: line numbers are anchors from commit `3c99078f` — implementers locate the quoted
text and never trust raw numbers.

---

### Task 1: `kind` discriminator + `CredentialLease` + `resolve_lease`

**Files:**
- Modify: `omnigent/db/enum_codecs.py`
- Modify: `omnigent/db/db_models.py` (add `kind` to `SqlGitCredential`, ~`:1176`, and its `CheckConstraint`, ~`:1183`)
- Create: `omnigent/db/migrations/versions/z9a2b3c4d5e6_add_kind_to_git_credentials.py`
- Modify: `omnigent/stores/git_credential_store.py`
- Test: `tests/stores/test_git_credential_store.py`, `tests/db/test_git_credentials_schema.py`

**Interfaces:**
- Consumes: `SqlGitCredential` columns; `_find_owned_row` (existing, from P1c-2); `GitCredentialCipher.decrypt`.
- Produces:
  - `GIT_CREDENTIAL_KIND: dict[str, int] = {"pat": 1, "oauth": 2}`; `encode_git_credential_kind(name: str) -> int`; `decode_git_credential_kind(code: int) -> str`.
  - `SqlGitCredential.kind: Mapped[int]` (`SmallInteger`, `nullable=False`, `server_default="1"`) + `CheckConstraint("kind IN (1, 2)", name="ck_git_credentials_kind")`.
  - `GitCredential` dataclass gains `kind: str`.
  - `GitCredentialStore.create(*, owner_user_id, host_id, provider, label, username, token, kind: str = "pat") -> GitCredential`.
  - `CredentialLease` frozen dataclass `{token: str, expires_at: int | None}` (`repr=False`, redacting `__repr__`).
  - `GitCredentialStore.resolve_lease(*, owner_user_id: str, host_id: str, credential_id: str) -> CredentialLease | None` — **replaces** `resolve_token`. Same 4-column authorization, same `InvalidUuidError → None` tolerance. `expires_at` is always `None` for P1 (pat only).

- [ ] **Step 1: Write the failing tests** — the `_store` helper and imports already exist at the top of `tests/stores/test_git_credential_store.py`. First **replace every `store.resolve_token(...)` call in that file** with the lease form. There are exactly these call sites (verified): `test_resolve_token_by_id_roundtrips` (×2), `test_resolve_token_requires_matching_owner_and_host` (×3), `test_resolve_token_malformed_id_returns_none` (×1), `test_multiple_identities_per_host_coexist` (×2), `test_delete_then_absent` (×1), `test_unknown_well_formed_id_returns_none` (×1), `test_credentials_are_workspace_isolated` (×2). The mechanical rewrite:
  - a `== "<token>"` assertion becomes `.token == "<token>"` on the lease, e.g.
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) == "tok"` →
    `store.resolve_lease(owner_user_id="alice", host_id="h", credential_id=cred.id).token == "tok"`
  - an `is None` assertion becomes the same call on `resolve_lease`, unchanged tail:
    `store.resolve_lease(owner_user_id="bob", host_id="acme-forgejo", credential_id=cred.id) is None`

  Then add these new tests at the end of the file:

```python
def test_resolve_lease_is_uniform_with_no_expiry_for_pat(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username=None,
        token="s3cret",
    )
    lease = store.resolve_lease(owner_user_id="alice", host_id="h", credential_id=cred.id)
    assert lease is not None
    assert lease.token == "s3cret"
    # PAT is long-lived; the lease shape is uniform (oauth expiry is P3).
    assert lease.expires_at is None


def test_credential_lease_repr_hides_token(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username=None,
        token="topsecret",
    )
    lease = store.resolve_lease(owner_user_id="alice", host_id="h", credential_id=cred.id)
    assert lease is not None
    assert "topsecret" not in repr(lease)
    assert "redacted" in repr(lease)


def test_create_defaults_kind_pat_and_accepts_kind(tmp_path) -> None:
    store = _store(tmp_path)
    default = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="default",
        username=None,
        token="t",
    )
    assert default.kind == "pat"
    explicit = store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="oauthy",
        username=None,
        token="t",
        kind="oauth",
    )
    assert explicit.kind == "oauth"
```

  Also extend the schema round-trip test `tests/db/test_git_credentials_schema.py` to assert the new column defaults to the `pat` code (`1`) when unset by adding, inside its existing `session_maker()` read block:

```python
        assert all(r.kind == 1 for r in rows)  # SmallInteger pat code, server_default
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py -q`
Expected: FAIL — `AttributeError: 'GitCredentialStore' object has no attribute 'resolve_lease'` (and no `kind`).

- [ ] **Step 3: Add the enum codec** — in `omnigent/db/enum_codecs.py`, add the code table next to the others (after `HOST_STATUS`, ~`:80`) and the encode/decode pair next to `encode_host_status`/`decode_host_status` (~`:251`). Also add `git_credentials.kind` to the module-docstring list of int-coded columns (the parenthesised list at ~`:2-9`).

```python
GIT_CREDENTIAL_KIND: dict[str, int] = {
    "pat": 1,
    "oauth": 2,
}
```

```python
def encode_git_credential_kind(name: str) -> int:
    """Encode a ``git_credentials.kind`` name to its int code."""
    return _encode(GIT_CREDENTIAL_KIND, name, field="git_credentials.kind")


def decode_git_credential_kind(code: int) -> str:
    """Decode a ``git_credentials.kind`` int code to its name."""
    return _decode(GIT_CREDENTIAL_KIND, code, field="git_credentials.kind")
```

- [ ] **Step 4: Add the `kind` column** — in `omnigent/db/db_models.py`, inside `SqlGitCredential`, add the column after `token_ciphertext` (~`:1179`):

```python
    # Enum stored as a stable int code (see omnigent.db.enum_codecs
    # GIT_CREDENTIAL_KIND: pat=1, oauth=2). Records the credential type; the
    # resolver normalizes every kind into a uniform lease so consumers never
    # branch on it. P1 ships pat only (default); oauth is P3.
    kind: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default="1")
```

  and add the check constraint to `__table_args__` (alongside the existing `UniqueConstraint`, ~`:1183`):

```python
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "owner_user_id",
            "host_id",
            "label",
            name="uq_git_credentials_workspace_owner_host_label",
        ),
        CheckConstraint("kind IN (1, 2)", name="ck_git_credentials_kind"),
    )
```

  (`SmallInteger` and `CheckConstraint` are already imported at the top of `db_models.py`.)

- [ ] **Step 5: Write the migration** — first confirm the current head:

Run: `env -u NODE_ENV uv run python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; c=Config(); c.set_main_option('script_location','omnigent/db/migrations'); print(ScriptDirectory.from_config(c).get_heads())"`
Expected: `['z8a2b3c4d5e6']`. If it prints anything else, use that value as `down_revision` below.

  Create `omnigent/db/migrations/versions/z9a2b3c4d5e6_add_kind_to_git_credentials.py` (mirrors the `q1a2b3c4d5e6_add_scope_to_policies` SQLite `recreate` idiom for adding an enum column + check to an existing table):

```python
"""add kind discriminator to git_credentials

Revision ID: z9a2b3c4d5e6
Revises: z8a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Adds ``git_credentials.kind`` — a stable int code (pat=1, oauth=2; see
omnigent.db.enum_codecs GIT_CREDENTIAL_KIND) recording the credential type.
``server_default='1'`` backfills existing rows to ``pat`` (P1 ships pat only),
which is required for a NOT NULL add against a populated table. A
``CHECK (kind IN (1, 2))`` mirrors the other enum columns. No foreign keys
(Rule R032); no partial indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z9a2b3c4d5e6"
down_revision: str | None = "z8a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Add the NOT NULL ``kind`` column (default pat) and its CHECK."""
    sqlite = _is_sqlite()
    with op.batch_alter_table(
        "git_credentials", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.add_column(
            sa.Column("kind", sa.SmallInteger(), nullable=False, server_default="1")
        )
        batch_op.create_check_constraint("ck_git_credentials_kind", "kind IN (1, 2)")


def downgrade() -> None:
    """Drop the ``kind`` column and its CHECK."""
    sqlite = _is_sqlite()
    with op.batch_alter_table(
        "git_credentials", recreate="always" if sqlite else "auto"
    ) as batch_op:
        batch_op.drop_constraint("ck_git_credentials_kind", type_="check")
        batch_op.drop_column("kind")
```

- [ ] **Step 6: Update the store** — in `omnigent/stores/git_credential_store.py`:

  (a) Extend the imports (the `from omnigent.db.db_models import ...` line and add the codec import):

```python
from omnigent.db.db_models import InvalidUuidError, SqlGitCredential, current_workspace_id
from omnigent.db.enum_codecs import decode_git_credential_kind, encode_git_credential_kind
```

  (b) Add `kind` to the `GitCredential` dataclass (after `label`):

```python
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
```

  (c) Add the `CredentialLease` type directly after the `GitCredential` dataclass:

```python
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
```

  (d) Update `_row_to_entity` to carry `kind`:

```python
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
```

  (e) Add `kind: str = "pat"` to `create`'s keyword-only params and set it on the row. Change the signature line and the `SqlGitCredential(...)` construction:

```python
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
```

  and in the `row = SqlGitCredential(...)` construction add `kind=encode_git_credential_kind(kind),` (place it after `provider=provider,`).

  (f) **Replace** the whole `resolve_token` method with `resolve_lease`:

```python
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
```

- [ ] **Step 7: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py tests/server/test_git_credentials_route.py -q`
Expected: PASS (store + schema + no route regression — the route builds its response from explicit fields, so the new `kind` on the entity does not leak into it).

- [ ] **Step 8: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/db omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py && env -u NODE_ENV uv run ruff format omnigent/db omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py
git add omnigent/db/enum_codecs.py omnigent/db/db_models.py omnigent/db/migrations/versions/z9a2b3c4d5e6_add_kind_to_git_credentials.py omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py
pre-commit run --files omnigent/db/enum_codecs.py omnigent/db/db_models.py omnigent/db/migrations/versions/z9a2b3c4d5e6_add_kind_to_git_credentials.py omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py
git commit -m "feat(git-hosts): kind discriminator + resolve_lease (P1c-3)"
```

---

### Task 2: Widen `RepoWorkspace` with the `ClonePlan` fields it drops today

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`RepoWorkspace` ~`:401-437`, `resolve_repo_workspace` ~`:586-594`)
- Test: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Consumes: `ClonePlan` (`omnigent/git_hosts/base.py`) via `resolve_clone_plan`.
- Produces: `RepoWorkspace` gains non-secret fields — `host_id: str | None`, `api_base: str | None`, `auth_scheme: str | None`, `ca_bundle: str | None`, `ssh_host: str | None`, `ssh_port: int | None` (all default `None`). `resolve_repo_workspace(workspace, hosts)` copies them from the `ClonePlan`. **No behavior change to selection** (that is Task 3); `credential_slot_id` is added in Task 3.

- [ ] **Step 1: Write the failing test** — add to `tests/server/test_managed_hosts.py`, in the `resolve_repo_workspace` section (after `test_resolve_repo_workspace_enriches_configured_host`, ~`:892`):

```python
def test_resolve_repo_workspace_carries_widened_plan_fields() -> None:
    repo = resolve_repo_workspace("https://git.acme.com/team/proj#main", _GH_HOSTS)
    # The fields resolve_repo_workspace used to drop from the ClonePlan.
    assert repo.host_id == "acme"
    assert repo.auth_scheme == "basic"
    assert repo.api_base == _GH_HOSTS[0].api_base
    assert repo.ca_bundle is None
    assert repo.ssh_host is None
    assert repo.ssh_port is None


def test_resolve_repo_workspace_github_default_host_id() -> None:
    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)
    assert repo.host_id == "github"
    assert repo.auth_scheme == "basic"
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k resolve_repo_workspace`
Expected: FAIL — `AttributeError: 'RepoWorkspace' object has no attribute 'host_id'`.

- [ ] **Step 3: Widen the dataclass** — replace the `RepoWorkspace` field block (the lines after its docstring, ~`:431-437`) with:

```python
    url: str
    branch: str | None
    repo_name: str
    host_id: str | None = None
    canonical_host: str | None = None
    provider: str | None = None
    api_base: str | None = None
    credential_source: str | None = None
    clone_username: str | None = None
    auth_scheme: str | None = None
    ca_bundle: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None
```

  and update the docstring `:param:` block above it to enumerate the new fields (add, matching the existing terse style):

```python
    :param host_id: Operator git-host id the repo resolved to (``"github"``
        for the built-in default); ``None`` when unresolved.
    :param api_base: API base URL for the resolved host; ``None`` when unresolved.
    :param auth_scheme: HTTPS auth scheme (``"basic"``/``"token"``) from the
        provider clone binding; ``None`` when unresolved.
    :param ca_bundle: Path to the host's CA bundle for a private forge, or ``None``.
    :param ssh_host: SSH host override for the resolved host, or ``None``.
    :param ssh_port: SSH port override for the resolved host, or ``None``.
```

- [ ] **Step 4: Copy the fields through** — replace the `return RepoWorkspace(...)` in `resolve_repo_workspace` (~`:586-594`) with:

```python
    return RepoWorkspace(
        url=parsed.url,
        branch=parsed.branch,
        repo_name=parsed.repo_name,
        host_id=plan.host_id,
        canonical_host=plan.canonical_host,
        provider=plan.provider,
        api_base=plan.api_base,
        credential_source=plan.credential_source,
        clone_username=plan.auth.username,
        auth_scheme=plan.auth.scheme,
        ca_bundle=plan.ca_bundle,
        ssh_host=plan.ssh_host,
        ssh_port=plan.ssh_port,
    )
```

  and extend the function docstring's `:returns:`/summary to note it now carries the full non-secret plan (host id, api base, auth scheme, CA, SSH) for the launch/handoff, not only the four fields it copied before.

- [ ] **Step 5: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q`
Expected: PASS (existing resolver/clone-env tests still green — the new fields default `None` and every prior assertion is unchanged).

- [ ] **Step 6: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git add omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): widen RepoWorkspace with dropped ClonePlan fields (P1c-3)"
```

---

### Task 3: Owner-aware slot selection + create-request `git_credential_label`

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`resolve_repo_workspace` signature + a new `_select_credential_slot` + `CredentialSelectionError` + `RepoWorkspace.credential_slot_id`)
- Modify: `omnigent/server/schemas.py` (`SessionCreateRequest.git_credential_label`, ~`:1322`)
- Modify: `omnigent/server/routes/sessions.py` (the managed-hosts import block ~`:165-174`, the pre-create gate ~`:14501-14518`)
- Test: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Consumes: `GitCredentialStore.list_for_owner_host(owner_user_id, host_id) -> list[GitCredential]` (existing); `ClonePlan`; `RepoWorkspace` (Task 2 widened).
- Produces:
  - `RepoWorkspace.credential_slot_id: str | None = None`.
  - `CredentialSelectionError(ValueError)`.
  - `resolve_repo_workspace(workspace, hosts, *, owner_user_id: str | None = None, credential_store: GitCredentialStore | None = None, label: str | None = None) -> RepoWorkspace` — when a store **and** owner are given, applies §12.3 selection and sets `credential_slot_id`; otherwise `credential_slot_id` stays `None` (today's behavior).
  - Selection rule (§12.3 precedence): given `slots = list_for_owner_host(owner, plan.host_id)` — if a `label` is given it must match exactly one slot (else `CredentialSelectionError`); if `label` is `None`, exactly-one auto-selects, **multiple raises** `CredentialSelectionError` listing labels, **zero** leaves `credential_slot_id=None` (fall back to the operator `credential_source`).
  - `SessionCreateRequest.git_credential_label: str | None = None`.

- [ ] **Step 1: Write the failing tests** — add to `tests/server/test_managed_hosts.py` (top of file already imports `pytest`, `RepoWorkspace`, `resolve_repo_workspace`, `load_git_hosts`; add `CredentialSelectionError` to the `from omnigent.server.managed_hosts import (...)` block). Add a store helper + tests in the resolver section:

```python
def _cred_store(tmp_path):
    from cryptography.fernet import Fernet

    from omnigent.git_hosts.crypto import GitCredentialCipher
    from omnigent.stores.git_credential_store import GitCredentialStore

    return GitCredentialStore(
        f"sqlite:///{tmp_path}/creds.db",
        GitCredentialCipher([Fernet.generate_key().decode()]),
    )


def test_resolve_repo_workspace_selects_single_slot(tmp_path) -> None:
    store = _cred_store(tmp_path)
    slot = store.create(
        owner_user_id="alice", host_id="acme", provider="forgejo",
        label="work", username=None, token="t",
    )
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    assert repo.credential_slot_id == slot.id


def test_resolve_repo_workspace_multiple_slots_requires_label(tmp_path) -> None:
    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="t1")
    personal = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                            label="personal", username=None, token="t2")
    with pytest.raises(CredentialSelectionError, match="multiple git credentials"):
        resolve_repo_workspace(
            "https://git.acme.com/team/proj", _GH_HOSTS,
            owner_user_id="alice", credential_store=store,
        )
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store, label="personal",
    )
    assert repo.credential_slot_id == personal.id


def test_resolve_repo_workspace_unknown_label_lists_available(tmp_path) -> None:
    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="t")
    with pytest.raises(CredentialSelectionError, match="work"):
        resolve_repo_workspace(
            "https://git.acme.com/team/proj", _GH_HOSTS,
            owner_user_id="alice", credential_store=store, label="nope",
        )


def test_resolve_repo_workspace_zero_slots_falls_back(tmp_path) -> None:
    store = _cred_store(tmp_path)
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    assert repo.credential_slot_id is None
    assert repo.credential_source == "env:ACME_TOKEN"


def test_resolve_repo_workspace_no_store_no_selection() -> None:
    # Backward compat: no store -> no owner-aware selection (today's behavior).
    repo = resolve_repo_workspace("https://git.acme.com/team/proj", _GH_HOSTS)
    assert repo.credential_slot_id is None
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k "slot or selection or fall_back or no_selection"`
Expected: FAIL — `ImportError: cannot import name 'CredentialSelectionError'`.

- [ ] **Step 3: Add imports + the error + the field** — in `omnigent/server/managed_hosts.py`:

  (a) extend imports: change `from omnigent.git_hosts.base import HostConfig` to `from omnigent.git_hosts.base import ClonePlan, HostConfig`, and add to the `TYPE_CHECKING:` block (~`:126`):

```python
if TYPE_CHECKING:
    from omnigent.onboarding.sandboxes import SandboxLauncher
    from omnigent.stores.git_credential_store import GitCredentialStore
```

  (b) add the error class near the other module-level types (e.g. just above `RepoWorkspace`, ~`:400`):

```python
class CredentialSelectionError(ValueError):
    """A managed session's git-credential slot cannot be chosen unambiguously.

    Raised when the owner holds multiple labeled identities on the resolved
    host and the request did not name one (or named one that does not exist).
    A ``ValueError`` subclass so the create route's existing ``except
    ValueError`` renders it as a 422 — never a silent pick (design §12.3).
    """
```

  (c) add `credential_slot_id: str | None = None` to the `RepoWorkspace` field block (place it after `credential_source`), and add a `:param:` line:

```python
    :param credential_slot_id: The owner's selected credential slot id for the
        resolved host (design §8.3/§12.3), or ``None`` when the owner has no
        slot (fall back to the operator ``credential_source``) or owner-aware
        selection was not requested.
```

- [ ] **Step 4: Add the selection helper + owner-aware signature** — add `_select_credential_slot` directly above `resolve_repo_workspace` (~`:568`):

```python
def _select_credential_slot(
    *,
    plan: ClonePlan,
    owner_user_id: str | None,
    credential_store: GitCredentialStore | None,
    label: str | None,
) -> str | None:
    """Pick the owner's credential slot for the resolved host (design §12.3).

    Precedence: the owner's slot for this host (model A) → else ``None`` so the
    caller falls back to the operator ``credential_source`` → else legacy
    ambient ``GIT_TOKEN``. A given *label* must match exactly one slot; without
    a label, exactly-one auto-selects and multiple is a hard error (never a
    silent pick).

    :returns: The selected slot id, or ``None`` when the owner has no slot or
        owner-aware selection was not requested (no store / no owner — today's
        behavior).
    :raises CredentialSelectionError: On an ambiguous or unmatched selection.
    """
    if credential_store is None or owner_user_id is None:
        return None
    slots = credential_store.list_for_owner_host(owner_user_id, plan.host_id)
    if not slots:
        return None
    available = ", ".join(sorted(s.label for s in slots))
    if label is not None:
        for slot in slots:
            if slot.label == label:
                return slot.id
        raise CredentialSelectionError(
            f"no git credential labeled {label!r} for host {plan.host_id!r}; "
            f"available: {available}"
        )
    if len(slots) == 1:
        return slots[0].id
    raise CredentialSelectionError(
        f"host {plan.host_id!r} has multiple git credentials for this user "
        f"({available}); set 'git_credential_label' to choose one"
    )
```

  Then change `resolve_repo_workspace`'s signature and body. Replace its `def` line and the `plan = resolve_clone_plan(...)` + `return RepoWorkspace(...)` block with:

```python
def resolve_repo_workspace(
    workspace: str,
    hosts: Sequence[HostConfig],
    *,
    owner_user_id: str | None = None,
    credential_store: GitCredentialStore | None = None,
    label: str | None = None,
) -> RepoWorkspace:
```

  and inside, after `plan = resolve_clone_plan(workspace, hosts)`:

```python
    parsed = parse_repo_workspace(workspace)
    plan = resolve_clone_plan(workspace, hosts)
    credential_slot_id = _select_credential_slot(
        plan=plan,
        owner_user_id=owner_user_id,
        credential_store=credential_store,
        label=label,
    )
    return RepoWorkspace(
        url=parsed.url,
        branch=parsed.branch,
        repo_name=parsed.repo_name,
        host_id=plan.host_id,
        canonical_host=plan.canonical_host,
        provider=plan.provider,
        api_base=plan.api_base,
        credential_source=plan.credential_source,
        credential_slot_id=credential_slot_id,
        clone_username=plan.auth.username,
        auth_scheme=plan.auth.scheme,
        ca_bundle=plan.ca_bundle,
        ssh_host=plan.ssh_host,
        ssh_port=plan.ssh_port,
    )
```

  Extend the docstring `:param:` block with `owner_user_id`, `credential_store`, `label`, and note that selection is skipped (`credential_slot_id=None`) when either the store or the owner is absent.

- [ ] **Step 5: Add the request field** — in `omnigent/server/schemas.py`, add to `SessionCreateRequest`'s field block (after `harness_override: str | None = None`, ~`:1322`):

```python
    git_credential_label: str | None = None
```

  and a `:param:` entry in the class docstring (after the `harness_override` entry, ~`:1305`):

```python
    :param git_credential_label: For a ``host_type: "managed"`` session whose
        repository resolves to an operator git host on which the creator holds
        MULTIPLE labeled credentials, the label choosing which one the session
        uses (design §12.3). Ignored when the creator holds zero or one
        credential on the host. ``None`` (the default) auto-selects the single
        credential or falls back to the operator credential source.
```

- [ ] **Step 6: Wire the create gate** — in `omnigent/server/routes/sessions.py`:

  (a) add `CredentialSelectionError` to the `from omnigent.server.managed_hosts import (...)` block (~`:165-174`), keeping alphabetical order (it slots before `ManagedHostLaunch` or wherever ruff-isort places it — let ruff sort):

```python
from omnigent.server.managed_hosts import (
    CredentialSelectionError,
    ManagedHostLaunch,
    ManagedLaunch,
    ManagedLaunchTracker,
    ManagedSandboxConfig,
    RepoWorkspace,
    host_resume_supported,
    host_sandbox_is_running,
    resolve_repo_workspace,
)
```

  (b) replace the pre-create gate's `resolve_repo_workspace` call + its single `except ValueError` (~`:14501-14518`) with the owner-aware call and a `CredentialSelectionError`-first except chain:

```python
        managed_repo: RepoWorkspace | None = None
        if body.host_type == "managed" and body.workspace is not None:
            try:
                managed_repo = resolve_repo_workspace(
                    body.workspace,
                    getattr(request.app.state, "git_hosts", ()),
                    owner_user_id=user_id,
                    credential_store=getattr(
                        request.app.state, "git_credential_store", None
                    ),
                    label=body.git_credential_label,
                )
            except CredentialSelectionError as exc:
                # Ambiguous/unmatched credential label — point the 422 at the
                # label field, listing the choices (str(exc)); never a silent pick.
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "type": "value_error",
                            "loc": ["body", "git_credential_label"],
                            "msg": str(exc),
                            "input": body.git_credential_label,
                        },
                    ],
                ) from exc
            except ValueError as exc:
                # Same list-of-errors 422 shape as the schema validators
                # (describeCreateError renders detail[0].msg).
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "type": "value_error",
                            "loc": ["body", "workspace"],
                            "msg": str(exc),
                            "input": body.workspace,
                        },
                    ],
                ) from exc
```

  (`CredentialSelectionError` subclasses `ValueError`, so it MUST be caught first.)

- [ ] **Step 7: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q && env -u NODE_ENV uv run pytest tests/server/test_app_git_hosts.py -q`
Expected: PASS.

- [ ] **Step 8: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/managed_hosts.py omnigent/server/schemas.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/managed_hosts.py omnigent/server/schemas.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git add omnigent/server/managed_hosts.py omnigent/server/schemas.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py omnigent/server/schemas.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): owner-aware credential-slot selection at session create (P1c-3)"
```

---

### Task 4: Relaunch binding persistence + re-authorization + drift handling

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (label-key constants ~`:234`; `host_config_hash`; `build_relaunch_binding_labels`; `reauthorize_relaunch_binding`; `RelaunchBindingError`)
- Modify: `omnigent/server/routes/sessions.py` (persist at create ~`:14618-14627`; relaunch re-auth at `_kick_managed_relaunch` ~`:7034-7045`)
- Test: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Consumes: `RepoWorkspace` (host_id, canonical_host, credential_slot_id — Tasks 2/3); `HostConfig`; `GitCredentialStore.list_for_owner_host`; `MANAGED_REPO_LABEL_KEY` (existing).
- Produces:
  - Label-key constants: `MANAGED_GIT_HOST_ID_LABEL_KEY`, `MANAGED_GIT_HOST_HASH_LABEL_KEY`, `MANAGED_GIT_CANONICAL_URL_LABEL_KEY`, `MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY` (all `"omnigent.sandbox.git_*"`).
  - `host_config_hash(cfg: HostConfig) -> str` — deterministic SHA-256 over `(id, provider, web_host, api_base, credential_source, ssh_host, ssh_port, ca_bundle)`.
  - `build_relaunch_binding_labels(repo: RepoWorkspace, hosts: Sequence[HostConfig]) -> dict[str, str]` — `{}` for the github default; the four binding labels for an operator host (slot label only when a slot was selected).
  - `RelaunchBindingError(RuntimeError)`.
  - `reauthorize_relaunch_binding(*, raw_repo: str, labels: Mapping[str, str], owner: str, hosts: Sequence[HostConfig], credential_store: GitCredentialStore | None) -> RepoWorkspace` — re-resolves topology against the LIVE config; refuses ONLY on semantic rebind or lost authorization (host removed, URL now resolving to a different host id, slot revoked). Same-host config drift (hash mismatch) is logged and **takes effect** (design §9); the caller refreshes the persisted binding labels after a successful re-auth. Returns the `RepoWorkspace` carrying the re-authorized `credential_slot_id`.

- [ ] **Step 1: Write the failing tests** — add to `tests/server/test_managed_hosts.py`. Extend the `from omnigent.server.managed_hosts import (...)` block with `MANAGED_REPO_LABEL_KEY, MANAGED_GIT_HOST_ID_LABEL_KEY, MANAGED_GIT_HOST_HASH_LABEL_KEY, MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY, RelaunchBindingError, build_relaunch_binding_labels, host_config_hash, reauthorize_relaunch_binding` (let ruff sort). Uses the `_cred_store` helper from Task 3.

```python
def _bound_labels(repo, store, tmp_path):
    return {
        MANAGED_REPO_LABEL_KEY: "https://git.acme.com/team/proj",
        **build_relaunch_binding_labels(repo, _GH_HOSTS),
    }


def test_build_relaunch_binding_labels_for_operator_host(tmp_path) -> None:
    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="t")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    labels = build_relaunch_binding_labels(repo, _GH_HOSTS)
    assert labels[MANAGED_GIT_HOST_ID_LABEL_KEY] == "acme"
    assert labels[MANAGED_GIT_HOST_HASH_LABEL_KEY] == host_config_hash(_GH_HOSTS[0])
    assert labels[MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY] == slot.id


def test_build_relaunch_binding_labels_empty_for_github() -> None:
    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)
    assert build_relaunch_binding_labels(repo, _GH_HOSTS) == {}


def test_reauthorize_relaunch_binding_happy_path(tmp_path) -> None:
    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="t")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    labels = _bound_labels(repo, store, tmp_path)
    out = reauthorize_relaunch_binding(
        raw_repo="https://git.acme.com/team/proj", labels=labels,
        owner="alice", hosts=_GH_HOSTS, credential_store=store,
    )
    assert out.credential_slot_id == slot.id


def test_reauthorize_refuses_when_host_removed(tmp_path) -> None:
    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="t")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    labels = _bound_labels(repo, store, tmp_path)
    with pytest.raises(RelaunchBindingError):
        reauthorize_relaunch_binding(
            raw_repo="https://git.acme.com/team/proj", labels=labels,
            owner="alice", hosts=(), credential_store=store,
        )


def test_reauthorize_same_host_config_drift_takes_effect(tmp_path, caplog) -> None:
    # Same host id, changed config: operator changes deliberately take effect
    # on relaunch (design §9) — no refusal; the drift is logged and the
    # refreshed binding labels carry the NEW hash. (Add `import logging` to
    # this test file's imports.)
    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="t")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    labels = _bound_labels(repo, store, tmp_path)
    changed = load_git_hosts([{
        "id": "acme", "provider": "forgejo", "web_host": "git.acme.com",
        "credential_source": "env:DIFFERENT_TOKEN",
    }])
    with caplog.at_level(logging.WARNING, logger="omnigent.server.managed_hosts"):
        out = reauthorize_relaunch_binding(
            raw_repo="https://git.acme.com/team/proj", labels=labels,
            owner="alice", hosts=changed, credential_store=store,
        )
    # The live config took effect and the slot survived re-authorization.
    assert out.credential_source == "env:DIFFERENT_TOKEN"
    assert out.credential_slot_id == slot.id
    # The drift was logged (host id only — never config values).
    assert any("acme" in r.message and "configuration changed" in r.message for r in caplog.records)
    assert not any("DIFFERENT_TOKEN" in r.message for r in caplog.records)
    # Rebuilding the binding from the re-authorized workspace yields the NEW hash.
    refreshed = build_relaunch_binding_labels(out, changed)
    assert refreshed[MANAGED_GIT_HOST_HASH_LABEL_KEY] == host_config_hash(changed[0])
    assert refreshed[MANAGED_GIT_HOST_HASH_LABEL_KEY] != labels[MANAGED_GIT_HOST_HASH_LABEL_KEY]


def test_reauthorize_refuses_when_slot_revoked(tmp_path) -> None:
    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="t")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    labels = _bound_labels(repo, store, tmp_path)
    store.delete(slot.id)  # owner lost the slot between create and relaunch
    with pytest.raises(RelaunchBindingError, match="no longer available"):
        reauthorize_relaunch_binding(
            raw_repo="https://git.acme.com/team/proj", labels=labels,
            owner="alice", hosts=_GH_HOSTS, credential_store=store,
        )


def test_reauthorize_no_binding_preserves_degrade() -> None:
    # A pre-P1c-3 session (only the raw repo label, no binding labels) whose
    # host the operator later removed still raises a plain ValueError, so the
    # relaunch caller keeps the degrade-to-empty-workspace behavior.
    with pytest.raises(ValueError):
        reauthorize_relaunch_binding(
            raw_repo="https://git.gone.com/x/y",
            labels={MANAGED_REPO_LABEL_KEY: "https://git.gone.com/x/y"},
            owner="alice", hosts=(), credential_store=None,
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k "reauthorize or binding_labels"`
Expected: FAIL — `ImportError: cannot import name 'RelaunchBindingError'`.

- [ ] **Step 3: Add the label keys, hash, error, and helpers** — in `omnigent/server/managed_hosts.py`:

  (a) add `Mapping` to the collections import and `hashlib` to the stdlib imports:

```python
import hashlib
```
```python
from collections.abc import Callable, Mapping, Sequence
```

  (b) add the label keys next to `MANAGED_REPO_LABEL_KEY` (~`:234`):

```python
MANAGED_REPO_LABEL_KEY = "omnigent.sandbox.repo"
# Server-owned relaunch-binding labels (design §9). Persisted at create so a
# relaunch can detect operator topology drift and re-authorize the same
# credential slot deterministically, instead of silently re-resolving the raw
# URL against whatever git_hosts is live. These are HINTS re-validated at every
# launch — tampering can only cause a refusal, never a silent rebind or a
# privilege escalation (the slot is re-authorized against the live
# owner/host-scoped store on every relaunch).
MANAGED_GIT_HOST_ID_LABEL_KEY = "omnigent.sandbox.git_host_id"
MANAGED_GIT_HOST_HASH_LABEL_KEY = "omnigent.sandbox.git_host_hash"
MANAGED_GIT_CANONICAL_URL_LABEL_KEY = "omnigent.sandbox.git_canonical_url"
MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY = "omnigent.sandbox.git_credential_slot"
```

  (c) add the error class near `CredentialSelectionError` (Task 3, ~`:400`):

```python
class RelaunchBindingError(RuntimeError):
    """A session's persisted git-host binding no longer holds at relaunch.

    Raised when the bound host no longer resolves the session's repository
    (removed), the URL now resolves to a DIFFERENT host than the one bound
    (semantic rebind), or the owner lost the bound credential slot. Same-host
    configuration drift does NOT raise — it deliberately takes effect on
    relaunch (design §9). The relaunch refuses rather than silently rebinding
    to a different host/credential or degrading to an empty workspace.
    Persisted binding labels are hints re-validated every launch; tampering
    with one can only cause this refusal (never escalation), because the slot
    is re-authorized against the live owner/host-scoped store.
    """
```

  (d) add the hash + helpers (place near `resolve_repo_workspace`, e.g. after it, ~`:595`):

```python
def host_config_hash(cfg: HostConfig) -> str:
    """Return a deterministic hash of a host's non-secret topology.

    Covers the host's identity and non-secret topology: id, provider,
    canonical host, API base, the credential-source *reference*
    (``"env:NAME"`` — not a secret), and the SSH/CA overrides. The hash is a
    drift-detection/audit signal (and the future §8.7 re-resolution notice
    hook), NOT a gate: same-host changes deliberately take effect on relaunch
    (design §9) and refresh the persisted binding. Refusal is reserved for the
    separately-checked rebind / lost-slot cases.
    """
    parts = (
        cfg.id,
        cfg.provider,
        cfg.web_host,
        cfg.api_base,
        cfg.credential_source,
        cfg.ssh_host or "",
        "" if cfg.ssh_port is None else str(cfg.ssh_port),
        cfg.ca_bundle or "",
    )
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def build_relaunch_binding_labels(
    repo: RepoWorkspace, hosts: Sequence[HostConfig]
) -> dict[str, str]:
    """Server-owned labels pinning a session's git-host binding for relaunch.

    Empty for the github.com built-in default (host id ``"github"`` has no
    ``HostConfig`` and no user credentials — nothing to drift). For an operator
    host: the host id, its topology hash, the canonical clone URL, and the
    selected credential slot id (only when a slot was chosen).
    """
    cfg = next((h for h in hosts if h.id == repo.host_id), None)
    if cfg is None:
        return {}
    labels = {
        MANAGED_GIT_HOST_ID_LABEL_KEY: cfg.id,
        MANAGED_GIT_HOST_HASH_LABEL_KEY: host_config_hash(cfg),
        MANAGED_GIT_CANONICAL_URL_LABEL_KEY: repo.url,
    }
    if repo.credential_slot_id is not None:
        labels[MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY] = repo.credential_slot_id
    return labels


def reauthorize_relaunch_binding(
    *,
    raw_repo: str,
    labels: Mapping[str, str],
    owner: str,
    hosts: Sequence[HostConfig],
    credential_store: GitCredentialStore | None,
) -> RepoWorkspace:
    """Re-resolve and re-authorize a session's repo binding for a relaunch.

    Re-resolves *raw_repo* against the LIVE operator hosts and enforces the
    binding's SECURITY invariants — refusing (raising
    :class:`RelaunchBindingError`) only when one is violated:

    - the bound host id no longer resolves the URL (operator removed the host);
    - the URL now resolves to a DIFFERENT host id (a semantic rebind — because a
      fixed URL matches the same host id only if that host's ``web_host`` is
      unchanged, "same host id" already guarantees the canonical DESTINATION is
      invariant, so this is the destination-integrity gate);
    - a bound credential slot no longer resolves for *owner* (revoked/lost — the
      ownership gate).

    A ``host_config_hash`` mismatch under the SAME host id is NOT a gate.
    Design §9 is explicit: "Topology/credential changes deliberately take effect
    on relaunch." A ``ca_bundle``/``api_base``/``credential_source``-ref/SSH/
    provider change keeps the same destination and the same (re-authorized)
    owner slot, so the relaunch proceeds with the LIVE config; the mismatch is
    logged (host id only — never config values or secrets) as a drift/audit
    signal (and the future §8.7 re-resolution notice hook), and the CALLER
    refreshes the persisted binding labels so later relaunches compare against
    current config instead of logging forever.

    A session with NO persisted binding (pre-P1c-3, or the github default) is
    left to the caller's existing behavior: topology resolution succeeds
    normally, and an unresolvable URL raises a plain ``ValueError``
    (degrade-to-empty).

    :returns: The re-resolved :class:`RepoWorkspace` carrying the
        re-authorized ``credential_slot_id`` (``None`` when unbound). The caller
        re-persists ``build_relaunch_binding_labels(returned_repo, hosts)`` on
        success to refresh a drifted hash/URL.
    """
    bound_host_id = labels.get(MANAGED_GIT_HOST_ID_LABEL_KEY)
    try:
        repo = resolve_repo_workspace(raw_repo, hosts)
    except ValueError:
        if bound_host_id is not None:
            # The bound host no longer resolves the URL — operator removed it.
            raise RelaunchBindingError(
                f"the git host {bound_host_id!r} this session was created "
                "against no longer resolves its repository; refusing to relaunch"
            ) from None
        raise  # no binding persisted -> caller keeps the degrade-to-empty path
    if bound_host_id is None:
        return repo
    cfg = next((h for h in hosts if h.id == bound_host_id), None)
    if cfg is None or repo.host_id != bound_host_id:
        # Destination-integrity gate: the URL rebound to a different host id
        # (or the host is gone). Never send the credential to a new host.
        raise RelaunchBindingError(
            f"the git host {bound_host_id!r} this session was created against "
            "is no longer configured; refusing to relaunch with a different host"
        )
    if labels.get(MANAGED_GIT_HOST_HASH_LABEL_KEY) != host_config_hash(cfg):
        # Topology/credential changes deliberately take effect on relaunch
        # (design §9) — NOT a gate. Same host id => same destination; the slot
        # is still re-authorized below. Proceed with the live config; log the
        # drift (host id only, no config values/secrets) as an audit signal and
        # the future §8.7 notice hook. The caller refreshes the persisted binding.
        _logger.warning(
            "git host %r configuration changed since session create; "
            "relaunching with the live config",
            bound_host_id,
        )
    bound_slot = labels.get(MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY)
    if bound_slot is None:
        return repo  # operator-credential-source binding; no per-owner slot
    owned = (
        {c.id for c in credential_store.list_for_owner_host(owner, bound_host_id)}
        if credential_store is not None
        else set()
    )
    if bound_slot not in owned:
        raise RelaunchBindingError(
            "the git credential this session was created with is no longer "
            "available to its owner; refusing to relaunch"
        )
    repo.credential_slot_id = bound_slot
    return repo
```

- [ ] **Step 4: Persist the binding at create** — in `omnigent/server/routes/sessions.py`, extend the managed-hosts import at ~`:14612` (it is a lazy `from omnigent.server.managed_hosts import ...` inside the create handler) and the label-set at ~`:14618-14627`. Change the lazy import line:

```python
            from omnigent.server.auth import RESERVED_USER_LOCAL
            from omnigent.server.managed_hosts import (
                MANAGED_REPO_LABEL_KEY,
                build_relaunch_binding_labels,
            )
```

  and replace the `if body.workspace is not None:` label write with one that also persists the binding:

```python
            repo = managed_repo
            if body.workspace is not None:
                # The session row's workspace is overwritten with the CLONED
                # path at bind time; record the raw request value so a sandbox
                # relaunch can re-clone the same repository, PLUS the resolved
                # binding (host-config id + topology hash + canonical URL +
                # credential slot id) so relaunch re-authorizes deterministically
                # instead of silently re-resolving against whatever git_hosts is
                # live. Empty for the github.com default.
                _repo_labels = {MANAGED_REPO_LABEL_KEY: body.workspace}
                if repo is not None:
                    _repo_labels.update(
                        build_relaunch_binding_labels(
                            repo, getattr(request.app.state, "git_hosts", ())
                        )
                    )
                await asyncio.to_thread(
                    conversation_store.set_labels, resp.id, _repo_labels
                )
```

- [ ] **Step 5: Re-authorize at relaunch** — in `omnigent/server/routes/sessions.py`, in `_kick_managed_relaunch` (~`:7027-7045`), replace the lazy import + resolve+degrade block with a re-authorizing call (refusing only on rebind / lost slot; same-host drift takes effect) and refresh the persisted binding after success. `_kick_managed_relaunch` is a plain `def` (sync), so the refresh calls `conversation_store.set_labels` directly. Change the lazy import:

```python
    from omnigent.server.managed_hosts import (
        MANAGED_REPO_LABEL_KEY,
        RelaunchBindingError,
        build_relaunch_binding_labels,
        reauthorize_relaunch_binding,
    )
```

  and replace the `repo = None ... raw_repo ...` block:

```python
    repo = None
    raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
    if raw_repo is not None:
        try:
            repo = reauthorize_relaunch_binding(
                raw_repo=raw_repo,
                labels=conv.labels,
                owner=host.owner,
                hosts=getattr(app_state, "git_hosts", ()),
                credential_store=getattr(app_state, "git_credential_store", None),
            )
        except RelaunchBindingError as exc:
            # Rebind / lost slot: the bound host is gone (or the URL now
            # resolves to a different host), or the owner lost the credential
            # slot. Refuse rather than silently rebinding or degrading to an
            # empty workspace (design §9). Settle the tracker so a waiting
            # message POST reports the reason.
            _logger.warning("Refusing relaunch of session %s: %s", session_id, exc)
            tracker.begin(session_id)
            tracker.fail(session_id, str(exc))
            _publish_sandbox_status(session_id, "failed", str(exc))
            return
        except ValueError:
            # No persisted binding and an unparseable/unknown raw label
            # (pre-P1c-3 session or corrupt label): keep the historical
            # degrade-to-empty-workspace behavior.
            _logger.warning(
                "Session %s has an unparseable %s label (%r); relaunching with an empty workspace",
                session_id,
                MANAGED_REPO_LABEL_KEY,
                raw_repo,
            )
        else:
            # Successful re-auth: refresh the persisted binding so later
            # relaunches compare against the config that actually took effect
            # (same-host drift deliberately takes effect, design §9).
            refreshed = build_relaunch_binding_labels(
                repo, getattr(app_state, "git_hosts", ())
            )
            if refreshed:
                conversation_store.set_labels(
                    session_id, {MANAGED_REPO_LABEL_KEY: raw_repo, **refreshed}
                )
```

- [ ] **Step 6: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q && env -u NODE_ENV uv run pytest tests/server/integration/test_host_session_binding.py -q`
Expected: PASS (the relaunch integration guard still green — a github/no-binding session degrades exactly as before).

- [ ] **Step 7: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git add omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): persist + re-authorize relaunch binding; drift takes effect (P1c-3)"
```

---

### Task 5: `launch_generation` monotonic counter

**Files:**
- Modify: `omnigent/db/db_models.py` (`SqlConversation`, ~`:662-669`)
- Create: `omnigent/db/migrations/versions/za1b2c3d4e5f_add_launch_generation_to_conversations.py`
- Modify: `omnigent/entities/conversation.py` (`Conversation`, after `archived`, ~`:213`)
- Modify: `omnigent/stores/conversation_store/__init__.py` (abstract method)
- Modify: `omnigent/stores/conversation_store/sqlalchemy_store.py` (`_to_conversation` ~`:161`; new impl)
- Modify: `omnigent/server/routes/sessions.py` (`_run_managed_launch`, top, ~`:6556`)
- Test: `tests/stores/test_conversation_store.py` (or the nearest existing conversation-store test module)

**Interfaces:**
- Produces: `SqlConversation.launch_generation: Mapped[int]` (`Integer`, `nullable=False`, `server_default="0"`, `default=0`); `Conversation.launch_generation: int = 0`; `ConversationStore.increment_launch_generation(conversation_id: str) -> int` (abstract + sqlalchemy impl) — atomically bumps and returns the new generation, raising `ConversationNotFoundError` when absent.
- Consumes (Task 6 will read this): `Conversation.launch_generation` via `get_conversation`.

**Semantics:** starts at `0` on the row; `_run_managed_launch` (which serves BOTH the create launch and every managed relaunch) bumps it once at the top → the create launch is generation `1`, each relaunch `2, 3, …`. A **wake** goes through `_run_managed_wake` (a separate function) and does **not** bump it — it reattaches the same volume.

- [ ] **Step 1: Write the failing test** — add to `tests/stores/test_conversation_store.py`, which already provides a `conversation_store` fixture and calls `create_conversation()` with no args throughout:

```python
def test_increment_launch_generation_is_monotonic(conversation_store) -> None:
    conv = conversation_store.create_conversation()
    assert conversation_store.get_conversation(conv.id).launch_generation == 0
    assert conversation_store.increment_launch_generation(conv.id) == 1
    assert conversation_store.increment_launch_generation(conv.id) == 2
    assert conversation_store.get_conversation(conv.id).launch_generation == 2


def test_increment_launch_generation_missing_raises(conversation_store) -> None:
    import pytest

    from omnigent.stores.conversation_store import ConversationNotFoundError

    with pytest.raises(ConversationNotFoundError):
        conversation_store.increment_launch_generation("f" * 32)
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/stores -q -k launch_generation`
Expected: FAIL — `AttributeError: ... has no attribute 'increment_launch_generation'` / `'Conversation' object has no attribute 'launch_generation'`.

- [ ] **Step 3: Add the model column** — in `omnigent/db/db_models.py`, `SqlConversation`, add after `next_position` (~`:662`):

```python
    # Monotonic per-session launch counter. Advances on the create launch and
    # on every managed RELAUNCH (a fresh sandbox generation), NOT on a wake
    # (which reattaches the same volume). The P1c-4 credential-delivery frame
    # binds to it as its anti-replay anchor (runner_id alone recurs across
    # relaunches). Starts at 0; the first launch bumps it to 1.
    launch_generation: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0", default=0
    )
```

- [ ] **Step 4: Write the migration** — confirm the current head is Task 1's revision:

Run: `env -u NODE_ENV uv run python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; c=Config(); c.set_main_option('script_location','omnigent/db/migrations'); print(ScriptDirectory.from_config(c).get_heads())"`
Expected: `['z9a2b3c4d5e6']`. Use whatever it prints as `down_revision`.

  Create `omnigent/db/migrations/versions/za1b2c3d4e5f_add_launch_generation_to_conversations.py` (mirrors `3b9be5d67c90_add_archived_to_conversations` — a NOT NULL add with a `server_default` backfill, batch mode for SQLite):

```python
"""add launch_generation to conversations

Revision ID: za1b2c3d4e5f
Revises: z9a2b3c4d5e6
Create Date: 2026-07-16 00:00:00.000000

Adds ``conversations.launch_generation``: a monotonic per-session launch
counter (create launch = 1, each managed relaunch increments, wake does not).
``server_default='0'`` backfills existing rows (required for a NOT NULL add
against a populated table) and matches the ORM model default. Batch mode for
SQLite, consistent with the other conversations migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za1b2c3d4e5f"
down_revision: str | None = "z9a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the NOT NULL ``launch_generation`` column (default 0)."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "launch_generation",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )


def downgrade() -> None:
    """Drop the ``launch_generation`` column."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("launch_generation")
```

- [ ] **Step 5: Add the entity field + mapper** — in `omnigent/entities/conversation.py`, add to the `Conversation` dataclass after `archived: bool = False` (~`:213`):

```python
    launch_generation: int = 0
```

  and a `:param:` line in the class docstring (before the closing `"""`, near the `archived` entry):

```python
    :param launch_generation: Monotonic per-session launch counter — 0 before
        the first launch, incremented on the create launch and each managed
        relaunch (not on wake). The anti-replay anchor the credential-delivery
        handoff binds to.
```

  In `omnigent/stores/conversation_store/sqlalchemy_store.py`, `_to_conversation`, add to the `Conversation(...)` construction after `archived=row.archived,` (~`:161`):

```python
        launch_generation=row.launch_generation,
```

- [ ] **Step 6: Add the abstract method** — in `omnigent/stores/conversation_store/__init__.py`, add to the `ConversationStore` ABC (next to the other abstract mutators, e.g. after `set_host_id`):

```python
    @abstractmethod
    def increment_launch_generation(self, conversation_id: str) -> int:
        """Atomically increment and return a session's launch generation.

        The launch generation is a monotonic per-session counter that advances
        on the create launch and on every managed sandbox RELAUNCH (a fresh
        generation), but NOT on a wake (which reattaches the same volume). It
        anchors the P1c-4 credential-delivery frame against replay of a stale
        generation's delivery.

        :param conversation_id: The session to bump.
        :returns: The new launch generation (1 on the first launch).
        :raises ConversationNotFoundError: When the session row is absent.
        """
```

- [ ] **Step 7: Add the sqlalchemy impl** — in `omnigent/stores/conversation_store/sqlalchemy_store.py`, add the method next to `increment_session_usage` (~`:1257`). It mirrors the AP-table atomic-allocator idiom already used for `next_position` (`_conv_session_immediate` + `_supports_for_update` on the conversations engine):

```python
    def increment_launch_generation(self, conversation_id: str) -> int:
        """Atomically bump and return a session's launch generation.

        Serialises concurrent bumps the same way as the ``next_position``
        allocator on this AP table: ``SELECT … FOR UPDATE`` on
        PostgreSQL/MySQL, and ``BEGIN IMMEDIATE`` (``_conv_session_immediate``)
        on SQLite so the write lock is taken before the read.
        """
        with self._conv_session_immediate() as session:
            q = select(SqlConversation).where(
                SqlConversation.workspace_id == current_workspace_id(),
                SqlConversation.id == conversation_id,
            )
            if self._supports_for_update:
                q = q.with_for_update()
            row = session.scalars(q).first()
            if row is None:
                raise ConversationNotFoundError(conversation_id)
            row.launch_generation = (row.launch_generation or 0) + 1
            return row.launch_generation
```

  (`select`, `current_workspace_id`, `SqlConversation`, and `ConversationNotFoundError` are already imported/used in this module — verify with a quick grep and add only what is missing.)

- [ ] **Step 8: Bump on managed launch/relaunch (never wake)** — in `omnigent/server/routes/sessions.py`, at the TOP of `_run_managed_launch` (before the `managed = await _provision_managed_sandbox(...)` call, ~`:6556`):

```python
    # Advance the anti-replay launch generation for this launch/relaunch. Both
    # the create launch and every managed relaunch funnel through here; a wake
    # uses _run_managed_wake and deliberately does NOT bump (same volume). A
    # row deleted mid-flight is caught downstream at the bind step, so a bump
    # failure must not abort the launch.
    try:
        await asyncio.to_thread(
            conversation_store.increment_launch_generation, session_id
        )
    except ConversationNotFoundError:
        pass
```

  (`ConversationNotFoundError` is already imported in `sessions.py` — verified at the top-of-file store imports — so no new import is needed.)

- [ ] **Step 9: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/stores -q -k launch_generation && env -u NODE_ENV uv run pytest tests/stores -q -k conversation`
Expected: PASS. Also confirm a single linear alembic head:

Run: `env -u NODE_ENV uv run python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; c=Config(); c.set_main_option('script_location','omnigent/db/migrations'); print(ScriptDirectory.from_config(c).get_heads())"`
Expected: `['za1b2c3d4e5f']` (exactly one head).

- [ ] **Step 10: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/db omnigent/entities/conversation.py omnigent/stores/conversation_store omnigent/server/routes/sessions.py tests/stores && env -u NODE_ENV uv run ruff format omnigent/db omnigent/entities/conversation.py omnigent/stores/conversation_store omnigent/server/routes/sessions.py tests/stores
git add omnigent/db/db_models.py omnigent/db/migrations/versions/za1b2c3d4e5f_add_launch_generation_to_conversations.py omnigent/entities/conversation.py omnigent/stores/conversation_store/__init__.py omnigent/stores/conversation_store/sqlalchemy_store.py omnigent/server/routes/sessions.py tests/stores
pre-commit run --files omnigent/db/db_models.py omnigent/db/migrations/versions/za1b2c3d4e5f_add_launch_generation_to_conversations.py omnigent/entities/conversation.py omnigent/stores/conversation_store/__init__.py omnigent/stores/conversation_store/sqlalchemy_store.py omnigent/server/routes/sessions.py
git commit -m "feat(git-hosts): launch_generation anti-replay counter (P1c-3)"
```

---

### Task 6: `_build_clone_env` owner-aware lease consumption + backward-compat sweep

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`_build_clone_env` ~`:597`; thread `credential_store` through `launch_managed_host` ~`:1784`, `relaunch_managed_host` ~`:1858`, `_arm_and_start_host` ~`:1935`, and the `_build_clone_env` call ~`:1999`)
- Modify: `omnigent/server/routes/sessions.py` (`_run_managed_launch` ~`:6496` + `_provision_managed_sandbox` ~`:6580` threading; the two `_run_managed_launch(...)` call sites at ~`:6899`/relaunch-kick and ~`:14634`/create)
- Test: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Consumes: `GitCredentialStore.resolve_lease` (Task 1); `RepoWorkspace.credential_slot_id`/`host_id`/`clone_username` (Tasks 2/3).
- Produces: `_build_clone_env(repo, *, owner: str | None = None, credential_store: GitCredentialStore | None = None) -> dict[str, str] | None` — when the repo carries a `credential_slot_id` and both `owner` and `credential_store` are present, resolves the lease and uses `lease.token`; else the operator `credential_source`; else `None`. Fails closed (`ValueError`, no token in the message) when a bound slot cannot be resolved at launch. `credential_store` is threaded from `app.state.git_credential_store` down to the call site.

- [ ] **Step 1: Write the failing tests** — add to `tests/server/test_managed_hosts.py` (uses `_cred_store` from Task 3):

```python
def test_build_clone_env_uses_owner_slot_lease(tmp_path) -> None:
    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="SLOT_TOKEN")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    env = _build_clone_env(repo, owner="alice", credential_store=store)
    assert env == {"GIT_TOKEN": "SLOT_TOKEN", "GIT_USERNAME": "oauth2"}


def test_build_clone_env_fails_closed_when_slot_revoked(tmp_path) -> None:
    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="SLOT_TOKEN")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    store.delete(slot.id)  # owner lost the slot between create and launch
    with pytest.raises(ValueError) as exc:
        _build_clone_env(repo, owner="alice", credential_store=store)
    assert "SLOT_TOKEN" not in str(exc.value)  # fail-closed message names no token


def test_build_clone_env_operator_source_when_no_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACME_TOKEN", "OPERATOR_TOKEN")
    repo = resolve_repo_workspace("https://git.acme.com/team/proj", _GH_HOSTS)
    env = _build_clone_env(repo, owner="alice", credential_store=None)
    assert env == {"GIT_TOKEN": "OPERATOR_TOKEN", "GIT_USERNAME": "oauth2"}


def test_build_clone_env_github_default_unchanged() -> None:
    # Backward compat: github.com default -> no credential_source, no slot ->
    # None (the ambient GIT_TOKEN path is preserved).
    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)
    assert _build_clone_env(repo, owner="alice", credential_store=None) is None
    # And the legacy no-arg / no-repo forms still work unchanged.
    assert _build_clone_env(None) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k build_clone_env`
Expected: FAIL — `_build_clone_env()` got an unexpected keyword argument `owner`.

- [ ] **Step 3: Rewrite `_build_clone_env`** — replace the whole function (~`:597-613`):

```python
def _build_clone_env(
    repo: RepoWorkspace | None,
    *,
    owner: str | None = None,
    credential_store: GitCredentialStore | None = None,
) -> dict[str, str] | None:
    """Resolve a repo's credential into per-clone env, or ``None``.

    Precedence (design §12.3): the session *owner's* selected credential slot
    for the resolved host (decrypted here, in the trusted server process), else
    the operator host ``credential_source``, else ``None`` (the github.com
    default keeps today's ambient-``GIT_TOKEN`` behavior). The resolved value
    rides only the single prefixed clone command (launch-scoped) and never
    enters ``RepoWorkspace``.

    ``repo.host_id`` is the operator GIT-host id the credential is keyed by —
    NOT the sandbox host id.

    :param repo: The enriched workspace, or ``None`` for no-repo launches.
    :param owner: The session owner the credential slot must belong to.
    :param credential_store: The per-user credential store, or ``None`` when
        the feature is not configured (opt-in).
    :returns: ``{"GIT_TOKEN": ..., "GIT_USERNAME": ...}`` or ``None``.
    :raises ValueError: When a bound slot cannot be resolved for *owner* at
        launch (fail closed — never clone unauthenticated), or when the
        operator source cannot be resolved. The token is never in the message.
    """
    if repo is None:
        return None
    username = repo.clone_username or "x-access-token"
    if (
        repo.credential_slot_id is not None
        and owner is not None
        and credential_store is not None
    ):
        lease = credential_store.resolve_lease(
            owner_user_id=owner,
            host_id=repo.host_id or "",
            credential_id=repo.credential_slot_id,
        )
        if lease is None:
            # The owner lost access or the slot was deleted between create and
            # launch. Fail closed rather than clone unauthenticated. No token
            # is named in this message.
            raise ValueError(
                "the git credential bound to this session is no longer "
                "available to its owner"
            )
        return {"GIT_TOKEN": lease.token, "GIT_USERNAME": username}
    if repo.credential_source is None:
        return None
    token = resolve_credential(repo.credential_source, parent_env=os.environ.copy())
    return {"GIT_TOKEN": token, "GIT_USERNAME": username}
```

- [ ] **Step 4: Thread `credential_store` to the call site** — in `omnigent/server/managed_hosts.py`. The store is forwarded, unresolved, down to the single secret-materialization point; the secret is only ever produced inside `_build_clone_env`.

  (a) `launch_managed_host` (~`:1784`) — add to its keyword-only params, after the `repo: RepoWorkspace | None = None,` line:

```python
    credential_store: GitCredentialStore | None = None,
```

  and forward it in the `await _arm_and_start_host(` call (~`:1844`) by adding, after `repo=repo,`:

```python
        credential_store=credential_store,
```

  and add a `:param credential_store:` docstring line ("Per-user git credential store used to resolve the owner's selected slot at launch, or ``None`` when the feature is not configured.").

  (b) `relaunch_managed_host` (~`:1858`) — add the same `credential_store: GitCredentialStore | None = None,` param after its `repo` param, and forward `credential_store=credential_store,` in its `await _arm_and_start_host(` call (~`:1920`, after `repo=repo,`); add the same `:param:` line.

  (c) `_arm_and_start_host` (~`:1935`) — add `credential_store: GitCredentialStore | None = None,` after the `keep_host_on_failure: bool = False,` param, add a `:param:` line, and change the `clone_env = _build_clone_env(repo)` call (~`:1999`) to:

```python
        clone_env = _build_clone_env(repo, owner=owner, credential_store=credential_store)
```

  (`owner` is already a param of `_arm_and_start_host`.)

- [ ] **Step 5: Thread `credential_store` from the route** — in `omnigent/server/routes/sessions.py`:

  (a) `_run_managed_launch` (~`:6496`) — add, after the `relaunch_host: Host | None = None,` param:

```python
    credential_store: GitCredentialStore | None = None,
```

  and forward it into the `await _provision_managed_sandbox(` call (~`:6557`) by adding, after `relaunch_host=relaunch_host,`:

```python
        credential_store=credential_store,
```

  (b) `_provision_managed_sandbox` (~`:6580`) — add the same `credential_store: GitCredentialStore | None = None,` param (after `relaunch_host: Host | None,`), and forward `credential_store=credential_store,` into BOTH the `relaunch_managed_host(` and `launch_managed_host(` calls it makes.

  (c) At the create call site (~`:14634` `launch_task = asyncio.create_task(_run_managed_launch(...))`), add:

```python
                    credential_store=getattr(
                        request.app.state, "git_credential_store", None
                    ),
```

  (d) At the relaunch-kick call site inside `_kick_managed_relaunch` (~`:7055` `relaunch_task = asyncio.create_task(_run_managed_launch(...))`), add:

```python
            credential_store=getattr(app_state, "git_credential_store", None),
```

  Import type: `GitCredentialStore` is only needed for the annotation. With `from __future__ import annotations` at the top of `sessions.py` (verify — it is present), add it under a `TYPE_CHECKING` block if one exists, or as a normal import (no circular risk — `git_credential_store` imports only db + crypto). Prefer the existing import style in the file.

  **Multi-tenancy note (add as a code comment at the `_build_clone_env` slot-resolution branch):** `resolve_lease` scopes by `current_workspace_id()` ambiently. The single-tenant default (workspace `0`) resolves correctly with no scope wrapper — this is the backward-compat baseline. A multi-tenant deployment that stores credentials under a non-default workspace must run the background launch inside that session's `workspace_scope`; threading the session workspace into the launch task is a follow-up (out of scope for P1c-3, which only lands the resolution seam). Do NOT silently resolve to workspace 0 for a multi-tenant session — flag it in the report.

- [ ] **Step 6: Run to verify pass + backward-compat sweep**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q && env -u NODE_ENV uv run pytest tests/server/test_app_git_hosts.py tests/server/integration/test_host_session_binding.py -q`
Expected: PASS — including `test_build_clone_env_github_default_unchanged` and the pre-existing `test_build_clone_env_none_without_credential_source` (the ambient-`GIT_TOKEN` / no-store path is byte-identical).

- [ ] **Step 7: Secret-hygiene grep**

Run: `grep -rn "lease\.token\|\.token" omnigent/server/managed_hosts.py`
Expected: `lease.token` appears only inside the `_build_clone_env` slot branch's returned dict — never in a log line, an f-string error, or a repr.

- [ ] **Step 8: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git add omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): resolve owner credential lease at launch, fail closed (P1c-3)"
```

---

### Task 7: Full-suite gate (verification only)

**Files:** none.

- [ ] **Step 1: Feature suites green**

Run: `env -u NODE_ENV uv run pytest tests/stores/test_git_credential_store.py tests/db/test_git_credentials_schema.py tests/server/test_managed_hosts.py tests/server/test_app_git_hosts.py tests/server/test_git_credentials_route.py -q`
Expected: all pass.

- [ ] **Step 2: No launch/relaunch/binding regression**

Run: `env -u NODE_ENV uv run pytest tests/server/integration/test_host_session_binding.py tests/stores -q -k "conversation or launch_generation"`
Expected: all pass.

- [ ] **Step 3: Single alembic head**

Run: `env -u NODE_ENV uv run python -c "from alembic.script import ScriptDirectory; from alembic.config import Config; c=Config(); c.set_main_option('script_location','omnigent/db/migrations'); print(ScriptDirectory.from_config(c).get_heads())"`
Expected: `['za1b2c3d4e5f']` — exactly one head; the chain is `z8a2b3c4d5e6 → z9a2b3c4d5e6 → za1b2c3d4e5f`.

- [ ] **Step 4: Lint + suppression sweep**

Run: `env -u NODE_ENV uv run ruff check omnigent/db omnigent/stores omnigent/server/managed_hosts.py omnigent/server/routes/sessions.py omnigent/server/schemas.py omnigent/entities/conversation.py && grep -rn "noqa\|type: ignore" omnigent/stores/git_credential_store.py omnigent/server/managed_hosts.py`
Expected: ruff clean; grep empty.

- [ ] **Step 5: Secret-hygiene sweep**

Run: `grep -rn "token" omnigent/stores/git_credential_store.py | grep -iv "ciphertext\|token_ciphertext\|credential_id\|def \|:param\|:returns\|token: str\|token=\|the token\|a token\|bearer token\|access token"`
Expected: confirm the plaintext token appears only inside `resolve_lease` (the `_cipher.decrypt(...)` return) and `create` (the `encrypt`/param) — never in a log, error, or repr. `CredentialLease.__repr__` redacts it.

---

## What this plan does NOT do (next slices)

- **P1c-4:** the sealed, ACKed, type-tagged `deliver_credential` frame + `invalidate_credential` in the contract; host-parent egress-proxy install with a **repo-path-scoped** rewrite rule (exec/bwrap/seatbelt); the egress-rule auto-merge; kill/relaunch revocation. This slice only prepares the store lease + `credential_slot_id` on the widened `RepoWorkspace` + `launch_generation` those consume.
- **P1c-5:** k8s in-Pod proxy + init-container clone Secret; SSH ssh-agent; the tmux terminal swap decision.
- **P1c-6:** commit identity (`GIT_AUTHOR_*`/`GIT_COMMITTER_*` = session starter) + the session-sharing notice (§8.7).
- **P3:** OAuth kind wiring — only the `kind` column + the uniform lease shape land now; `resolve_lease` returns `expires_at=None` for the sole `pat` kind.
- **Multi-tenant workspace threading** for the background launch's `resolve_lease` (single-tenant/workspace-0 is correct now; see Task 6 Step 5 note).

## Self-Review (controller, before dispatch)

- **Spec coverage vs §14 "P1c-3":** `kind` column → Task 1; owner-aware resolver + `RepoWorkspace`/`ClonePlan` field-widening → Tasks 2+3; relaunch **binding persistence** (host-config id/version-hash + canonical URL + slot id) → Task 4; **add `launch_generation`** → Task 5. §9 owner-aware resolution before durable create → Task 3 (the gate at `:14502` already runs before `_create_session_from_existing_agent` at `:14520`; we make it owner-aware). §9 relaunch re-authorize slot for `host.owner` → Task 4. §12.2 `kind` SmallInteger + CheckConstraint + default pat → Task 1. §12.3 lease + precedence → Tasks 1+3+6. §8.5 `launch_generation` anti-replay anchor → Task 5. Launch-time lease consumption (§8.4/§12.3) → Task 6.
- **Placeholder scan:** every novel unit (codec, column, migration, `resolve_lease`, `CredentialLease`, `_select_credential_slot`, `host_config_hash`, `build_relaunch_binding_labels`, `reauthorize_relaunch_binding`, `increment_launch_generation`, `_build_clone_env`) ships complete code. "Mirror the neighbor" appears only for repo-specific fixture/threading boilerplate (the conversation-store test fixture name, the deep `credential_store` signature threading) whose exact shape is dictated by the file.
- **Type consistency across tasks:** `CredentialLease{token, expires_at}` produced in Task 1 is consumed verbatim in Task 6. `resolve_lease(*, owner_user_id, host_id, credential_id) -> CredentialLease | None` identical in Task 1 impl/tests and Task 6 call. `RepoWorkspace.credential_slot_id`/`host_id`/`clone_username` set in Tasks 2/3, read in Tasks 4/6. `_select_credential_slot`/`resolve_repo_workspace(..., owner_user_id=, credential_store=, label=)` identical in Task 3 and its callers (create gate Task 3, relaunch Task 4). `build_relaunch_binding_labels`/`reauthorize_relaunch_binding`/`RelaunchBindingError`/`host_config_hash` names identical in Task 4 impl and the sessions.py wiring. `increment_launch_generation(conversation_id) -> int` identical in Task 5 ABC, impl, and the `_run_managed_launch` call. `git_credential_label` identical in the schema (Task 3) and the gate read (Task 3). Label-key constant names identical between Task 4's definitions and both write (create) and read (relaunch) sites.
- **Backward-compat proof:** Task 3 `test_resolve_repo_workspace_no_store_no_selection`, Task 6 `test_build_clone_env_github_default_unchanged` + the retained `test_build_clone_env_none_without_credential_source`, Task 4 `test_reauthorize_no_binding_preserves_degrade`.

## Design ambiguities resolved (for the report)

1. **Where the label comes from (Task 3):** the spec says "the create request must name the label"; there is no existing channel. Resolved by adding an optional `SessionCreateRequest.git_credential_label` (default `None`, backward-compatible) and threading it into the gate. Alternative (encode in the workspace URL) rejected — the workspace is a clone URL.
2. **`launch_generation` home (Task 5):** placed on the AP `SqlConversation` table (not `SqlConversationMetadata`), mirroring the `next_position` monotonic allocator and `archived` — both recent NOT-NULL adds mapped directly from the row in `_to_conversation`, and both already have the `_conv_session_immediate`/`_supports_for_update` for-update infrastructure this counter reuses.
3. **Increment funnel (Task 5):** bump once at the top of `_run_managed_launch` (serves create + every relaunch); wake uses the separate `_run_managed_wake` and is untouched — this yields create=1, relaunch=2… exactly, with a single edit and no wake special-casing.
4. **Relaunch is re-auth, not re-selection (Task 4):** at relaunch there is no interactive label; the persisted slot id is re-authorized against `list_for_owner_host(host.owner, host_id)` — a lost/revoked slot (or a semantic rebind) is refused, while same-host config drift takes effect (design §9). Label-based selection is create-only.
5. **`expires_at` with no expiry column (Task 1):** `resolve_lease` returns `expires_at=None` (pat is long-lived); no `expires_at` column is added (out of scope — oauth minting is P3). The uniform lease shape is the P1 deliverable, not the value.

## Flagged (could not fully resolve here)

- **Multi-tenant workspace scope at launch (Task 6):** `resolve_lease` is workspace-ambient. Single-tenant (workspace 0, the backward-compat baseline) is correct. A multi-tenant deployment storing credentials under a non-default workspace needs the background launch task to run inside that session's `workspace_scope`; the session workspace is not currently threaded into `_run_managed_launch`. Documented as a code comment + deferred; it does not affect the single-tenant acceptance path, but a reviewer should confirm this deferral is acceptable for the target deployment.
