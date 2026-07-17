"""Sealed git credential delivery for managed session runners."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Sequence
from typing import Any

from omnigent.entities import Conversation
from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.managed_workspace import (
    MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY,
    MANAGED_REPO_LABEL_KEY,
    RepoWorkspace,
    reauthorize_relaunch_binding,
)
from omnigent.git_hosts.url import managed_repo_path, managed_repo_path_allows
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.stores.git_credential_store import GitCredentialStore
from omnigent.stores.host_store import HostStore

_HOST_CREDENTIAL_RESULT_TIMEOUT_S = 10.0


async def deliver_credential_for_launch(
    *,
    host_conn: HostConnection,
    host_registry: HostRegistry,
    runner_id: str,
    launch_generation: int,
    session_id: str,
    repo: RepoWorkspace | None,
    owner: str | None,
    credential_store: GitCredentialStore | None,
) -> str | None:
    """Seal + deliver the owner's git credential for a pending runner.

    Sent (and ACKed) BEFORE the launch frame so the host caches it and can
    thread it into the runner at spawn — the sandboxed git op cannot precede
    the rewrite rule. Resolves the owner's lease via the persisted slot
    binding, seals the token to the host's ``host.hello`` key, and awaits the
    host ACK.

    :returns: ``None`` on success OR when there is nothing to deliver (no
        credential slot / no store / an SSH repo — the backward-compatible or
        P1c-5-deferred path). A token-free error string when an HTTPS
        credential IS bound but delivery fails (the caller fails the launch
        closed). Single-delivery is structural (one call per launch) plus the
        host's own generation/runner NACK — no server-side accounting.
    """
    # Backward-compat: no slot bound (github default / no store) -> nothing to
    # deliver; the launch proceeds exactly as today.
    if repo is None or repo.credential_slot_id is None or credential_store is None:
        return None
    # SSH repos (git@host / ssh://) are the P1c-5 ssh-agent path, not this
    # HTTP-token slice. P1c-3 slot selection is scheme-agnostic, so an SSH
    # workspace can carry a slot; skip cleanly rather than garbling the
    # scp-form URL or failing the launch closed for a repo that never needed
    # the HTTP credential.
    from urllib.parse import urlparse

    if urlparse(repo.url).scheme not in ("http", "https"):
        return None
    if owner is None:
        return "credential is bound but the session has no owner to authorize"
    # An older host that cannot receive a sealed credential -> fail closed
    # rather than shipping a token in cleartext.
    sealing_public_key = host_conn.hello.sealing_public_key
    if sealing_public_key is None:
        return "managed host does not support sealed credential delivery"
    lease = await asyncio.to_thread(
        credential_store.resolve_lease,
        owner_user_id=owner,
        host_id=repo.host_id,
        credential_id=repo.credential_slot_id,
    )
    if lease is None:
        return "the session owner's git credential could not be resolved"
    repo_path = managed_repo_path(repo.url)
    if not repo_path:
        return "could not derive a repository path for credential scoping"
    # The runner's egress proxy attaches the token only to request paths its
    # allowlist accepts (see git_hosts.url.managed_repo_path_allows). A repo
    # path with a character outside that allowlist (e.g. "+", a space) would
    # be delivered but never actually attached — the repo's own git would go
    # tokenless and 401 with no clear cause. Refuse it here, loudly, using the
    # exact same predicate so the two sides cannot drift.
    if not managed_repo_path_allows(repo_path, repo_path):
        return "the repository path contains characters unsupported by credential scoping"
    from omnigent.host.frames import (
        HTTP_TOKEN_CREDENTIAL_KIND,
        HostDeliverCredentialFrame,
        build_credential_delivery_aad,
        encode_host_frame,
    )
    from omnigent.host.sealing import SealError, seal

    # Bind the token to this exact frame identity via the AEAD AAD (the frame
    # fields below must match these values verbatim, or the host's tag check
    # fails). Prevents lifting the sealed blob onto a different frame.
    canonical_host = repo.canonical_host or ""
    auth_scheme = repo.auth_scheme or "basic"
    credential_kind = HTTP_TOKEN_CREDENTIAL_KIND
    host_id = repo.host_id or ""
    aad = build_credential_delivery_aad(
        runner_id=runner_id,
        launch_generation=launch_generation,
        session_id=session_id,
        host_id=host_id,
        credential_slot=repo.credential_slot_id,
        canonical_host=canonical_host,
        repo_path=repo_path,
        credential_kind=credential_kind,
        auth_scheme=auth_scheme,
        username=repo.clone_username,
    )
    try:
        sealed = seal(lease.token, recipient_public_key_b64=sealing_public_key, aad=aad)
    except SealError:
        return "could not seal the git credential for delivery"
    request_id = secrets.token_hex(8)
    cred_future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_credentials[request_id] = cred_future
    frame = encode_host_frame(
        HostDeliverCredentialFrame(
            request_id=request_id,
            runner_id=runner_id,
            launch_generation=launch_generation,
            session_id=session_id,
            credential_slot=repo.credential_slot_id,
            canonical_host=canonical_host,
            repo_path=repo_path,
            credential_kind=credential_kind,
            auth_scheme=auth_scheme,
            username=repo.clone_username,
            sealed_credential=sealed,
            host_id=host_id,
        )
    )
    try:
        host_registry.send_text(host_conn, frame)
    except ConnectionError:
        host_conn.pending_credentials.pop(request_id, None)
        return "managed host connection lost before credential delivery"
    try:
        result = await asyncio.wait_for(cred_future, timeout=_HOST_CREDENTIAL_RESULT_TIMEOUT_S)
    except asyncio.TimeoutError:
        host_conn.pending_credentials.pop(request_id, None)
        return "credential delivery to the managed host timed out"
    if result.get("status") != "installed":
        return "the managed host rejected the credential delivery"
    return None


def reauthorize_managed_repo_for_delivery(
    conv: Conversation,
    *,
    host_store: HostStore | None,
    hosts: Sequence[HostConfig],
    credential_store: GitCredentialStore | None,
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
    :param host_store: Persistent host registrations, for the owner lookup.
    :param hosts: Live operator git-host configuration.
    :param credential_store: Per-user git credential store.
    :returns: ``(repo, owner, credential_store)`` or ``None`` when nothing is
        bound to deliver.
    :raises RelaunchBindingError: When the binding integrity is broken.
    """
    if not conv.labels.get(MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY):
        return None  # no bound credential -> respawn unchanged
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
        hosts=hosts,
        credential_store=credential_store,
    )
    return repo, host.owner, credential_store
