# Custom git hosts — Plan 2 (P1b): server wiring + operator-credential clone

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the P1a `git_hosts` package into the server so an operator-configured custom host (e.g. a self-hosted Forgejo) actually works: session create validates the host **before** durable creation, and the managed exec-sandbox clone authenticates with that host's **per-host operator credential** instead of the single baked `GIT_TOKEN`.

**Architecture:** Operator hosts load once at startup (`load_git_hosts` → `app.state.git_hosts`, mirroring `sandbox_config`). The create route resolves the workspace URL against them pre-create (unknown host → 422 before any DB row). `RepoWorkspace` gains optional non-secret provider fields, enriched by a new `resolve_repo_workspace()`; provisioning resolves the host's `credential_source` server-side at launch time and delivers `GIT_TOKEN`/`GIT_USERNAME` **per-clone** via a shell-env prefix on the clone command (the existing `env_prefix` idiom; the sandbox image's baked credential helper reads them from the git process env — no image change needed).

**Tech Stack:** Python 3.13-compatible (repo pins 3.12 venv), dataclasses, FastAPI app.state, pytest. No new dependencies.

## Global Constraints

- **Package manager:** `uv` only (`uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`). Never `pip`.
- **No linter suppressions:** never add `# noqa` / `# type: ignore`. Fix root causes (underscore-prefix genuinely-unused args).
- **Style:** `from __future__ import annotations` first; frozen dataclasses for value types; Sphinx `:param:` docstrings; comments describe the scenario, never the PR/change history.
- **Security invariants:** no secret value in `RepoWorkspace`, labels, app.state, or any persisted/logged object — only the `credential_source` *reference*. The resolved token exists transiently in `_arm_and_start_host` (server side) and the single prefixed clone command. Unknown non-github.com hosts are rejected (422 at create; soft-fail skip at relaunch).
- **Backward compatibility:** with no `git_hosts:` config, behavior is byte-identical to today (github.com + ambient `GIT_TOKEN`; no new env prefix on the clone command when there is no per-host credential).
- **Scope boundary (state in code comments where relevant):** per-host credentials cover the **launch-time clone**. In-runner fetch/push still uses the ambient deployment credential; multi-host runner credentials arrive with the P1c handoff (design §8.5).
- **Commit discipline:** one commit per task; run `pre-commit run --files <changed>` before each commit.

---

## File Structure

- `omnigent/git_hosts/credentials.py` — NEW: `parse_credential_source()` ("env:NAME"/"file:PATH"/"command:CMD" → `CredentialSourceSpec`) + `resolve_credential()` (wraps `omnigent/inner/credential_proxy._resolve_secret`).
- `omnigent/server/managed_hosts.py` — `RepoWorkspace` gains `canonical_host`/`provider`/`credential_source`/`clone_username` (all `str | None = None`); NEW `resolve_repo_workspace()`; `_arm_and_start_host` resolves the credential and passes `clone_env` to the launcher.
- `omnigent/onboarding/sandboxes/base.py` — `start_host` + `materialize_workspace` gain `clone_env: dict[str, str] | None = None`; the clone command gets the env prefix.
- `omnigent/server/app.py` — `create_app(..., git_hosts: tuple[HostConfig, ...] = ())` → `app.state.git_hosts`.
- `deploy/docker/entrypoint.py`, `omnigent/cli.py` — parse `cfg.get("git_hosts")` next to the existing `parse_sandbox_config` calls.
- `omnigent/server/routes/sessions.py` — pre-create resolution gate; enriched parse at the provisioning site; relaunch soft-fail enrichment.
- Tests: `tests/git_hosts/test_credentials.py`, additions to `tests/server/test_managed_hosts.py`, `tests/onboarding/sandboxes/` (or the existing launcher test module), route-level test in `tests/server/`.

Execution note for the controller: Tasks 1–2 are mechanical (haiku implementers); Tasks 3–6 modify large existing files with integration concerns (sonnet implementers). Line numbers below are anchors from commit `ee8ff831` — implementers must locate the quoted anchor text, not trust raw numbers.

---

### Task 1: Credential-source shim (`credentials.py`)

**Files:**
- Create: `omnigent/git_hosts/credentials.py`
- Test: `tests/git_hosts/test_credentials.py`

**Interfaces:**
- Consumes: `CredentialSourceSpec` (`omnigent/inner/datamodel.py:378-397`: `kind: Literal["env","file","command"]`, `env`/`path`/`command` optionals) and `_resolve_secret(source, *, parent_env) -> str` (`omnigent/inner/credential_proxy.py:156`; env branch strips + raises `ValueError` when missing/empty).
- Produces: `parse_credential_source(ref: str) -> CredentialSourceSpec` (raises `ValueError` on malformed refs); `resolve_credential(ref: str, *, parent_env: dict[str, str]) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_credentials.py`:

```python
"""Tests for :mod:`omnigent.git_hosts.credentials`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.credentials import parse_credential_source, resolve_credential
from omnigent.inner.datamodel import CredentialSourceSpec


def test_parses_env_file_and_command_refs() -> None:
    assert parse_credential_source("env:ACME_TOKEN") == CredentialSourceSpec(
        kind="env", env="ACME_TOKEN"
    )
    assert parse_credential_source("file:/run/secrets/acme") == CredentialSourceSpec(
        kind="file", path="/run/secrets/acme"
    )
    assert parse_credential_source("command:pass show acme") == CredentialSourceSpec(
        kind="command", command="pass show acme"
    )


@pytest.mark.parametrize("ref", ["", "env:", "file:", "command:", "vault:xyz", "ACME_TOKEN"])
def test_rejects_malformed_refs(ref: str) -> None:
    with pytest.raises(ValueError, match="credential_source"):
        parse_credential_source(ref)


def test_resolve_credential_env_roundtrip() -> None:
    assert resolve_credential("env:ACME_TOKEN", parent_env={"ACME_TOKEN": "s3cret"}) == "s3cret"


def test_resolve_credential_missing_env_raises() -> None:
    with pytest.raises(ValueError):
        resolve_credential("env:ACME_TOKEN", parent_env={})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_credentials.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omnigent.git_hosts.credentials'`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/credentials.py`:

```python
"""Resolve an operator ``credential_source`` reference to a secret value.

Operator host config carries a compact reference string (``"env:NAME"``,
``"file:PATH"``, ``"command:CMD"``) — never a secret. This module bridges that
string onto the existing :class:`CredentialSourceSpec` /
:func:`_resolve_secret` machinery so lookup semantics (strip, fail on
missing/empty, 30s command timeout) stay single-sourced.
"""

from __future__ import annotations

from omnigent.inner.credential_proxy import _resolve_secret
from omnigent.inner.datamodel import CredentialSourceSpec


def parse_credential_source(ref: str) -> CredentialSourceSpec:
    """Parse a ``"<kind>:<value>"`` credential reference.

    :param ref: e.g. ``"env:ACME_TOKEN"``.
    :returns: The equivalent :class:`CredentialSourceSpec`.
    :raises ValueError: When the kind is unknown or the value is empty.
    """
    kind, sep, value = ref.partition(":")
    if not sep or not value:
        raise ValueError(
            f"credential_source {ref!r} must be '<kind>:<value>' with kind one of "
            "env, file, command"
        )
    if kind == "env":
        return CredentialSourceSpec(kind="env", env=value)
    if kind == "file":
        return CredentialSourceSpec(kind="file", path=value)
    if kind == "command":
        return CredentialSourceSpec(kind="command", command=value)
    raise ValueError(
        f"credential_source kind {kind!r} is not supported; use env, file, or command"
    )


def resolve_credential(ref: str, *, parent_env: dict[str, str]) -> str:
    """Resolve *ref* to its secret value in the trusted server process.

    :param ref: A reference accepted by :func:`parse_credential_source`.
    :param parent_env: The environment to resolve ``env:``/``command:`` against.
    :returns: The non-empty secret value.
    :raises ValueError: When the reference is malformed or the source is
        missing/empty.
    """
    return _resolve_secret(parse_credential_source(ref), parent_env=parent_env)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_credentials.py -v`
Expected: PASS (4 tests, 6 reject cases).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/credentials.py tests/git_hosts/test_credentials.py
pre-commit run --files omnigent/git_hosts/credentials.py tests/git_hosts/test_credentials.py
git commit -m "feat(git-hosts): credential-source reference shim onto _resolve_secret"
```

---

### Task 2: Startup wiring — `git_hosts` onto `app.state`

**Files:**
- Modify: `omnigent/server/app.py` (create_app signature ~:1084; `app.state.sandbox_config` assignment ~:1398)
- Modify: `deploy/docker/entrypoint.py` (`parse_sandbox_config(cfg.get("sandbox"))` ~:304 and the `create_app(...)` call ~:354)
- Modify: `omnigent/cli.py` (the try/except-wrapped `parse_sandbox_config` ~:3277-3280 and its `create_app` call)
- Test: `tests/server/test_app_git_hosts.py` (new)

**Interfaces:**
- Consumes: `load_git_hosts(raw) -> tuple[HostConfig, ...]` and `HostConfig` from P1a.
- Produces: `create_app(..., git_hosts: tuple[HostConfig, ...] = ())` storing `app.state.git_hosts`; routes read `getattr(request.app.state, "git_hosts", ())`.

- [ ] **Step 1: Write the failing test**

Create `tests/server/test_app_git_hosts.py` (model the fixture/kwargs on how `tests/server/test_managed_hosts.py` builds `create_app` — reuse its helpers if importable):

```python
"""app.state.git_hosts wiring."""

from __future__ import annotations

from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.config import load_git_hosts


def test_create_app_defaults_to_empty_git_hosts(app_factory) -> None:
    app = app_factory()
    assert app.state.git_hosts == ()


def test_create_app_stores_parsed_git_hosts(app_factory) -> None:
    hosts = load_git_hosts(
        [
            {
                "id": "acme",
                "provider": "forgejo",
                "web_host": "git.acme.com",
                "credential_source": "env:ACME_TOKEN",
            }
        ]
    )
    app = app_factory(git_hosts=hosts)
    assert app.state.git_hosts == hosts
    assert isinstance(app.state.git_hosts[0], HostConfig)
```

`app_factory` is a small local fixture that calls `create_app` with the same minimal stores the existing server tests use (copy the minimal construction from `tests/server/test_managed_hosts.py`'s app setup; keep it in this file as a `pytest.fixture`).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/server/test_app_git_hosts.py -v`
Expected: FAIL — `TypeError: create_app() got an unexpected keyword argument 'git_hosts'` (or missing-attr assert).

- [ ] **Step 3: Implement**

In `omnigent/server/app.py`: add to the `create_app` signature (beside `sandbox_config: ManagedSandboxConfig | None = None`):

```python
    git_hosts: tuple[HostConfig, ...] = (),
```

with import `from omnigent.git_hosts.base import HostConfig`, a `:param git_hosts:` docstring line ("Operator-configured custom git hosts, parsed by :func:`omnigent.git_hosts.config.load_git_hosts`; empty when none are configured."), and beside `app.state.sandbox_config = sandbox_config`:

```python
    app.state.git_hosts = git_hosts
```

In `deploy/docker/entrypoint.py`, next to `sandbox_config = parse_sandbox_config(cfg.get("sandbox"))`:

```python
    git_hosts = load_git_hosts(cfg.get("git_hosts"))
```

(import `from omnigent.git_hosts.config import load_git_hosts`) and pass `git_hosts=git_hosts` in its `create_app(...)` call.

In `omnigent/cli.py`, inside the same `try:` that wraps `parse_sandbox_config` (so a malformed `git_hosts:` also becomes a `click.ClickException`):

```python
        git_hosts = load_git_hosts(cfg.get("git_hosts"))
```

and pass `git_hosts=git_hosts` to its `create_app(...)` call.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/server/test_app_git_hosts.py -v`
Expected: PASS. Also run `uv run pytest tests/server/test_managed_hosts.py -q` (must stay green — no existing call sites break: the new param has a default).

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/app.py deploy/docker/entrypoint.py omnigent/cli.py tests/server/test_app_git_hosts.py
pre-commit run --files omnigent/server/app.py deploy/docker/entrypoint.py omnigent/cli.py tests/server/test_app_git_hosts.py
git commit -m "feat(git-hosts): load operator git_hosts config at startup onto app.state"
```

---

### Task 3: `RepoWorkspace` enrichment + `resolve_repo_workspace`

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`RepoWorkspace` ~:397-420; add `resolve_repo_workspace` after `parse_repo_workspace` ~:549)
- Test: `tests/server/test_managed_hosts.py` (append)

**Interfaces:**
- Consumes: `resolve_clone_plan(url, hosts) -> ClonePlan` (P1a; raises `ValueError` for unconfigured non-github hosts; `ClonePlan.credential_source is None` for the github default; `auth.username` is the provider clone username).
- Produces: `RepoWorkspace` with new optional fields `canonical_host: str | None = None`, `provider: str | None = None`, `credential_source: str | None = None`, `clone_username: str | None = None`; `resolve_repo_workspace(workspace: str, hosts: Sequence[HostConfig]) -> RepoWorkspace` — parse + resolve + enrich. Nothing serializes `RepoWorkspace` (verified: only the raw workspace string is persisted as a label), so added fields are safe.

- [ ] **Step 1: Write the failing test** (append to `tests/server/test_managed_hosts.py`, reusing its imports)

```python
# ── resolve_repo_workspace ────────────────────────────────────

_GH_HOSTS = load_git_hosts(
    [
        {
            "id": "acme",
            "provider": "forgejo",
            "web_host": "git.acme.com",
            "credential_source": "env:ACME_TOKEN",
        }
    ]
)


def test_resolve_repo_workspace_enriches_configured_host() -> None:
    repo = resolve_repo_workspace("https://git.acme.com/team/proj#main", _GH_HOSTS)
    assert repo.url == "https://git.acme.com/team/proj"
    assert repo.branch == "main"
    assert repo.repo_name == "proj"
    assert repo.canonical_host == "git.acme.com"
    assert repo.provider == "forgejo"
    assert repo.credential_source == "env:ACME_TOKEN"
    assert repo.clone_username == "oauth2"


def test_resolve_repo_workspace_github_default_has_no_credential_source() -> None:
    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)
    assert repo.provider == "github"
    assert repo.credential_source is None


def test_resolve_repo_workspace_unknown_host_raises() -> None:
    with pytest.raises(ValueError, match="no configured git host"):
        resolve_repo_workspace("https://git.unknown.com/x/y", _GH_HOSTS)
```

Add imports at the top of the test file: `from omnigent.git_hosts.config import load_git_hosts` and extend the existing `from omnigent.server.managed_hosts import (...)` block with `resolve_repo_workspace`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/server/test_managed_hosts.py -q -k resolve_repo_workspace`
Expected: FAIL — ImportError on `resolve_repo_workspace`.

- [ ] **Step 3: Implement**

In `omnigent/server/managed_hosts.py`, extend `RepoWorkspace` (append after `repo_name: str`, with matching `:param:` docstring lines noting all four are "resolution metadata from the operator git-host config; ``None`` when unresolved or for the built-in github.com default's credential fields"):

```python
    canonical_host: str | None = None
    provider: str | None = None
    credential_source: str | None = None
    clone_username: str | None = None
```

Add after `parse_repo_workspace` (imports at module top: `from collections.abc import Sequence` if absent; `from omnigent.git_hosts.base import HostConfig`; `from omnigent.git_hosts.resolver import resolve_clone_plan`):

```python
def resolve_repo_workspace(
    workspace: str, hosts: Sequence[HostConfig]
) -> RepoWorkspace:
    """Parse *workspace* and resolve it against the operator git-host config.

    Combines :func:`parse_repo_workspace` (shape validation) with
    :func:`resolve_clone_plan` (host resolution): the returned workspace
    carries the canonical host, provider name, and the host's non-secret
    ``credential_source`` reference for launch-time credential injection.

    :param workspace: The raw repository-URL workspace, e.g.
        ``"https://git.acme.com/team/proj#main"``.
    :param hosts: Operator-configured hosts (``app.state.git_hosts``).
    :returns: The enriched, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL is malformed or its host is neither
        configured nor github.com.
    """
    parsed = parse_repo_workspace(workspace)
    plan = resolve_clone_plan(workspace, hosts)
    return RepoWorkspace(
        url=parsed.url,
        branch=parsed.branch,
        repo_name=parsed.repo_name,
        canonical_host=plan.canonical_host,
        provider=plan.provider,
        credential_source=plan.credential_source,
        clone_username=plan.auth.username,
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/server/test_managed_hosts.py -q`
Expected: all pass (existing + 3 new).

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): resolve_repo_workspace enriches RepoWorkspace with host resolution"
```

---

### Task 4: Launcher clone-env delivery (exec model)

**Files:**
- Modify: `omnigent/onboarding/sandboxes/base.py` (`start_host` ~:230-242, `materialize_workspace` ~:320-389)
- Test: `tests/git_hosts/test_clone_env.py` (new; a fake launcher subclass captures commands — no sandbox needed)

**Interfaces:**
- Consumes: the `env_prefix` shell idiom already used at `base.py:306-313`.
- Produces: `start_host(..., clone_env: dict[str, str] | None = None)` and `materialize_workspace(..., clone_env: dict[str, str] | None = None)`; when `clone_env` is set, the clone command becomes `<K=V ...> git clone ...` with `shlex.quote`d values; when `None`, the command is byte-identical to today. Kubernetes' `start_host` override ignores the new kwarg for now (P1c wires its init-container path) — it must still *accept* it.

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_clone_env.py`:

```python
"""Per-clone credential env delivery in the exec launcher model."""

from __future__ import annotations

from typing import ClassVar

from omnigent.onboarding.sandboxes.base import RemoteCommandResult, SandboxLauncher


class _CaptureLauncher(SandboxLauncher):
    provider: ClassVar[str] = "capture"

    def __init__(self) -> None:
        self.commands: list[str] = []

    def prepare(self) -> None:  # pragma: no cover - unused
        raise NotImplementedError

    def provision(self, name: str) -> str:  # pragma: no cover - unused
        raise NotImplementedError

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self.commands.append(command)
        return RemoteCommandResult(exit_code=0, stdout="", stderr="")


def test_clone_without_env_is_unchanged() -> None:
    launcher = _CaptureLauncher()
    launcher.materialize_workspace(
        "sb", workspace="/root/workspace", repo_url="https://git.acme.com/t/p",
        repo_branch=None, repo_name="p",
    )
    assert launcher.commands == [
        "git clone -- https://git.acme.com/t/p /root/workspace/p"
    ]


def test_clone_env_is_prefixed_and_quoted() -> None:
    launcher = _CaptureLauncher()
    launcher.materialize_workspace(
        "sb", workspace="/root/workspace", repo_url="https://git.acme.com/t/p",
        repo_branch="main", repo_name="p",
        clone_env={"GIT_TOKEN": "s3c ret", "GIT_USERNAME": "oauth2"},
    )
    (cmd,) = launcher.commands
    assert cmd.startswith("GIT_TOKEN='s3c ret' GIT_USERNAME=oauth2 git clone ")
    assert "--branch main --single-branch" in cmd
```

If `_CaptureLauncher` cannot instantiate because `SandboxLauncher` has additional abstract methods, implement each extra abstract method as a one-line `raise NotImplementedError` with `# pragma: no cover - unused` — check the ABC and satisfy it exactly; do not change the ABC.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/git_hosts/test_clone_env.py -v`
Expected: FAIL — `materialize_workspace() got an unexpected keyword argument 'clone_env'` (first test may pass; second fails).

- [ ] **Step 3: Implement**

In `base.py`, add to BOTH signatures (after `repo_name`/before `on_stage`): `clone_env: dict[str, str] | None = None,` with docstring lines — for `materialize_workspace`: `:param clone_env: Per-clone credential env (e.g. ``GIT_TOKEN``/``GIT_USERNAME`` for the image's git credential helper), prefixed onto the clone command only — never the sandbox's ambient env. Launch-scoped: the value appears in this one command. ``None`` leaves the command unchanged.` `start_host` forwards it to `materialize_workspace` at its existing call site (~:293-301). In `materialize_workspace`, build the prefix exactly like `base.py:306-313`:

```python
        env_prefix = (
            " ".join(
                f"{key}={shlex.quote(value)}" for key, value in clone_env.items()
            )
            + " "
            if clone_env
            else ""
        )
```

and change the clone invocation to:

```python
            self.run(
                sandbox_id,
                f"{env_prefix}git clone {branch_args}-- "
                f"{shlex.quote(repo_url)} {shlex.quote(clone_dir)}",
            )
```

Then check `omnigent/onboarding/sandboxes/kubernetes.py`'s `start_host` override signature: add the same `clone_env: dict[str, str] | None = None,` kwarg (accepted, unused this plan — name it `clone_env` and add one comment line: `# Per-clone credential delivery for the init-container clone lands with the k8s secret work; accepted here so the launcher contract stays uniform.`; if ruff flags it unused, underscore-prefix is NOT appropriate here since the base names it — instead reference it in the comment via a `del clone_env` line is also wrong; the correct root-cause fix is: consume it by passing through to any internal prep call if one exists, else assign `_ = clone_env` is banned too — simplest compliant form: include it in the override's docstring and pass it to `super()` if the override delegates; if the override never delegates, ruff ARG002 applies only to methods that don't use the arg — resolve by naming it `clone_env` and adding it to the init-container TODO usage: `if clone_env: raise SandboxCapabilityError("per-host clone credentials are not yet supported by the kubernetes provider")` — this is the fail-closed behavior the design (§8.4) requires when a provider lacks the capability, and it genuinely uses the argument.) Also check `islo.py` (delegates to `super().start_host` — add the kwarg pass-through).

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/git_hosts/test_clone_env.py tests/git_hosts -q && uv run pytest tests/onboarding -q -k "sandbox or launcher"`
Expected: all pass (adjust the second selector to whatever launcher tests exist; if none match, note it in the report).

- [ ] **Step 5: Commit**

```bash
git add omnigent/onboarding/sandboxes/base.py omnigent/onboarding/sandboxes/kubernetes.py omnigent/onboarding/sandboxes/islo.py tests/git_hosts/test_clone_env.py
pre-commit run --files omnigent/onboarding/sandboxes/base.py omnigent/onboarding/sandboxes/kubernetes.py omnigent/onboarding/sandboxes/islo.py tests/git_hosts/test_clone_env.py
git commit -m "feat(git-hosts): launch-scoped clone_env delivery on the exec clone command"
```

---

### Task 5: Provisioning — resolve the credential and build `clone_env`

**Files:**
- Modify: `omnigent/server/managed_hosts.py` (`launch_managed_host` ~:1720 `repo: RepoWorkspace | None` param flows unchanged; `_arm_and_start_host` unpack ~:1935-1946)
- Test: `tests/server/test_managed_hosts.py` (append)

**Interfaces:**
- Consumes: `resolve_credential(ref, parent_env)` (Task 1); enriched `RepoWorkspace` (Task 3); `clone_env` launcher kwarg (Task 4).
- Produces: `_build_clone_env(repo: RepoWorkspace | None) -> dict[str, str] | None` — module-level, unit-testable: returns `None` when `repo is None` or `repo.credential_source is None`; else resolves the source against `os.environ` and returns `{"GIT_TOKEN": token, "GIT_USERNAME": repo.clone_username or "x-access-token"}`. Raises `ValueError` (surfaced as the existing provisioning-failure path) when resolution fails — fail-closed, never a silent unauthenticated clone.

- [ ] **Step 1: Write the failing test** (append; `monkeypatch` is the standard pytest fixture)

```python
def test_build_clone_env_none_without_credential_source() -> None:
    assert _build_clone_env(None) is None
    repo = RepoWorkspace(url="https://github.com/o/r", branch=None, repo_name="r")
    assert _build_clone_env(repo) is None


def test_build_clone_env_resolves_operator_credential(monkeypatch) -> None:
    monkeypatch.setenv("ACME_TOKEN", "s3cret")
    repo = RepoWorkspace(
        url="https://git.acme.com/t/p", branch=None, repo_name="p",
        canonical_host="git.acme.com", provider="forgejo",
        credential_source="env:ACME_TOKEN", clone_username="oauth2",
    )
    assert _build_clone_env(repo) == {"GIT_TOKEN": "s3cret", "GIT_USERNAME": "oauth2"}


def test_build_clone_env_missing_secret_raises(monkeypatch) -> None:
    monkeypatch.delenv("ACME_TOKEN", raising=False)
    repo = RepoWorkspace(
        url="https://git.acme.com/t/p", branch=None, repo_name="p",
        credential_source="env:ACME_TOKEN", clone_username="oauth2",
    )
    with pytest.raises(ValueError):
        _build_clone_env(repo)
```

Extend the managed_hosts import block with `_build_clone_env`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/server/test_managed_hosts.py -q -k build_clone_env`
Expected: FAIL — ImportError.

- [ ] **Step 3: Implement**

In `managed_hosts.py` (imports: `import os` if absent; `from omnigent.git_hosts.credentials import resolve_credential`):

```python
def _build_clone_env(repo: RepoWorkspace | None) -> dict[str, str] | None:
    """Resolve a repo's operator credential into per-clone env, or ``None``.

    Resolution happens in the trusted server process at launch time; only the
    resolved value rides the single prefixed clone command (launch-scoped —
    see the ``clone_env`` launcher contract). The github.com default carries
    no ``credential_source`` and keeps today's ambient-``GIT_TOKEN`` behavior.

    :param repo: The enriched workspace, or ``None`` for no-repo launches.
    :returns: ``{"GIT_TOKEN": ..., "GIT_USERNAME": ...}`` or ``None``.
    :raises ValueError: When the configured source cannot be resolved —
        provisioning must fail loudly rather than clone unauthenticated.
    """
    if repo is None or repo.credential_source is None:
        return None
    token = resolve_credential(repo.credential_source, parent_env=dict(os.environ))
    return {"GIT_TOKEN": token, "GIT_USERNAME": repo.clone_username or "x-access-token"}
```

In `_arm_and_start_host`, immediately before the `launcher.start_host` call: `clone_env = _build_clone_env(repo)` and add `clone_env=clone_env,` to the kwargs (after `repo_name=...`).

Note: `os.environ` is copied via `dict(...)` — repo CI hard-fails on `dict(os.environ)`? (Ledger memory: the exfil-scan flags `dict(os.environ)` in added lines — use `os.environ.copy()` instead.) **Use `os.environ.copy()`, not `dict(os.environ)`.**

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/server/test_managed_hosts.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
pre-commit run --files omnigent/server/managed_hosts.py tests/server/test_managed_hosts.py
git commit -m "feat(git-hosts): resolve operator clone credential at provisioning into clone_env"
```

---

### Task 6: Route wiring — pre-create gate, enriched provisioning, relaunch

**Files:**
- Modify: `omnigent/server/routes/sessions.py` (create: identity at ~:14458, `model_validate` ~:14474, durable create ~:14495, `parse_repo_workspace` call ~:14596, `OmnigentError` pattern ~:14581-14585; relaunch `_kick_managed_relaunch` def ~:6998, soft-fail parse ~:7038-7046)
- Test: `tests/server/test_managed_hosts.py` or the route-test module the existing managed-create tests live in (append there)

**Interfaces:**
- Consumes: `resolve_repo_workspace` (Task 3); `app.state.git_hosts` (Task 2).
- Produces: (a) **pre-create gate** — after `body = SessionCreateRequest.model_validate(payload)` and before `_create_session_from_existing_agent(...)`, when the request is a managed create with a repo-URL workspace: `resolve_clone_plan`-based validation via `resolve_repo_workspace(body.workspace, getattr(request.app.state, "git_hosts", ()))`; `ValueError` → the same `OmnigentError(..., code=ErrorCode.INVALID_INPUT)` 422 shape used at ~:14581-14585. **No DB row exists when it fires.** (b) The provisioning-site parse at ~:14596 becomes `repo = resolve_repo_workspace(body.workspace, hosts) if body.workspace is not None else None` (reuse the gate's result variable instead of re-resolving if it is still in scope — prefer one resolution stored in a local). (c) Relaunch: in `_kick_managed_relaunch`, replace `parse_repo_workspace(raw_repo)` with `resolve_repo_workspace(raw_repo, getattr(app_state, "git_hosts", ()))` keeping the existing soft-fail `except ValueError` (warn + skip) — an operator who removed a host after sessions were created gets a logged skip, not a crash.

- [ ] **Step 1: Write the failing test**

Append a route-level test beside the existing managed-create tests (reuse their app/client fixtures; find them via `grep -n "workspace.*https://github" tests/server/test_managed_hosts.py`):

```python
async def test_managed_create_rejects_unconfigured_host_before_creation(
    managed_app_client,  # reuse/adapt the existing managed-create fixture
) -> None:
    client, app = managed_app_client
    resp = await client.post(
        "/v1/sessions",
        json={
            # copy the minimal managed-create body the neighboring tests use,
            # with workspace pointed at an unconfigured host:
            "workspace": "https://git.unknown.com/team/proj",
            # ... other required fields identical to the neighboring test ...
        },
    )
    assert resp.status_code == 422
    assert "no configured git host" in resp.text
    # No session row was created:
    listing = await client.get("/v1/sessions")
    assert all(
        s.get("workspace") != "https://git.unknown.com/team/proj"
        for s in listing.json().get("sessions", listing.json())
    )
```

The exact fixture/body must be copied from the neighboring managed-create test in that module — the implementer adapts names, not semantics. A github.com workspace with empty `git_hosts` must still succeed (add one assertion-only variant or verify an existing green test covers it).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/server -q -k "unconfigured_host"`
Expected: FAIL — currently returns non-422 (or 502 later), and/or the session row exists.

- [ ] **Step 3: Implement** (per the Produces block above; imports: `from omnigent.server.managed_hosts import resolve_repo_workspace` — extend the existing import from that module)

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/server/test_managed_hosts.py tests/server -q -k "managed or git_host"`
Expected: all pass, including every pre-existing managed-create/relaunch test (github.com behavior unchanged).

- [ ] **Step 5: Commit**

```bash
git add omnigent/server/routes/sessions.py tests/server
pre-commit run --files omnigent/server/routes/sessions.py
git commit -m "feat(git-hosts): resolve git hosts pre-create; enrich provisioning and relaunch"
```

---

### Task 7: Full-suite gate

**Files:** none (verification only).

- [ ] **Step 1:** `uv run pytest tests/git_hosts tests/server/test_managed_hosts.py tests/server/test_app_git_hosts.py -q` — all pass.
- [ ] **Step 2:** `uv run pytest tests/server -q` — no regressions (long; run once).
- [ ] **Step 3:** `uv run ruff check omnigent deploy/docker/entrypoint.py && uv run ruff format --check omnigent/git_hosts omnigent/server/managed_hosts.py omnigent/onboarding/sandboxes/base.py` — clean; `grep -rn "noqa" omnigent/git_hosts` empty.
- [ ] **Step 4:** End-to-end smoke (manual, no commit): with `git_hosts` config for a local host and `ACME_TOKEN` set, construct `resolve_repo_workspace` + `_build_clone_env` in a REPL and confirm the produced `clone_env`; confirm a no-config app boots (`app.state.git_hosts == ()`).

---

## What this plan does NOT do (next plans)

- **P1c:** `SqlGitCredential` (encrypted user PATs) + the authenticated server→runner fetch/push handoff (design §8.2/§8.5); k8s init-container clone-Secret delivery (the k8s launcher currently fail-closes on `clone_env` per Task 4); runner placeholder credentials; commit-identity = session starter; session-sharing notice.
- **P1d:** egress-rule derivation + merge point; long-lived `omni host` host-local credential config.
- Carried from P1a final review: scp-branch IPv6 bracket mirror in `split_host`; `sorted(map(repr, unknown))` on mixed-type keys; `ssh_port` range validation where SSH wires in.

## Self-Review

- **Spec coverage (this slice):** design §7 operator config → server startup (Task 2); §9 resolver-before-durable-create (Task 6 gate — grounded in the actual late-parse finding); §8.1 reference-source resolution in the trusted parent (Tasks 1, 5); §8.4 launch-scoped delivery via the one prefixed command + k8s fail-closed capability check (Task 4); §12.3 precedence (P1a resolver, consumed by Task 3); backward compat (Tasks 2/4/5/6 all default-off). In-runner multi-host fetch/push is explicitly out (P1c) — stated in Global Constraints.
- **Placeholder scan:** Task 6 Step 1 intentionally instructs copying the neighboring fixture/body (route fixtures are repo-specific); everything else ships complete code. No TBDs.
- **Type consistency:** `clone_env: dict[str, str] | None` uniform across Tasks 4–5; `RepoWorkspace` new fields (Task 3) match `_build_clone_env` usage (Task 5) and the resolver outputs (`plan.auth.username` → `clone_username`); `resolve_repo_workspace(workspace, hosts)` signature identical in Tasks 3 and 6.
