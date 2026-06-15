"""Resolve a probe credential for a :class:`ProviderAccount`.

Subscriptions keep their OAuth token in an isolated CLI config dir
(Claude: ``<dir>/.credentials.json``; Codex: ``<dir>/auth.json``); API
keys resolve through the existing secret store. The proactive poller uses
these to authenticate its headroom probe. All loaders are best-effort:
any failure returns ``None`` so a probe is simply skipped.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from omnigent.errors import OmnigentError
from omnigent.onboarding.provider_config import resolve_secret
from omnigent.subscription_tokens.domain.entities.provider_account import ProviderAccount

_DEFAULT_CLAUDE_DIR = "~/.claude"
_DEFAULT_CODEX_DIR = "~/.codex"


def _read_json(path: Path) -> dict[str, object] | None:
    """Read and parse a JSON object from *path*, or ``None`` on any error."""
    try:
        with path.expanduser().open(encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    return cast("dict[str, object]", data) if isinstance(data, dict) else None


def _nested_str(data: dict[str, object], outer: str, inner: str) -> str | None:
    """Return ``data[outer][inner]`` as a string, if present."""
    block = data.get(outer)
    if isinstance(block, dict):
        value = block.get(inner)
        if isinstance(value, str):
            return value
    return None


def load_subscription_token(account: ProviderAccount) -> str | None:
    """Load the OAuth access token for a subscription *account*.

    :returns: The bearer token read from the account's isolated config
        dir, or ``None`` when absent/unreadable.
    """
    if account.family == "anthropic":
        base = account.claude_config_dir or _DEFAULT_CLAUDE_DIR
        data = _read_json(Path(base) / ".credentials.json")
        if data is None:
            return None
        token = _nested_str(data, "claudeAiOauth", "accessToken")
        if token:
            return token
        direct = data.get("accessToken")
        return direct if isinstance(direct, str) else None

    base = account.codex_config_dir or _DEFAULT_CODEX_DIR
    data = _read_json(Path(base) / "auth.json")
    if data is None:
        return None
    token = _nested_str(data, "tokens", "access_token")
    if token:
        return token
    direct = data.get("OPENAI_API_KEY")
    return direct if isinstance(direct, str) else None


def resolve_account_api_key(account: ProviderAccount) -> str | None:
    """Resolve the API key for an api_key *account*, or ``None``.

    :returns: The resolved secret, or ``None`` when the account has no
        ``api_key_ref`` or the reference cannot be resolved.
    """
    if not account.api_key_ref:
        return None
    try:
        return resolve_secret(account.api_key_ref)
    except OmnigentError:
        return None
