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


def _derive_key(shared_secret: bytes, ephemeral_public: bytes, recipient_public: bytes) -> bytes:
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
    return base64.b64encode(_SEAL_VERSION_TAG + ephemeral_public + nonce + ciphertext).decode(
        "ascii"
    )


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
