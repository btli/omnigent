# Custom git hosts — Plan 1 (P1a): provider foundation + resolver

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the `GitHostProvider` abstraction, its registry, the operator-host config parser, and an owner-agnostic resolver that turns a repo URL + operator config into a validated, non-secret `ClonePlan` — the foundation every later clone/credential task consumes.

**Architecture:** A new `omnigent/git_hosts/` package mirrors the sandbox-launcher registry (`omnigent/onboarding/sandboxes/__init__.py`): an abstract `GitHostProvider` + concrete per-forge classes selected by name through a lazy `get_git_host()` factory. Operator hosts are parsed from server config into immutable `HostConfig` records; `resolve_clone_plan()` matches a repo URL's canonical host against them (falling back to the built-in github.com provider) and emits a `ClonePlan`. This plan is pure/in-memory — no DB, no sandbox, no secrets — so it is fully unit-testable in isolation. Later plans (P1b/P1c) consume `ClonePlan` for credential injection, the server→runner handoff, and the clone wiring.

**Tech Stack:** Python 3.13+, dataclasses, `abc.ABC`, `importlib` (lazy provider import), pytest. No new dependencies.

## Global Constraints

- **Package manager:** `uv` only — `uv run pytest`, `uv run ruff check --fix`, `uv run ruff format`. Never `pip`.
- **Type checker:** omnigent uses **pyrefly** (`pyrefly.toml`), not mypy. Run `uv run pyrefly check omnigent/git_hosts` if available; the `pre-commit` hook runs the authoritative gates.
- **No linter suppressions:** never add `# noqa`, `# type: ignore`, or `eslint-disable`. Fix the root cause.
- **Style:** every module starts with `from __future__ import annotations`; use `@dataclass(frozen=True)` for value types and `ClassVar` for provider identity, matching `omnigent/onboarding/sandboxes/base.py` and `omnigent/server/managed_hosts.py`.
- **Operator-only topology:** users never define hosts; an unconfigured, non-github.com host is a hard error (design §7). No secret value ever lives in these types — only a `credential_source` *reference* string (design §8.1).
- **Commit discipline:** one commit per task (TDD: failing test → implementation → passing test → commit). Run `pre-commit run --files <changed files>` before each commit.

---

## File Structure

- `omnigent/git_hosts/__init__.py` — the provider registry: `_GIT_HOSTS` name→"module:Class" map, `get_git_host()`, `available_providers()`.
- `omnigent/git_hosts/base.py` — `GitHostProvider(ABC)`, and the value types `HostConfig`, `CloneAuthBinding`, `ClonePlan`.
- `omnigent/git_hosts/github.py` — `GitHubProvider`, `GitHubEnterpriseProvider`.
- `omnigent/git_hosts/gitea.py` — `GiteaProvider` (serves both Forgejo and Gitea; same API).
- `omnigent/git_hosts/url.py` — `split_host()` canonical-host extraction (lowercase, userinfo rejected).
- `omnigent/git_hosts/config.py` — `load_git_hosts()` typed, fail-closed parser for the server-config `git_hosts:` value.
- `omnigent/git_hosts/resolver.py` — `resolve_clone_plan()`.
- `tests/git_hosts/` — one test module per source module.

---

### Task 1: Base types and the provider ABC

**Files:**
- Create: `omnigent/git_hosts/__init__.py` (empty package marker for now — the registry lands in Task 2)
- Create: `omnigent/git_hosts/base.py`
- Test: `tests/git_hosts/__init__.py` (empty), `tests/git_hosts/test_base.py`

**Interfaces:**
- Produces: `HostConfig(id, provider, web_host, api_base, credential_source, ssh_host=None, ssh_port=None, ca_bundle=None)` (frozen dataclass); `CloneAuthBinding(scheme: str, username: str)` (frozen); `ClonePlan(provider, host_id, canonical_host, normalized_url, api_base, auth: CloneAuthBinding, credential_source: str | None, ssh_host=None, ssh_port=None, ca_bundle=None)` (frozen); `GitHostProvider(ABC)` with `provider: ClassVar[str]`, `default_clone_username: ClassVar[str]`, abstract `matches(host: str) -> bool` and `default_api_base(web_host: str) -> str`, concrete `normalize_repo_url(url: str) -> str` (default: identity) and `clone_binding() -> CloneAuthBinding`.

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/__init__.py` (empty file) and `tests/git_hosts/test_base.py`:

```python
"""Tests for :mod:`omnigent.git_hosts.base`."""

from __future__ import annotations

from omnigent.git_hosts.base import (
    CloneAuthBinding,
    ClonePlan,
    GitHostProvider,
    HostConfig,
)


class _StubProvider(GitHostProvider):
    provider = "stub"
    default_clone_username = "x-access-token"

    def matches(self, host: str) -> bool:
        return host == "stub.example.com"

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v1"


def test_clone_binding_uses_default_username() -> None:
    binding = _StubProvider().clone_binding()
    assert binding == CloneAuthBinding(scheme="basic", username="x-access-token")


def test_normalize_repo_url_is_identity_by_default() -> None:
    url = "https://stub.example.com/org/repo"
    assert _StubProvider().normalize_repo_url(url) == url


def test_host_config_and_clone_plan_are_frozen_value_types() -> None:
    cfg = HostConfig(
        id="acme",
        provider="stub",
        web_host="stub.example.com",
        api_base="https://stub.example.com/api/v1",
        credential_source="env:ACME_TOKEN",
    )
    plan = ClonePlan(
        provider="stub",
        host_id="acme",
        canonical_host="stub.example.com",
        normalized_url="https://stub.example.com/org/repo",
        api_base=cfg.api_base,
        auth=CloneAuthBinding(scheme="basic", username="x-access-token"),
        credential_source=cfg.credential_source,
    )
    assert plan.credential_source == "env:ACME_TOKEN"
    assert plan.ssh_host is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_base.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'omnigent.git_hosts'`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/__init__.py` (empty for now; the registry is added in Task 2):

```python
"""Git-host provider abstraction (design docs/claude/custom-git-hosts-design.md)."""

from __future__ import annotations
```

Create `omnigent/git_hosts/base.py`:

```python
"""Git-host provider abstraction: identity, URL normalization, and the
resolved, non-secret clone plan a launcher consumes.

A ``GitHostProvider`` is the per-forge behavior seam (github.com, GitHub
Enterprise, Forgejo/Gitea, …), mirroring the sandbox-launcher registry in
:mod:`omnigent.onboarding.sandboxes`. This module holds the abstract base and
the plain value types; concrete providers live in sibling modules and are
registered in :mod:`omnigent.git_hosts`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class HostConfig:
    """An operator-configured git host — topology only, never a secret.

    Built by :func:`omnigent.git_hosts.config.load_git_hosts`. ``credential_source``
    is a *reference* (e.g. ``"env:NAME"``), resolved later in the trusted parent —
    never a secret value.

    :param id: Operator-assigned stable id, e.g. ``"acme-forgejo"``.
    :param provider: Registered provider name, e.g. ``"forgejo"``.
    :param web_host: Canonical lowercase host, e.g. ``"git.acme.com"``.
    :param api_base: API base URL, e.g. ``"https://git.acme.com/api/v1"``.
    :param credential_source: Reference-source descriptor, e.g. ``"env:ACME_TOKEN"``.
    :param ssh_host: Optional SSH host override.
    :param ssh_port: Optional SSH port override.
    :param ca_bundle: Optional path to a CA bundle for a private forge.
    """

    id: str
    provider: str
    web_host: str
    api_base: str
    credential_source: str
    ssh_host: str | None = None
    ssh_port: int | None = None
    ca_bundle: str | None = None


@dataclass(frozen=True)
class CloneAuthBinding:
    """How a provider authenticates an HTTPS git operation.

    :param scheme: The git-credential scheme, ``"basic"`` or ``"token"``.
    :param username: The HTTPS basic-auth username the forge expects
        (GitHub: ``"x-access-token"``; GitLab: ``"oauth2"``).
    """

    scheme: str
    username: str


@dataclass(frozen=True)
class ClonePlan:
    """The resolved, non-secret plan for cloning one repository.

    Produced by :func:`omnigent.git_hosts.resolver.resolve_clone_plan`; consumed
    by the launcher clone path (a later plan). Carries no secret — only a
    ``credential_source`` reference the trusted parent resolves.

    :param provider: Provider name from operator config, or ``"github"`` for the
        built-in default.
    :param host_id: Operator host id, or ``"github"`` for the built-in default.
    :param canonical_host: Canonical lowercase host.
    :param normalized_url: The clone URL after provider normalization.
    :param api_base: API base URL for this host.
    :param auth: The HTTPS auth binding.
    :param credential_source: Reference-source descriptor, or ``None`` for the
        built-in github.com default (which uses the legacy ``GIT_TOKEN``).
    """

    provider: str
    host_id: str
    canonical_host: str
    normalized_url: str
    api_base: str
    auth: CloneAuthBinding
    credential_source: str | None
    ssh_host: str | None = None
    ssh_port: int | None = None
    ca_bundle: str | None = None


class GitHostProvider(ABC):
    """Per-forge behavior: host identity, URL normalization, and auth shape.

    Subclasses set ``provider`` and ``default_clone_username`` and implement
    :meth:`matches` and :meth:`default_api_base`. Later phases extend this with
    MCP, egress, and OAuth hooks.
    """

    provider: ClassVar[str]
    default_clone_username: ClassVar[str]

    @abstractmethod
    def matches(self, host: str) -> bool:
        """Whether this provider serves *host* (canonical lowercase).

        Used only for the built-in default (github.com); operator-configured
        hosts select their provider explicitly by name, so self-hosted providers
        return ``False`` here.
        """

    @abstractmethod
    def default_api_base(self, web_host: str) -> str:
        """The API base URL for *web_host* when the operator omits one."""

    def normalize_repo_url(self, url: str) -> str:
        """Return the clone URL, normalized. Default: unchanged."""
        return url

    def clone_binding(self) -> CloneAuthBinding:
        """The HTTPS auth shape for this provider (default: basic + username)."""
        return CloneAuthBinding(scheme="basic", username=self.default_clone_username)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_base.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/__init__.py omnigent/git_hosts/base.py tests/git_hosts/__init__.py tests/git_hosts/test_base.py
pre-commit run --files omnigent/git_hosts/__init__.py omnigent/git_hosts/base.py tests/git_hosts/test_base.py
git commit -m "feat(git-hosts): GitHostProvider ABC + HostConfig/ClonePlan value types"
```

---

### Task 2: Concrete providers + the registry

**Files:**
- Create: `omnigent/git_hosts/github.py`, `omnigent/git_hosts/gitea.py`
- Modify: `omnigent/git_hosts/__init__.py` (add the registry)
- Test: `tests/git_hosts/test_registry.py`

**Interfaces:**
- Consumes: `GitHostProvider`, `CloneAuthBinding` from Task 1.
- Produces: `get_git_host(provider: str) -> GitHostProvider` (raises `ValueError` for an unknown name); `available_providers() -> list[str]` (sorted). Provider classes `GitHubProvider` (`provider="github"`, matches `"github.com"`, api base `https://api.github.com`), `GitHubEnterpriseProvider` (`provider="ghe"`, matches nothing, api base `https://<host>/api/v3`), `GiteaProvider` (`provider="gitea"`, matches nothing, api base `https://<host>/api/v1`). Registry keys: `github`, `ghe`, `gitea`, `forgejo` (forgejo → GiteaProvider).

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_registry.py`:

```python
"""Tests for the git-host provider registry."""

from __future__ import annotations

import pytest

from omnigent.git_hosts import available_providers, get_git_host
from omnigent.git_hosts.gitea import GiteaProvider
from omnigent.git_hosts.github import GitHubProvider


def test_available_providers_lists_known_names() -> None:
    assert available_providers() == ["forgejo", "ghe", "gitea", "github"]


def test_get_git_host_returns_provider_instance() -> None:
    assert isinstance(get_git_host("github"), GitHubProvider)
    # Forgejo and Gitea share the Gitea API implementation.
    assert isinstance(get_git_host("forgejo"), GiteaProvider)
    assert isinstance(get_git_host("gitea"), GiteaProvider)


def test_get_git_host_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown git host provider 'svn'"):
        get_git_host("svn")


def test_github_matches_only_github_com() -> None:
    gh = GitHubProvider()
    assert gh.matches("github.com") is True
    assert gh.matches("git.acme.com") is False
    assert gh.default_api_base("github.com") == "https://api.github.com"


def test_enterprise_and_gitea_default_api_bases() -> None:
    assert get_git_host("ghe").default_api_base("ghe.acme.com") == "https://ghe.acme.com/api/v3"
    assert get_git_host("gitea").default_api_base("git.acme.com") == "https://git.acme.com/api/v1"
    # Self-hosted providers never auto-match a host; they are selected by config.
    assert get_git_host("ghe").matches("ghe.acme.com") is False
    assert get_git_host("gitea").matches("git.acme.com") is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'available_providers'` / no module `github`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/github.py`:

```python
"""GitHub.com and GitHub Enterprise Server providers."""

from __future__ import annotations

from omnigent.git_hosts.base import GitHostProvider


class GitHubProvider(GitHostProvider):
    """github.com — the built-in default."""

    provider = "github"
    default_clone_username = "x-access-token"

    def matches(self, host: str) -> bool:
        return host == "github.com"

    def default_api_base(self, web_host: str) -> str:
        return "https://api.github.com"


class GitHubEnterpriseProvider(GitHostProvider):
    """GitHub Enterprise Server — same API as github.com, operator-configured host.

    The API lives at ``<host>/api/v3`` (not ``api.<host>``); same-host git/API
    credential binding is a P2 concern (design §10).
    """

    provider = "ghe"
    default_clone_username = "x-access-token"

    def matches(self, host: str) -> bool:
        return False

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v3"
```

Create `omnigent/git_hosts/gitea.py`:

```python
"""Forgejo / Gitea provider (both speak the Gitea API)."""

from __future__ import annotations

from omnigent.git_hosts.base import GitHostProvider


class GiteaProvider(GitHostProvider):
    """Forgejo and Gitea. Registered under both names; the API is ``<host>/api/v1``.

    ``default_clone_username`` is a starting default; the exact HTTPS username a
    Gitea/Forgejo token expects is refined when credential injection lands (P1b).
    """

    provider = "gitea"
    default_clone_username = "oauth2"

    def matches(self, host: str) -> bool:
        return False

    def default_api_base(self, web_host: str) -> str:
        return f"https://{web_host}/api/v1"
```

Replace `omnigent/git_hosts/__init__.py` with:

```python
"""Git-host provider abstraction (design docs/claude/custom-git-hosts-design.md).

The registry maps a provider name to a ``"module:ClassName"`` target imported
lazily on first use — mirroring :mod:`omnigent.onboarding.sandboxes` — so a
provider's optional dependencies never load unless it is selected.
"""

from __future__ import annotations

import importlib

from omnigent.git_hosts.base import GitHostProvider

# Provider name → "module:ClassName". Closed set; forgejo and gitea share one
# implementation (identical API).
_GIT_HOSTS: dict[str, str] = {
    "github": "omnigent.git_hosts.github:GitHubProvider",
    "ghe": "omnigent.git_hosts.github:GitHubEnterpriseProvider",
    "gitea": "omnigent.git_hosts.gitea:GiteaProvider",
    "forgejo": "omnigent.git_hosts.gitea:GiteaProvider",
}


def get_git_host(provider: str) -> GitHostProvider:
    """Instantiate the provider registered under *provider*.

    :param provider: A registered provider name, e.g. ``"forgejo"``.
    :returns: A fresh provider instance.
    :raises ValueError: When *provider* is not registered.
    """
    try:
        target = _GIT_HOSTS[provider]
    except KeyError:
        raise ValueError(
            f"unknown git host provider {provider!r}; known: {available_providers()}"
        ) from None
    module_path, _, class_name = target.partition(":")
    module = importlib.import_module(module_path)
    provider_cls = getattr(module, class_name)
    instance = provider_cls()
    assert isinstance(instance, GitHostProvider)
    return instance


def available_providers() -> list[str]:
    """The sorted list of registered provider names."""
    return sorted(_GIT_HOSTS)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_registry.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/github.py omnigent/git_hosts/gitea.py omnigent/git_hosts/__init__.py tests/git_hosts/test_registry.py
pre-commit run --files omnigent/git_hosts/github.py omnigent/git_hosts/gitea.py omnigent/git_hosts/__init__.py tests/git_hosts/test_registry.py
git commit -m "feat(git-hosts): provider registry with github/ghe/gitea/forgejo"
```

---

### Task 3: Canonical host extraction

**Files:**
- Create: `omnigent/git_hosts/url.py`
- Test: `tests/git_hosts/test_url.py`

**Interfaces:**
- Produces: `split_host(url: str) -> str` — returns the canonical lowercase host of an `https://<host>/<path>` or `git@<host>:<path>` URL; raises `ValueError` for an unsupported form or embedded userinfo. Port is stripped for P1a (default-port assumption; custom-port/SSH handling is a later plan, design §9).

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_url.py`:

```python
"""Tests for :func:`omnigent.git_hosts.url.split_host`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.url import split_host


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/org/repo", "github.com"),
        ("https://GitHub.com/org/repo.git", "github.com"),
        ("https://git.acme.com/team/proj#main", "git.acme.com"),
        ("git@git.acme.com:team/proj.git", "git.acme.com"),
        ("https://git.acme.com:8443/team/proj", "git.acme.com"),
    ],
)
def test_split_host_extracts_canonical_lowercase_host(url: str, expected: str) -> None:
    assert split_host(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://user@git.acme.com/team/proj",
        "https://user:tok@git.acme.com/team/proj",
        "ftp://git.acme.com/team/proj",
        "team/proj",
        "https:///team/proj",
    ],
)
def test_split_host_rejects_unsupported_or_userinfo(url: str) -> None:
    with pytest.raises(ValueError):
        split_host(url)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_url.py -v`
Expected: FAIL — no module `omnigent.git_hosts.url`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/url.py`:

```python
"""Canonical-host extraction for git repository URLs.

Supports the two managed-clone URL forms (matching
:func:`omnigent.server.managed_hosts.parse_repo_workspace`): ``https://<host>/<path>``
and scp-style ``git@<host>:<path>``. Rejects embedded userinfo (a
credential-in-URL smuggling vector). Port is dropped for P1a — custom ports and
SSH transport land with the clone-wiring plan (design §9).
"""

from __future__ import annotations


def split_host(url: str) -> str:
    """Return the canonical lowercase host of a git repository URL.

    :param url: An ``https://<host>/<path>`` or ``git@<host>:<path>`` URL,
        optionally with a ``#<branch>`` fragment.
    :returns: The lowercase host, e.g. ``"git.acme.com"``.
    :raises ValueError: When the URL form is unsupported or embeds userinfo.
    """
    if url.startswith("https://"):
        authority = url[len("https://") :].split("/", 1)[0]
        host = authority.split(":", 1)[0]  # drop any :port
    elif url.startswith("git@"):
        rest = url[len("git@") :]
        host, sep, _path = rest.partition(":")
        if not sep:
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<path>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<path>' or 'git@<host>:<path>'"
        )
    if "@" in host:
        raise ValueError("a repository URL must not embed userinfo (user[:password]@host)")
    if not host:
        raise ValueError(f"could not extract a host from '{url}'")
    return host.lower()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_url.py -v`
Expected: PASS (10 parametrized cases).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/url.py tests/git_hosts/test_url.py
pre-commit run --files omnigent/git_hosts/url.py tests/git_hosts/test_url.py
git commit -m "feat(git-hosts): canonical-host extraction (rejects userinfo)"
```

---

### Task 4: Operator-host config parser

**Files:**
- Create: `omnigent/git_hosts/config.py`
- Test: `tests/git_hosts/test_config.py`

**Interfaces:**
- Consumes: `HostConfig` (Task 1); `get_git_host`, `available_providers` (Task 2); `split_host` is *not* used here (config supplies bare hostnames, validated directly).
- Produces: `load_git_hosts(raw: object) -> tuple[HostConfig, ...]` — parses the server-config `git_hosts:` value (a list of mappings). Fail-closed: any malformed entry raises `ValueError`. Required keys per entry: `id`, `provider`, `web_host`, `credential_source`. Optional: `api_base` (defaulted via the provider), `ssh_host`, `ssh_port`, `ca_bundle`. `web_host` is lowercased; duplicate `web_host` across entries is an error.

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_config.py`:

```python
"""Tests for :func:`omnigent.git_hosts.config.load_git_hosts`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.config import load_git_hosts


def test_none_and_empty_yield_no_hosts() -> None:
    assert load_git_hosts(None) == ()
    assert load_git_hosts([]) == ()


def test_parses_a_host_and_defaults_api_base_from_provider() -> None:
    hosts = load_git_hosts(
        [
            {
                "id": "acme-forgejo",
                "provider": "forgejo",
                "web_host": "Git.Acme.com",
                "credential_source": "env:ACME_FORGEJO_TOKEN",
            }
        ]
    )
    assert hosts == (
        HostConfig(
            id="acme-forgejo",
            provider="forgejo",
            web_host="git.acme.com",
            api_base="https://git.acme.com/api/v1",
            credential_source="env:ACME_FORGEJO_TOKEN",
        ),
    )


def test_explicit_api_base_and_optional_fields_are_kept() -> None:
    (host,) = load_git_hosts(
        [
            {
                "id": "ghe",
                "provider": "ghe",
                "web_host": "ghe.acme.com",
                "credential_source": "env:GHE_TOKEN",
                "api_base": "https://ghe.acme.com/api/v3",
                "ssh_host": "ssh.ghe.acme.com",
                "ssh_port": 2222,
                "ca_bundle": "/etc/ssl/acme-ca.pem",
            }
        ]
    )
    assert host.api_base == "https://ghe.acme.com/api/v3"
    assert host.ssh_host == "ssh.ghe.acme.com"
    assert host.ssh_port == 2222
    assert host.ca_bundle == "/etc/ssl/acme-ca.pem"


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        ({"not": "a list"}, "must be a list"),
        ([{"provider": "forgejo", "web_host": "x", "credential_source": "env:X"}], "id"),
        ([{"id": "a", "web_host": "x", "credential_source": "env:X"}], "provider"),
        (
            [{"id": "a", "provider": "svn", "web_host": "x", "credential_source": "env:X"}],
            "unknown git host provider",
        ),
        (
            [
                {"id": "a", "provider": "forgejo", "web_host": "dup.com", "credential_source": "env:A"},
                {"id": "b", "provider": "gitea", "web_host": "Dup.com", "credential_source": "env:B"},
            ],
            "duplicate",
        ),
    ],
)
def test_fail_closed_on_malformed_config(raw: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_git_hosts(raw)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_config.py -v`
Expected: FAIL — no module `omnigent.git_hosts.config`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/config.py`:

```python
"""Typed, fail-closed parser for the operator ``git_hosts:`` server-config value.

Operator config is authoritative (design §7): a malformed entry raises rather
than being silently dropped. Stores only non-secret topology; the
``credential_source`` is a reference string, never a secret.
"""

from __future__ import annotations

from typing import Any

from omnigent.git_hosts import available_providers, get_git_host
from omnigent.git_hosts.base import HostConfig

_REQUIRED = ("id", "provider", "web_host", "credential_source")


def _require_str(entry: dict[str, Any], key: str, index: int) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"git_hosts[{index}].{key} is required and must be a non-empty string")
    return value


def load_git_hosts(raw: object) -> tuple[HostConfig, ...]:
    """Parse the server-config ``git_hosts:`` value into validated records.

    :param raw: The raw value (``None``, or a list of mappings).
    :returns: A tuple of validated :class:`HostConfig`, deduplicated by host.
    :raises ValueError: On any malformed entry (fail-closed).
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("git_hosts must be a list of host mappings")

    hosts: list[HostConfig] = []
    seen: set[str] = set()
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise ValueError(f"git_hosts[{index}] must be a mapping")
        for key in _REQUIRED:
            _require_str(entry, key, index)
        provider = entry["provider"]
        if provider not in available_providers():
            raise ValueError(
                f"git_hosts[{index}].provider: unknown git host provider {provider!r}; "
                f"known: {available_providers()}"
            )
        web_host = _require_str(entry, "web_host", index).lower()
        if web_host in seen:
            raise ValueError(f"git_hosts[{index}].web_host: duplicate host {web_host!r}")
        seen.add(web_host)

        api_base_raw = entry.get("api_base")
        if api_base_raw is not None and (not isinstance(api_base_raw, str) or not api_base_raw):
            raise ValueError(f"git_hosts[{index}].api_base must be a non-empty string when set")
        api_base = api_base_raw or get_git_host(provider).default_api_base(web_host)

        ssh_port_raw = entry.get("ssh_port")
        if ssh_port_raw is not None and not isinstance(ssh_port_raw, int):
            raise ValueError(f"git_hosts[{index}].ssh_port must be an integer when set")

        hosts.append(
            HostConfig(
                id=entry["id"],
                provider=provider,
                web_host=web_host,
                api_base=api_base,
                credential_source=entry["credential_source"],
                ssh_host=entry.get("ssh_host"),
                ssh_port=ssh_port_raw,
                ca_bundle=entry.get("ca_bundle"),
            )
        )
    return tuple(hosts)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_config.py -v`
Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/config.py tests/git_hosts/test_config.py
pre-commit run --files omnigent/git_hosts/config.py tests/git_hosts/test_config.py
git commit -m "feat(git-hosts): fail-closed operator git_hosts config parser"
```

---

### Task 5: The resolver — URL + operator config → ClonePlan

**Files:**
- Create: `omnigent/git_hosts/resolver.py`
- Test: `tests/git_hosts/test_resolver.py`

**Interfaces:**
- Consumes: `HostConfig`, `ClonePlan`, `CloneAuthBinding` (Task 1); `get_git_host` (Task 2); `split_host` (Task 3).
- Produces: `resolve_clone_plan(url: str, hosts: Sequence[HostConfig]) -> ClonePlan`. Matches the URL's canonical host against `hosts` (exact); on a match, builds a `ClonePlan` via the host's provider. If unmatched but the host is github.com, returns the built-in github default plan (`host_id="github"`, `credential_source=None`). Otherwise raises `ValueError` (operator-only topology — unknown hosts are rejected).

- [ ] **Step 1: Write the failing test**

Create `tests/git_hosts/test_resolver.py`:

```python
"""Tests for :func:`omnigent.git_hosts.resolver.resolve_clone_plan`."""

from __future__ import annotations

import pytest

from omnigent.git_hosts.base import CloneAuthBinding, HostConfig
from omnigent.git_hosts.resolver import resolve_clone_plan

_ACME = HostConfig(
    id="acme-forgejo",
    provider="forgejo",
    web_host="git.acme.com",
    api_base="https://git.acme.com/api/v1",
    credential_source="env:ACME_FORGEJO_TOKEN",
)


def test_resolves_a_configured_host() -> None:
    plan = resolve_clone_plan("https://git.acme.com/team/proj#main", [_ACME])
    assert plan.provider == "forgejo"
    assert plan.host_id == "acme-forgejo"
    assert plan.canonical_host == "git.acme.com"
    assert plan.api_base == "https://git.acme.com/api/v1"
    assert plan.credential_source == "env:ACME_FORGEJO_TOKEN"
    assert plan.auth == CloneAuthBinding(scheme="basic", username="oauth2")


def test_github_com_falls_back_to_builtin_default() -> None:
    plan = resolve_clone_plan("https://github.com/org/repo", [_ACME])
    assert plan.provider == "github"
    assert plan.host_id == "github"
    assert plan.api_base == "https://api.github.com"
    # Built-in default carries no per-host credential source (legacy GIT_TOKEN).
    assert plan.credential_source is None
    assert plan.auth == CloneAuthBinding(scheme="basic", username="x-access-token")


def test_unconfigured_non_github_host_is_rejected() -> None:
    with pytest.raises(ValueError, match="no configured git host for 'git.unknown.com'"):
        resolve_clone_plan("https://git.unknown.com/team/proj", [_ACME])


def test_host_match_is_case_insensitive() -> None:
    plan = resolve_clone_plan("https://Git.Acme.com/team/proj", [_ACME])
    assert plan.host_id == "acme-forgejo"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/git_hosts/test_resolver.py -v`
Expected: FAIL — no module `omnigent.git_hosts.resolver`.

- [ ] **Step 3: Write minimal implementation**

Create `omnigent/git_hosts/resolver.py`:

```python
"""Resolve a repository URL + operator host config into a non-secret ``ClonePlan``.

Operator hosts are matched by exact canonical host; github.com falls back to the
built-in provider (legacy ``GIT_TOKEN``). Any other host is rejected — topology
is operator-only (design §7, §12.3). This function has no request identity; the
owner-aware wiring (which credential slot to use) is a later plan.
"""

from __future__ import annotations

from collections.abc import Sequence

from omnigent.git_hosts import get_git_host
from omnigent.git_hosts.base import ClonePlan, HostConfig
from omnigent.git_hosts.url import split_host


def resolve_clone_plan(url: str, hosts: Sequence[HostConfig]) -> ClonePlan:
    """Resolve *url* against operator-configured *hosts*.

    :param url: A managed repository URL (``https://`` or ``git@`` form).
    :param hosts: The operator-configured hosts (from ``load_git_hosts``).
    :returns: The resolved :class:`ClonePlan`.
    :raises ValueError: When the URL form is unsupported, or its host is neither
        configured nor github.com.
    """
    host = split_host(url)
    for cfg in hosts:
        if cfg.web_host == host:
            provider = get_git_host(cfg.provider)
            return ClonePlan(
                provider=cfg.provider,
                host_id=cfg.id,
                canonical_host=host,
                normalized_url=provider.normalize_repo_url(url),
                api_base=cfg.api_base,
                auth=provider.clone_binding(),
                credential_source=cfg.credential_source,
                ssh_host=cfg.ssh_host,
                ssh_port=cfg.ssh_port,
                ca_bundle=cfg.ca_bundle,
            )

    github = get_git_host("github")
    if github.matches(host):
        return ClonePlan(
            provider="github",
            host_id="github",
            canonical_host=host,
            normalized_url=github.normalize_repo_url(url),
            api_base=github.default_api_base(host),
            auth=github.clone_binding(),
            credential_source=None,
        )

    raise ValueError(
        f"no configured git host for {host!r}; operators register hosts under the "
        "server-config 'git_hosts' key"
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/git_hosts/test_resolver.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
uv run ruff check --fix omnigent/git_hosts tests/git_hosts && uv run ruff format omnigent/git_hosts tests/git_hosts
git add omnigent/git_hosts/resolver.py tests/git_hosts/test_resolver.py
pre-commit run --files omnigent/git_hosts/resolver.py tests/git_hosts/test_resolver.py
git commit -m "feat(git-hosts): resolve repo URL + operator config into a ClonePlan"
```

---

### Task 6: Full-suite gate

**Files:** none (verification only).

- [ ] **Step 1: Run the git_hosts suite**

Run: `uv run pytest tests/git_hosts -v`
Expected: PASS (all Task 1–5 tests).

- [ ] **Step 2: Lint, format, and type-check the package**

Run: `uv run ruff check omnigent/git_hosts tests/git_hosts && uv run ruff format --check omnigent/git_hosts tests/git_hosts`
Then, if pyrefly is available: `uv run pyrefly check omnigent/git_hosts`
Expected: clean.

- [ ] **Step 3: Confirm nothing else regressed**

Run: `uv run pytest tests/git_hosts -q`
Expected: all green. (No existing module imports `omnigent.git_hosts` yet, so the wider suite is unaffected; the resolver wiring into `parse_repo_workspace`/the create route is Plan 1b.)

---

## What this plan does NOT do (next plans)

- **P1b:** wire `resolve_clone_plan` into `parse_repo_workspace`/the create route (post-auth resolver, design §9); per-host credential injection for the exec clone using the operator `credential_source`; `SqlGitCredential` (encrypted user creds) + the server→runner handoff (design §8.2–§8.5).
- **P1c:** Kubernetes clone-Secret isolation (design §8.4); the session-sharing notice (§8.7); commit-identity = session starter (§8.6).
- **P1d:** egress merge point (§11); long-lived `omni host` host-local credential config (§8.5).

## Self-Review

- **Spec coverage (this slice):** §6 provider ABC + registry → Tasks 1–2. §7 operator topology config (typed, fail-closed) → Task 4. §12.3 resolution/precedence (operator → github default; unknown rejected) → Task 5. Host canonicalization / userinfo rejection (§9) → Task 3. Credential *reference* only, no secret at rest in these types (§8.1) → Task 1 (`credential_source: str`). Clone wiring, credentials-at-rest, handoff, sharing, egress are explicitly deferred to P1b–P1d above.
- **Placeholder scan:** none — every step ships runnable code and a concrete command with expected output.
- **Type consistency:** `HostConfig`, `CloneAuthBinding`, `ClonePlan` field names/types are defined in Task 1 and used unchanged in Tasks 4–5; `get_git_host`/`available_providers` signatures defined in Task 2 and consumed in Tasks 4–5; `split_host` defined in Task 3 and consumed in Task 5. `GiteaProvider.default_clone_username = "oauth2"` in Task 2 matches the `CloneAuthBinding(..., username="oauth2")` asserted in Task 5.
