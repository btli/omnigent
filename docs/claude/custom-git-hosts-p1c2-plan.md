# P1c-2 Implementation Plan — owner/host-scoped `resolve_token`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `GitCredentialStore.resolve_token` resolve a credential slot against the full
authorization tuple `(workspace, owner_user_id, host_id, credential_id)` and decrypt **only** on a
complete match — closing the P1c-1-deferred hard requirement that an opaque UUID is an identifier,
not an authorization.

**Architecture:** A single store-layer change. `resolve_token` today filters workspace-only (via the
shared `_find_row`) and returns any workspace-visible row's plaintext to whoever knows the id — the
exact gap its own docstring warns about. This slice adds an ownership-scoped lookup
(`_find_owned_row`) that `resolve_token` alone uses, re-signs `resolve_token` to keyword-only
`(owner_user_id, host_id, credential_id)`, and proves the boundary with adversarial tests
(cross-user / cross-host / cross-workspace / malformed id all → `None`). `get`/`delete` keep their
route-guarded workspace-only lookup unchanged (their owner check lives at the route layer) but gain a
docstring noting they are **not** an authorization boundary for non-route callers.

**Tech Stack:** Python 3.13, SQLAlchemy 2 (`select`, `scalar_one_or_none`), `Uuid16` TypeDecorator
(`InvalidUuidError` → treat as not-found), ambient workspace scoping via `current_workspace_id()` /
`workspace_scope()` ContextVar, pytest. Package manager `uv` (use `env -u NODE_ENV` for uv ops).

## Global Constraints

- **Keyword-only for the security call.** `resolve_token`'s new params MUST be keyword-only
  (`def resolve_token(self, *, owner_user_id, host_id, credential_id)`), so a positional
  `(owner, host)` transposition on a security-critical call is impossible.
- **Workspace scoping stays ambient.** Do **not** add a `workspace_id` parameter. Scope comes from
  `current_workspace_id()` inside the query, exactly as `create`/`list_*`/`_find_row` already do.
  Tests drive it via `workspace_scope(n)`.
- **Preserve `InvalidUuidError` → `None`.** A malformed (non-hex) `credential_id` addresses no row and
  MUST return `None`, never raise — same tolerance `_find_row` already has.
- **No secret in any error or log.** `resolve_token` returns plaintext to the caller only; it must not
  place a token into an exception message, log line, or repr. (No new error paths are introduced here.)
- **No linter suppressions.** No `# noqa`, `# type: ignore`. Fix root causes.
- **`get`/`delete` unchanged in behavior/signature** — docstring-only hardening.

---

### Task 1: Owner/host-scoped `resolve_token`

**Files:**
- Modify: `omnigent/stores/git_credential_store.py`
- Test: `tests/stores/test_git_credential_store.py`

**Interfaces:**
- Consumes: `SqlGitCredential` columns `workspace_id, id, owner_user_id, host_id, token_ciphertext`;
  `current_workspace_id()`; `InvalidUuidError`; `GitCredentialCipher.decrypt`.
- Produces (relied on by the future P1c-4 handoff): the new signature
  `resolve_token(self, *, owner_user_id: str, host_id: str, credential_id: str) -> str | None` —
  returns the decrypted token **iff** the row matching all four of
  `(current_workspace_id(), owner_user_id, host_id, credential_id)` exists, else `None`.
  Provider re-derivation and refusal on an unconfigured `host_id` are **the consumer's** job (they
  need `app.state.git_hosts`, which the store does not hold) — out of scope here.

> Blast radius (verified): `GitCredentialStore.resolve_token` has **zero non-test callers**. The only
> call sites are lines 51, 52, 75, 76, 177, 186, 206, 211 of
> `tests/stores/test_git_credential_store.py`. `_find_row` stays in use by `get`/`delete`.

- [ ] **Step 1: Write the failing adversarial tests**

Add these tests to `tests/stores/test_git_credential_store.py` (the `_store` helper and imports
already exist at the top of the file):

```python
def test_resolve_token_requires_matching_owner_and_host(tmp_path) -> None:
    store = _store(tmp_path)
    cred = store.create(
        owner_user_id="alice",
        host_id="acme-forgejo",
        provider="forgejo",
        label="work",
        username=None,
        token="s3cret",
    )
    # The full, correct authorization tuple decrypts.
    assert (
        store.resolve_token(
            owner_user_id="alice", host_id="acme-forgejo", credential_id=cred.id
        )
        == "s3cret"
    )
    # A different user who knows the id gets nothing (the id is not a capability).
    assert (
        store.resolve_token(
            owner_user_id="bob", host_id="acme-forgejo", credential_id=cred.id
        )
        is None
    )
    # The right owner but the wrong host also gets nothing.
    assert (
        store.resolve_token(
            owner_user_id="alice", host_id="other-host", credential_id=cred.id
        )
        is None
    )


def test_resolve_token_malformed_id_returns_none(tmp_path) -> None:
    store = _store(tmp_path)
    store.create(
        owner_user_id="alice",
        host_id="h",
        provider="forgejo",
        label="work",
        username=None,
        token="tok",
    )
    # A non-hex id addresses no row (InvalidUuidError path) -> None, not a raise.
    assert (
        store.resolve_token(owner_user_id="alice", host_id="h", credential_id="not-a-uuid")
        is None
    )
```

Then migrate every existing `resolve_token(...)` call in this file to the keyword-only signature.
The current positional calls and their required replacements:

- `test_resolve_token_by_id_roundtrips` (cred is owner `alice`, host `h`):
  - `store.resolve_token(cred.id) == "tok"` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) == "tok"`
  - `store.resolve_token("nonexistent-id") is None` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id="nonexistent-id") is None`
- `test_multiple_identities_per_host_coexist` (owner `alice`, host `h`):
  - `store.resolve_token(work.id) == "wtok"` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=work.id) == "wtok"`
  - `store.resolve_token(personal.id) == "ptok"` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=personal.id) == "ptok"`
- `test_delete_then_absent` (owner `alice`, host `h`):
  - `store.resolve_token(cred.id) is None` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) is None`
- `test_unknown_well_formed_id_returns_none` (no row created; `absent = uuid.uuid4().hex`):
  - `store.resolve_token(absent) is None` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=absent) is None`
- `test_credentials_are_workspace_isolated` (cred owner `alice`, host `h`):
  - line in `workspace_scope(2)`: `store.resolve_token(cred.id) is None` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) is None`
    (this is the **cross-workspace** rejection — the ambient scope makes the row invisible)
  - line back in `workspace_scope(1)`: `store.resolve_token(cred.id) == "secret-w1"` →
    `store.resolve_token(owner_user_id="alice", host_id="h", credential_id=cred.id) == "secret-w1"`

- [ ] **Step 2: Run the tests to verify they fail**

Run: `env -u NODE_ENV uv run pytest tests/stores/test_git_credential_store.py -q`
Expected: FAIL — the new tests and the migrated calls raise `TypeError` (`resolve_token()` got an
unexpected keyword argument / takes 2 positional arguments) because the signature is still
`resolve_token(self, credential_id)`.

- [ ] **Step 3: Add the owner/host-scoped lookup helper**

In `omnigent/stores/git_credential_store.py`, add a new private helper directly **after** `_find_row`
(keep `_find_row` — `get`/`delete` still use it):

```python
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
```

- [ ] **Step 4: Re-sign `resolve_token` to use it**

Replace the whole `resolve_token` method with:

```python
    def resolve_token(
        self,
        *,
        owner_user_id: str,
        host_id: str,
        credential_id: str,
    ) -> str | None:
        """Decrypt the token for a credential slot, or ``None`` if not authorized.

        The slot is resolved against the full authorization tuple
        ``(workspace, owner_user_id, host_id, credential_id)`` and decrypted
        **only** when all four match. A caller supplying another user's id, a
        mismatched host, a foreign workspace (ambient, via
        :func:`current_workspace_id`), or a malformed id gets ``None`` — never
        plaintext. ``credential_id`` is an identifier, not a capability:
        ownership is proven by the query, not by possession of the id.

        This is the only method that returns plaintext; call server-side only.
        The caller (the fetch/push handoff) is responsible for the remaining
        checks the store cannot make — re-deriving the provider from the live
        operator host config and refusing an unconfigured host.

        :param owner_user_id: The authenticated owner the slot must belong to.
        :param host_id: The operator host id the slot must be bound to.
        :param credential_id: The opaque slot id to resolve.
        :returns: The decrypted token, or ``None`` if no owned row matches.
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
            return self._cipher.decrypt(row.token_ciphertext)
```

- [ ] **Step 5: Harden `get`/`delete` docstrings (no behavior change)**

`get` and `delete` remain workspace-scoped (their owner check is enforced by the route layer). Add a
one-line docstring to each making that boundary explicit so a future non-route caller does not mistake
them for an authorization gate. Replace:

```python
    def get(self, credential_id: str) -> GitCredential | None:
        with self._session() as session:
```
with:
```python
    def get(self, credential_id: str) -> GitCredential | None:
        """Fetch a credential's metadata by id, workspace-scoped only.

        Not an authorization boundary: ownership is enforced by the route
        layer. A handoff needing an owned, host-bound slot must use
        :meth:`resolve_token`.
        """
        with self._session() as session:
```

and replace:

```python
    def delete(self, credential_id: str) -> None:
        with self._session() as session:
```
with:
```python
    def delete(self, credential_id: str) -> None:
        """Delete a credential by id, workspace-scoped only.

        Not an authorization boundary: the route layer verifies the caller
        owns the credential before calling this. Tolerates a malformed id
        (matches no row).
        """
        with self._session() as session:
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `env -u NODE_ENV uv run pytest tests/stores/test_git_credential_store.py -q`
Expected: PASS (all pre-existing tests + the two new adversarial tests).

- [ ] **Step 7: Lint + type-check the changed files**

Run: `env -u NODE_ENV uv run ruff check omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py`
Expected: no findings, zero suppressions.

- [ ] **Step 8: Commit**

```bash
git add omnigent/stores/git_credential_store.py tests/stores/test_git_credential_store.py
git commit -m "feat(git-hosts): owner/host-scoped resolve_token (P1c-2 authz)"
```

## Self-Review (controller, before dispatch)

- **Spec coverage:** closes the single P1c-1-deferred hard requirement (resolve against
  `(workspace, owner, host, id)`; opaque id ≠ authorization). Provider re-derivation / unconfigured-host
  refusal are explicitly deferred to the handoff consumer (needs `git_hosts`), not this slice.
- **Placeholder scan:** none — every step carries complete code.
- **Type consistency:** new signature is used verbatim in the migrated + new tests; `_find_owned_row`
  mirrors `_find_row`'s `InvalidUuidError → None` contract.
