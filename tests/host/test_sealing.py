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
