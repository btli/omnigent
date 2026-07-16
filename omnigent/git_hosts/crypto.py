"""Encrypt/decrypt per-user git credentials at rest with a rotatable key list.

Uses :class:`cryptography.fernet.MultiFernet`: the first key in the list
encrypts new tokens, and any key in the list can decrypt — so rotating a key
means prepending a new key while retaining the old one until re-encryption.
The key list is operator-provided out-of-band (``OMNIGENT_GIT_CREDENTIAL_KEYS``);
this module never persists a key.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from cryptography.fernet import Fernet, MultiFernet

_ENV_VAR = "OMNIGENT_GIT_CREDENTIAL_KEYS"


class GitCredentialCipher:
    """Symmetric encrypt/decrypt over a rotatable Fernet key list.

    :param keys: One or more urlsafe-base64 Fernet keys; the first encrypts.
    :raises ValueError: When *keys* is empty or a key is malformed.
    """

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ValueError("at least one Fernet key is required")
        # Fernet(...) raises ValueError on a malformed key.
        self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])

    def encrypt(self, plaintext: str) -> str:
        """Return the ciphertext token for *plaintext* (encrypted with the first key)."""
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, token: str) -> str:
        """Return the plaintext for *token*.

        :raises cryptography.fernet.InvalidToken: When no key can decrypt it.
        """
        return self._fernet.decrypt(token.encode()).decode()


def load_cipher_from_env(env: Mapping[str, str] | None = None) -> GitCredentialCipher | None:
    """Build a cipher from ``OMNIGENT_GIT_CREDENTIAL_KEYS``, or ``None`` if unset.

    :param env: Environment mapping (defaults to ``os.environ``).
    :returns: A :class:`GitCredentialCipher`, or ``None`` when the var is unset/blank
        (the credential store is then disabled).
    :raises RuntimeError: When the var is set but contains a malformed key.
    """
    source = os.environ if env is None else env
    raw = source.get(_ENV_VAR, "").strip()
    if not raw:
        return None
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    try:
        return GitCredentialCipher(keys)
    except ValueError as exc:
        raise RuntimeError(
            f"{_ENV_VAR} contains an invalid Fernet key; each comma-separated entry must be "
            "a urlsafe-base64 32-byte key (generate with "
            "`python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'`)"
        ) from exc
