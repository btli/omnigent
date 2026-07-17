"""Managed git repository workspaces for sandbox-hosted sessions."""

from __future__ import annotations

import hashlib
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.credential_sources import resolve_credential
from omnigent.git_hosts import DEFAULT_CLONE_USERNAME
from omnigent.git_hosts.base import ClonePlan, HostConfig
from omnigent.git_hosts.resolver import resolve_clone_plan

if TYPE_CHECKING:
    from omnigent.stores.git_credential_store import GitCredentialStore

# Keep the established category so relaunch drift alerts retain their routing.
_logger = logging.getLogger("omnigent.server.managed_hosts")

# Session label recording the repository-URL workspace a managed
# session was created with (the raw ``<url>[#<branch>]`` request
# value). ``conversations.workspace`` is overwritten with the CLONED
# path at bind time, so this label is what a sandbox RELAUNCH parses
# to re-clone the repository into the fresh generation's workspace.
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
MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY = "omnigent.sandbox.git_credential_slot"


class CredentialSelectionError(ValueError):
    """A managed session's git-credential slot cannot be chosen unambiguously.

    Raised when the owner holds multiple labeled identities on the resolved
    host and the request did not name one (or named one that does not exist).
    A ``ValueError`` subclass so the create route's existing ``except
    ValueError`` renders it as a 422 — never a silent pick (design §12.3).
    """


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


@dataclass
class RepoWorkspace:
    """
    Parsed repository-URL workspace for a managed session.

    A managed create's ``workspace`` is a git repository URL with an
    optional ``#<branch>`` fragment (Docker build-context style): the
    URL fully describes what the server materializes inside the
    sandbox. Built by :func:`parse_repo_workspace` — construct via the
    parser, not directly, so every field has been validated.

    :param url: The clone URL with any fragment stripped, e.g.
        ``"https://github.com/org/repo.git"`` or
        ``"git@github.com:org/repo.git"``.
    :param branch: Branch to clone (``--branch … --single-branch``),
        e.g. ``"release-1.2"``, or ``None`` for the default branch.
    :param repo_name: Directory name the clone lands in under the
        sandbox workspace, derived from the URL's last path segment
        with ``.git`` stripped, e.g. ``"repo"``.
    :param canonical_host: Resolution metadata from the operator
        git-host config; ``None`` when unresolved.
    :param provider: Resolution metadata from the operator git-host
        config; ``None`` when unresolved.
    :param credential_source: Resolution metadata from the operator
        git-host config; ``None`` when unresolved or for the built-in
        github.com default's credential fields.
    :param credential_slot_id: The owner's selected credential slot id for the
        resolved host (design §8.3/§12.3), or ``None`` when the owner has no
        slot (fall back to the operator ``credential_source``) or owner-aware
        selection was not requested.
    :param clone_username: Resolution metadata from the operator
        git-host config; ``None`` when unresolved.
    :param host_id: Operator git-host id the repo resolved to (``"github"``
        for the built-in default); ``None`` when unresolved.
    :param api_base: API base URL for the resolved host; ``None`` when unresolved.
    :param auth_scheme: HTTPS auth scheme (``"basic"``/``"token"``) from the
        provider clone binding; ``None`` when unresolved.
    :param ca_bundle: Path to the host's CA bundle for a private forge, or ``None``.
    :param ssh_host: SSH host override for the resolved host, or ``None``.
    :param ssh_port: SSH port override for the resolved host, or ``None``.
    """

    url: str
    branch: str | None
    repo_name: str
    host_id: str | None = None
    canonical_host: str | None = None
    provider: str | None = None
    api_base: str | None = None
    credential_source: str | None = None
    credential_slot_id: str | None = None
    clone_username: str | None = None
    auth_scheme: str | None = None
    ca_bundle: str | None = None
    ssh_host: str | None = None
    ssh_port: int | None = None


# A full 40-hex object id — rejected as a clone fragment: cloning a
# commit lands the agent on a detached HEAD it cannot push from.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Directory names a repo URL may resolve to. Conservative on purpose:
# the name is interpolated into an in-sandbox shell path.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Characters git forbids in ref names (plus ``#``, which can never
# reach the fragment since the workspace splits on its FIRST ``#`` —
# a second ``#`` means the branch itself contains one, which the
# fragment form does not support).
_BRANCH_FORBIDDEN_CHARS = set(" \t~^:?*[\\#")


def is_repo_workspace(workspace: str) -> bool:
    """
    Return whether *workspace* is a repository-URL workspace.

    Used by the create-session schema to tell the managed form (a git
    URL) apart from the external form (an absolute host path) without
    fully parsing it.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo"`` or ``"/Users/me/repo"``.
    :returns: ``True`` for the ``https://`` / ``git@`` URL forms.
    """
    return workspace.startswith(("https://", "git@"))


def _validate_clone_branch(fragment: str) -> str:
    """
    Validate a ``#<branch>`` fragment as a clonable branch name.

    :param fragment: The fragment text after the first ``#``, e.g.
        ``"release-1.2"``.
    :returns: The validated branch name, unchanged.
    :raises ValueError: When the fragment is empty, is a commit SHA
        (detached HEAD — pin commits via git worktree options
        instead), or violates git ref-name rules.
    """
    if not fragment:
        raise ValueError("the '#' fragment must name a branch, e.g. '#main'")
    if _COMMIT_SHA_RE.fullmatch(fragment):
        raise ValueError(
            "the '#' fragment must be a branch, not a commit SHA — a commit "
            "checkout would leave the agent on a detached HEAD it cannot push"
        )
    if (
        any(c in _BRANCH_FORBIDDEN_CHARS or ord(c) < 0x20 for c in fragment)
        or fragment.startswith(("-", "/"))
        or fragment.endswith(("/", "."))
        or ".." in fragment
        or "@{" in fragment
    ):
        raise ValueError(f"'{fragment}' is not a valid git branch name")
    return fragment


def _derive_repo_name(url: str) -> str:
    """
    Derive the clone directory name from a repository URL.

    :param url: The fragment-stripped clone URL, e.g.
        ``"https://github.com/org/repo.git"``.
    :returns: The last path segment with ``.git`` stripped, e.g.
        ``"repo"``.
    :raises ValueError: When no usable name can be derived (empty
        path, or a name that is not filesystem-safe).
    """
    last = url.rstrip("/").split("/")[-1]
    # scp-style URLs with a single-segment path ("git@host:repo.git")
    # have no "/" after the colon — take what follows it.
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
    if not name or name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        raise ValueError(
            f"could not derive a repository directory name from '{url}' — "
            "the URL must end in the repository name, e.g. "
            "'https://github.com/org/repo'"
        )
    return name


def parse_repo_workspace(workspace: str) -> RepoWorkspace:
    """
    Parse and validate a managed session's repository-URL workspace.

    Grammar (Docker build-context style)::

        <repo>[#<branch>]
        <repo> := https://<host>/<path>  |  git@<host>:<path>

    The fragment splits on the FIRST ``#``; branches containing ``#``
    are not supported in this form. Fails loud on anything malformed
    so a bad workspace 422s at validation instead of surfacing as a
    mid-provision clone error.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo#release-1.2"``.
    :returns: The parsed, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL or branch fragment is malformed.
    """
    url, sep, fragment = workspace.partition("#")
    if any(ch.isspace() for ch in workspace):
        raise ValueError("a repository workspace must not contain whitespace")
    if url.startswith("https://"):
        host, slash, path = url[len("https://") :].partition("/")
        if not host or not slash or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable https repository URL — expected "
                "'https://<host>/<org>/<repo>'"
            )
    elif url.startswith("git@"):
        host, colon, path = url[len("git@") :].partition(":")
        if not host or not colon or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<org>/<repo>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<org>/<repo>' or 'git@<host>:<org>/<repo>'"
        )
    branch = _validate_clone_branch(fragment) if sep else None
    return RepoWorkspace(url=url, branch=branch, repo_name=_derive_repo_name(url))


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

    :returns: The selected slot id, or ``None`` when no *label* was requested
        and the owner has no slot / owner-aware selection is off (no store / no
        owner — today's operator-credential fallback).
    :raises CredentialSelectionError: On an ambiguous selection, an unmatched
        label, or an explicit *label* that cannot be honored (no store / no
        owner / no stored slots) — an explicit request is never silently
        ignored in favor of a different credential.
    """
    if credential_store is None or owner_user_id is None:
        # An explicit label must not be silently dropped: if per-user
        # credentials aren't configured for this session, say so rather than
        # falling back to the operator credential the user didn't ask for.
        if label is not None:
            raise CredentialSelectionError(
                f"a git credential labeled {label!r} was requested, but per-user "
                "git credentials are not configured for this session"
            )
        return None
    slots = credential_store.list_for_owner_host(owner_user_id, plan.host_id)
    if not slots:
        if label is not None:
            raise CredentialSelectionError(
                f"no git credential labeled {label!r} for host {plan.host_id!r}: "
                "you have no stored credentials for this host"
            )
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


def resolve_repo_workspace(
    workspace: str,
    hosts: Sequence[HostConfig],
    *,
    owner_user_id: str | None = None,
    credential_store: GitCredentialStore | None = None,
    label: str | None = None,
) -> RepoWorkspace:
    """Parse *workspace* and resolve it against the operator git-host config.

    Combines :func:`parse_repo_workspace` (shape validation) with
    :func:`resolve_clone_plan` (host resolution): the returned workspace
    carries the full non-secret plan — host id, canonical host, provider
    name, API base, the host's ``credential_source`` reference, and the
    auth/SSH/CA details — for launch-time credential injection and handoff.

    :param workspace: The raw repository-URL workspace, e.g.
        ``"https://git.acme.com/team/proj#main"``.
    :param hosts: Operator-configured hosts (``app.state.git_hosts``).
    :param owner_user_id: The session creator's user id, for owner-aware
        credential-slot selection (design §12.3). ``None`` skips selection
        (``credential_slot_id`` stays ``None`` — today's behavior).
    :param credential_store: The store to look up the owner's credential
        slots in. ``None`` skips selection the same as an absent
        *owner_user_id* — both must be given to select a slot.
    :param label: The request's chosen credential label, or ``None`` to
        auto-select when the owner has exactly one slot on the resolved host.
    :returns: The enriched, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL is malformed or its host is neither
        configured nor github.com.
    :raises CredentialSelectionError: When owner-aware selection is active
        and the owner's slots for the resolved host are ambiguous or the
        given *label* matches none of them.
    """
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
    host: the host id, its topology hash, and the selected credential slot id
    (only when a slot was chosen).
    """
    cfg = next((h for h in hosts if h.id == repo.host_id), None)
    if cfg is None:
        return {}
    labels = {
        MANAGED_GIT_HOST_ID_LABEL_KEY: cfg.id,
        MANAGED_GIT_HOST_HASH_LABEL_KEY: host_config_hash(cfg),
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
        success to refresh a drifted hash.
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
        launch (fail closed — never clone unauthenticated), when the
        operator source cannot be resolved, or when a credentialed repo's
        clone URL is not HTTPS. The token is never in the message.
    """
    if repo is None:
        return None
    username = repo.clone_username or DEFAULT_CLONE_USERNAME
    if repo.credential_slot_id is not None:
        # A bound session MUST clone with its owner's slot: fail closed if the
        # dependencies to resolve it are absent, rather than silently falling
        # through to the operator credential or an unauthenticated clone. In the
        # live call graph both are always threaded for a bound session (create
        # requires the store to select a slot; relaunch refuses upstream when it
        # is gone), so this is a local defense-in-depth backstop.
        if owner is None or credential_store is None:
            raise ValueError(
                "the git credential bound to this session cannot be resolved at launch"
            )
        _require_https_clone_url(repo)
        # resolve_lease scopes by current_workspace_id() ambiently (see
        # GitCredentialStore). The single-tenant default (workspace 0)
        # resolves correctly with no scope wrapper here; a multi-tenant
        # deployment must run this launch inside the session's
        # workspace_scope so the lookup lands in the right workspace —
        # threading that scope through the background launch task is a
        # follow-up, not covered by this resolution seam.
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
                "the git credential bound to this session is no longer available to its owner"
            )
        return {"GIT_TOKEN": lease.token, "GIT_USERNAME": username}
    if repo.credential_source is None:
        return None
    _require_https_clone_url(repo)
    token = resolve_credential(repo.credential_source, parent_env=os.environ.copy())
    return {"GIT_TOKEN": token, "GIT_USERNAME": username}
