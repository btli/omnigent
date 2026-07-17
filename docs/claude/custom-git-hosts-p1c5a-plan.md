# P1c-5a — Kubernetes parity for per-user git credentials: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Kubernetes launcher accepts `clone_env` (init-clone via a distinct, init-scoped,
delete-after-init per-Pod Secret) instead of rejecting it, plus two provider-neutral fail-closed
gates (HTTPS-only credentialed URLs at the server; sandbox-inactive consumption refusal in the
runner).

**Architecture:** Spec §8.4a of `docs/claude/custom-git-hosts-design.md` (v5, tri-engine-reviewed
over 5 rounds, user-ratified 2026-07-16). The server seam is already provider-uniform
(`_build_clone_env` → `start_host(clone_env=...)` on launch and relaunch), so no server-route
changes: the work is `kubernetes.py` (builders + lifecycle), one guard in
`managed_hosts._build_clone_env`, and one hoisted check in `inner/os_env.py`.

**Tech Stack:** Python 3.12, pytest, kubernetes SDK faked via the existing `_FakeCore` in
`tests/onboarding/sandboxes/test_kubernetes.py`.

## Global Constraints

- The credential value appears ONLY in the clone-Secret create call body: never in the Pod
  manifest, `click.echo` lines, or exception strings — error compositions on the clone path are
  scrubbed with `replace(value, "***")` (same mechanism as `base.py`'s exec redaction).
- The **main container never references the clone Secret in any form**, and keeps ALL
  `env_literals` unchanged (colliding names included).
- With `clone_env=None` the Pod manifest is **byte-identical to today** (existing tests keep
  passing unmodified — that is the regression proof).
- The clone Secret is **distinct** from the token Secret (name `"{pod}-clone-cred"`), carries the
  same managed-by/role GC labels, and is deleted: right after the Running wait; on both
  `start_host` failure paths; and in `terminate()` **independently of earlier teardown failures**.
- ownerRef PATCH is best-effort: RBAC/patch failure warns and continues, never fails the launch.
- Fail closed everywhere; no new dependencies; no lint suppressions (no `noqa`/`type: ignore`).
- Run everything with `env -u NODE_ENV uv run pytest ...` (NODE_ENV=production breaks unrelated
  tooling); pre-commit before each commit.
- Spec references in comments: describe the scenario, never PR/issue/spec numbers.

---

### Task 1: Server HTTPS-only gate for credentialed clone URLs

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`_build_clone_env`, ~line 882)
- Test: `tests/server/test_managed_hosts.py`

**Interfaces:**
- Consumes: `_build_clone_env(repo, *, owner, credential_store)` as it exists (returns
  `{"GIT_TOKEN": ..., "GIT_USERNAME": ...}` or `None`; raises `ValueError` fail-closed — callers
  already wrap into HTTP 502).
- Produces: same signature; NEW behavior — raises `ValueError` when a credential would be
  delivered for a non-HTTPS `repo.url`. Both branches (bound slot AND operator
  `credential_source`) are gated. Message contains no token.

- [ ] **Step 1: Write the failing tests**

Add to `tests/server/test_managed_hosts.py`, next to the existing `_build_clone_env` tests
(search for `_build_clone_env` in the file and reuse that section's existing fixtures/helpers for
constructing a `RepoWorkspace` and a credential store — mirror the neighboring tests' setup
exactly; only the URL and expectations below are new):

```python
def test_build_clone_env_refuses_ssh_url_for_bound_slot(...existing fixtures...):
    """A bound user slot with a non-HTTPS clone URL fails closed at launch."""
    repo = <RepoWorkspace as in the neighboring bound-slot test, but url="ssh://git@forge.example/org/repo.git">
    with pytest.raises(ValueError, match="requires an HTTPS clone URL"):
        _build_clone_env(repo, owner="alice", credential_store=store)


def test_build_clone_env_refuses_scp_like_url_for_operator_source(...):
    """An operator credential_source with a git@ URL fails closed at launch."""
    repo = <RepoWorkspace as in the neighboring operator-source test, but url="git@forge.example:org/repo.git">
    with pytest.raises(ValueError, match="requires an HTTPS clone URL"):
        _build_clone_env(repo, owner=None, credential_store=None)


def test_build_clone_env_refuses_plain_http_for_bound_slot(...):
    """Plain http would send the token in cleartext — refused like SSH."""
    repo = <bound-slot RepoWorkspace, url="http://forge.example/org/repo.git">
    with pytest.raises(ValueError, match="requires an HTTPS clone URL"):
        _build_clone_env(repo, owner="alice", credential_store=store)


def test_build_clone_env_ssh_url_without_credential_is_unaffected(...):
    """No slot + no operator source: SSH URLs keep today's None (ambient) path."""
    repo = <RepoWorkspace with url="ssh://git@forge.example/org/repo.git", credential_slot_id=None, credential_source=None>
    assert _build_clone_env(repo, owner=None, credential_store=None) is None
```

The `<...>` placeholders mean: copy the construction pattern of the adjacent existing test for
that branch verbatim, changing only the URL. Do not invent new fixtures.

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -k "clone_env" -x -q`
Expected: the four new tests FAIL (no gate yet); existing clone_env tests PASS.

- [ ] **Step 3: Implement the gate**

In `omnigent/server/managed_hosts.py`, inside `_build_clone_env`, add a module-level helper just
above the function and call it from BOTH credentialed branches:

```python
def _require_https_clone_url(repo: RepoWorkspace) -> None:
    """A credentialed clone must ride HTTPS.

    The delivered pair feeds git's HTTP credential helper and the runner's
    HTTPS rewrite rule; git ignores both for SSH transport, so a credentialed
    SSH/scp-style (or cleartext http) URL would silently clone under ambient
    identity instead of the selected credential. Refuse rather than bypass.
    """
    if not repo.url.lower().startswith("https://"):
        raise ValueError(
            "this session's git credential requires an HTTPS clone URL; "
            "SSH and non-HTTPS remotes cannot use a stored credential yet"
        )
```

Call sites inside `_build_clone_env`:
1. In the bound-slot branch: immediately after the `if owner is None or credential_store is None:`
   guard (BEFORE `resolve_lease` — fail fast without decrypting).
2. In the operator branch: immediately after `if repo.credential_source is None: return None`
   (BEFORE `resolve_credential`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -k "clone_env" -q`
Expected: ALL pass (new + existing).

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): refuse non-HTTPS clone URLs for credentialed launches"
```

---

### Task 2: Runner sandbox-inactive consumption gate

**Files:**
- Modify: `omnigent/inner/os_env.py` (`_OsEnvHelper._start_locked`, ~lines 580–640)
- Test: `tests/inner/test_os_env.py`

**Interfaces:**
- Consumes: `_read_managed_git_delivery(environ)` (returns a 5-tuple or `None`; raises
  `ManagedGitCredentialError` on partial binding), `ManagedGitCredentialError`
  (`os_env.py:267`), `SandboxPolicy.active`.
- Produces: `_start_locked` raises `ManagedGitCredentialError` when delivery env is present and
  `sandbox.active` is `False`, BEFORE any helper process is spawned. Active-sandbox behavior is
  unchanged (the existing consumption block reuses the hoisted `delivery` value instead of
  re-reading).

- [ ] **Step 1: Write the failing test**

Add to `tests/inner/test_os_env.py`, following the existing managed-git delivery tests in that
file (they already construct the delivery env — reuse their env-var constants from
`omnigent.runner.identity` and their helper/OsEnv construction pattern; a `sandbox.type: none` /
inactive `SandboxPolicy` already appears in that file's passthrough tests — reuse it):

```python
def test_inactive_sandbox_with_delivery_fails_closed(...existing fixtures...):
    """Delivered credential + sandbox.type none must refuse, not silently drop the swap."""
    <set the full 5-var delivery env via monkeypatch, as the neighboring delivery tests do>
    <build the helper with an INACTIVE SandboxPolicy, as the passthrough tests do>
    with pytest.raises(ManagedGitCredentialError, match="sandbox is inactive"):
        <trigger helper start — same call the neighboring tests use to force _start_locked>


def test_inactive_sandbox_without_delivery_is_unchanged(...):
    """No delivery env: inactive sandbox keeps today's passthrough behavior."""
    <build the helper with an INACTIVE SandboxPolicy, no delivery env>
    <trigger helper start; assert it starts and serves, as the existing inactive-path test does>
```

- [ ] **Step 2: Run tests to verify the first fails**

Run: `env -u NODE_ENV uv run pytest tests/inner/test_os_env.py -k "inactive" -q`
Expected: `test_inactive_sandbox_with_delivery_fails_closed` FAILS (no gate yet).

- [ ] **Step 3: Implement the hoisted gate**

In `_start_locked`, replace the current in-branch read with a hoisted read + gate. Current code:

```python
        helper_cwd = self.cwd
        credential_runtime: CredentialProxyRuntime | None = None
        if sandbox.active:
```
and further down inside the branch:
```python
            delivery = _read_managed_git_delivery(os.environ)
            if delivery is not None:
```

New code:

```python
        helper_cwd = self.cwd
        credential_runtime: CredentialProxyRuntime | None = None
        # A server-delivered managed-git credential is consumable only by an
        # active sandbox (the swap lives in the egress proxy). With
        # sandbox.type: none the managed token is stripped from the helper env,
        # so git would silently run on whatever ambient credentials the
        # environment carries — refuse to start instead.
        delivery = _read_managed_git_delivery(os.environ)
        if delivery is not None and not sandbox.active:
            raise ManagedGitCredentialError(
                "a managed git credential was delivered but the sandbox is "
                "inactive (sandbox.type: none); refusing to run git without "
                "the credential swap"
            )
        if sandbox.active:
```
and inside the branch, delete the now-duplicate read, keeping:
```python
            if delivery is not None:
```

- [ ] **Step 4: Run the file's full suite**

Run: `env -u NODE_ENV uv run pytest tests/inner/test_os_env.py -q`
Expected: ALL pass (the hoisted read must not disturb the active-sandbox delivery tests or the
partial-binding fail-closed tests).

- [ ] **Step 5: Commit**

```bash
git add omnigent/inner/os_env.py tests/inner/test_os_env.py
git commit -m "feat(git-hosts): fail closed when a delivered credential meets an inactive sandbox"
```

---

### Task 3: Pure k8s manifest builders (clone Secret + Pod projection)

**Files:**
- Modify: `omnigent/onboarding/sandboxes/kubernetes.py` (`_token_secret_name` neighborhood ~349;
  `build_pod_manifest` 448–592)
- Test: `tests/onboarding/sandboxes/test_kubernetes.py`

**Interfaces:**
- Consumes: `_MANAGED_BY_LABEL/_MANAGED_BY_VALUE/_ROLE_LABEL/_ROLE_VALUE`, `_HOME_DIR`,
  existing `build_pod_manifest` parameters.
- Produces (Task 4 relies on these exact names):
  - `_clone_secret_name(pod_name: str) -> str` → `f"{pod_name}-clone-cred"`
  - `build_clone_secret_manifest(*, secret_name: str, namespace: str, clone_env: Mapping[str, str]) -> dict[str, object]`
  - `build_pod_manifest(..., clone_secret_name: str | None = None, clone_env_keys: Sequence[str] | None = None)`

- [ ] **Step 1: Write the failing tests**

```python
def test_clone_secret_manifest_shape() -> None:
    """Opaque, GC-labeled like the token Secret, stringData = the env pairs."""
    manifest = build_clone_secret_manifest(
        secret_name="omnigent-pod-1-clone-cred",
        namespace="ns",
        clone_env={"GIT_TOKEN": "tok-value", "GIT_USERNAME": "alice"},
    )
    assert manifest["type"] == "Opaque"
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "managed-sandbox",
    }
    assert manifest["stringData"] == {"GIT_TOKEN": "tok-value", "GIT_USERNAME": "alice"}


def test_pod_manifest_clone_secret_projection() -> None:
    """Init container swaps to the clone Secret; main container is untouched."""
    manifest = _pod_manifest_kwargs_helper(  # build exactly as the existing
        # build_pod_manifest tests in this file do, adding:
        clone_secret_name="omnigent-pod-1-clone-cred",
        clone_env_keys=("GIT_TOKEN", "GIT_USERNAME"),
        env_literals={"HTTPS_PROXY": "http://proxy:3128", "GIT_USERNAME": "operator-lit"},
        harness_secret="omnigent-creds",
    )
    init = manifest["spec"]["initContainers"][0]
    main = manifest["spec"]["containers"][0]
    # Init: clone Secret ref REPLACES the harness ref.
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-pod-1-clone-cred"}}]
    # Init env: HOME + env_literals MINUS clone_env_keys (collision rule).
    init_names = [e["name"] for e in init["env"]]
    assert "HTTPS_PROXY" in init_names
    assert "GIT_USERNAME" not in init_names
    # Main: harness ref intact, ALL env_literals intact (colliding name included),
    # and no reference to the clone Secret anywhere in the container spec.
    assert main["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert {"name": "GIT_USERNAME", "value": "operator-lit"} in main["env"]
    assert "clone-cred" not in json.dumps(manifest["spec"]["containers"])


def test_pod_manifest_without_clone_secret_is_unchanged() -> None:
    """clone_secret_name=None keeps today's exact init shape (regression)."""
    manifest = _pod_manifest_kwargs_helper(harness_secret="omnigent-creds")
    init = manifest["spec"]["initContainers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert init["env"] == [{"name": "HOME", "value": "/home/omnigent"}]
```

(`_pod_manifest_kwargs_helper` stands for calling `build_pod_manifest` with the same literal
kwargs the file's existing manifest tests use — copy their call, don't build a shared helper
unless one already exists.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `env -u NODE_ENV uv run pytest tests/onboarding/sandboxes/test_kubernetes.py -k "clone_secret or without_clone" -q`
Expected: FAIL — `build_clone_secret_manifest` undefined; unknown kwargs.

- [ ] **Step 3: Implement the builders**

Next to `_token_secret_name` (~line 357):

```python
def _clone_secret_name(pod_name: str) -> str:
    """
    Name of the per-Pod clone-credential Secret for *pod_name*.

    Distinct from the token Secret: the clone credential is deleted as soon
    as init succeeds, while the token Secret lives until terminate.

    :param pod_name: The Pod name (≤63 chars).
    :returns: The Secret name, e.g. ``"omnigent-managed-a1b2c3d4-1a2b3c-clone-cred"``.
    """
    return f"{pod_name}-clone-cred"
```

Next to `build_token_secret_manifest` (~line 445):

```python
def build_clone_secret_manifest(
    *, secret_name: str, namespace: str, clone_env: Mapping[str, str]
) -> dict[str, object]:
    """
    Build the per-Pod clone-credential Secret manifest as a plain dict.

    Projected ONLY into the init container (per-key ownership stays with the
    Pod builder); carries the same GC labels as the token Secret and is
    deleted right after the init containers succeed — it never lives for the
    Pod's lifetime.

    :param secret_name: The Secret name (see :func:`_clone_secret_name`).
    :param namespace: Namespace the Secret is created in.
    :param clone_env: The credential env pairs (e.g. ``GIT_TOKEN`` /
        ``GIT_USERNAME``); values ride ``stringData`` only.
    :returns: The Secret manifest dict.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": {_MANAGED_BY_LABEL: _MANAGED_BY_VALUE, _ROLE_LABEL: _ROLE_VALUE},
        },
        "type": "Opaque",
        "stringData": dict(clone_env),
    }
```

In `build_pod_manifest`: add parameters `clone_secret_name: str | None = None` and
`clone_env_keys: Sequence[str] | None = None` (document both: names only, never values; the
collision rule; init-only projection). Replace the init-container construction:

```python
    init_env: list[dict[str, object]] = [{"name": "HOME", "value": _HOME_DIR}]
    if clone_secret_name:
        # The clone credential Secret REPLACES the shared harness ref for the
        # init container: the clone step sees only the git pair plus the
        # non-secret operator env passthrough — not the deployment's LLM
        # credentials. Keys the Secret owns are excluded from the literal
        # projection (explicit env would beat envFrom and half-override the
        # delivered pair).
        excluded = frozenset(clone_env_keys or ())
        init_env.extend(
            {"name": name, "value": value}
            for name, value in env_literals.items()
            if name not in excluded
        )
    init_container: dict[str, object] = {
        "name": _INIT_CONTAINER_NAME,
        "image": image,
        "workingDir": _HOME_DIR,
        "command": _render_workspace_prep_command(workspace, clone_dir, repo_url, repo_branch),
        "env": init_env,
        "resources": pod_resources,
        "securityContext": container_security,
        "volumeMounts": home_mount,
    }
    if clone_secret_name:
        init_container["envFrom"] = [{"secretRef": {"name": clone_secret_name}}]
    elif harness_secret:
        # The clone may need GIT_TOKEN (private repos) from the harness Secret.
        init_container["envFrom"] = [{"secretRef": {"name": harness_secret}}]
```

Add `Mapping`/`Sequence` to the existing `collections.abc` import if absent. The main-container
code path is NOT touched.

- [ ] **Step 4: Run the whole test file**

Run: `env -u NODE_ENV uv run pytest tests/onboarding/sandboxes/test_kubernetes.py -q`
Expected: ALL pass — pre-existing manifest tests unmodified (the byte-identical regression proof).

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/kubernetes.py tests/onboarding/sandboxes/test_kubernetes.py
git commit -m "feat(git-hosts): k8s clone-credential Secret + init-scoped Pod projection (pure builders)"
```

---

### Task 4: `start_host` / `terminate` clone-Secret lifecycle

**Files:**
- Modify: `omnigent/onboarding/sandboxes/kubernetes.py` (`start_host` 1014–1143,
  `_best_effort_delete` ~1330, `terminate` ~1384)
- Test: `tests/onboarding/sandboxes/test_kubernetes.py` (extend `_FakeCore`; REPLACE
  `test_start_host_rejects_clone_env`)

**Interfaces:**
- Consumes (from Task 3): `_clone_secret_name`, `build_clone_secret_manifest`,
  `build_pod_manifest(clone_secret_name=..., clone_env_keys=...)`.
- Produces: `start_host` accepts `clone_env` (no more `SandboxCapabilityError`); a module-level
  `_redact_values(text: str, values: Iterable[str]) -> str`; `_best_effort_delete` gains
  `clone_secret_name: str | None = None`; `terminate` deletes the derivable clone Secret
  independently first.

- [ ] **Step 1: Extend `_FakeCore` (test harness only)**

In `_FakeCore`: `create_namespaced_pod` returns
`SimpleNamespace(metadata=SimpleNamespace(uid="pod-uid-123"))` after recording; add

```python
        self.patched_secrets: list[tuple[str, dict[str, object]]] = []
        self.patch_secret_error: Exception | None = None
        self.delete_secret_errors: list[Exception | None] = []

    def patch_namespaced_secret(self, name, namespace, body, _request_timeout=None):
        self.calls.append("patch_secret")
        if self.patch_secret_error is not None:
            raise self.patch_secret_error
        self.patched_secrets.append((name, body))
```
and in `delete_namespaced_secret`, pop/raise from `delete_secret_errors` exactly the way
`delete_namespaced_pod` handles `delete_pod_errors`.

- [ ] **Step 2: Write the failing tests** (REPLACING `test_start_host_rejects_clone_env`)

```python
_CLONE_ENV = {"GIT_TOKEN": "tok-secret-value", "GIT_USERNAME": "alice"}


def _start_with_clone(fake_core: _FakeCore) -> str:
    fake_core.read_queue = [_pod(phase="Running")]
    return _launcher().start_host(
        "omnigent-pod-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
        repo_url="https://forge.example/org/repo.git",
        repo_name="repo",
        clone_env=dict(_CLONE_ENV),
    )


def test_clone_env_lifecycle_happy_path(fake_core: _FakeCore) -> None:
    """token Secret → clone Secret → Pod → ownerRef patch → delete clone Secret."""
    workspace = _start_with_clone(fake_core)
    assert workspace == "/home/omnigent/workspace/repo"
    assert fake_core.calls.index("create_secret") < fake_core.calls.index("create_pod")
    assert fake_core.calls.count("create_secret") == 2
    assert fake_core.calls.index("create_pod") < fake_core.calls.index("patch_secret")
    clone = fake_core.created_secrets[1]
    assert clone["metadata"]["name"] == "omnigent-pod-1-clone-cred"
    assert clone["stringData"] == _CLONE_ENV
    name, body = fake_core.patched_secrets[0]
    assert name == "omnigent-pod-1-clone-cred"
    assert body["metadata"]["ownerReferences"][0]["uid"] == "pod-uid-123"
    assert body["metadata"]["ownerReferences"][0]["name"] == "omnigent-pod-1"
    # Deleted right after Running; the token Secret and Pod survive.
    assert fake_core.deleted_secrets == ["omnigent-pod-1-clone-cred"]
    assert fake_core.deleted_pods == []
    init = fake_core.created_pods[0]["spec"]["initContainers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-pod-1-clone-cred"}}]


def test_clone_env_values_never_leave_secret_body(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    _start_with_clone(fake_core)
    assert "tok-secret-value" not in json.dumps(fake_core.created_pods)
    assert "tok-secret-value" not in capsys.readouterr().out


def test_clone_secret_create_failure_cleans_up(fake_core: _FakeCore) -> None:
    """Failure creating the SECOND secret still tears down the first + any Pod."""
    fake_core.create_secret_errors = [None, _FakeApiException(500, "boom")]  # see Step 1 note
    with pytest.raises(click.ClickException) as excinfo:
        _start_with_clone(fake_core)
    assert "tok-secret-value" not in str(excinfo.value)
    assert "omnigent-pod-1-token" in fake_core.deleted_secrets
    assert "omnigent-pod-1-clone-cred" in fake_core.deleted_secrets


def test_wait_failure_redacts_and_reaps_all_three(fake_core: _FakeCore) -> None:
    """A failed init clone reaps Pod + both Secrets; the log tail is scrubbed."""
    fake_core.read_queue = [
        _pod(phase="Pending", init_statuses=[_terminated(128, name="workspace-init")])
    ]
    fake_core.logs["workspace-init"] = "fatal: auth failed for tok-secret-value"
    with pytest.raises(click.ClickException) as excinfo:
        _start_with_clone(fake_core)
    assert "tok-secret-value" not in str(excinfo.value)
    assert "***" in str(excinfo.value)
    assert "omnigent-pod-1" in fake_core.deleted_pods
    assert set(fake_core.deleted_secrets) == {
        "omnigent-pod-1-token", "omnigent-pod-1-clone-cred",
    }


def test_owner_ref_patch_failure_warns_and_continues(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_core.patch_secret_error = _FakeApiException(403, "forbidden")
    workspace = _start_with_clone(fake_core)
    assert workspace.endswith("/repo")
    assert "could not set owner reference" in capsys.readouterr().err
    assert fake_core.deleted_secrets == ["omnigent-pod-1-clone-cred"]


def test_delete_after_running_failure_warns_not_fails(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_core.delete_secret_errors = [_FakeApiException(500, "hiccup")]
    workspace = _start_with_clone(fake_core)
    assert workspace.endswith("/repo")
    assert "could not delete clone credential secret" in capsys.readouterr().err


def test_invalid_or_colliding_clone_env_keys_fail_before_api(fake_core: _FakeCore) -> None:
    for bad in ({"BAD-KEY": "v"}, {HOST_TOKEN_ENV_VAR: "v"}):
        with pytest.raises(click.ClickException):
            _launcher().start_host(
                "omnigent-pod-x", token=_TOKEN, host_id="h", host_name="m",
                server_url="http://srv.example.com",
                repo_url="https://forge.example/org/repo.git", repo_name="repo",
                clone_env=bad,
            )
    assert fake_core.calls == []


def test_no_clone_env_makes_zero_clone_secret_calls(fake_core: _FakeCore) -> None:
    fake_core.read_queue = [_pod(phase="Running")]
    _launcher().start_host(
        "omnigent-pod-1", token=_TOKEN, host_id="h", host_name="m",
        server_url="http://srv.example.com",
    )
    assert fake_core.calls.count("create_secret") == 1
    assert "patch_secret" not in fake_core.calls
    assert fake_core.deleted_secrets == []


def test_terminate_deletes_clone_secret_even_when_pod_delete_raises(
    fake_core: _FakeCore,
) -> None:
    fake_core.delete_pod_errors = [_FakeApiException(500, "boom")]
    with pytest.raises(click.ClickException):
        _launcher().terminate("omnigent-pod-1")
    assert "omnigent-pod-1-clone-cred" in fake_core.deleted_secrets
```

Step 1 note: `create_secret_error` is currently a single value; generalize it to a
`create_secret_errors: list[Exception | None]` popped per call (keep a backward-compatible
property or update the one existing test that sets `create_secret_error`).

- [ ] **Step 3: Run tests to verify they fail**

Run: `env -u NODE_ENV uv run pytest tests/onboarding/sandboxes/test_kubernetes.py -q`
Expected: new tests FAIL on `SandboxCapabilityError`; everything else passes.

- [ ] **Step 4: Implement**

Module-level helper (near `_api_reason`):

```python
def _redact_values(text: str, values: Iterable[str]) -> str:
    """
    Scrub credential *values* from *text* before it reaches logs / errors.

    API response bodies and container log tails can reflect a submitted value
    (an admission webhook echoing the object; a hostile remote seeding the git
    output), so every clone-path error/warning composition passes through here.
    """
    for value in values:
        if value:
            text = text.replace(value, "***")
    return text
```

In `start_host`:
1. Delete the `if clone_env: raise SandboxCapabilityError(...)` block and the two doc lines
   describing it; document the delivery instead.
2. Validate keys up front (before `_ensure_sdk()`):
   ```python
   if clone_env:
       for key in clone_env:
           if not key.isidentifier():
               raise click.ClickException(
                   f"clone_env key {key!r} is not a valid environment variable name"
               )
           if key == HOST_TOKEN_ENV_VAR:
               raise click.ClickException(
                   f"clone_env key {key!r} collides with the launch token variable"
               )
   ```
3. `clone_secret = _clone_secret_name(sandbox_id) if clone_env else None`
4. In the create `try`: after the token-Secret create, when `clone_env`:
   ```python
   core.create_namespaced_secret(
       namespace,
       build_clone_secret_manifest(
           secret_name=clone_secret, namespace=namespace, clone_env=clone_env
       ),
       _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
   )
   ```
   Pass `clone_secret_name=clone_secret, clone_env_keys=tuple(clone_env)` (or `None`s) to
   `build_pod_manifest`. After `create_namespaced_pod` (capture its return value), when
   `clone_env`:
   ```python
   pod_uid = getattr(getattr(created, "metadata", None), "uid", None)
   if pod_uid:
       try:
           core.patch_namespaced_secret(
               clone_secret, namespace,
               {"metadata": {"ownerReferences": [{
                   "apiVersion": "v1", "kind": "Pod",
                   "name": sandbox_id, "uid": pod_uid,
               }]}},
               _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
           )
       except (ApiException, HTTPError) as exc:
           click.echo(
               f"  → warning: could not set owner reference on '{clone_secret}' "
               f"({_api_reason(exc)}); the delete-after-init lifecycle still applies",
               err=True,
           )
   ```
5. Both existing failure paths pass the clone name:
   `self._best_effort_delete(namespace, sandbox_id, secret_name, clone_secret_name=clone_secret)`;
   in the create-failure branch, wrap both composed messages with
   `_redact_values(..., clone_env.values())` when `clone_env`; in the wait-failure branch:
   ```python
   except BaseException as exc:
       self._best_effort_delete(
           namespace, sandbox_id, secret_name, clone_secret_name=clone_secret
       )
       if clone_env and isinstance(exc, click.ClickException):
           raise click.ClickException(
               _redact_values(exc.message, clone_env.values())
           ) from exc
       raise
   ```
6. After the wait succeeds, when `clone_env`: best-effort delete the clone Secret
   (404 == success; any other failure warns `could not delete clone credential secret` to
   stderr and continues) — a small private helper
   `_delete_secret_best_effort(self, core, namespace, name)` shared with `_best_effort_delete`.

`_best_effort_delete(...)`: add `clone_secret_name: str | None = None` keyword; when set,
append a third `("secret", <delete clone secret>)` entry (its per-item try/except already makes
entries independent).

`terminate(...)`: FIRST line inside the `try` (before the raising loop):
```python
        self._delete_secret_best_effort(
            self._load_core(), namespace, _clone_secret_name(sandbox_id)
        )
```
so the credential-bearing Secret is always attempted regardless of Pod/token-Secret delete
failures. The existing loop is unchanged.

- [ ] **Step 5: Run the whole file, then the affected suites**

Run: `env -u NODE_ENV uv run pytest tests/onboarding/sandboxes/test_kubernetes.py -q`
Expected: ALL pass.
Run: `env -u NODE_ENV uv run pytest tests/onboarding -q`
Expected: ALL pass (base/exec providers untouched).

- [ ] **Step 6: Commit**

```bash
git add omnigent/onboarding/sandboxes/kubernetes.py tests/onboarding/sandboxes/test_kubernetes.py
git commit -m "feat(git-hosts): k8s init-clone credential delivery via delete-after-init per-Pod Secret"
```

---

### Task 5: Slice close-out — full suites, docs touch-ups, review package

**Files:**
- Modify: `omnigent/onboarding/sandboxes/kubernetes.py` (docstrings only, if any stale text
  remains about clone_env being unsupported — module docstring ~line 23 and `start_host` doc)
- No new tests.

- [ ] **Step 1: Stale-text sweep**

Run: `grep -rn "does not support per-host clone" omnigent/ && grep -n "clone_env" omnigent/onboarding/sandboxes/kubernetes.py`
Expected: no remaining "not yet supported" language; docstrings describe delivery (init-scoped
Secret, delete-after-init, ownerRef best-effort, redaction).

- [ ] **Step 2: Full test + lint pass**

```bash
env -u NODE_ENV uv run pytest tests/onboarding tests/inner/test_os_env.py tests/server/test_managed_hosts.py -q
pre-commit run --all-files
```
Expected: green. Fix anything reported; re-run.

- [ ] **Step 3: Commit any touch-ups**

```bash
git add -A && git commit -m "docs(git-hosts): k8s launcher docstrings reflect clone credential delivery"
```
(Skip if Step 1 found nothing.)

- [ ] **Step 4: Whole-slice review package**

Controller runs `scripts/review-package <BASE> HEAD` (BASE = commit before Task 1) and dispatches
the final review per the program's tri-engine convention (agy + Codex + controller trace).

- [ ] **Step 5: Live validation (two tiers — controller/user decision at execution)**

- Tier 1 (stock `ghcr.io/omnigent-ai/omnigent-host` image, homelab k3s, single-user recipe):
  a private Forgejo repo + per-user credential slot on the k8s provider — verify launch creates
  `-clone-cred`, the init clone succeeds with the per-user token, and the Secret is gone
  (`kubectl get secrets -w`) while the session runs; an ambient-only session as regression.
  The init-clone path is all server-side, so the stock image suffices.
- Tier 2 (in-runner §8.5 fetch/push on k8s) requires a host image built from this branch —
  defer to post-land CI or build on request; do not block the slice on it.

## Plan Self-Review (completed)

- Spec coverage: §8.4a items 1–7 → Tasks 3 (items 1–2), 4 (items 3–5), 1 (item 6), 2 (item 7);
  Testing paragraph → Tasks 1–4 test lists; Out-of-scope respected (no SSH work, no sweeper —
  deviation ratified).
- No placeholders beyond the two `<copy the neighboring test's construction>` directives, which
  are deliberate follow-existing-pattern instructions with the assertions fully specified.
- Type consistency: `_clone_secret_name`/`build_clone_secret_manifest`/`clone_secret_name`/
  `clone_env_keys` names match across Tasks 3–4.
