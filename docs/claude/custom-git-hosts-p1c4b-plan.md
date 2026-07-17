# P1c-4b — Re-deliver the git credential on wake & relaunch-after-Stop

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to
> implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the P1c-4 gap where a credential-bound managed session loses private-repo git
after its first sleep/wake or relaunch-after-Stop, because those respawn paths never re-deliver
the server-held git credential.

**Architecture:** P1c-4 delivers the credential only on the create/first-launch path
(`_launch_runner_on_host` reached from `_run_managed_launch` → `_bind_and_launch_managed_runner`).
The host discards its per-runner credential cache on every runner exit, so a respawned runner
(new `runner_id`) has no credential. Two other call sites of `_launch_runner_on_host` spawn a
runner without delivering: `_run_managed_wake` (sleep/wake resume) and `post_event`'s
relaunch-after-Stop. This slice re-resolves the repo binding at those two sites — using the SAME
`reauthorize_relaunch_binding` the wired relaunch (`_kick_managed_relaunch`) already uses — and
passes `repo`/`owner`/`credential_store` into `_launch_runner_on_host`, which already delivers +
ACKs + fails closed. A shared helper does the re-resolution.

**Tech Stack:** Python, asyncio, FastAPI. Package manager `uv` (`env -u NODE_ENV uv run ...`).

## Global Constraints

- **No-op for non-credential sessions.** The new logic is gated on a persisted credential-slot
  label (`MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY`). A session with no bound credential slot must
  wake / relaunch-after-Stop **byte-identically to today** — no new `reauthorize_relaunch_binding`
  call, no new failure mode. This is the safety boundary: the wake path does NO re-authorization
  today, so unconditional re-auth could newly fail a public-repo managed session whose host was
  removed while it slept.
- **Owner is `host.owner`.** The credential must resolve against the session owner (the `Host`
  entity's persisted `owner`), NOT the request caller / event triggerer. Obtain it via
  `host_store.get_host(conv.host_id).owner`, matching `_kick_managed_relaunch`.
- **Fail closed on a respawn re-delivery failure** (user-ratified): a revoked slot / broken
  binding (`RelaunchBindingError`) or a delivery failure (`_CREDENTIAL_DELIVERY_ERROR_CODE`) must
  refuse the respawn with a clear, token-free reason — never resume with silently-broken git.
- **No token in any log or error string.** Reuse the existing token-free error surfaces.
- **Do NOT relax** `reauthorize_relaunch_binding`'s security gates or the delivery's fail-closed
  contract. Label-refresh-on-drift (the wired relaunch's `build_relaunch_binding_labels`
  re-persist) is intentionally OUT of scope here: `reauthorize_relaunch_binding` already logs
  drift and returns the live config, so re-delivery is correct without it; skipping the refresh
  only means a drifted-config session logs drift on each respawn (benign).
- Run `pre-commit run --files ...` before each commit; never disable a lint rule.

---

### Task 1: Shared re-authorization helper + wire the wake path

**Files:**
- Modify: `omnigent/server/routes/sessions.py` — add `_reauthorize_managed_repo_for_delivery`
  helper (near `_deliver_credential_for_launch`, ~`:660`); add `app_state` param to
  `_run_managed_wake` (~`:7407`) and forward it from `_kick_managed_wake`'s call (~`:7392`);
  wire the helper + fail-closed handling into `_run_managed_wake`'s `_launch_runner_on_host`
  call (~`:7476`).
- Test: `tests/server/test_managed_hosts.py`.

**Interfaces:**
- Consumes: `reauthorize_relaunch_binding` / `RelaunchBindingError` / `MANAGED_REPO_LABEL_KEY` /
  `MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY` (all in `omnigent.server.managed_hosts`); `RepoWorkspace`
  (already module-imported); `GitCredentialStore`; `_launch_runner_on_host(..., repo=, owner=,
  credential_store=)`; `_CREDENTIAL_DELIVERY_ERROR_CODE`.
- Produces:
  - `_reauthorize_managed_repo_for_delivery(conv: Conversation, *, app_state: Any, host_store:
    HostStore | None) -> tuple[RepoWorkspace, str, GitCredentialStore] | None` — returns `None`
    when there is nothing to deliver (no credential-slot label / no store / no host row / no repo
    label); returns `(repo, owner, credential_store)` for a credential-bound session; **raises
    `RelaunchBindingError`** when the binding integrity is broken (revoked slot / host removed /
    rebind). Never logs or returns a token.

- [ ] **Step 1: Write the failing helper tests** — add to `tests/server/test_managed_hosts.py`
  (reuses `_cred_store`, `resolve_repo_workspace`/`_GH_HOSTS` is not needed here — the helper reads
  `app_state.git_hosts`). Add a tiny fake host + host_store and a labels-carrying conv stub:

```python
def test_reauthorize_managed_repo_for_delivery_returns_binding_for_credential_session(
    tmp_path,
) -> None:
    from types import SimpleNamespace

    from omnigent.server.managed_hosts import (
        MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY,
        MANAGED_GIT_HOST_ID_LABEL_KEY,
        MANAGED_REPO_LABEL_KEY,
    )
    from omnigent.server.routes.sessions import _reauthorize_managed_repo_for_delivery

    store = _cred_store(tmp_path)
    slot = store.create(
        owner_user_id="alice", host_id="acme", provider="forgejo",
        label="work", username=None, token="ghp_secret",
    )
    conv = SimpleNamespace(
        host_id="host_1",
        labels={
            MANAGED_REPO_LABEL_KEY: "https://git.acme.com/team/proj",
            MANAGED_GIT_HOST_ID_LABEL_KEY: "acme",
            MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY: slot.id,
        },
    )
    host_store = SimpleNamespace(get_host=lambda hid: SimpleNamespace(owner="alice"))
    app_state = SimpleNamespace(git_hosts=_GH_HOSTS, git_credential_store=store)

    result = _reauthorize_managed_repo_for_delivery(
        conv, app_state=app_state, host_store=host_store
    )
    assert result is not None
    repo, owner, cred_store = result
    assert owner == "alice"
    assert cred_store is store
    assert repo.credential_slot_id == slot.id
    assert repo.canonical_host == "git.acme.com"


def test_reauthorize_managed_repo_for_delivery_noop_without_credential_slot() -> None:
    from types import SimpleNamespace

    from omnigent.server.managed_hosts import MANAGED_REPO_LABEL_KEY
    from omnigent.server.routes.sessions import _reauthorize_managed_repo_for_delivery

    # A managed session with a repo label but NO credential-slot label: the
    # respawn must behave exactly as before — helper is a pure no-op.
    conv = SimpleNamespace(
        host_id="host_1",
        labels={MANAGED_REPO_LABEL_KEY: "https://github.com/org/repo"},
    )
    app_state = SimpleNamespace(git_hosts=_GH_HOSTS, git_credential_store=object())
    result = _reauthorize_managed_repo_for_delivery(
        conv, app_state=app_state, host_store=SimpleNamespace(get_host=lambda hid: None)
    )
    assert result is None


def test_reauthorize_managed_repo_for_delivery_raises_on_revoked_slot(tmp_path) -> None:
    from types import SimpleNamespace

    import pytest

    from omnigent.server.managed_hosts import (
        MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY,
        MANAGED_GIT_HOST_ID_LABEL_KEY,
        MANAGED_REPO_LABEL_KEY,
        RelaunchBindingError,
    )
    from omnigent.server.routes.sessions import _reauthorize_managed_repo_for_delivery

    store = _cred_store(tmp_path)
    slot = store.create(
        owner_user_id="alice", host_id="acme", provider="forgejo",
        label="work", username=None, token="ghp_secret",
    )
    conv = SimpleNamespace(
        host_id="host_1",
        labels={
            MANAGED_REPO_LABEL_KEY: "https://git.acme.com/team/proj",
            MANAGED_GIT_HOST_ID_LABEL_KEY: "acme",
            MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY: slot.id,
        },
    )
    store.delete(slot.id)  # revoked while the session slept
    host_store = SimpleNamespace(get_host=lambda hid: SimpleNamespace(owner="alice"))
    app_state = SimpleNamespace(git_hosts=_GH_HOSTS, git_credential_store=store)
    with pytest.raises(RelaunchBindingError) as exc:
        _reauthorize_managed_repo_for_delivery(
            conv, app_state=app_state, host_store=host_store
        )
    assert "ghp_secret" not in str(exc.value)
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k reauthorize_managed_repo_for_delivery`
Expected: FAIL — `ImportError: cannot import name '_reauthorize_managed_repo_for_delivery'`.

- [ ] **Step 3: Add the helper** — in `sessions.py`, right after `_deliver_credential_for_launch`
  (~`:660`, before the next non-credential helper):

```python
def _reauthorize_managed_repo_for_delivery(
    conv: Conversation,
    *,
    app_state: Any,
    host_store: HostStore | None,
) -> tuple[RepoWorkspace, str, GitCredentialStore] | None:
    """Re-resolve a credential-bound session's repo binding for a respawn.

    Returns ``(repo, owner, credential_store)`` so the wake / relaunch-after-Stop
    paths can re-deliver the owner's git credential to the freshly spawned
    runner (the host discards its per-runner cache on every runner exit). Gated
    on a persisted credential-slot label: a session with no bound credential
    returns ``None`` and its respawn is unchanged. The owner is the host's
    persisted owner, never the request caller. Re-authorization reuses
    :func:`reauthorize_relaunch_binding`, so a revoked slot / removed host /
    rebind raises :class:`RelaunchBindingError` (the caller fails the respawn
    closed); the message names no token.

    :param conv: The session row (carries ``host_id`` and ``labels``).
    :param app_state: ``request.app.state`` (supplies ``git_hosts`` +
        ``git_credential_store``).
    :param host_store: Persistent host registrations, for the owner lookup.
    :returns: ``(repo, owner, credential_store)`` or ``None`` when nothing is
        bound to deliver.
    :raises RelaunchBindingError: When the binding integrity is broken.
    """
    from omnigent.server.managed_hosts import (
        MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY,
        MANAGED_REPO_LABEL_KEY,
        reauthorize_relaunch_binding,
    )

    if not conv.labels.get(MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY):
        return None  # no bound credential -> respawn unchanged
    credential_store = getattr(app_state, "git_credential_store", None)
    if credential_store is None or host_store is None:
        return None  # feature not configured
    raw_repo = conv.labels.get(MANAGED_REPO_LABEL_KEY)
    if not raw_repo:
        return None
    host = host_store.get_host(conv.host_id)
    if host is None:
        return None  # owner unknowable; respawn's own host handling takes over
    repo = reauthorize_relaunch_binding(
        raw_repo=raw_repo,
        labels=conv.labels,
        owner=host.owner,
        hosts=getattr(app_state, "git_hosts", ()),
        credential_store=credential_store,
    )
    return repo, host.owner, credential_store
```

- [ ] **Step 4: Thread `app_state` into `_run_managed_wake`.** Add `app_state: Any` to its
  keyword-only signature (~`:7416`, after `tunnel_registry`) with a `:param app_state:` doc line
  ("``request.app.state`` — supplies the git-hosts + credential registries for credential
  re-delivery"). In `_kick_managed_wake`'s call to `_run_managed_wake` (~`:7392`), add
  `app_state=app_state,` (it is already in `_kick_managed_wake`'s scope).

- [ ] **Step 5: Re-deliver on wake.** In `_run_managed_wake`, replace the credential-blind launch
  call (~`:7476`) with a re-resolve + fail-closed form:

```python
        if host_conn is not None:
            try:
                delivery = _reauthorize_managed_repo_for_delivery(
                    refreshed, app_state=app_state, host_store=host_store
                )
            except RelaunchBindingError as exc:
                tracker.fail(session_id, str(exc))
                _publish_sandbox_status(session_id, "failed", str(exc))
                return
            repo = owner = credential_store = None
            if delivery is not None:
                repo, owner, credential_store = delivery
            launch_attempt = await _launch_runner_on_host(
                refreshed,
                conversation_store,
                host_registry,
                host_conn,
                repo=repo,
                owner=owner,
                credential_store=credential_store,
            )
            if launch_attempt.error_code == _CREDENTIAL_DELIVERY_ERROR_CODE:
                reason = launch_attempt.error or "git credential delivery failed"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
```

  Add the `RelaunchBindingError` import to the function-local import block already present at the
  top of `_run_managed_wake` (~`:7446`, `from omnigent.server.managed_hosts import
  resume_managed_host`) → extend it to also import `RelaunchBindingError`. Keep the rest of the
  existing launch/rendezvous body below unchanged.

- [ ] **Step 6: Run the helper + wake suites**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k "reauthorize or managed_wake or wake"`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git add omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): re-deliver git credential on managed wake (P1c-4b)"
```

---

### Task 2: Wire the relaunch-after-Stop path in `post_event`

**Files:**
- Modify: `omnigent/server/routes/sessions.py` — the `post_event` relaunch-after-Stop
  `_launch_runner_on_host` call (~`:20512`) and the branch just above it.
- Test: `tests/server/test_managed_hosts.py` (helper already covered; add a focused assertion
  that the credential error code is surfaced via the failure-turn path — or, if that is not unit-
  reachable, assert the helper is invoked with `host.owner`; keep it non-vacuous).

**Interfaces:**
- Consumes: `_reauthorize_managed_repo_for_delivery` (Task 1); `_persist_host_launch_failure_turn(
  session_id, conv, body, conversation_store, host_error, runner_router, *, created_by) -> str`;
  `_CREDENTIAL_DELIVERY_ERROR_CODE`; `RelaunchBindingError`.

- [ ] **Step 1: Re-deliver on relaunch-after-Stop.** In `post_event`, at the host-alive relaunch
  (~`:20509-20517`), re-resolve before the launch and pass the binding, mirroring the existing
  `_HARNESS_NOT_CONFIGURED_ERROR_CODE` failure-turn surface for the credential case:

```python
            if runner_client is None and _host_reg is not None:
                _host_conn = _host_reg.get(conv.host_id)
                if _host_conn is not None:
                    try:
                        _delivery = _reauthorize_managed_repo_for_delivery(
                            conv,
                            app_state=request.app.state,
                            host_store=getattr(request.app.state, "host_store", None),
                        )
                    except RelaunchBindingError as exc:
                        item_id = await _persist_host_launch_failure_turn(
                            session_id, conv, body, conversation_store,
                            str(exc), runner_router,
                            created_by=_attribution_user(user_id),
                        )
                        return {"queued": True, "item_id": item_id}
                    _repo = _owner = _cred_store = None
                    if _delivery is not None:
                        _repo, _owner, _cred_store = _delivery
                    launch_attempt = await _launch_runner_on_host(
                        conv,
                        conversation_store,
                        _host_reg,
                        _host_conn,
                        repo=_repo,
                        owner=_owner,
                        credential_store=_cred_store,
                    )
                    if launch_attempt.error_code == _HARNESS_NOT_CONFIGURED_ERROR_CODE:
                        # (unchanged existing branch — persist failure turn, return)
                        ...
                    if launch_attempt.error_code == _CREDENTIAL_DELIVERY_ERROR_CODE:
                        item_id = await _persist_host_launch_failure_turn(
                            session_id, conv, body, conversation_store,
                            launch_attempt.error, runner_router,
                            created_by=_attribution_user(user_id),
                        )
                        return {"queued": True, "item_id": item_id}
                    relaunched_runner_id = launch_attempt.runner_id
```

  Add `RelaunchBindingError` to the appropriate import scope in `post_event` (function-local
  `from omnigent.server.managed_hosts import RelaunchBindingError`, or extend an existing local
  import block). Leave the existing `_HARNESS_NOT_CONFIGURED_ERROR_CODE` block and the
  `else:`/`_maybe_relaunch_managed_sandbox` fall-through (host tunnel gone) exactly as they are —
  that fall-through reaches the wake path (Task 1), so it is already covered.

- [ ] **Step 2: Add a focused test** — an integration-style assertion is heavy here; instead add
  a unit test that a credential-bound `conv` routed through the helper at this site yields the
  binding (owner = `host.owner`), and that a non-credential `conv` yields `None` (so the existing
  relaunch is untouched). If the Task-1 helper tests already cover both, add a single test that
  the `post_event` credential-failure surfaces as a failure turn by monkeypatching
  `_reauthorize_managed_repo_for_delivery` to raise `RelaunchBindingError` and asserting the
  route returns `{"queued": True, ...}` without launching — only if the route is reachable in the
  existing test harness; otherwise document in the report why the wiring is covered by the helper
  unit tests + manual trace and leave a `# covered by` note.

- [ ] **Step 3: Run the affected suites**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q`
Expected: PASS.

- [ ] **Step 4: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py && env -u NODE_ENV uv run ruff format omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git add omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): re-deliver git credential on relaunch-after-Stop (P1c-4b)"
```

---

## What this slice does NOT do
- Label-refresh-on-drift at respawn (benign drift logging only; orthogonal to delivery).
- Bump `launch_generation` on wake (deliberately unchanged; anti-replay holds via the fresh
  `runner_id` in the AAD).
- The seam-E eager-validation improvement (no-egress-allowlist reports as a repeating tool-error)
  — separate P1d-adjacent follow-up.
