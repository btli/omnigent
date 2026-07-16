# P1c-4 Implementation Plan — the sealed, ACKed `deliver_credential` handoff

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a running managed *exec* (bwrap/seatbelt) sandbox its owner's git fetch/push
credential over the host tunnel — sealed, ACKed, bound to the launch — so the trusted runner parent
installs a repo-path-scoped swap-on-access rewrite rule in its egress-proxy and the sandboxed helper's
tokenless git traffic gets the real token attached on the upstream leg only. HTTPS-token only.

**Architecture:** Launch-scoped secretless swap (design §8.5 v4, architecture A). The server resolves
the owner's lease (P1c-3 `resolve_lease`, keyed by the persisted slot binding), **seals** the token to a
per-connection X25519 key the host advertised on `host.hello`, and sends a dedicated **ACKed**
`host.deliver_credential` frame **before** the launch RPC (pre-spawn). The host daemon unseals, caches
per-`runner_id` (validated against `launch_generation`), ACKs, then at spawn threads the token into the
runner process via a **child-stripped** env var (the `RUNNER_AUTH_SECRET_ENV_VARS` precedent). The
runner's `os_env` layer appends a **repo-path-scoped** `CredentialRewriteRule` (`synthetic=None`,
`repo_path` set) for the canonical host plus a repo-scoped egress allow-rule, at the seam where
`prepare_credential_proxy_runtime` returns. The swap (`_rewrite_authorization`) is **path-aware**: it
attaches the token only when the request path is within the repo prefix (rejecting `..`/encoded
traversal), so the credential binds to the repo even against a `..` normalization trick or a broad
coexisting egress rule; the repo-scoped egress allow-rule is defense-in-depth. Delivering into a sandbox
with no egress allowlist **fails closed** rather than silently narrowing it. A paired
`host.invalidate_credential` frame is
defined in the contract now (server push-revoke ships later; revocation is kill+relaunch).

**Tech Stack:** Python 3.13, `cryptography>=43` (X25519 + HKDF-SHA256 + ChaCha20-Poly1305 — already in
deps, no PyNaCl), the existing host-tunnel JSON frame transport, FastAPI, pytest. Package manager `uv`
(prefix every uv command with `env -u NODE_ENV`).

## Global Constraints

- **Package manager:** `uv` only, every command prefixed `env -u NODE_ENV` (a stray `NODE_ENV` breaks
  the web test path). `env -u NODE_ENV uv run pytest`, `env -u NODE_ENV uv run ruff check --fix`,
  `env -u NODE_ENV uv run ruff format`. Never `pip`.
- **No linter suppressions:** never `# noqa` / `# type: ignore`. Fix root causes. No blind
  `except Exception` (ruff BLE001) — catch the specific exception (`SealError`, `ValueError`,
  `InvalidUuidError`, `asyncio.TimeoutError`, `ConnectionError`).
- **The token / sealed blob is never observable.** The unsealed token and the sealed blob must not
  appear in a log line, an exception message, a `repr`, telemetry, or the frame-dump path. Secret-bearing
  dataclasses (`_DeliveredCredential`, `SealingKeypair`) define a redacting `__repr__`. The frame's secret
  field is named `sealed_credential` so the telemetry redactor (`_REDACT_KEY_SUBSTRINGS` contains
  `"credential"`) blanks it; the plaintext token is **never** a frame field. Fail-closed error messages
  name no token.
- **Frames: closed enum + `match`, both ends updated together.** `omnigent/host/frames.py` is shared by
  server and host; new kinds land in the enum, the `HostFrame` union, `encode_host_frame`, a `_decode_*`
  function, and the `_decode_known_host_frame` match — all in one task. An older peer that does not know a
  new kind raises `ValueError` in `decode_host_frame` and **drops** the frame (host: `_handle_raw_message`
  swallows it; server: `_receive_loop` logs+continues). Fail-safe: a dropped `deliver_credential` yields
  no ACK, the server's `pending_credentials` future times out, and the launch **fails closed** for a
  credential-bound session (never silent tokenless git). A non-credential session sends no frame and is
  byte-identical to today.
- **Keyword-only security calls; server-derived authority only.** `resolve_lease` keeps its P1c-2/P1c-3
  keyword-only signature (`*, owner_user_id, host_id, credential_id`). The delivery helper and sealing
  API are keyword-only. Owner/host/slot are server-derived (from the persisted binding + `host.owner`),
  never client-supplied.
- **DB / P1c-3 contract (consume verbatim, do not redefine):** `CredentialLease{token: str,
  expires_at: int | None}` (redacting repr); `GitCredentialStore.resolve_lease(*, owner_user_id, host_id,
  credential_id) -> CredentialLease | None`; the widened `RepoWorkspace` (`host_id`, `api_base`,
  `auth_scheme`, `ca_bundle`, `ssh_host`, `ssh_port`, `credential_slot_id`, plus the pre-existing `url`,
  `canonical_host`, `provider`, `credential_source`, `clone_username`);
  `ConversationStore.increment_launch_generation(conversation_id) -> int` + `Conversation.launch_generation`;
  the label keys `MANAGED_GIT_HOST_ID_LABEL_KEY` / `MANAGED_GIT_HOST_HASH_LABEL_KEY` /
  `MANAGED_GIT_CANONICAL_URL_LABEL_KEY` / `MANAGED_GIT_CREDENTIAL_SLOT_LABEL_KEY`.
- **Backward compatibility (explicit test, Task 7):** a session with **no credential binding** (no slot
  selected, or `app.state.git_credential_store is None`) behaves byte-identically to today — no
  `deliver_credential` frame is sent, no proxy/egress rule is installed, the ambient `GIT_TOKEN` clone
  path is untouched. Delivery engages **only** when `repo.credential_slot_id` is set.
- **Scope — P1c-4 delivers on exec (bwrap/seatbelt) MANAGED sandboxes only.** Out (later slices): k8s
  in-Pod proxy + init-container clone Secret, SSH ssh-agent, tmux terminal swap (**P1c-5**); commit
  identity + sharing notice (**P1c-6**); OAuth minting/refresh (**P3**); server-driven push-revocation
  (the `invalidate_credential` frame is contract-only); the full §11 operator/agent egress merge-point
  component (**P1d** — P1c-4 appends only the resolved host's repo-scoped rule at the runner seam).
- **Commit discipline:** one commit per task; `env -u NODE_ENV uv run ruff check --fix <changed> &&
  env -u NODE_ENV uv run ruff format <changed>` then `pre-commit run --files <changed>` before each
  commit.

---

## File Structure

- `omnigent/host/sealing.py` — **NEW.** X25519+HKDF+ChaCha20Poly1305 seal/unseal pair + per-connection
  keypair; the pluggable primitive `binding_token` can adopt later (Task 1).
- `omnigent/host/frames.py` — `HostFrameKind` gains `DELIVER_CREDENTIAL` / `DELIVER_CREDENTIAL_RESULT` /
  `INVALIDATE_CREDENTIAL`; the three frame dataclasses; `HostHelloFrame.sealing_public_key`; encode/decode/
  match/union (Task 2).
- `omnigent/runner/identity.py` — the `MANAGED_GIT_*` runner env-var names; `MANAGED_GIT_TOKEN_ENV_VAR`
  added to `RUNNER_AUTH_SECRET_ENV_VARS` (Task 4).
- `omnigent/host/connect.py` — `_DeliveredCredential`; `self._pending_credentials` + `self._sealing_keypair`;
  `_handle_deliver_credential` / `_handle_invalidate_credential`; hello keypair; dispatch branches;
  lifecycle discard on all three runner-exit paths + the launch-failure path (Task 3); `_build_runner_env`
  + `_handle_launch` env threading (Task 4).
- `omnigent/inner/credential_proxy.py` — `CredentialRewriteRule.repo_path` (Task 5).
- `omnigent/inner/egress/proxy.py` — `_managed_repo_path_allows` + path-aware `_rewrite_authorization`
  (threads `path` at both call sites) so the credential rule itself enforces the repo scope (Task 5).
- `omnigent/inner/os_env.py` — `ManagedGitCredentialError`; `_install_managed_git_credential` (path-aware
  rule) + `_merge_managed_git_egress_rules` + `_apply_managed_git_credential` (fail-closed) + the
  `_start_locked` seam wiring (Task 5).
- `omnigent/server/host_registry.py` — `HostConnection.pending_credentials` (Task 6).
- `omnigent/server/routes/host_tunnel.py` — `_receive_loop` branch resolving the ACK future (Task 6).
- `omnigent/server/routes/sessions.py` — `_deliver_credential_for_launch` helper (SSH-scheme gate,
  host-idempotent single-delivery, no server accounting); `_launch_runner_on_host` gains
  `repo`/`owner`/`credential_store`; threading from `_run_managed_launch` →
  `_bind_and_launch_managed_runner`; `_CREDENTIAL_DELIVERY_ERROR_CODE` handling (Task 6).
- Tests: `tests/host/test_sealing.py` (NEW), `tests/host/test_frames.py`, `tests/host/test_connect.py`,
  `tests/inner/egress/test_proxy.py`, `tests/inner/test_os_env.py`, `tests/server/test_managed_hosts.py`,
  `tests/server/integration/test_host_tunnel_route.py`.

**Controller note:** line numbers below are anchors from commit `e1923569`. Implementers locate the
quoted text and never trust raw numbers — P1c-3 is landing concurrently and shifts
`omnigent/server/managed_hosts.py` and `omnigent/server/routes/sessions.py`. Treat every P1c-3 symbol
named in **Global Constraints** as its committed-plan interface, not as whatever the live line says
mid-change.

---

### Task 1: The pluggable sealing module

**Files:**
- Create: `omnigent/host/sealing.py`
- Test: `tests/host/test_sealing.py`

**Interfaces:**
- Consumes: `cryptography` primitives (`x25519`, `HKDF`, `ChaCha20Poly1305`, `InvalidTag`).
- Produces:
  - `SEAL_VERSION: str` (the human-readable construction id) and a wire version byte (`0x01`) that is
    prepended to every envelope and authenticated in the AAD (so a downgrade/strip fails the tag).
  - `SealError(Exception)`.
  - `SealingKeypair` frozen dataclass `{private_key: X25519PrivateKey, public_key_b64: str}` (redacting `__repr__`).
  - `generate_sealing_keypair() -> SealingKeypair`.
  - `seal(plaintext: str, *, recipient_public_key_b64: str, aad: bytes = b"") -> str` — base64
    envelope; `aad` is AEAD associated data (the credential path binds the full frame-identity tuple
    into it, so a sealed blob cannot be replayed against a different runner/generation/host/repo).
  - `unseal(sealed_b64: str, *, private_key: X25519PrivateKey, aad: bytes = b"") -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/host/test_sealing.py`:

```python
"""Tests for the host-tunnel credential sealing primitive."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from omnigent.host.sealing import (
    SealError,
    generate_sealing_keypair,
    seal,
    unseal,
)


def test_seal_unseal_roundtrips() -> None:
    kp = generate_sealing_keypair()
    sealed = seal("ghp_realtoken", recipient_public_key_b64=kp.public_key_b64)
    assert sealed != "ghp_realtoken"
    assert "ghp_realtoken" not in sealed
    assert unseal(sealed, private_key=kp.private_key) == "ghp_realtoken"


def test_each_seal_is_fresh() -> None:
    kp = generate_sealing_keypair()
    a = seal("tok", recipient_public_key_b64=kp.public_key_b64)
    b = seal("tok", recipient_public_key_b64=kp.public_key_b64)
    # Ephemeral sender key + random nonce -> distinct ciphertexts for one plaintext.
    assert a != b
    assert unseal(a, private_key=kp.private_key) == "tok"
    assert unseal(b, private_key=kp.private_key) == "tok"


def test_unseal_with_wrong_key_fails_closed() -> None:
    kp = generate_sealing_keypair()
    other = generate_sealing_keypair()
    sealed = seal("tok", recipient_public_key_b64=kp.public_key_b64)
    with pytest.raises(SealError):
        unseal(sealed, private_key=other.private_key)


def test_unseal_rejects_tampered_ciphertext() -> None:
    kp = generate_sealing_keypair()
    sealed = seal("tok", recipient_public_key_b64=kp.public_key_b64)
    tampered = sealed[:-4] + ("AAAA" if not sealed.endswith("AAAA") else "BBBB")
    with pytest.raises(SealError):
        unseal(tampered, private_key=kp.private_key)


def test_seal_rejects_malformed_recipient_key() -> None:
    with pytest.raises(SealError):
        seal("tok", recipient_public_key_b64="not-base64!!")


def test_unseal_requires_matching_aad() -> None:
    # The credential path binds the frame identity into the AAD; a blob sealed
    # for one identity must not unseal under another (replay protection).
    kp = generate_sealing_keypair()
    sealed = seal("tok", recipient_public_key_b64=kp.public_key_b64, aad=b"runner-1|gen-1")
    assert unseal(sealed, private_key=kp.private_key, aad=b"runner-1|gen-1") == "tok"
    with pytest.raises(SealError):
        unseal(sealed, private_key=kp.private_key, aad=b"runner-2|gen-1")


def test_unseal_rejects_unknown_version() -> None:
    # The wire version byte is embedded AND authenticated; an envelope carrying
    # a version this build doesn't know is rejected, not silently reinterpreted.
    import base64

    kp = generate_sealing_keypair()
    sealed = seal("tok", recipient_public_key_b64=kp.public_key_b64)
    raw = bytearray(base64.b64decode(sealed))
    raw[0] = 0x02  # bump the wire version byte to an unsupported value
    forged = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(SealError):
        unseal(forged, private_key=kp.private_key)


def test_keypair_repr_hides_private_key() -> None:
    kp = generate_sealing_keypair()
    text = repr(kp)
    assert kp.public_key_b64 in text
    assert "private_key" not in text.lower() or "X25519PrivateKey" not in text


def test_generated_public_key_is_usable_by_sender() -> None:
    kp = generate_sealing_keypair()
    # The public key travels as base64 on host.hello; a sender only needs that.
    assert isinstance(kp.private_key, X25519PrivateKey)
    assert len(kp.public_key_b64) > 0
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/host/test_sealing.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'omnigent.host.sealing'`.

- [ ] **Step 3: Write the module**

Create `omnigent/host/sealing.py`:

```python
"""Pluggable sealing for secret-bearing host-tunnel frames.

X25519 ephemeral-static ECDH + HKDF-SHA256 + ChaCha20-Poly1305. The
recipient (the host daemon) generates a keypair per tunnel connection and
advertises the public key on ``host.hello``; the sender (the server) seals a
secret to that public key so the plaintext never crosses the tunnel — even on
a ``ws://`` loopback deployment where the transport itself is cleartext.

Designed as a reusable ``seal`` / ``unseal`` pair: the credential-delivery
frame uses it first, and ``binding_token`` can adopt it later without a
contract change (the envelope carries its own version tag).

Uses only :mod:`cryptography` (already a dependency); no PyNaCl.
"""

from __future__ import annotations

import base64
import binascii
import os
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# Human-readable construction id. The single wire byte below is what actually
# travels in the envelope; SEAL_VERSION documents what version 1 means.
SEAL_VERSION = "x25519-hkdf-chacha20poly1305.v1"
# Wire version byte prepended to (and authenticated in the AAD of) every
# envelope. A new/incompatible construction gets a new byte; unseal rejects
# any byte it does not recognise, and the byte's inclusion in the AAD makes a
# downgrade fail the tag rather than silently reinterpret the bytes.
_SEAL_VERSION_TAG = b"\x01"

_HKDF_INFO = b"omnigent.host.sealing.v1"
_VERSION_LEN = 1
_PUBLIC_KEY_LEN = 32
_NONCE_LEN = 12
_MIN_TAG_LEN = 16


class SealError(Exception):
    """A frame secret could not be sealed or unsealed.

    Deliberately carries no plaintext: raised on a malformed key, a
    malformed envelope, or a failed AEAD tag check, and its message never
    includes the secret.
    """


@dataclass(frozen=True)
class SealingKeypair:
    """A recipient keypair; the private half never leaves the host process.

    :param private_key: X25519 private key used to unseal delivered secrets.
    :param public_key_b64: base64 of the 32-byte raw public key, safe to put
        on ``host.hello`` (a public key is not a secret).
    """

    private_key: X25519PrivateKey
    public_key_b64: str

    def __repr__(self) -> str:
        # Keep the private key off any repr/log; the public half is fine.
        return f"SealingKeypair(public_key_b64={self.public_key_b64!r})"


def generate_sealing_keypair() -> SealingKeypair:
    """Generate a fresh X25519 recipient keypair (one per tunnel connection).

    :returns: The keypair whose public half is advertised on ``host.hello``.
    """
    private_key = X25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes_raw()
    return SealingKeypair(
        private_key=private_key,
        public_key_b64=base64.b64encode(public_bytes).decode("ascii"),
    )


def _derive_key(
    shared_secret: bytes, ephemeral_public: bytes, recipient_public: bytes
) -> bytes:
    """Derive the AEAD key from the ECDH shared secret and both public keys.

    Binding the salt to both public keys ties the derived key to this exact
    sender/recipient pair.
    """
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=ephemeral_public + recipient_public,
        info=_HKDF_INFO,
    ).derive(shared_secret)


def seal(plaintext: str, *, recipient_public_key_b64: str, aad: bytes = b"") -> str:
    """Seal *plaintext* to a recipient public key (sender / server side).

    :param plaintext: The secret to seal, e.g. a git PAT.
    :param recipient_public_key_b64: base64 public key from ``host.hello``.
    :param aad: AEAD associated data authenticated (not encrypted) alongside
        the ciphertext. The credential-delivery path passes the frame-identity
        tuple here so the recipient's tag check fails if the sealed blob is
        replayed under a different identity. Defaults to empty so the
        primitive stays generally reusable.
    :returns: A base64 envelope ``version(1) || ephemeral_pub(32) || nonce(12)
        || ct+tag``. The version byte is authenticated via the AAD.
    :raises SealError: If the recipient key is malformed.
    """
    try:
        recipient_public = base64.b64decode(recipient_public_key_b64, validate=True)
        recipient_key = X25519PublicKey.from_public_bytes(recipient_public)
    except (ValueError, binascii.Error) as exc:
        raise SealError("invalid recipient public key") from exc
    ephemeral = X25519PrivateKey.generate()
    ephemeral_public = ephemeral.public_key().public_bytes_raw()
    shared = ephemeral.exchange(recipient_key)
    key = _derive_key(shared, ephemeral_public, recipient_public)
    nonce = os.urandom(_NONCE_LEN)
    # The version byte is both embedded (in the envelope) and authenticated (in
    # the AAD), so it cannot be stripped or downgraded without failing the tag.
    ciphertext = ChaCha20Poly1305(key).encrypt(
        nonce, plaintext.encode("utf-8"), _SEAL_VERSION_TAG + aad
    )
    return base64.b64encode(
        _SEAL_VERSION_TAG + ephemeral_public + nonce + ciphertext
    ).decode("ascii")


def unseal(sealed_b64: str, *, private_key: X25519PrivateKey, aad: bytes = b"") -> str:
    """Unseal an envelope with the recipient private key (host side).

    :param sealed_b64: The base64 envelope produced by :func:`seal`.
    :param private_key: The recipient's X25519 private key.
    :param aad: The same associated data the sender bound (see :func:`seal`).
        A mismatch fails the AEAD tag check — this is how frame-identity
        binding is enforced.
    :returns: The recovered plaintext.
    :raises SealError: On a malformed envelope or a failed AEAD tag check.
    """
    try:
        blob = base64.b64decode(sealed_b64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SealError("sealed credential is not valid base64") from exc
    if len(blob) < _VERSION_LEN + _PUBLIC_KEY_LEN + _NONCE_LEN + _MIN_TAG_LEN:
        raise SealError("sealed credential envelope is too short")
    version = blob[:_VERSION_LEN]
    if version != _SEAL_VERSION_TAG:
        raise SealError("unsupported seal version")
    ephemeral_public = blob[_VERSION_LEN : _VERSION_LEN + _PUBLIC_KEY_LEN]
    nonce = blob[_VERSION_LEN + _PUBLIC_KEY_LEN : _VERSION_LEN + _PUBLIC_KEY_LEN + _NONCE_LEN]
    ciphertext = blob[_VERSION_LEN + _PUBLIC_KEY_LEN + _NONCE_LEN :]
    try:
        ephemeral_key = X25519PublicKey.from_public_bytes(ephemeral_public)
        shared = private_key.exchange(ephemeral_key)
        recipient_public = private_key.public_key().public_bytes_raw()
        key = _derive_key(shared, ephemeral_public, recipient_public)
        # Authenticate the same version byte the sender bound.
        plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, version + aad)
    except (ValueError, InvalidTag) as exc:
        raise SealError("failed to unseal credential") from exc
    return plaintext.decode("utf-8")


__all__ = [
    "SEAL_VERSION",
    "SealError",
    "SealingKeypair",
    "generate_sealing_keypair",
    "seal",
    "unseal",
]
```

- [ ] **Step 4: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/host/test_sealing.py -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/host/sealing.py tests/host/test_sealing.py && env -u NODE_ENV uv run ruff format omnigent/host/sealing.py tests/host/test_sealing.py
git add omnigent/host/sealing.py tests/host/test_sealing.py
pre-commit run --files omnigent/host/sealing.py tests/host/test_sealing.py
git commit -m "feat(git-hosts): add pluggable X25519 sealing for host-tunnel secrets (P1c-4)"
```

---

### Task 2: The `deliver_credential` / `invalidate_credential` frame contract

**Files:**
- Modify: `omnigent/host/frames.py` (`HostFrameKind` ~`:37-58`; `HostHelloFrame` ~`:63-90`; `HostFrame`
  union ~`:522-541`; `encode_host_frame` hello branch ~`:578` and the end ~`:763`; `_decode_known_host_frame`
  match ~`:823`; `_decode_host_hello` ~`:863`)
- Test: `tests/host/test_frames.py`

**Interfaces:**
- Consumes: the existing field validators (`_required_str`, `_required_int`, `_optional_nullable_str`).
- Produces:
  - `HostFrameKind.DELIVER_CREDENTIAL = "host.deliver_credential"`,
    `DELIVER_CREDENTIAL_RESULT = "host.deliver_credential_result"`,
    `INVALIDATE_CREDENTIAL = "host.invalidate_credential"`.
  - `HostHelloFrame.sealing_public_key: str | None = None`.
  - `HostDeliverCredentialFrame{request_id, runner_id, launch_generation: int, session_id, credential_slot,
    canonical_host, repo_path, credential_kind, auth_scheme, username: str | None, sealed_credential, host_id}`.
  - `HostDeliverCredentialResultFrame{request_id, status, error: str | None = None}` — `status` is
    `"installed"` or `"rejected"`.
  - `HostInvalidateCredentialFrame{runner_id, launch_generation: int, reason: str | None = None}` (one-way).
  - `build_credential_delivery_aad(*, runner_id, launch_generation, session_id, host_id,
    credential_slot, canonical_host, repo_path, credential_kind, auth_scheme, username) -> bytes` — the
    canonical AEAD associated-data both the server (seal) and host (unseal) compute from the frame's
    non-secret binding fields, so a sealed blob is cryptographically bound to its exact frame identity.
  - Round-trip encode/decode + the redaction guarantee (`sealed_credential` blanks on a span).

- [ ] **Step 1: Write the failing tests** — add to `tests/host/test_frames.py`:

```python
def test_deliver_credential_frame_roundtrips() -> None:
    from omnigent.host.frames import (
        HostDeliverCredentialFrame,
        decode_host_frame,
        encode_host_frame,
    )

    frame = HostDeliverCredentialFrame(
        request_id="req1",
        runner_id="runner_abc",
        launch_generation=3,
        session_id="conv_1",
        credential_slot="slot_1",
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        credential_kind="http-token",
        auth_scheme="basic",
        username="x-access-token",
        sealed_credential="U0VBTEVE",
        host_id="host_9",
    )
    decoded = decode_host_frame(encode_host_frame(frame))
    assert decoded == frame


def test_deliver_credential_result_roundtrips() -> None:
    from omnigent.host.frames import (
        HostDeliverCredentialResultFrame,
        decode_host_frame,
        encode_host_frame,
    )

    for result in (
        HostDeliverCredentialResultFrame(request_id="req1", status="installed"),
        HostDeliverCredentialResultFrame(
            request_id="req1", status="rejected", error="unknown credential kind"
        ),
    ):
        assert decode_host_frame(encode_host_frame(result)) == result


def test_invalidate_credential_roundtrips() -> None:
    from omnigent.host.frames import (
        HostInvalidateCredentialFrame,
        decode_host_frame,
        encode_host_frame,
    )

    frame = HostInvalidateCredentialFrame(
        runner_id="runner_abc", launch_generation=3, reason="revoked"
    )
    assert decode_host_frame(encode_host_frame(frame)) == frame


def test_hello_carries_sealing_public_key() -> None:
    from omnigent.host.frames import HostHelloFrame, decode_host_frame, encode_host_frame

    hello = HostHelloFrame(
        version="0.1.0",
        frame_protocol_version=1,
        name="corey-laptop",
        sealing_public_key="cHVia2V5",
    )
    assert decode_host_frame(encode_host_frame(hello)).sealing_public_key == "cHVia2V5"


def test_hello_sealing_public_key_defaults_none_for_older_host() -> None:
    from omnigent.host.frames import decode_host_frame

    # An older host omits the field entirely; decode must tolerate it.
    text = (
        '{"kind": "host.hello", "version": "0.1.0", '
        '"frame_protocol_version": 1, "name": "old"}'
    )
    assert decode_host_frame(text).sealing_public_key is None


def test_sealed_credential_is_redacted_on_telemetry() -> None:
    from omnigent.runtime.telemetry import _redact_payload

    redacted = _redact_payload(
        {"kind": "host.deliver_credential", "sealed_credential": "SEALEDBLOB", "host_id": "h"}
    )
    assert redacted["sealed_credential"] == "[redacted]"
    assert redacted["host_id"] == "h"


def test_unknown_frame_kind_still_raises_valueerror() -> None:
    # Version-skew contract: an unknown kind must raise ValueError so the
    # receive loops drop it (fail-safe), not crash.
    import pytest

    from omnigent.host.frames import decode_host_frame

    with pytest.raises(ValueError):
        decode_host_frame('{"kind": "host.does_not_exist", "request_id": "x"}')


def test_credential_delivery_aad_is_deterministic_and_identity_bound() -> None:
    from omnigent.host.frames import build_credential_delivery_aad

    kwargs = dict(
        runner_id="runner_abc",
        launch_generation=3,
        session_id="conv_1",
        host_id="host_9",
        credential_slot="slot_1",
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        credential_kind="http-token",
        auth_scheme="basic",
        username="x-access-token",
    )
    aad = build_credential_delivery_aad(**kwargs)
    assert isinstance(aad, bytes)
    # Deterministic for the same identity...
    assert build_credential_delivery_aad(**kwargs) == aad
    # ...and different when ANY bound field changes (here: the generation).
    assert build_credential_delivery_aad(**{**kwargs, "launch_generation": 4}) != aad
    assert build_credential_delivery_aad(**{**kwargs, "runner_id": "runner_xyz"}) != aad
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/host/test_frames.py -q -k "credential or sealing or unknown_frame"`
Expected: FAIL — `ImportError: cannot import name 'HostDeliverCredentialFrame'`.

- [ ] **Step 3: Add the enum members** — in `HostFrameKind` (after `CREATE_DIR_RESULT`, ~`:57`):

```python
    DELIVER_CREDENTIAL = "host.deliver_credential"
    DELIVER_CREDENTIAL_RESULT = "host.deliver_credential_result"
    INVALIDATE_CREDENTIAL = "host.invalidate_credential"
```

- [ ] **Step 4: Add `sealing_public_key` to `HostHelloFrame`** — add the field after `telemetry_opt_out`
  (~`:89`) and a `:param:` line in its docstring:

```python
    sealing_public_key: str | None = None
```

  Docstring `:param:` (add near `telemetry_opt_out`):

```python
    :param sealing_public_key: base64 X25519 public key this connection
        advertises so the server can seal a delivered credential to it
        (see :mod:`omnigent.host.sealing`). Regenerated per tunnel
        connection; ``None`` from an older host that cannot receive
        sealed credentials.
```

- [ ] **Step 5: Add the three frame dataclasses** — after `HostCreateDirResultFrame` (~`:520`):

```python
@dataclass
class HostDeliverCredentialFrame:
    """Server → host: deliver a sealed git credential for a pending runner.

    Sent (and ACKed) BEFORE ``host.launch_runner`` so the host caches the
    credential and can thread it into the runner at spawn — before any git
    op can run. Consumed only by the trusted runner parent.

    :param request_id: Correlates the result, e.g. ``"req_cred_1"``.
    :param runner_id: The runner this credential is bound to (derived
        server-side from the launch binding token via
        ``token_bound_runner_id``), e.g. ``"runner_abc..."``.
    :param launch_generation: Monotonic per-session launch counter — the
        anti-replay anchor. A credential for a generation the host does not
        expect for this runner is rejected.
    :param session_id: Conversation/session id, e.g. ``"conv_abc"``.
    :param credential_slot: The opaque server-side credential slot id the
        token was resolved from (bookkeeping / audit).
    :param canonical_host: Exact host the swap binds to, e.g.
        ``"git.acme.com"``.
    :param repo_path: Repo path prefix the egress rule scopes to (leading
        slash, no ``.git``, no trailing slash), e.g. ``"/team/proj"``.
    :param credential_kind: Envelope type tag: ``"http-token"`` (the only
        kind implemented). ``"ssh-key"`` / ``"oauth"`` are reserved so the
        frame never needs a redesign; the host rejects them for now.
    :param auth_scheme: Upstream ``Authorization`` scheme, one of
        ``"basic"`` / ``"bearer"`` / ``"token"``.
    :param username: Basic-auth username emitted upstream when
        ``auth_scheme="basic"``, e.g. ``"x-access-token"``; ``None`` for
        ``bearer`` / ``token``.
    :param sealed_credential: The token sealed to the host's
        ``sealing_public_key`` (see :mod:`omnigent.host.sealing`). Never the
        plaintext token; the field name triggers telemetry redaction.
    :param host_id: The operator git-host id (part of the binding tuple).
    """

    request_id: str
    runner_id: str
    launch_generation: int
    session_id: str
    credential_slot: str
    canonical_host: str
    repo_path: str
    credential_kind: str
    auth_scheme: str
    username: str | None
    sealed_credential: str
    host_id: str


@dataclass
class HostDeliverCredentialResultFrame:
    """Host → server: outcome of a credential delivery.

    :param request_id: Correlates to the
        :class:`HostDeliverCredentialFrame`, e.g. ``"req_cred_1"``.
    :param status: ``"installed"`` (cached, will thread at spawn) or
        ``"rejected"``.
    :param error: Reason when ``status`` is ``"rejected"`` (never names a
        token), e.g. ``"unknown credential kind"``. ``None`` on success.
    """

    request_id: str
    status: str
    error: str | None = None


@dataclass
class HostInvalidateCredentialFrame:
    """Server → host: discard a cached credential (one-way; contract-only).

    Defined now so server-driven revocation can ship without a contract
    change. In P1 the operational revocation story is kill+relaunch; the
    host handler simply drops the cached credential for the runner. No
    result frame.

    :param runner_id: The runner whose cached credential to discard.
    :param launch_generation: The generation the discard targets.
    :param reason: Optional human-readable reason (audit only).
    """

    runner_id: str
    launch_generation: int
    reason: str | None = None
```

- [ ] **Step 6: Extend the `HostFrame` union** — add the three types to the union (~`:522`):

```python
    | HostCreateDirResultFrame
    | HostDeliverCredentialFrame
    | HostDeliverCredentialResultFrame
    | HostInvalidateCredentialFrame
)
```

- [ ] **Step 7: Add the encode branches** — in `encode_host_frame`, add the `sealing_public_key` to the
  existing hello branch (~`:586`) and three new branches before the final `raise TypeError` (~`:763`):

  In the `HostHelloFrame` branch, add the key to the payload dict:

```python
                "telemetry_opt_out": frame.telemetry_opt_out,
                "sealing_public_key": frame.sealing_public_key,
```

  New branches (before `raise TypeError`):

```python
    if isinstance(frame, HostDeliverCredentialFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.DELIVER_CREDENTIAL.value,
                "request_id": frame.request_id,
                "runner_id": frame.runner_id,
                "launch_generation": frame.launch_generation,
                "session_id": frame.session_id,
                "credential_slot": frame.credential_slot,
                "canonical_host": frame.canonical_host,
                "repo_path": frame.repo_path,
                "credential_kind": frame.credential_kind,
                "auth_scheme": frame.auth_scheme,
                "username": frame.username,
                "sealed_credential": frame.sealed_credential,
                "host_id": frame.host_id,
            }
        )
    if isinstance(frame, HostDeliverCredentialResultFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.DELIVER_CREDENTIAL_RESULT.value,
                "request_id": frame.request_id,
                "status": frame.status,
                "error": frame.error,
            }
        )
    if isinstance(frame, HostInvalidateCredentialFrame):
        return _encode_payload(
            {
                "kind": HostFrameKind.INVALIDATE_CREDENTIAL.value,
                "runner_id": frame.runner_id,
                "launch_generation": frame.launch_generation,
                "reason": frame.reason,
            }
        )
```

- [ ] **Step 8: Add the match arms + decoders** — in `_decode_known_host_frame` (before the final
  `raise ValueError`, ~`:859`):

```python
        case HostFrameKind.DELIVER_CREDENTIAL:
            return _decode_deliver_credential(msg)
        case HostFrameKind.DELIVER_CREDENTIAL_RESULT:
            return _decode_deliver_credential_result(msg)
        case HostFrameKind.INVALIDATE_CREDENTIAL:
            return _decode_invalidate_credential(msg)
```

  Add `sealing_public_key` to `_decode_host_hello` (~`:869`):

```python
        telemetry_opt_out=bool(msg.get("telemetry_opt_out", False)),
        sealing_public_key=_optional_nullable_str(msg, "sealing_public_key"),
    )
```

  Add the three decoder functions after `_decode_create_dir_result` (~`:1166`):

```python
def _decode_deliver_credential(msg: dict[str, Any]) -> HostDeliverCredentialFrame:
    """Decode a host.deliver_credential frame.

    :param msg: Decoded frame object.
    :returns: Typed host.deliver_credential frame.
    """
    return HostDeliverCredentialFrame(
        request_id=_required_str(msg, "request_id"),
        runner_id=_required_str(msg, "runner_id"),
        launch_generation=_required_int(msg, "launch_generation"),
        session_id=_required_str(msg, "session_id"),
        credential_slot=_required_str(msg, "credential_slot"),
        canonical_host=_required_str(msg, "canonical_host"),
        repo_path=_required_str(msg, "repo_path"),
        credential_kind=_required_str(msg, "credential_kind"),
        auth_scheme=_required_str(msg, "auth_scheme"),
        username=_optional_nullable_str(msg, "username"),
        sealed_credential=_required_str(msg, "sealed_credential"),
        host_id=_required_str(msg, "host_id"),
    )


def _decode_deliver_credential_result(
    msg: dict[str, Any],
) -> HostDeliverCredentialResultFrame:
    """Decode a host.deliver_credential_result frame.

    :param msg: Decoded frame object.
    :returns: Typed host.deliver_credential_result frame.
    """
    return HostDeliverCredentialResultFrame(
        request_id=_required_str(msg, "request_id"),
        status=_required_str(msg, "status"),
        error=_optional_nullable_str(msg, "error"),
    )


def _decode_invalidate_credential(
    msg: dict[str, Any],
) -> HostInvalidateCredentialFrame:
    """Decode a host.invalidate_credential frame.

    :param msg: Decoded frame object.
    :returns: Typed host.invalidate_credential frame.
    """
    return HostInvalidateCredentialFrame(
        runner_id=_required_str(msg, "runner_id"),
        launch_generation=_required_int(msg, "launch_generation"),
        reason=_optional_nullable_str(msg, "reason"),
    )
```

- [ ] **Step 8b: Add the AAD builder** — after the new decoders (or near the frame dataclasses), add the
  canonical associated-data function both ends use:

```python
def build_credential_delivery_aad(
    *,
    runner_id: str,
    launch_generation: int,
    session_id: str,
    host_id: str,
    credential_slot: str,
    canonical_host: str,
    repo_path: str,
    credential_kind: str,
    auth_scheme: str,
    username: str | None,
) -> bytes:
    """Canonical AEAD associated-data binding a sealed credential to its frame.

    The server builds this from the values it puts on the
    :class:`HostDeliverCredentialFrame` and passes it to
    :func:`omnigent.host.sealing.seal`; the host rebuilds it from the RECEIVED
    frame's fields and passes it to :func:`~omnigent.host.sealing.unseal`. Any
    tampering with a bound field — or replaying a sealed blob under a
    different runner / generation / host / repo — changes the AAD, so the
    ChaCha20-Poly1305 tag check fails and the delivery is rejected. This
    cryptographically binds the sealed token to its exact frame identity (the
    plaintext binding fields are then tamper-evident, not merely advisory).

    :returns: The associated-data bytes (unit-separator-joined; the fields are
        ASCII ids / paths that cannot contain ``0x1f``).
    """
    parts = [
        runner_id,
        str(launch_generation),
        session_id,
        host_id,
        credential_slot,
        canonical_host,
        repo_path,
        credential_kind,
        auth_scheme,
        username if username is not None else "",
    ]
    return "\x1f".join(parts).encode("utf-8")
```

- [ ] **Step 9: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/host/test_frames.py -q`
Expected: PASS (existing frame tests still green; new ones pass).

- [ ] **Step 10: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/host/frames.py tests/host/test_frames.py && env -u NODE_ENV uv run ruff format omnigent/host/frames.py tests/host/test_frames.py
git add omnigent/host/frames.py tests/host/test_frames.py
pre-commit run --files omnigent/host/frames.py tests/host/test_frames.py
git commit -m "feat(git-hosts): add deliver/invalidate_credential frames + hello sealing key (P1c-4)"
```

---

### Task 3: Host-side receipt — keypair, unseal, cache, ACK, lifecycle discard

**Files:**
- Modify: `omnigent/host/connect.py` (`HostProcess.__init__` ~`:637-675`; `_serve_frames` hello
  ~`:1847-1859`; `_dispatch_host_frame` ~`:1942-1957`; `_handle_stop` ~`:1168`; `_watch_runner`
  ~`:1214-1220`; `_cleanup_runners` ~`:1710-1724`; add `_DeliveredCredential` near `_RunnerHandle`
  ~`:611`)
- Test: `tests/host/test_connect.py`

**Interfaces:**
- Consumes: `SealingKeypair` / `generate_sealing_keypair` / `unseal` / `SealError` (Task 1);
  `HostDeliverCredentialFrame` / `HostDeliverCredentialResultFrame` / `HostInvalidateCredentialFrame` (Task 2).
- Produces:
  - `_DeliveredCredential` dataclass `{token, launch_generation, session_id, credential_slot,
    canonical_host, repo_path, auth_scheme, username}` (redacting `__repr__`).
  - `HostProcess._pending_credentials: dict[str, _DeliveredCredential]` (keyed by `runner_id`).
  - `HostProcess._sealing_keypair: SealingKeypair | None` (regenerated per connection).
  - `HostProcess._handle_deliver_credential(frame) -> HostDeliverCredentialResultFrame`.
  - `HostProcess._handle_invalidate_credential(frame) -> None`.
  - Discard of `_pending_credentials[runner_id]` on `_handle_stop`, `_watch_runner` (crash), and
    `_cleanup_runners`.

- [ ] **Step 1: Write the failing tests** — add to `tests/host/test_connect.py` (uses the module's
  existing `HostProcess` / `HostIdentity` construction pattern; check the top of the file for the identity
  fixture and mirror it):

```python
def _managed_deliver_frame(kp_public_b64, *, runner_id, generation, token="ghp_tok"):
    from omnigent.host.frames import (
        HostDeliverCredentialFrame,
        build_credential_delivery_aad,
    )
    from omnigent.host.sealing import seal

    fields = dict(
        runner_id=runner_id,
        launch_generation=generation,
        session_id="conv_1",
        host_id="host_9",
        credential_slot="slot_1",
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        credential_kind="http-token",
        auth_scheme="basic",
        username="x-access-token",
    )
    aad = build_credential_delivery_aad(**fields)
    return HostDeliverCredentialFrame(
        request_id="req_cred_1",
        sealed_credential=seal(token, recipient_public_key_b64=kp_public_b64, aad=aad),
        **fields,
    )


def _host_process():
    from omnigent.host.connect import HostProcess
    from omnigent.host.identity import HostIdentity

    return HostProcess(
        HostIdentity(host_id="host_9", name="corey-laptop"),
        "https://example.test",
    )


def test_deliver_credential_unseals_caches_and_acks() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    frame = _managed_deliver_frame(
        proc._sealing_keypair.public_key_b64, runner_id="runner_abc", generation=1
    )
    result = proc._handle_deliver_credential(frame)
    assert result.status == "installed"
    cached = proc._pending_credentials["runner_abc"]
    assert cached.token == "ghp_tok"
    assert cached.canonical_host == "git.acme.com"
    assert cached.repo_path == "/team/proj"
    assert cached.launch_generation == 1


def test_deliver_credential_rejects_unknown_kind_without_token() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    frame = _managed_deliver_frame(
        proc._sealing_keypair.public_key_b64, runner_id="runner_abc", generation=1
    )
    frame.credential_kind = "ssh-key"  # reserved, not implemented
    result = proc._handle_deliver_credential(frame)
    assert result.status == "rejected"
    assert "runner_abc" not in proc._pending_credentials
    assert "ghp_tok" not in (result.error or "")


def test_deliver_credential_rejects_bad_seal_without_token() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    other = generate_sealing_keypair()
    # Sealed to a DIFFERENT key than the host holds.
    frame = _managed_deliver_frame(other.public_key_b64, runner_id="runner_abc", generation=1)
    result = proc._handle_deliver_credential(frame)
    assert result.status == "rejected"
    assert "runner_abc" not in proc._pending_credentials
    assert "ghp_tok" not in (result.error or "")


def test_deliver_credential_rejects_generation_conflict() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    pub = proc._sealing_keypair.public_key_b64
    assert proc._handle_deliver_credential(
        _managed_deliver_frame(pub, runner_id="runner_abc", generation=1)
    ).status == "installed"
    # A second delivery for the same runner with a different generation is a replay/conflict.
    result = proc._handle_deliver_credential(
        _managed_deliver_frame(pub, runner_id="runner_abc", generation=2)
    )
    assert result.status == "rejected"
    assert proc._pending_credentials["runner_abc"].launch_generation == 1


def test_invalidate_credential_discards_cache() -> None:
    from omnigent.host.frames import HostInvalidateCredentialFrame
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    proc._handle_deliver_credential(
        _managed_deliver_frame(proc._sealing_keypair.public_key_b64, runner_id="r1", generation=1)
    )
    proc._handle_invalidate_credential(
        HostInvalidateCredentialFrame(runner_id="r1", launch_generation=1)
    )
    assert "r1" not in proc._pending_credentials


def test_handle_stop_discards_cached_credential() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    proc._handle_deliver_credential(
        _managed_deliver_frame(proc._sealing_keypair.public_key_b64, runner_id="r1", generation=1)
    )
    from omnigent.host.frames import HostStopRunnerFrame

    proc._handle_stop(HostStopRunnerFrame(request_id="req", runner_id="r1"))
    assert "r1" not in proc._pending_credentials


def test_deliver_credential_repr_hides_token() -> None:
    from omnigent.host.sealing import generate_sealing_keypair

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    proc._handle_deliver_credential(
        _managed_deliver_frame(proc._sealing_keypair.public_key_b64, runner_id="r1", generation=1)
    )
    assert "ghp_tok" not in repr(proc._pending_credentials["r1"])
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/host/test_connect.py -q -k "deliver or invalidate or stop_discards"`
Expected: FAIL — `AttributeError: 'HostProcess' object has no attribute '_handle_deliver_credential'`.

- [ ] **Step 3: Add `_DeliveredCredential`** — after the `_RunnerHandle` dataclass (~`:623`):

```python
@dataclass
class _DeliveredCredential:
    """A git credential delivered for a pending/live runner (host-side cache).

    Held from ``host.deliver_credential`` receipt until the runner exits;
    discarded on every runner-exit path. The token is the real secret — kept
    out of every repr/log.

    :param token: The unsealed upstream git token (the real secret).
    :param launch_generation: Generation this delivery is bound to.
    :param session_id: Session the runner serves.
    :param credential_slot: Server-side slot id the token came from.
    :param canonical_host: Host the swap binds to, e.g. ``"git.acme.com"``.
    :param repo_path: Repo path prefix the egress rule scopes to.
    :param auth_scheme: Upstream ``Authorization`` scheme.
    :param username: Basic-auth username, or ``None``.
    """

    token: str
    launch_generation: int
    session_id: str
    credential_slot: str
    canonical_host: str
    repo_path: str
    auth_scheme: str
    username: str | None

    def __repr__(self) -> str:
        return (
            "_DeliveredCredential(token=<redacted>, "
            f"canonical_host={self.canonical_host!r}, "
            f"launch_generation={self.launch_generation})"
        )
```

- [ ] **Step 4: Add the state fields** — in `HostProcess.__init__`, after `self._runners = {}` (~`:649`):

```python
        # Git credentials delivered per pending/live runner, keyed by
        # runner_id. Populated by host.deliver_credential (pre-spawn),
        # consumed at _handle_launch, discarded on every runner-exit path.
        # Kept across a transient tunnel reconnect (this process survives it);
        # re-delivery happens only on relaunch (a fresh runner_id).
        self._pending_credentials: dict[str, _DeliveredCredential] = {}
        # X25519 keypair advertised on host.hello so the server can seal
        # delivered credentials to it. Regenerated on every (re)connect.
        self._sealing_keypair: SealingKeypair | None = None
```

  Add the imports near the other `omnigent.host` imports at the top of the file (with the
  `from omnigent.host.frames import ...` block and a new sealing import):

```python
from omnigent.host.frames import (
    HostDeliverCredentialFrame,
    HostDeliverCredentialResultFrame,
    HostInvalidateCredentialFrame,
    build_credential_delivery_aad,
    # ... keep the existing names imported here ...
)
from omnigent.host.sealing import (
    SealError,
    SealingKeypair,
    generate_sealing_keypair,
    unseal,
)
```

  (The three new frame names must be added to whatever `omnigent.host.frames` import form the file already
  uses; if `connect.py` imports frames lazily inside functions, add them there instead — locate the
  existing `HostLaunchRunnerFrame` import and extend it.)

- [ ] **Step 5: Generate the keypair on connect** — in `_serve_frames`, before building `hello`
  (~`:1847`):

```python
        self._sealing_keypair = generate_sealing_keypair()
        hello = HostHelloFrame(
            version=VERSION,
            frame_protocol_version=1,
            name=self._identity.name,
            runners=self._alive_runner_ids(),
            configured_harnesses=await asyncio.to_thread(configured_harness_map),
            telemetry_opt_out=_tel_opt_out,
            sealing_public_key=self._sealing_keypair.public_key_b64,
        )
```

- [ ] **Step 6: Add the two handlers** — after `_handle_stop` (~`:1190`):

```python
    def _handle_deliver_credential(
        self,
        frame: HostDeliverCredentialFrame,
    ) -> HostDeliverCredentialResultFrame:
        """Unseal, validate, and cache a delivered git credential.

        Sent (and ACKed) before ``host.launch_runner`` so the credential is
        cached before the runner spawns. The unsealed token is threaded into
        the runner at spawn (see :meth:`_handle_launch`) and never leaves the
        trusted parent. Rejects (NACKs) without ever naming a token.

        :param frame: The delivery frame.
        :returns: ``"installed"`` on success, else ``"rejected"`` with a
            token-free reason.
        """
        if self._sealing_keypair is None:  # pragma: no cover — hello sets it
            return HostDeliverCredentialResultFrame(
                request_id=frame.request_id,
                status="rejected",
                error="host connection has no sealing key",
            )
        # Only HTTPS-token swaps are implemented; ssh-key / oauth are reserved
        # envelope values that slot in later without a frame redesign.
        if frame.credential_kind != "http-token":
            return HostDeliverCredentialResultFrame(
                request_id=frame.request_id,
                status="rejected",
                error=f"unsupported credential kind: {frame.credential_kind!r}",
            )
        # Binding validation (anti-replay): delivery must be pre-spawn, and a
        # runner must not be re-bound to a different generation.
        if frame.runner_id in self._runners:
            return HostDeliverCredentialResultFrame(
                request_id=frame.request_id,
                status="rejected",
                error="runner already launched",
            )
        existing = self._pending_credentials.get(frame.runner_id)
        if existing is not None and existing.launch_generation != frame.launch_generation:
            return HostDeliverCredentialResultFrame(
                request_id=frame.request_id,
                status="rejected",
                error="credential generation conflict",
            )
        # Rebuild the AAD from the RECEIVED frame's binding fields: if any was
        # tampered, or the blob is replayed under a different identity, the tag
        # check fails and we reject without ever exposing a token.
        aad = build_credential_delivery_aad(
            runner_id=frame.runner_id,
            launch_generation=frame.launch_generation,
            session_id=frame.session_id,
            host_id=frame.host_id,
            credential_slot=frame.credential_slot,
            canonical_host=frame.canonical_host,
            repo_path=frame.repo_path,
            credential_kind=frame.credential_kind,
            auth_scheme=frame.auth_scheme,
            username=frame.username,
        )
        try:
            token = unseal(
                frame.sealed_credential,
                private_key=self._sealing_keypair.private_key,
                aad=aad,
            )
        except SealError:
            return HostDeliverCredentialResultFrame(
                request_id=frame.request_id,
                status="rejected",
                error="could not unseal delivered credential",
            )
        self._pending_credentials[frame.runner_id] = _DeliveredCredential(
            token=token,
            launch_generation=frame.launch_generation,
            session_id=frame.session_id,
            credential_slot=frame.credential_slot,
            canonical_host=frame.canonical_host,
            repo_path=frame.repo_path,
            auth_scheme=frame.auth_scheme,
            username=frame.username,
        )
        _logger.info(
            "Cached delivered credential for runner %s (host %s, generation %d)",
            frame.runner_id,
            frame.canonical_host,
            frame.launch_generation,
        )
        return HostDeliverCredentialResultFrame(
            request_id=frame.request_id,
            status="installed",
        )

    def _handle_invalidate_credential(
        self,
        frame: HostInvalidateCredentialFrame,
    ) -> None:
        """Discard a cached credential (one-way; contract-only in P1).

        The live runner still holds the credential in its own egress-proxy
        heap (there is no post-spawn rotate channel); operational revocation
        is kill+relaunch. This only drops the host's cached copy.

        :param frame: The invalidate frame.
        :returns: None.
        """
        self._pending_credentials.pop(frame.runner_id, None)
        _logger.info(
            "Discarded cached credential for runner %s (generation %d)",
            frame.runner_id,
            frame.launch_generation,
        )
```

- [ ] **Step 7: Discard on the runner-exit paths.**

  In `_handle_stop`, right after the `handle = self._runners.pop(...)` line (~`:1168`):

```python
        handle = self._runners.pop(frame.runner_id, None)
        self._pending_credentials.pop(frame.runner_id, None)
```

  In `_watch_runner`, after the crash is confirmed (after the `is not handle` guard `return`, ~`:1217`,
  just before composing the error):

```python
        self._pending_credentials.pop(runner_id, None)
        error = _runner_exit_error(handle.proc.returncode, handle.log_path)
```

  In `_cleanup_runners`, after `self._runners.clear()` (~`:1724`):

```python
        self._runners.clear()
        self._pending_credentials.clear()
```

- [ ] **Step 8: Dispatch the two frames** — in `_dispatch_host_frame`, after the
  `HostListWorktreesFrame` branch (~`:1957`):

```python
        elif isinstance(frame, HostDeliverCredentialFrame):
            await ws.send(encode_host_frame(self._handle_deliver_credential(frame)))
        elif isinstance(frame, HostInvalidateCredentialFrame):
            self._handle_invalidate_credential(frame)
```

- [ ] **Step 9: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/host/test_connect.py -q`
Expected: PASS (new credential tests + existing connect tests green).

- [ ] **Step 10: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/host/connect.py tests/host/test_connect.py && env -u NODE_ENV uv run ruff format omnigent/host/connect.py tests/host/test_connect.py
git add omnigent/host/connect.py tests/host/test_connect.py
pre-commit run --files omnigent/host/connect.py tests/host/test_connect.py
git commit -m "feat(git-hosts): host-side deliver/invalidate credential handling + cache lifecycle (P1c-4)"
```

---

### Task 4: Thread the delivered credential into the runner at spawn (child-stripped)

**Files:**
- Modify: `omnigent/runner/identity.py` (add the `MANAGED_GIT_*` env-var names; add
  `MANAGED_GIT_TOKEN_ENV_VAR` to `RUNNER_AUTH_SECRET_ENV_VARS` ~`:55`)
- Modify: `omnigent/host/connect.py` (`_build_runner_env` ~`:499-550`; `_handle_launch` runner_id + env
  build + spawn-failure returns ~`:1070-1131`)
- Test: `tests/host/test_connect.py`, `tests/runner/test_identity.py` (or the nearest existing
  `runner/identity` test module — if none, add the strip assertion to `tests/host/test_connect.py`)

**Interfaces:**
- Consumes: `_DeliveredCredential` (Task 3); `RUNNER_AUTH_SECRET_ENV_VARS` / `strip_runner_auth_secrets`
  (existing).
- Produces:
  - `MANAGED_GIT_TOKEN_ENV_VAR = "OMNIGENT_MANAGED_GIT_TOKEN"` (in `RUNNER_AUTH_SECRET_ENV_VARS` →
    stripped at the runner→sandbox-helper boundary).
  - `MANAGED_GIT_CANONICAL_HOST_ENV_VAR` / `MANAGED_GIT_REPO_PATH_ENV_VAR` /
    `MANAGED_GIT_AUTH_SCHEME_ENV_VAR` / `MANAGED_GIT_USERNAME_ENV_VAR` (non-secret binding).
  - `_build_runner_env(..., credential: _DeliveredCredential | None = None)` sets those vars when a
    credential is present; `_handle_launch` looks the credential up by `runner_id` and passes it.

- [ ] **Step 1: Write the failing tests** — add to `tests/host/test_connect.py`:

```python
def test_build_runner_env_omits_managed_git_vars_without_credential() -> None:
    from omnigent.host.connect import _build_runner_env
    from omnigent.runner.identity import MANAGED_GIT_TOKEN_ENV_VAR

    env = _build_runner_env(
        {},
        server_url="https://x",
        runner_id="r1",
        binding_token="bt",
        workspace="/w",
        parent_pid=1,
    )
    assert MANAGED_GIT_TOKEN_ENV_VAR not in env


def test_build_runner_env_sets_managed_git_vars_with_credential() -> None:
    from omnigent.host.connect import _DeliveredCredential, _build_runner_env
    from omnigent.runner.identity import (
        MANAGED_GIT_AUTH_SCHEME_ENV_VAR,
        MANAGED_GIT_CANONICAL_HOST_ENV_VAR,
        MANAGED_GIT_REPO_PATH_ENV_VAR,
        MANAGED_GIT_TOKEN_ENV_VAR,
        MANAGED_GIT_USERNAME_ENV_VAR,
    )

    cred = _DeliveredCredential(
        token="ghp_tok",
        launch_generation=1,
        session_id="conv_1",
        credential_slot="slot_1",
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        auth_scheme="basic",
        username="x-access-token",
    )
    env = _build_runner_env(
        {},
        server_url="https://x",
        runner_id="r1",
        binding_token="bt",
        workspace="/w",
        parent_pid=1,
        credential=cred,
    )
    assert env[MANAGED_GIT_TOKEN_ENV_VAR] == "ghp_tok"
    assert env[MANAGED_GIT_CANONICAL_HOST_ENV_VAR] == "git.acme.com"
    assert env[MANAGED_GIT_REPO_PATH_ENV_VAR] == "/team/proj"
    assert env[MANAGED_GIT_AUTH_SCHEME_ENV_VAR] == "basic"
    assert env[MANAGED_GIT_USERNAME_ENV_VAR] == "x-access-token"


def test_managed_git_token_is_stripped_from_child_env() -> None:
    from omnigent.runner.identity import (
        MANAGED_GIT_CANONICAL_HOST_ENV_VAR,
        MANAGED_GIT_TOKEN_ENV_VAR,
        RUNNER_AUTH_SECRET_ENV_VARS,
        strip_runner_auth_secrets,
    )

    # The real token is stripped before the runner spawns the sandbox helper;
    # the non-secret binding vars are not.
    assert MANAGED_GIT_TOKEN_ENV_VAR in RUNNER_AUTH_SECRET_ENV_VARS
    stripped = strip_runner_auth_secrets(
        {
            MANAGED_GIT_TOKEN_ENV_VAR: "ghp_tok",
            MANAGED_GIT_CANONICAL_HOST_ENV_VAR: "git.acme.com",
        }
    )
    assert MANAGED_GIT_TOKEN_ENV_VAR not in stripped
    assert stripped[MANAGED_GIT_CANONICAL_HOST_ENV_VAR] == "git.acme.com"
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/host/test_connect.py -q -k "managed_git"`
Expected: FAIL — `ImportError: cannot import name 'MANAGED_GIT_TOKEN_ENV_VAR'`.

- [ ] **Step 3: Add the env-var names + strip registration** — in `omnigent/runner/identity.py`, near
  `RUNNER_AUTH_SECRET_ENV_VARS` (~`:37-55`):

```python
# Server-delivered git fetch/push credential threaded host→runner at spawn.
# The TOKEN var is a real secret: it is registered in
# RUNNER_AUTH_SECRET_ENV_VARS below so it is stripped at every runner→child
# spawn boundary (the sandboxed helper never inherits it — it lives only in
# the trusted runner's egress-proxy). The rest are non-secret binding metadata
# the runner's os_env reads to build the swap + repo-scoped egress rule.
MANAGED_GIT_TOKEN_ENV_VAR = "OMNIGENT_MANAGED_GIT_TOKEN"
MANAGED_GIT_CANONICAL_HOST_ENV_VAR = "OMNIGENT_MANAGED_GIT_CANONICAL_HOST"
MANAGED_GIT_REPO_PATH_ENV_VAR = "OMNIGENT_MANAGED_GIT_REPO_PATH"
MANAGED_GIT_AUTH_SCHEME_ENV_VAR = "OMNIGENT_MANAGED_GIT_AUTH_SCHEME"
MANAGED_GIT_USERNAME_ENV_VAR = "OMNIGENT_MANAGED_GIT_USERNAME"
```

  Extend the frozenset (~`:55`) to include the token var:

```python
RUNNER_AUTH_SECRET_ENV_VARS: frozenset[str] = frozenset(
    {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, MANAGED_GIT_TOKEN_ENV_VAR}
)
```

- [ ] **Step 4: Consume the credential in `_build_runner_env`** — add the import at the top of
  `connect.py` (extend the existing `from omnigent.runner.identity import (...)` block, ~`:71`) with the
  five `MANAGED_GIT_*` names, then add the parameter and the env writes. Change the signature (~`:499`):

```python
def _build_runner_env(
    base_env: Mapping[str, str],
    *,
    server_url: str,
    runner_id: str,
    binding_token: str,
    workspace: str,
    parent_pid: int,
    credential: _DeliveredCredential | None = None,
) -> dict[str, str]:
```

  and, just before `return env` (~`:550`), add:

```python
    if credential is not None:
        # The real token is a child-stripped secret (see
        # RUNNER_AUTH_SECRET_ENV_VARS); the rest is non-secret binding the
        # runner's os_env reads to install the swap + repo-scoped egress rule.
        env[MANAGED_GIT_TOKEN_ENV_VAR] = credential.token
        env[MANAGED_GIT_CANONICAL_HOST_ENV_VAR] = credential.canonical_host
        env[MANAGED_GIT_REPO_PATH_ENV_VAR] = credential.repo_path
        env[MANAGED_GIT_AUTH_SCHEME_ENV_VAR] = credential.auth_scheme
        if credential.username is not None:
            env[MANAGED_GIT_USERNAME_ENV_VAR] = credential.username
    return env
```

  Add a `:param credential:` line to the docstring:

```python
    :param credential: A git credential delivered for this runner via
        ``host.deliver_credential``, or ``None``. When present, the real
        token (child-stripped) and its non-secret binding are added so the
        runner's os_env can install the fetch/push swap.
```

- [ ] **Step 5: Look up + pass the credential in `_handle_launch`** — change the `_build_runner_env`
  call (~`:1070`):

```python
        runner_id = token_bound_runner_id(frame.binding_token)
        credential = self._pending_credentials.get(runner_id)
        env = _build_runner_env(
            os.environ,
            server_url=self._server_url,
            runner_id=runner_id,
            binding_token=frame.binding_token,
            workspace=str(workspace),
            parent_pid=os.getpid(),
            credential=credential,
        )
```

  On the two spawn-failure returns (the `except OSError` at ~`:1114` and the `proc.poll() is not None`
  branch at ~`:1121`), drop the cache entry so a never-launched credential does not linger. Add before
  each of those `return HostLaunchRunnerResultFrame(... status="failed" ...)`:

```python
            self._pending_credentials.pop(runner_id, None)
```

  (The success path leaves the entry in place — it is now owned by the runner's lifecycle and discarded on
  exit by Task 3's `_watch_runner` / `_handle_stop` / `_cleanup_runners`.)

- [ ] **Step 6: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/host/test_connect.py -q -k "managed_git or deliver or stop_discards"`
Expected: PASS.

- [ ] **Step 7: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/runner/identity.py omnigent/host/connect.py tests/host/test_connect.py && env -u NODE_ENV uv run ruff format omnigent/runner/identity.py omnigent/host/connect.py tests/host/test_connect.py
git add omnigent/runner/identity.py omnigent/host/connect.py tests/host/test_connect.py
pre-commit run --files omnigent/runner/identity.py omnigent/host/connect.py tests/host/test_connect.py
git commit -m "feat(git-hosts): thread delivered git credential into runner env (child-stripped) (P1c-4)"
```

---

### Task 5: Repo-path-scoped credential swap — path-aware proxy rule + os_env install

> **Security keystone.** §8.5 v4 requires the rewrite RULE to be repo-path-scoped. Relying on the
> egress allow-rule alone is **bypassable**: (a) `rules.py` does no `..` normalization and the proxy
> matches the raw path (`_handle_http` proxy.py:798, `_handle_connect` proxy.py:590), so
> `/team/proj/../secret-repo/...` matches `/team/proj/**` and the forge normalizes it → the owner's
> token lands on a different repo (a malicious hook/submodule in the session's own repo can drive this);
> (b) any broader coexisting `* <host>/**` egress rule re-opens the whole host because `_cred_by_host` is
> host-keyed. So the credential rule itself is made path-aware, and the egress allow-rule is kept as
> defense-in-depth.

**Files:**
- Modify: `omnigent/inner/credential_proxy.py` (`CredentialRewriteRule.repo_path`)
- Modify: `omnigent/inner/egress/proxy.py` (add `import re`; `_GIT_PATH_SAFE` allowlist +
  `_managed_repo_path_allows` helper; make `_rewrite_authorization` path-aware; thread `path` at both call
  sites — `_forward_https` ~`:679`, `_handle_http` ~`:819`)
- Modify: `omnigent/inner/os_env.py` (`ManagedGitCredentialError`; `_install_managed_git_credential`
  carrying `repo_path`; `_merge_managed_git_egress_rules`; `_apply_managed_git_credential` fail-closed
  wrapper; wire into `_start_locked` at the seam ~`:427-456`)
- Test: `tests/inner/egress/test_proxy.py`, `tests/inner/test_os_env.py`

**Interfaces:**
- Consumes: `CredentialProxyRuntime` / `CredentialRewriteRule` (already imported at `os_env.py:31-33`);
  the `MANAGED_GIT_*` env-var names (Task 4); `parse_rules` / `EgressProxy` / the `ca_paths` fixture
  (existing in `test_proxy.py`).
- Produces:
  - `CredentialRewriteRule.repo_path: str | None = None` — `None` keeps host-scoped operator-credential
    behavior (byte-identical); a set value scopes the swap to that repo prefix.
  - `_GIT_PATH_SAFE` regex + `_managed_repo_path_allows(request_path: str, repo_path: str) -> bool` (proxy
    module fn) — fails closed via an **allowlist** (`[A-Za-z0-9._~/-]` only, so `%`/`\`/`;`/control/null/
    unicode are all rejected in one rule — provably complete vs a blocklist), then a `..`-segment check and
    the repo-prefix check (bare or `.git`).
  - `_rewrite_authorization(self, *, host: str, path: str, headers_raw: bytes)` — `path` added; a
    repo-scoped rule attaches the token only when `_managed_repo_path_allows` passes.
  - `ManagedGitCredentialError(ValueError)` — a deterministic managed-git misconfiguration (no egress
    allowlist, or a host already bound), surfaced as the session-failure reason (message names no token).
  - `_install_managed_git_credential(..., repo_path: str) -> CredentialProxyRuntime` — appends a
    path-scoped swap-on-access rule (`synthetic=None`, `repo_path` set); raises `ManagedGitCredentialError`
    on a duplicate host.
  - `_merge_managed_git_egress_rules(rules, *, canonical_host, repo_path) -> list[str]` — appends
    `"* <host><repo_path>/**"` + `"* <host><repo_path>.git/**"` (deduped; defense-in-depth).
  - `_apply_managed_git_credential(runtime, egress_rules, *, canonical_host, repo_path, auth_scheme,
    username, token) -> tuple[CredentialProxyRuntime, list[str]]` — **fails closed** (no egress allowlist)
    per the user decision; otherwise installs the rule + merges the egress rules.
  - `_start_locked` calls `_apply_managed_git_credential` when `OMNIGENT_MANAGED_GIT_TOKEN` is present.

#### Part A — path-aware credential rule (proxy)

- [ ] **Step 1: Write the failing proxy tests** — add to `tests/inner/egress/test_proxy.py` (its top
  already imports `CredentialRewriteRule`, `EgressProxy`, `parse_rules`, and provides the `ca_paths`
  fixture):

```python
def test_repo_scoped_swap_only_attaches_within_repo_path(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """A repo-scoped credential rule attaches ONLY within the repo prefix.

    A BROAD egress rule co-exists, proving the credential RULE (not just the
    egress allow-rule) enforces the scope — and that a `..`/encoded traversal
    that the forge would normalize elsewhere never gets the token.
    """
    cert_path, key_path, _ = ca_paths
    rule = CredentialRewriteRule(
        host="git.acme.com",
        scheme="basic",
        real_secret="SECRET",
        synthetic=None,
        username="x-access-token",
        repo_path="/team/proj",
    )
    proxy = EgressProxy(
        parse_rules(["* git.acme.com/**"]),  # broad host allow — the trap
        cert_path,
        key_path,
        credential_rewrites=[rule],
    )
    header_block = b"Host: git.acme.com\r\n\r\n"

    def _attaches(path: str) -> bool:
        result = proxy._rewrite_authorization(
            host="git.acme.com", path=path, headers_raw=header_block
        )
        return b"authorization" in result.headers.lower()

    # Legit git smart-HTTP paths for THIS repo (bare + .git) -> attached.
    assert _attaches("/team/proj/info/refs?service=git-upload-pack")
    assert _attaches("/team/proj.git/git-receive-pack")
    # Dot-segment traversal the forge would normalize to another repo -> NOT attached.
    assert not _attaches("/team/proj/../other/info/refs")
    # Percent-encoded traversal -> NOT attached.
    assert not _attaches("/team/proj/%2e%2e/other/info/refs")
    # A different repo on the same host (reachable via the broad egress rule) -> NOT attached.
    assert not _attaches("/other/repo.git/git-receive-pack")


def test_host_scoped_operator_rule_still_attaches_to_any_path(
    ca_paths: tuple[Path, Path, Path],
) -> None:
    """repo_path=None keeps operator-credential behavior byte-identical."""
    cert_path, key_path, _ = ca_paths
    rule = CredentialRewriteRule(
        host="git.acme.com", scheme="bearer", real_secret="OP", synthetic=None
    )
    proxy = EgressProxy(
        parse_rules(["* git.acme.com/**"]),
        cert_path,
        key_path,
        credential_rewrites=[rule],
    )
    result = proxy._rewrite_authorization(
        host="git.acme.com",
        path="/anything/at/all",
        headers_raw=b"Host: git.acme.com\r\n\r\n",
    )
    assert b"Bearer OP" in result.headers


def test_managed_repo_path_allows_vectors() -> None:
    from omnigent.inner.egress.proxy import _managed_repo_path_allows

    base = "/team/proj"
    # Legit git paths + exact-prefix roots -> attached.
    assert _managed_repo_path_allows("/team/proj/info/refs", base)
    assert _managed_repo_path_allows("/team/proj.git/git-receive-pack", base)
    assert _managed_repo_path_allows("/team/proj/git-receive-pack?x=1", base)  # query stripped
    assert _managed_repo_path_allows("/team/proj", base)  # exact bare root
    assert _managed_repo_path_allows("/team/proj.git", base)  # exact .git root
    # Escape vectors the allowlist rejects in one rule.
    assert not _managed_repo_path_allows("/team/proj/../secret/info/refs", base)  # literal ..
    assert not _managed_repo_path_allows("/team/proj/%2e%2e/secret", base)  # single-encoded
    assert not _managed_repo_path_allows("/team/proj/%252e%252e/other", base)  # double-encoded
    assert not _managed_repo_path_allows("/team/proj/..\\other", base)  # literal backslash
    assert not _managed_repo_path_allows("/team/proj/..;x/other", base)  # matrix param
    assert not _managed_repo_path_allows("/team/proj/%2fother", base)  # any percent-encoding
    assert not _managed_repo_path_allows("/team/proj/a\\b", base)  # any backslash
    # Prefix-not-boundary and cross-repo.
    assert not _managed_repo_path_allows("/team/project/info/refs", base)  # prefix, not boundary
    assert not _managed_repo_path_allows("/other/repo.git/info/refs", base)
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/inner/egress/test_proxy.py -q -k "repo_scoped or managed_repo_path or operator_rule_still"`
Expected: FAIL — `TypeError: _rewrite_authorization() got an unexpected keyword argument 'path'` / `cannot import name '_managed_repo_path_allows'`.

- [ ] **Step 3: Add `repo_path` to `CredentialRewriteRule`** — in `omnigent/inner/credential_proxy.py`,
  add the field after `username` (~`:83`) and a `:param:` line:

```python
    host: str
    scheme: str
    real_secret: str
    synthetic: str | None = None
    username: str | None = None
    repo_path: str | None = None
```

  Docstring `:param:` (add after the `username` entry):

```python
    :param repo_path: When set (server-delivered per-repo git credential),
        the swap attaches ONLY to requests whose path is within this repo
        prefix (leading slash, no ``.git``/trailing slash, e.g.
        ``"/team/proj"``); a ``..``/encoded traversal or an out-of-prefix
        path gets no token even if a broader egress rule allowed the request.
        ``None`` (operator credentials) keeps the host-scoped behavior.
```

- [ ] **Step 4: Make `_rewrite_authorization` path-aware** — in `omnigent/inner/egress/proxy.py`, add the
  module helper (near the other module-level helpers, e.g. above the `EgressProxy` class) and thread `path`
  through the swap. **Ensure `import re` is present at the top of the file** (proxy.py does not import it
  today).

  Add the module-level regex + helper. This is the sole credential boundary, so it uses an **allowlist**
  (provably complete in one rule) rather than a blocklist — a blocklist misses double-encoding (`%252e`),
  literal `\`, matrix params (`..;x`), etc. Legit git smart-HTTP paths are entirely within the safe
  charset (repo/org/namespace names are `[A-Za-z0-9._-]`; endpoints are plain ASCII `/info/refs`,
  `/git-upload-pack`, `/git-receive-pack`, `/info/lfs/...`; the `?query` is stripped first):

```python
_GIT_PATH_SAFE = re.compile(r"\A[A-Za-z0-9._~/-]+\Z")


def _managed_repo_path_allows(request_path: str, repo_path: str) -> bool:
    """Whether a repo-scoped credential may attach to *request_path*.

    Fails closed via an allowlist: a legit git smart-HTTP path is plain ASCII
    within ``[A-Za-z0-9._~/-]`` (repo/namespace names + fixed endpoints; the
    ``?query`` is stripped first), so anything with ``%`` (any/double
    percent-encoding), ``\\``, ``;`` (matrix params), a control/null byte, or a
    non-ASCII char — every path a forge might normalize to escape the prefix —
    is rejected in one rule. Then the ``..``-segment and repo-prefix checks
    (bare and ``.git``) pin it to this repository.

    The prefix match is case-sensitive: a case-variant path on a
    case-insensitive forge declines the swap (fail-closed, functional-only —
    git uses the clone URL's exact case).
    """
    target = request_path.split("?", 1)[0]
    if not _GIT_PATH_SAFE.match(target):
        return False
    if ".." in target.split("/"):
        return False
    for base in (repo_path, f"{repo_path}.git"):
        if target == base or target.startswith(f"{base}/"):
            return True
    return False
```

  Change the `_rewrite_authorization` signature (~`:1084`) to take `path`:

```python
    def _rewrite_authorization(
        self, *, host: str, path: str, headers_raw: bytes
    ) -> _AuthRewriteResult:
```

  and replace the swap-on-access branch (the `elif host_rule is not None:` block, ~`:1145-1149`) with the
  path-aware form:

```python
        elif host_rule is not None and (
            host_rule.repo_path is None
            or _managed_repo_path_allows(path, host_rule.repo_path)
        ):
            # Swap-on-access: a bound host with no Authorization header. For a
            # repo-scoped rule (managed git) attach ONLY within the repo prefix
            # — a broad co-existing egress rule or a `..`/encoded traversal must
            # not steer the owner's token onto another repo. Out-of-scope paths
            # fall through untouched (tokenless), never leaking the credential.
            msg["Authorization"] = self._format_real_auth(host_rule)
            changed = True
```

  Update the docstring `:param:` block to add `path` and note the repo-scope gate. Then thread `path` at
  both call sites:
  - `_forward_https` (~`:679`): `rewrite = self._rewrite_authorization(host=host, path=path, headers_raw=headers_raw)`
  - `_handle_http` (~`:819`): `rewrite = self._rewrite_authorization(host=host, path=path, headers_raw=headers_raw)`

  (`path` is already the authorized request path in scope at both sites — the same value passed to
  `check_request`.)

- [ ] **Step 5: Run to verify the proxy tests pass**

Run: `env -u NODE_ENV uv run pytest tests/inner/egress/test_proxy.py -q`
Expected: PASS — the new path-scope tests plus every existing credential-rewrite test (operator rules
default `repo_path=None`, so their behavior is unchanged).

#### Part B — os_env install (path-aware rule + fail-closed)

- [ ] **Step 6: Write the failing os_env tests** — add to `tests/inner/test_os_env.py`:

```python
def test_install_managed_git_credential_appends_path_scoped_swap_rule() -> None:
    from omnigent.inner.credential_proxy import CredentialProxyRuntime
    from omnigent.inner.os_env import _install_managed_git_credential

    runtime = _install_managed_git_credential(
        None,
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        auth_scheme="basic",
        username="x-access-token",
        token="ghp_tok",
    )
    assert isinstance(runtime, CredentialProxyRuntime)
    rule = runtime.rewrites[0]
    assert rule.host == "git.acme.com"
    assert rule.real_secret == "ghp_tok"
    assert rule.synthetic is None  # swap-on-access: nothing enters the sandbox
    assert rule.repo_path == "/team/proj"  # path-aware


def test_merge_managed_git_egress_rules_scopes_to_repo_path() -> None:
    from omnigent.inner.os_env import _merge_managed_git_egress_rules

    rules = _merge_managed_git_egress_rules(
        None, canonical_host="git.acme.com", repo_path="/team/proj"
    )
    assert rules == [
        "* git.acme.com/team/proj/**",
        "* git.acme.com/team/proj.git/**",
    ]
    again = _merge_managed_git_egress_rules(
        rules, canonical_host="git.acme.com", repo_path="/team/proj"
    )
    assert again == rules  # idempotent


def test_apply_managed_git_credential_installs_rule_and_preserves_egress() -> None:
    from omnigent.inner.os_env import _apply_managed_git_credential

    runtime, egress = _apply_managed_git_credential(
        None,
        ["* github.com/**"],
        canonical_host="git.acme.com",
        repo_path="/team/proj",
        auth_scheme="basic",
        username="x-access-token",
        token="ghp_tok",
    )
    assert runtime.rewrites[0].repo_path == "/team/proj"
    assert "* git.acme.com/team/proj/**" in egress
    assert "* git.acme.com/team/proj.git/**" in egress
    assert "* github.com/**" in egress  # existing allowlist preserved


def test_apply_managed_git_credential_fails_closed_without_egress_allowlist() -> None:
    import pytest

    from omnigent.inner.os_env import ManagedGitCredentialError, _apply_managed_git_credential

    # #4 (user decision): a credential into a sandbox with NO egress allowlist
    # must NOT silently narrow the network — fail closed with an actionable msg.
    for empty in (None, []):
        with pytest.raises(ManagedGitCredentialError) as exc:
            _apply_managed_git_credential(
                None,
                empty,
                canonical_host="git.acme.com",
                repo_path="/team/proj",
                auth_scheme="basic",
                username=None,
                token="ghp_tok",
            )
        assert "egress allowlist" in str(exc.value)
        assert "ghp_tok" not in str(exc.value)


def test_apply_managed_git_credential_rejects_operator_host_conflict() -> None:
    import pytest

    from omnigent.inner.credential_proxy import CredentialProxyRuntime, CredentialRewriteRule
    from omnigent.inner.os_env import ManagedGitCredentialError, _apply_managed_git_credential

    operator = CredentialProxyRuntime(
        rewrites=[CredentialRewriteRule(host="git.acme.com", scheme="basic", real_secret="op")]
    )
    with pytest.raises(ManagedGitCredentialError) as exc:
        _apply_managed_git_credential(
            operator,
            ["* git.acme.com/**"],
            canonical_host="GIT.ACME.COM",  # case-insensitive clash
            repo_path="/team/proj",
            auth_scheme="basic",
            username=None,
            token="ghp_tok",
        )
    assert "ghp_tok" not in str(exc.value)
```

- [ ] **Step 7: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/inner/test_os_env.py -q -k "managed_git or apply_managed"`
Expected: FAIL — `ImportError: cannot import name '_apply_managed_git_credential'`.

- [ ] **Step 8: Add the os_env helpers + dedicated error** — in `omnigent/inner/os_env.py`, extend the
  `from omnigent.runner.identity import ...` block (~`:26`) with the `MANAGED_GIT_*` names, and add the
  error class + helpers near the other module-level helpers (e.g. after `_build_credential_proxy_parent_env`,
  ~`:230`):

```python
from omnigent.runner.identity import (
    MANAGED_GIT_AUTH_SCHEME_ENV_VAR,
    MANAGED_GIT_CANONICAL_HOST_ENV_VAR,
    MANAGED_GIT_REPO_PATH_ENV_VAR,
    MANAGED_GIT_TOKEN_ENV_VAR,
    MANAGED_GIT_USERNAME_ENV_VAR,
    strip_runner_auth_secrets,
)
```

```python
class ManagedGitCredentialError(ValueError):
    """A server-delivered git credential could not be installed in the sandbox.

    Raised for the deterministic misconfigurations the runner cannot recover
    from — no egress allowlist to scope the swap to, or a host already bound by
    an operator ``credential_proxy`` entry. A ``ValueError`` subclass so
    existing ``except ValueError`` paths still catch it; it surfaces as the
    session-failure reason via os_env's error path and names no token.
    """


def _install_managed_git_credential(
    runtime: CredentialProxyRuntime | None,
    *,
    canonical_host: str,
    repo_path: str,
    auth_scheme: str,
    username: str | None,
    token: str,
) -> CredentialProxyRuntime:
    """Append a path-scoped swap-on-access rewrite rule for a delivered token.

    ``synthetic=None`` means pure swap-on-access — nothing credential-shaped
    enters the sandbox; the proxy attaches the real token on the upstream leg
    of a tokenless request to :paramref:`canonical_host`. ``repo_path`` is set
    on the rule so the proxy attaches ONLY within the repo prefix (the rule is
    path-aware; see ``EgressProxy._rewrite_authorization``).

    Fails closed on a duplicate host: the proxy's ``_cred_by_host`` is
    last-wins, so silently clobbering an operator ``credential_proxy`` binding
    for the same host is refused here (the parser's duplicate guard does not
    cover this direct-mint path).

    :raises ManagedGitCredentialError: If a rewrite already binds this host.
    """
    if runtime is None:
        runtime = CredentialProxyRuntime()
    host_lower = canonical_host.lower()
    for existing in runtime.rewrites:
        if existing.host.lower() == host_lower:
            raise ManagedGitCredentialError(
                f"managed git credential for host {canonical_host!r} conflicts "
                "with an existing credential_proxy binding for the same host"
            )
    runtime.rewrites.append(
        CredentialRewriteRule(
            host=canonical_host,
            scheme=auth_scheme,
            real_secret=token,
            synthetic=None,
            username=username or None,
            repo_path=repo_path,
        )
    )
    return runtime


def _merge_managed_git_egress_rules(
    rules: list[str] | None,
    *,
    canonical_host: str,
    repo_path: str,
) -> list[str]:
    """Append repo-path-scoped egress allow-rules for a delivered credential.

    Defense-in-depth alongside the path-aware credential rule: the egress proxy
    is default-deny, so the git host+path must be allowed for git to leave at
    all; scoping the allow-rule to the repo prefix keeps that hole tight. Both
    the bare and ``.git`` path forms are emitted because forges accept either.

    :param rules: Existing egress rule strings.
    :param canonical_host: Host to scope to, e.g. ``"git.acme.com"``.
    :param repo_path: Repo path prefix (leading slash, no ``.git``/trailing
        slash), e.g. ``"/team/proj"``.
    :returns: The merged rule list (deduped, order-preserving).
    """
    merged = list(rules or [])
    for candidate in (
        f"* {canonical_host}{repo_path}/**",
        f"* {canonical_host}{repo_path}.git/**",
    ):
        if candidate not in merged:
            merged.append(candidate)
    return merged


def _apply_managed_git_credential(
    runtime: CredentialProxyRuntime | None,
    egress_rules: list[str] | None,
    *,
    canonical_host: str,
    repo_path: str,
    auth_scheme: str,
    username: str | None,
    token: str,
) -> tuple[CredentialProxyRuntime, list[str]]:
    """Install the swap rule + repo-scoped egress rule, or fail closed.

    A delivered credential is inert without an egress proxy, and starting the
    proxy on a sandbox that had NO egress allowlist would flip it from
    full-network to repo-only — a silent, surprising narrowing. Per the
    fail-closed decision, that case is refused with an actionable error rather
    than either narrowing the sandbox or auto-adding a broad rule (the
    preserve-broad-egress convenience is the P1d §11 merge-point's job).

    :raises ManagedGitCredentialError: When there is no existing egress
        allowlist, or a credential_proxy binding already covers this host.
    """
    if not egress_rules:
        raise ManagedGitCredentialError(
            "per-repo git credentials require an egress allowlist — "
            "add os_env.sandbox.egress_rules"
        )
    runtime = _install_managed_git_credential(
        runtime,
        canonical_host=canonical_host,
        repo_path=repo_path,
        auth_scheme=auth_scheme,
        username=username,
        token=token,
    )
    merged = _merge_managed_git_egress_rules(
        egress_rules, canonical_host=canonical_host, repo_path=repo_path
    )
    return runtime, merged
```

- [ ] **Step 9: Wire the seam in `_start_locked`** — inside `if sandbox.active:`, after the
  `if sandbox.credential_proxy is not None:` block (after `env.update(credential_runtime.helper_env_updates)`,
  ~`:443`) and **before** the `if self._egress_rules and self._tmpdir is not None:` check (~`:449`):

```python
            # A server-delivered managed-git credential (see
            # host.deliver_credential): the real token arrives in this
            # process's env, stripped from the sandbox helper's env. Install a
            # repo-path-scoped swap rule + egress allow-rule here so the egress
            # proxy below picks them up; the token never enters the sandbox. A
            # deterministic misconfig (no egress allowlist, or a host already
            # bound) raises ManagedGitCredentialError, which surfaces as the
            # session-failure reason via os_env's error path (finding #4/D).
            managed_token = os.environ.get(MANAGED_GIT_TOKEN_ENV_VAR)
            if managed_token:
                canonical_host = os.environ.get(MANAGED_GIT_CANONICAL_HOST_ENV_VAR, "")
                repo_path = os.environ.get(MANAGED_GIT_REPO_PATH_ENV_VAR, "")
                if canonical_host and repo_path:
                    credential_runtime, self._egress_rules = _apply_managed_git_credential(
                        credential_runtime,
                        self._egress_rules,
                        canonical_host=canonical_host,
                        repo_path=repo_path,
                        auth_scheme=os.environ.get(MANAGED_GIT_AUTH_SCHEME_ENV_VAR, "basic"),
                        username=os.environ.get(MANAGED_GIT_USERNAME_ENV_VAR),
                        token=managed_token,
                    )
```

  (`os` is already imported in `os_env.py`. The existing egress-proxy start at ~`:449` already threads
  `credential_runtime.rewrites` when `credential_runtime is not None`, so no change is needed there — the
  block above guarantees a non-``None`` runtime and non-empty `self._egress_rules` whenever a token is
  successfully applied.)

- [ ] **Step 10: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/inner/egress/test_proxy.py tests/inner/test_os_env.py -q`
Expected: PASS (proxy + os_env, including the existing credential-rewrite suite).

- [ ] **Step 11: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/inner/credential_proxy.py omnigent/inner/egress/proxy.py omnigent/inner/os_env.py tests/inner/egress/test_proxy.py tests/inner/test_os_env.py && env -u NODE_ENV uv run ruff format omnigent/inner/credential_proxy.py omnigent/inner/egress/proxy.py omnigent/inner/os_env.py tests/inner/egress/test_proxy.py tests/inner/test_os_env.py
git add omnigent/inner/credential_proxy.py omnigent/inner/egress/proxy.py omnigent/inner/os_env.py tests/inner/egress/test_proxy.py tests/inner/test_os_env.py
pre-commit run --files omnigent/inner/credential_proxy.py omnigent/inner/egress/proxy.py omnigent/inner/os_env.py tests/inner/egress/test_proxy.py tests/inner/test_os_env.py
git commit -m "feat(git-hosts): path-aware repo-scoped credential swap + fail-closed egress (P1c-4)"
```

---

### Task 6: Server-side delivery — resolve lease, seal, ACKed RPC, wire into launch

**Files:**
- Modify: `omnigent/server/host_registry.py` (`HostConnection.pending_credentials` ~`:218`)
- Modify: `omnigent/server/routes/host_tunnel.py` (`_receive_loop` — add a branch before the final
  `_logger.debug`, ~`:568`)
- Modify: `omnigent/server/routes/sessions.py` (`_deliver_credential_for_launch` + accounting +
  `_CREDENTIAL_DELIVERY_ERROR_CODE`; `_launch_runner_on_host` ~`:6371-6464`; thread `repo`/`owner`/
  `credential_store` from `_run_managed_launch` ~`:6497` → `_bind_and_launch_managed_runner` ~`:6665` →
  the `_launch_runner_on_host` call ~`:6732`; the `_bind_and_launch_managed_runner` credential-failure
  branch ~`:6738`)
- Test: `tests/server/test_managed_hosts.py` (unit-test the helper), `tests/server/integration/test_host_tunnel_route.py`
  (ACK-future resolution)

**Interfaces:**
- Consumes: `resolve_lease` + `CredentialLease` (P1c-3 Task 1); widened `RepoWorkspace`
  (`credential_slot_id`, `host_id`, `canonical_host`, `url`, `auth_scheme`, `clone_username` — P1c-3
  Tasks 2/3); `Conversation.launch_generation` (P1c-3 Task 5); `seal` (Task 1);
  `HostDeliverCredentialFrame` / `HostDeliverCredentialResultFrame` (Task 2); `HostConnection.hello`
  (carries `sealing_public_key`); `host_registry.send_text`.
- Produces:
  - `HostConnection.pending_credentials: dict[str, asyncio.Future[dict[str, Any]]]`.
  - `_CREDENTIAL_DELIVERY_ERROR_CODE = "credential_delivery_failed"`.
  - `_deliver_credential_for_launch(*, host_conn, host_registry, runner_id, launch_generation,
    session_id, repo, owner, credential_store) -> str | None` — returns `None` on success **or skip**
    (no slot / no store → backward-compat), and an error string on a fail-closed delivery failure.
  - `_launch_runner_on_host(conv, conversation_store, host_registry, host_conn, *, repo=None, owner=None,
    credential_store=None)` — delivers (and awaits the ACK) before the launch frame; a delivery failure
    returns `_HostLaunchAttempt(error_code=_CREDENTIAL_DELIVERY_ERROR_CODE)` **without** launching.

- [ ] **Step 1: Write the failing tests** — add to `tests/server/test_managed_hosts.py` (uses the
  `_cred_store` helper from P1c-3 Task 3 and `resolve_repo_workspace`; import
  `_deliver_credential_for_launch` and `_launch_runner_on_host` from `omnigent.server.routes.sessions`):

```python
class _FakeHostConn:
    def __init__(self, sealing_public_key):
        from omnigent.host.frames import HostHelloFrame

        self.hello = HostHelloFrame(
            version="0", frame_protocol_version=1, name="h",
            sealing_public_key=sealing_public_key,
        )
        self.pending_credentials = {}
        self.sent = []


class _FakeRegistry:
    def send_text(self, conn, data):
        conn.sent.append(data)


async def _resolve_pending(conn, *, status):
    # Mirror the host ACK the receive loop would resolve.
    import asyncio

    await asyncio.sleep(0)
    for req_id, fut in list(conn.pending_credentials.items()):
        if not fut.done():
            fut.set_result({"status": status, "error": None if status == "installed" else "no"})


@pytest.mark.asyncio
async def test_deliver_credential_for_launch_seals_and_acks(tmp_path) -> None:
    import asyncio

    from omnigent.host.frames import build_credential_delivery_aad, decode_host_frame
    from omnigent.host.sealing import generate_sealing_keypair, unseal
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    kp = generate_sealing_keypair()
    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="ghp_secret")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    conn = _FakeHostConn(kp.public_key_b64)
    registry = _FakeRegistry()
    task = asyncio.create_task(
        _deliver_credential_for_launch(
            host_conn=conn, host_registry=registry, runner_id="r1",
            launch_generation=1, session_id="conv_1", repo=repo,
            owner="alice", credential_store=store,
        )
    )
    await _resolve_pending(conn, status="installed")
    err = await task
    assert err is None
    # The frame carried a SEALED token, never plaintext.
    frame = decode_host_frame(conn.sent[0])
    assert "ghp_secret" not in conn.sent[0]
    aad = build_credential_delivery_aad(
        runner_id=frame.runner_id,
        launch_generation=frame.launch_generation,
        session_id=frame.session_id,
        host_id=frame.host_id,
        credential_slot=frame.credential_slot,
        canonical_host=frame.canonical_host,
        repo_path=frame.repo_path,
        credential_kind=frame.credential_kind,
        auth_scheme=frame.auth_scheme,
        username=frame.username,
    )
    assert unseal(frame.sealed_credential, private_key=kp.private_key, aad=aad) == "ghp_secret"
    assert frame.canonical_host == "git.acme.com"
    assert frame.repo_path == "/team/proj"
    assert frame.credential_kind == "http-token"


@pytest.mark.asyncio
async def test_deliver_credential_skips_when_no_slot(tmp_path) -> None:
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)  # no slot
    conn = _FakeHostConn("cHVi")
    registry = _FakeRegistry()
    err = await _deliver_credential_for_launch(
        host_conn=conn, host_registry=registry, runner_id="r1",
        launch_generation=1, session_id="conv_1", repo=repo,
        owner="alice", credential_store=None,
    )
    assert err is None
    assert conn.sent == []  # backward-compat: nothing sent


@pytest.mark.asyncio
async def test_deliver_credential_skips_ssh_repo_even_with_slot(tmp_path) -> None:
    from omnigent.host.sealing import generate_sealing_keypair
    from omnigent.server.managed_hosts import RepoWorkspace
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="ghp_secret")
    # P1c-3 slot selection is scheme-agnostic, so an SSH workspace can carry a
    # slot — but HTTP-token delivery must skip it cleanly (SSH is P1c-5), never
    # fail the launch closed or garble the scp-form URL.
    repo = RepoWorkspace(
        url="git@git.acme.com:team/proj.git",
        branch=None,
        repo_name="proj",
        host_id="acme",
        canonical_host="git.acme.com",
        credential_slot_id=slot.id,
        auth_scheme="basic",
    )
    conn = _FakeHostConn(generate_sealing_keypair().public_key_b64)
    err = await _deliver_credential_for_launch(
        host_conn=conn, host_registry=_FakeRegistry(), runner_id="r1",
        launch_generation=1, session_id="conv_1", repo=repo,
        owner="alice", credential_store=store,
    )
    assert err is None  # skipped cleanly, not failed closed
    assert conn.sent == []


@pytest.mark.asyncio
async def test_deliver_credential_fails_closed_when_host_cannot_seal(tmp_path) -> None:
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    store = _cred_store(tmp_path)
    store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                 label="work", username=None, token="ghp_secret")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    conn = _FakeHostConn(None)  # older host: no sealing key
    err = await _deliver_credential_for_launch(
        host_conn=conn, host_registry=_FakeRegistry(), runner_id="r1",
        launch_generation=1, session_id="conv_1", repo=repo,
        owner="alice", credential_store=store,
    )
    assert err is not None
    assert conn.sent == []
    assert "ghp_secret" not in err


@pytest.mark.asyncio
async def test_deliver_credential_fails_closed_when_slot_revoked(tmp_path) -> None:
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    store = _cred_store(tmp_path)
    slot = store.create(owner_user_id="alice", host_id="acme", provider="forgejo",
                        label="work", username=None, token="ghp_secret")
    repo = resolve_repo_workspace(
        "https://git.acme.com/team/proj", _GH_HOSTS,
        owner_user_id="alice", credential_store=store,
    )
    store.delete(slot.id)
    from omnigent.host.sealing import generate_sealing_keypair

    conn = _FakeHostConn(generate_sealing_keypair().public_key_b64)
    err = await _deliver_credential_for_launch(
        host_conn=conn, host_registry=_FakeRegistry(), runner_id="r1",
        launch_generation=1, session_id="conv_1", repo=repo,
        owner="alice", credential_store=store,
    )
    assert err is not None
    assert conn.sent == []
    assert "ghp_secret" not in err
```

- [ ] **Step 2: Run to verify failure**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py -q -k deliver_credential`
Expected: FAIL — `ImportError: cannot import name '_deliver_credential_for_launch'`.

- [ ] **Step 3: Add the pending dict** — in `omnigent/server/host_registry.py`, add to `HostConnection`
  after `pending_create_dirs` (~`:218`):

```python
    pending_credentials: dict[str, asyncio.Future[dict[str, Any]]] = field(
        default_factory=dict,
    )
```

  and a `:param:` line in the docstring:

```python
    :param pending_credentials: Per-``request_id`` futures for in-flight
        ``host.deliver_credential`` requests. Resolved when the host sends
        ``host.deliver_credential_result``. Values carry ``status`` /
        ``error``. Same ``Any`` typing rationale as ``pending_stats``.
```

- [ ] **Step 4: Resolve the ACK in `_receive_loop`** — in `omnigent/server/routes/host_tunnel.py`, add
  a branch before the final `_logger.debug("Host %s sent unexpected frame type ...)` (~`:568`). Extend the
  `from omnigent.host.frames import (...)` block at the top with `HostDeliverCredentialResultFrame`:

```python
        if isinstance(frame, HostDeliverCredentialResultFrame):
            cred_future = conn.pending_credentials.pop(frame.request_id, None)
            if cred_future is not None and not cred_future.done():
                cred_future.set_result({"status": frame.status, "error": frame.error})
            continue
```

- [ ] **Step 5: Add the delivery helper + error code** — in `omnigent/server/routes/sessions.py`, near
  `_HOST_LAUNCH_RESULT_TIMEOUT_S` (~`:627`):

```python
# Error code surfaced on a fail-closed credential delivery (an older host
# that can't seal, an unresolvable/revoked slot, a NACK, or a timeout). A
# credential-bound managed launch is aborted rather than running git
# tokenless — the launch fails closed.
_CREDENTIAL_DELIVERY_ERROR_CODE = "credential_delivery_failed"
_HOST_CREDENTIAL_RESULT_TIMEOUT_S = 10.0


def _managed_repo_path(url: str) -> str:
    """Derive the repo-path prefix an egress rule scopes to from a clone URL.

    :param url: The resolved clone URL, e.g. ``"https://git.acme.com/team/proj.git"``.
    :returns: A leading-slash path with no ``.git`` / trailing slash, e.g.
        ``"/team/proj"``; ``""`` when the URL carries no path.
    """
    from urllib.parse import urlparse

    path = urlparse(url).path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    return path


async def _deliver_credential_for_launch(
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
    repo_path = _managed_repo_path(repo.url)
    if not repo_path:
        return "could not derive a repository path for credential scoping"
    from omnigent.host.frames import (
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
    credential_kind = "http-token"
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
        result = await asyncio.wait_for(
            cred_future, timeout=_HOST_CREDENTIAL_RESULT_TIMEOUT_S
        )
    except asyncio.TimeoutError:
        host_conn.pending_credentials.pop(request_id, None)
        return "credential delivery to the managed host timed out"
    if result.get("status") != "installed":
        return "the managed host rejected the credential delivery"
    return None
```

  Ensure `GitCredentialStore` is importable in `sessions.py` (add to the imports if not already present):

```python
from omnigent.stores.git_credential_store import GitCredentialStore
```

- [ ] **Step 6: Deliver before launch in `_launch_runner_on_host`** — change the signature (~`:6371`)
  to accept the binding, and call the helper after `new_runner_id` is known (~`:6400`) but before the
  `pending_launches` registration (~`:6421`):

```python
async def _launch_runner_on_host(
    conv: Conversation,
    conversation_store: ConversationStore,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    *,
    repo: RepoWorkspace | None = None,
    owner: str | None = None,
    credential_store: GitCredentialStore | None = None,
) -> _HostLaunchAttempt:
```

  After `new_runner_id = token_bound_runner_id(binding_token)` and the `replace_runner_id` call, before
  `request_id = secrets.token_hex(8)` (~`:6421`):

```python
    # Fetch/push credential handoff (managed sandboxes): deliver + ACK BEFORE
    # the launch frame so the host caches the credential and threads it into
    # the runner at spawn. A credential-bound session that cannot be delivered
    # fails the launch closed (never silent tokenless git); a session with no
    # bound slot skips this and launches exactly as before.
    delivery_error = await _deliver_credential_for_launch(
        host_conn=host_conn,
        host_registry=host_registry,
        runner_id=new_runner_id,
        launch_generation=conv.launch_generation,
        session_id=conv.id,
        repo=repo,
        owner=owner,
        credential_store=credential_store,
    )
    if delivery_error is not None:
        _logger.warning(
            "Credential delivery failed for session %s on host %s: %s",
            conv.id,
            conv.host_id,
            delivery_error,
        )
        return _HostLaunchAttempt(
            runner_id=new_runner_id,
            error_code=_CREDENTIAL_DELIVERY_ERROR_CODE,
            error=delivery_error,
        )
```

- [ ] **Step 7: Thread `repo`/`owner`/`credential_store` to the call site.** In
  `_bind_and_launch_managed_runner` (~`:6665`), add the three params and pass them to
  `_launch_runner_on_host` (~`:6732`); in `_run_managed_launch` (~`:6569`) pass them through (it already
  has `owner` and `repo`; P1c-3 adds `credential_store` to `_run_managed_launch`).

  `_bind_and_launch_managed_runner` signature — add:

```python
    host_registry: HostRegistry | None,
    tunnel_registry: TunnelRegistry | None,
    repo: RepoWorkspace | None = None,
    owner: str | None = None,
    credential_store: GitCredentialStore | None = None,
) -> None:
```

  the call (~`:6732`):

```python
            launch_attempt = await _launch_runner_on_host(
                conv,
                conversation_store,
                host_registry,
                host_conn,
                repo=repo,
                owner=owner,
                credential_store=credential_store,
            )
```

  and the credential-failure branch, mirroring the harness-not-configured one right below it (~`:6738`):

```python
            if launch_attempt.error_code == _CREDENTIAL_DELIVERY_ERROR_CODE:
                reason = launch_attempt.error or "git credential delivery failed"
                tracker.fail(session_id, reason)
                _publish_sandbox_status(session_id, "failed", reason)
                return
```

  In `_run_managed_launch`, pass them into the `_bind_and_launch_managed_runner(...)` call (~`:6569`):

```python
    await _bind_and_launch_managed_runner(
        session_id=session_id,
        managed=managed,
        sandbox_config=sandbox_config,
        tracker=tracker,
        conversation_store=conversation_store,
        host_store=host_store,
        host_registry=host_registry,
        tunnel_registry=tunnel_registry,
        repo=repo,
        owner=owner,
        credential_store=credential_store,
    )
```

- [ ] **Step 8: Write the ACK-resolution integration test** — add to
  `tests/server/integration/test_host_tunnel_route.py`, directly mirroring the existing
  `test_host_tunnel_routes_launch_result_to_future` (~`:389`). Extend that file's
  `from omnigent.host.frames import (...)` block with `HostDeliverCredentialResultFrame`:

```python
async def test_host_tunnel_routes_credential_result_to_future(
    host_app: tuple[FastAPI, HostRegistry, HostStore],
) -> None:
    """A deliver_credential_result frame resolves the pending credential future.

    This is the mechanism by which _deliver_credential_for_launch awaits the
    host's ACK before it lets the launch proceed.
    """
    app, registry, _store = host_app
    comm = await _connect_route(app, _TUNNEL_PATH)
    await _send_hello_and_wait(comm, registry)

    conn = registry.get(_HOST_ID)
    assert conn is not None

    loop = asyncio.get_event_loop()
    future: asyncio.Future[dict[str, object]] = loop.create_future()
    conn.pending_credentials["req_cred"] = future

    result_frame = encode_host_frame(
        HostDeliverCredentialResultFrame(request_id="req_cred", status="installed")
    )
    await comm.send_input({"type": "websocket.receive", "text": result_frame})

    result = await asyncio.wait_for(future, timeout=2.0)
    assert result == {"status": "installed", "error": None}
```

- [ ] **Step 9: Run to verify pass**

Run: `env -u NODE_ENV uv run pytest tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py -q -k "credential or deliver"`
Expected: PASS.

- [ ] **Step 10: Lint + commit**

```bash
env -u NODE_ENV uv run ruff check --fix omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py && env -u NODE_ENV uv run ruff format omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py
git add omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py
pre-commit run --files omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py
git commit -m "feat(git-hosts): server-side sealed deliver_credential RPC wired into managed launch (P1c-4)"
```

---

### Task 7: End-to-end wiring, backward-compat proof, and the full gate

**Files:**
- Test: `tests/host/test_connect.py` (an end-to-end host→runner-env assertion),
  `tests/server/test_managed_hosts.py` (backward-compat)
- Verification only otherwise.

**Interfaces:**
- Consumes everything from Tasks 1–6. No new production symbols.

- [ ] **Step 1: Write the end-to-end + backward-compat tests.**

  In `tests/host/test_connect.py` — prove the full receiver chain (seal → deliver → cache → runner env,
  token child-stripped):

```python
def test_end_to_end_delivered_credential_reaches_runner_env_not_child() -> None:
    from omnigent.host.connect import _build_runner_env
    from omnigent.host.sealing import generate_sealing_keypair
    from omnigent.runner.identity import (
        MANAGED_GIT_TOKEN_ENV_VAR,
        strip_runner_auth_secrets,
    )

    proc = _host_process()
    proc._sealing_keypair = generate_sealing_keypair()
    proc._handle_deliver_credential(
        _managed_deliver_frame(
            proc._sealing_keypair.public_key_b64, runner_id="r1", generation=1, token="ghp_e2e"
        )
    )
    runner_env = _build_runner_env(
        {},
        server_url="https://x",
        runner_id="r1",
        binding_token="bt",
        workspace="/w",
        parent_pid=1,
        credential=proc._pending_credentials["r1"],
    )
    # The trusted runner receives the token...
    assert runner_env[MANAGED_GIT_TOKEN_ENV_VAR] == "ghp_e2e"
    # ...but the sandbox helper child never does.
    assert MANAGED_GIT_TOKEN_ENV_VAR not in strip_runner_auth_secrets(runner_env)
```

  In `tests/server/test_managed_hosts.py` — prove backward-compat (no slot -> no frame, github path
  untouched):

```python
@pytest.mark.asyncio
async def test_no_credential_delivery_for_github_default(tmp_path) -> None:
    from omnigent.host.sealing import generate_sealing_keypair
    from omnigent.server.routes.sessions import _deliver_credential_for_launch

    repo = resolve_repo_workspace("https://github.com/org/repo", _GH_HOSTS)
    conn = _FakeHostConn(generate_sealing_keypair().public_key_b64)
    err = await _deliver_credential_for_launch(
        host_conn=conn, host_registry=_FakeRegistry(), runner_id="r1",
        launch_generation=1, session_id="conv_1", repo=repo,
        owner="alice", credential_store=_cred_store(tmp_path),
    )
    assert err is None
    assert conn.sent == []  # no slot on the github default -> nothing delivered
```

- [ ] **Step 2: Run the touched-module suites**

Run: `env -u NODE_ENV uv run pytest tests/host/test_sealing.py tests/host/test_frames.py tests/host/test_connect.py tests/inner/test_os_env.py tests/server/test_managed_hosts.py tests/server/integration/test_host_tunnel_route.py -q`
Expected: PASS (all).

- [ ] **Step 3: Broader regression sweep** — the frame/env/proxy changes touch shared code; run the
  neighboring suites that exercise them:

Run: `env -u NODE_ENV uv run pytest tests/host tests/inner/test_credential_proxy.py tests/inner/test_os_env.py tests/stores/test_git_credential_store.py -q`
Expected: PASS.

- [ ] **Step 4: Full lint + type + pre-commit gate on every changed file**

Run:
```bash
env -u NODE_ENV uv run ruff check omnigent/host/sealing.py omnigent/host/frames.py omnigent/host/connect.py omnigent/runner/identity.py omnigent/inner/os_env.py omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py
env -u NODE_ENV uv run ruff format --check omnigent/host/sealing.py omnigent/host/frames.py omnigent/host/connect.py omnigent/runner/identity.py omnigent/inner/os_env.py omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py
pre-commit run --files omnigent/host/sealing.py omnigent/host/frames.py omnigent/host/connect.py omnigent/runner/identity.py omnigent/inner/os_env.py omnigent/server/host_registry.py omnigent/server/routes/host_tunnel.py omnigent/server/routes/sessions.py
```
Expected: all clean (no lint errors, formatting already applied, hooks pass).

- [ ] **Step 5: Commit any final test-only additions**

```bash
git add tests/host/test_connect.py tests/server/test_managed_hosts.py
pre-commit run --files tests/host/test_connect.py tests/server/test_managed_hosts.py
git commit -m "test(git-hosts): end-to-end + backward-compat proofs for credential delivery (P1c-4)"
```

---

## What this plan does NOT do (next slices / explicitly deferred)

- **k8s + init-container clone (P1c-5):** k8s has no parent-side egress proxy (the runner→helper→bwrap
  tree runs in-Pod); the swap layer must run inside the Pod, and the init-container clone is the *first*
  git op, predating the runner. Separate slice.
- **SSH ssh-agent (P1c-5):** the HTTP rewrite proxy cannot carry an SSH key. The `credential_kind`
  envelope reserves `"ssh-key"` so no frame redesign is needed; the host rejects it for now.
- **tmux terminal swap (P1c-5):** a human/agent typing `git push` in the interactive terminal is a new
  auth surface requiring a deliberate decision; not wired.
- **Commit identity + sharing notice (P1c-6):** `GIT_AUTHOR_*` / `GIT_COMMITTER_*` and the §8.7 notice.
- **OAuth minting/refresh (P3):** only `http-token` is delivered; `oauth` is a reserved envelope value.
- **Server-driven push-revocation:** `host.invalidate_credential` is contract-only (enum + frame + host
  handler that discards the cache). No server sender is built — that is the named follow-up seam.
  Operational revocation in P1 is kill+relaunch.
- **The full §11 egress merge-point component (P1d):** P1c-4 appends only the resolved host's
  repo-scoped allow-rule at the runner seam, and **fails closed** when the sandbox has no egress allowlist
  (rather than narrowing it). The operator/agent egress composition with immutable precedence +
  post-merge revalidation — and the "preserve a pre-existing broad egress while adding the repo credential"
  convenience — is P1d.
- **The directly-connected `omni host` launch path** (`sessions.py:14676`, caller-supplied `host_id`):
  per §8.5, `/v1/git-credentials` does not configure `omni host`; server credential delivery is scoped to
  **managed** sandboxes (the `_run_managed_launch` → `_launch_runner_on_host` path). External hosts use a
  host-local per-git-host credential config (a separate item).

## Self-Review (controller, before dispatch)

**§8.5 v4 bullet-by-bullet coverage:**
1. *Dedicated versioned frame over the host tunnel, consumed by the trusted runner parent, installs a
   repo-path-scoped rewrite rule WITHOUT converting the PAT into env/file/command policy or a real
   `GIT_TOKEN`* → Task 2 (frame), Task 3 (host consumes), Task 5 (`synthetic=None` swap rule, no
   `CredentialSourceSpec`, no `GIT_TOKEN`), Task 4 (a dedicated child-stripped var, NOT
   `_BASE_HARNESS_CREDENTIAL_ENV_VARS`). ✔
2. *Separate from the launch frame* → Task 2 (dedicated `host.deliver_credential`). ✔
3. *ACKed RPC; runner confirms before git is permitted; ACK drives single-delivery accounting* → Task 6
   (server awaits the ACK before sending launch), Task 3 (host ACKs). "Git gated until rule installed" is
   **structural**: delivery is pre-spawn, and the runner's os_env installs the rule (Task 5) before it
   spawns the git-capable sandbox helper — see the resolved-ambiguity note. Single-delivery is
   **structural** (`_launch_runner_on_host` calls the delivery helper exactly once per launch) plus the
   host's own runner-already-live / generation-conflict NACK (Task 3) — no server-side accounting set. ✔
4. *type-tagged `{http-token|ssh-key|oauth}`, keyed by `{credential_slot, canonical_host}`, multi-host = N
   rules* → Task 2 (`credential_kind`, `credential_slot`, `canonical_host`; http-token only, ssh-key/oauth
   reserved+rejected). Multi-host-in-one-session: **flagged** below (structurally supported by keying;
   P1c-4 delivers the single resolved repo's credential). ✔ (with flag)
5. *bound to `{host_id, runner_id, launch_generation, session_id, credential_slot, canonical_host,
   repo_path}`; `launch_generation` anti-replay anchor* → Task 2 (all fields on the frame **and** folded
   into the AEAD AAD via `build_credential_delivery_aad`), Task 3 (host validates runner_id-not-live +
   generation-consistency **and** rebuilds the AAD from the received frame — a tamper/replay fails the
   ChaCha20-Poly1305 tag), Task 6 (server seals with the same AAD, populated from `repo` +
   `conv.launch_generation`). The binding is thus both validated (plaintext checks) and cryptographic
   (AAD), so a sealed blob cannot be lifted onto a different runner/generation/host/repo. ✔
6. *Sealed to a runner-held key established at launch; pluggable; never in logs/telemetry; best-effort
   zeroization* → Task 1 (pluggable seal/unseal), Task 2 (`sealed_credential` redacted), Task 3 (unseal in
   the trusted host). **Deviation flagged:** the key is **host-daemon-held, established at tunnel hello**,
   not runner-held-at-launch — justified below. ✔ (with flag)
7. *Launch-scoped lifecycle: single-delivery; kept across a transient tunnel reconnect; re-authorized +
   re-delivered on relaunch; discarded on runner exit/stop/timeout* → Task 3 (discard on all three
   runner-exit paths + the launch-failure path; the cache survives a tunnel reconnect because the host
   process does), Task 6 (re-delivery per launch call — every relaunch re-runs `_launch_runner_on_host`;
   single-delivery is structural + host NACK). "timeout" = a runner that times out exits →
   `_watch_runner` discard. ✔
8. *Paired `invalidate_credential` defined now (push-revoke later); revocation kill+relaunch* → Task 2
   (frame), Task 3 (one-way host handler discards the cache). No server sender (named follow-up). ✔
9. *Repo-path scoping — the rewrite RULE matches the specific repo-path prefix* → Task 5. **Satisfied
   directly**: `CredentialRewriteRule.repo_path` makes the swap path-aware —
   `_rewrite_authorization` attaches the token only when the request path is within the repo prefix (bare
   or `.git`), rejecting `..` dot-segments and percent-encoded traversal. The repo-scoped egress allow-rule
   is kept as defense-in-depth. This closes the two bypasses (normalization `..`; a broad coexisting
   `* <host>/**` egress rule re-opening the host). ✔
10. *Accepted residual — PAT in trusted runner-parent memory for the launch* → documented (Task 3 cache +
    Task 5 rewrite rule). Matches the spec's accepted residual. ✔
11. *SSH-key a separate item; envelope carries `ssh-key`* → Task 2 reserves the value; Task 6 skips SSH
    workspaces cleanly (`urlparse(repo.url).scheme` gate) so an SSH slot never fails the HTTP launch. ✔
12. *Runner placeholder path only; do NOT add real tokens to `_BASE_HARNESS_CREDENTIAL_ENV_VARS`* →
    Task 4 leaves that frozenset unchanged; the managed token is an explicit child-stripped var. ✔
13. *Egress coupling — auto-merge the host's egress rule* → Task 5 (`_merge_managed_git_egress_rules`). A
    sandbox with **no** egress allowlist **fails closed** (`ManagedGitCredentialError`) rather than being
    silently narrowed from full-network to repo-only (user decision #4). Scoped minimally (resolved host
    only); the full §11 merge-point + preserve-broad-egress convenience is P1d. ✔
14. *k8s / init-clone / tmux / SSH deferred to separate slices* → "does NOT do". ✔
15. *Long-lived `omni host` uses host-local config; `/v1/git-credentials` does not configure it* → scoped
    to managed sandboxes; the directly-connected host path is explicitly out (stated). ✔

**Placeholder scan:** every novel unit ships complete code — the sealing module +
`build_credential_delivery_aad`, the three frames + encode/decode/match, `_DeliveredCredential`,
`_handle_deliver_credential` / `_handle_invalidate_credential`, the `_build_runner_env` threading,
`_managed_repo_path_allows` + the path-aware `_rewrite_authorization`, `ManagedGitCredentialError` +
`_install_managed_git_credential` / `_merge_managed_git_egress_rules` / `_apply_managed_git_credential`,
`_deliver_credential_for_launch` / `_managed_repo_path`. The only "mirror the neighbor" directions are for
repo-specific test harnesses whose exact shape is dictated by the file: the `test_host_tunnel_route.py`
receive-loop harness (Task 6 Step 8, given complete against the existing `_connect_route` /
`_send_hello_and_wait` helpers) and the `HostIdentity` / `EgressProxy` construction idioms at the top of
`test_connect.py` / `test_proxy.py` — all name the exact assertion to make.

**Type consistency across tasks:** `_DeliveredCredential{token, launch_generation, session_id,
credential_slot, canonical_host, repo_path, auth_scheme, username}` is defined in Task 3 and consumed
identically in Tasks 4 and 7. `HostDeliverCredentialFrame` field names are identical in Task 2 (dataclass +
encode + decode), Task 3 (host handler reads), and Task 6 (server builds). `seal(plaintext, *,
recipient_public_key_b64, aad=b"")` / `unseal(sealed_b64, *, private_key, aad=b"")` identical in Task 1,
Task 3 (unseal), and Task 6 (seal); `build_credential_delivery_aad(...)` produces the identical AAD bytes
from the same field set in Task 6 (server), Task 3 (host), and both tasks' tests. The `MANAGED_GIT_*` env-var names are identical in Task 4 (define + set), Task 5 (read), and
Task 7. `_deliver_credential_for_launch(*, host_conn, host_registry, runner_id, launch_generation,
session_id, repo, owner, credential_store) -> str | None` identical in Task 6 impl and its
`_launch_runner_on_host` caller. `pending_credentials` identical in Task 6's `HostConnection` field and the
`_receive_loop` branch. `CredentialRewriteRule.repo_path` set in Task 5's os_env install and read by Task
5's `_rewrite_authorization`; `_managed_repo_path_allows(request_path, repo_path)` identical in the proxy
impl and its tests; `ManagedGitCredentialError` / `_apply_managed_git_credential` identical in the os_env
impl, the seam call, and the tests. Consumed P1c-3 symbols (`resolve_lease`, `CredentialLease`,
`RepoWorkspace` fields, `Conversation.launch_generation`) are used exactly as their committed interfaces
declare.

## Design ambiguities resolved (for the report)

1. **Sealing key is host-daemon-held, established at `host.hello` (not "runner-held at launch").** §8.5
   says "encrypted to a runner-held key established at launch." Grounded architecture A terminates the
   tunnel at the **host daemon**, and the runner process does not exist at pre-spawn delivery time — so a
   runner-held-at-launch key is not reachable by the sealer. Resolved: the host daemon generates one
   X25519 keypair **per tunnel connection** and advertises the public key on `host.hello` (the only
   host→server frame that reliably precedes any launch; `launch_runner_result` is too late for pre-spawn
   delivery). The confidentiality goal is fully met — the token is sealed server→host-daemon and only
   unsealed inside the trusted host daemon, never crossing the tunnel in cleartext even on `ws://`. Both
   the host daemon and the runner are inside the trust boundary (above the sandbox); the seal protects the
   transit hop.
2. **Pre-spawn delivery ordering.** The server sends `deliver_credential` and **awaits its ACK** before
   sending `launch_runner` (both in `_launch_runner_on_host`, using the same `runner_id =
   token_bound_runner_id(binding_token)`). The host caches on delivery, so the credential is present
   before `_handle_launch` runs. "Git cannot precede the rule" is then structural, not timing-dependent:
   the runner's os_env installs the swap + egress rule (Task 5) **before** it spawns the git-capable
   sandbox helper.
3. **Repo-path scoping is enforced by the credential RULE itself (path-aware), with the egress allow-rule
   as defense-in-depth.** The first draft relied on the egress allow-rule alone; a code-verified review
   found that bypassable two ways — (a) `rules.py` does no `..` normalization, so `/team/proj/../secret`
   matches `/team/proj/**`, passes egress, and the forge normalizes it onto another repo; (b) any broad
   coexisting `* <host>/**` egress rule re-opens the whole host because `_cred_by_host` is host-keyed.
   Resolved by adding `CredentialRewriteRule.repo_path` and making `_rewrite_authorization` attach the
   token only when the request path is within the repo prefix (bare or `.git`) with no `..`/percent-encoded
   traversal — so the swap withholds the credential from any non-repo path regardless of what egress
   allowed. The repo-scoped egress allow-rule is kept too (git is default-deny, so the host+path must be
   allowed to leave at all). Operator credentials keep `repo_path=None` → host-scoped, byte-identical.
4. **Delivery is scoped to the managed-sandbox launch chokepoint** (`_launch_runner_on_host`, reached from
   `_run_managed_launch`/`_bind_and_launch_managed_runner`), not the directly-connected-host create path
   (`sessions.py:14676`). Justification: §8.5 confines server credential delivery to managed sandboxes;
   `omni host` uses host-local credential config. `repo` (with `credential_slot_id`) + `owner` flow
   through the managed path.
5. **Invalidate is one-way (no result frame).** The spec lists `deliver_credential` *with* a `_result`
   pair but `invalidate_credential` *without* one — read as: invalidate is contract-only, one-way, host
   discards the cache. No ACK is built (nothing consumes it in P1).
6. **No server-side single-delivery accounting.** An earlier draft kept an unbounded module-global
   `set[(session_id, launch_generation)]`; a review flagged it as unbounded-growth + a fragile fail-closed
   edge. Removed: single-delivery is **structural** (`_launch_runner_on_host` calls the delivery helper
   exactly once per launch) and enforced host-side (Task 3 NACKs a runner already live or a
   generation-conflict), so the set was redundant.
7. **`frame_protocol_version` is not bumped.** New kinds degrade safely (an older peer drops an unknown
   kind → the launch fails closed for credential-bound sessions); the `sealing_public_key` hello field is
   additive and ignored by older servers. A major-version bump (which the hello handshake refuses on
   mismatch) is unwarranted and would break rolling upgrades.
8. **Frame identity is bound into the AEAD associated data (AAD), not left to plaintext validation
   alone.** The sealed blob carries only the token; the binding tuple travels as plaintext frame fields.
   To make those fields tamper-evident and stop a sealed blob being lifted onto a different frame, the
   server seals with `aad = build_credential_delivery_aad({runner_id, launch_generation, session_id,
   host_id, credential_slot, canonical_host, repo_path, credential_kind, auth_scheme, username})` and the
   host unseals with the AAD rebuilt from the received frame. Any mismatch fails the ChaCha20-Poly1305 tag
   → reject. This is belt-and-suspenders with (a) the per-connection ephemeral recipient key (a blob from
   a prior connection is undecryptable) and (b) the host's plaintext binding checks (runner-not-live +
   generation-consistency).
9. **Path-aware swap fails closed toward "withhold the token", not 403.** For a repo-scoped rule a request
   path that isn't provably inside the repo prefix (a `..`/encoded traversal, or an out-of-prefix path a
   broad egress rule let through) simply does **not** get the token — the request proceeds tokenless rather
   than 403. This is uniform and non-disruptive (legitimate non-git traffic to the host still works), and
   it fully prevents the token reaching another repo (a tokenless fetch of a private repo 401s; a public
   one uses no secret). Chosen over a hard 403 so a deliberately broad operator egress rule for non-git
   host traffic is not broken.
10. **No egress allowlist → fail closed (user decision #4).** Installing the swap requires starting the
    egress proxy; doing that on a sandbox that had *no* egress allowlist would flip it from full-network to
    repo-only — a silent narrowing. Rather than narrow or auto-add a broad rule, the launch fails with
    `ManagedGitCredentialError("per-repo git credentials require an egress allowlist — add
    os_env.sandbox.egress_rules")`. The preserve-broad-egress convenience is deferred to the P1d §11
    merge-point.
11. **SSH workspaces are skipped, not failed (finding B).** P1c-3 slot selection is scheme-agnostic, so a
    `git@host:` / `ssh://` workspace can carry a `credential_slot_id`. `_deliver_credential_for_launch`
    gates on `urlparse(repo.url).scheme in ("http","https")` and returns cleanly for anything else, so an
    SSH repo neither garbles `_managed_repo_path` nor fails the launch closed over an HTTP credential it
    never needed. SSH is the P1c-5 ssh-agent path.
12. **Wire version byte is embedded and authenticated (review HIGH).** `SEAL_VERSION` was defined but
    absent from the envelope. A `_SEAL_VERSION_TAG` byte is now prepended to every envelope **and** folded
    into the AEAD AAD, so `unseal` rejects an unknown version and a strip/downgrade fails the tag.

## Flagged (could not fully satisfy here / needs a reviewer's eye)

- **Repo-path binding (§8.5 "repo-path-scoped rewrite rule") — RESOLVED.** Enforced by the path-aware
  credential rule itself (`CredentialRewriteRule.repo_path` + `_rewrite_authorization`), which withholds
  the token from any path outside the repo prefix — independent of egress. The path boundary
  (`_managed_repo_path_allows`) uses a **positive allowlist** (`[A-Za-z0-9._~/-]` only) rather than a
  blocklist, so `%` (single/double percent-encoding), `\`, `;` (matrix params), control/null bytes, and
  unicode are rejected in one provably-complete rule (legit git smart-HTTP paths are entirely within that
  charset). The egress merge-point that governs whether a broad operator rule may coexist is still
  **P1d (§11)** — but a broad rule no longer leaks the token, it only widens plain network reach.
- **Host-daemon-held sealing key vs §8.5 "runner-held key established at launch"** (ambiguity #1). The key
  is per-tunnel-**connection**, generated by the host daemon (the trusted runner-parent that terminates the
  tunnel) and advertised on `host.hello`; the runner does not exist at pre-spawn delivery, so a
  runner-held-at-launch key is unreachable. Confidentiality is fully met (unseal happens only inside the
  trusted host daemon). Flagged as a deliberate, justified deviation — confirm acceptance.
- **Multi-host-in-one-session.** The frame keying (`{credential_slot, canonical_host}`) and the host cache
  (per `runner_id`, one `_DeliveredCredential`) are shaped for N credentials, but P1c-4 delivers exactly
  **one** — the single resolved workspace repo's credential. A session that touches a *second* managed
  forge would need a second `deliver_credential` (and the host cache widened to a list/dict per runner).
  Not built (P1c-4 has a single-repo workspace); the frame contract does not need to change to add it.
- **Wake-path re-delivery.** `_launch_runner_on_host` is also called on the wake path
  (`sessions.py:~7197`). If that path does not thread a re-authorized `repo` (P1c-3's
  `reauthorize_relaunch_binding` runs on relaunch, and wake deliberately does not bump `launch_generation`),
  `repo` is `None` there → **no delivery on wake** → the woken runner has no fetch/push credential. Flagged
  as a gap to close when the wake path carries the binding; it does not affect first-launch or relaunch.
- **Multi-tenant workspace scope at delivery** (inherited from P1c-3's own flag). `resolve_lease` is
  workspace-ambient; the background launch task calls it in `_deliver_credential_for_launch` without
  entering a `workspace_scope`. Single-tenant (workspace 0) is correct; a multi-tenant deployment storing
  credentials under a non-default workspace needs the session workspace threaded into the launch task.
  Same deferral P1c-3 documented for `_build_clone_env`.
- **Deliver-without-launch residual.** If the server delivers but then never sends `launch_runner` (e.g.
  it crashes between the ACK and the launch send), the host's cached `_DeliveredCredential` lingers until
  the daemon exits (`_cleanup_runners`) — the three discard paths key on *runner exit*, and no runner was
  spawned. Minor: an unsealed token sits in host memory for a runner that never launched. A per-entry TTL
  or the (future) `invalidate_credential` sender would close it; not built (YAGNI). Launch *failures* after
  delivery already pop the entry (Task 4 Step 5).
