"""Parse the ``pools:`` block of ``~/.omnigent/config.yaml``.

The multi-subscription feature adds a top-level ``pools:`` key alongside
the existing ``providers:`` block (which :mod:`provider_config` owns and
which is left untouched). Each pool is family-scoped and lists member
credentials; this module turns that YAML into the domain entities
(:class:`CredentialPool` / :class:`ProviderAccount`) the rest of the
package operates on.

Account and pool ids are derived deterministically from names (not random)
so that re-parsing the same config — e.g. at every server start, to sync
into the DB — yields stable ids that match existing rows.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import cast

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.subscription_tokens.domain.entities.credential_pool import CredentialPool
from omnigent.subscription_tokens.domain.entities.provider_account import ProviderAccount
from omnigent.subscription_tokens.domain.value_objects.enums import (
    MAX_HEADROOM,
    NOTIFY,
    VALID_ACCOUNT_KINDS,
    VALID_FAILOVER_MODES,
    VALID_FAMILIES,
    VALID_ROTATION_MODES,
    AccountKind,
    FailoverMode,
    Family,
    RotationMode,
)

POOLS_CONFIG_KEY = "pools"


def _err(message: str) -> OmnigentError:
    """Build an ``INVALID_INPUT`` :class:`OmnigentError` for a config fault."""
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _stable_id(prefix: str, *parts: str) -> str:
    """Return a deterministic ``{prefix}_{hex}`` id derived from *parts*.

    Uses SHA-1 (not Python's randomised ``hash``) so the id is stable
    across processes — required for idempotent config→DB sync.
    """
    digest = hashlib.sha1("/".join(parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:16]}"


def pool_id_for(name: str) -> str:
    """Return the deterministic id for a pool named *name*."""
    return _stable_id("pool", name)


def account_id_for(pool_name: str, member_name: str) -> str:
    """Return the deterministic id for *member_name* within *pool_name*."""
    return _stable_id("pacct", pool_name, member_name)


def _require_mapping(value: object, what: str) -> dict[str, object]:
    """Return *value* as a dict, or raise if it is not a mapping."""
    if not isinstance(value, dict):
        raise _err(f"{what} must be a mapping, got {type(value).__name__}.")
    return cast("dict[str, object]", value)


def _parse_family(raw: object, pool_name: str) -> Family:
    """Validate and return a pool's ``family``."""
    if raw not in VALID_FAMILIES:
        valid = ", ".join(VALID_FAMILIES)
        raise _err(f"pool {pool_name!r}: 'family' must be one of {valid}; got {raw!r}.")
    return cast("Family", raw)


def _parse_failover(raw: object, pool_name: str) -> FailoverMode:
    """Validate and return a pool's ``failover`` mode (default ``notify``)."""
    if raw is None:
        return NOTIFY
    if raw not in VALID_FAILOVER_MODES:
        valid = ", ".join(VALID_FAILOVER_MODES)
        raise _err(f"pool {pool_name!r}: 'failover' must be one of {valid}; got {raw!r}.")
    return cast("FailoverMode", raw)


def _parse_rotation(raw: object, pool_name: str) -> RotationMode:
    """Validate and return a pool's ``rotation`` mode (default ``max_headroom``)."""
    if raw is None:
        return MAX_HEADROOM
    if raw not in VALID_ROTATION_MODES:
        valid = ", ".join(VALID_ROTATION_MODES)
        raise _err(f"pool {pool_name!r}: 'rotation' must be one of {valid}; got {raw!r}.")
    return cast("RotationMode", raw)


def _parse_kind(raw: object, pool_name: str, member_name: str) -> AccountKind:
    """Validate and return a member's ``kind`` (default ``subscription``)."""
    if raw is None:
        return "subscription"
    if raw not in VALID_ACCOUNT_KINDS:
        valid = ", ".join(VALID_ACCOUNT_KINDS)
        raise _err(
            f"pool {pool_name!r} member {member_name!r}: 'kind' must be one of "
            f"{valid}; got {raw!r}."
        )
    return cast("AccountKind", raw)


def _parse_priority(raw: object, pool_name: str, member_name: str) -> int:
    """Validate and return a member's integer ``priority`` (default ``0``)."""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise _err(
            f"pool {pool_name!r} member {member_name!r}: 'priority' must be an "
            f"integer; got {raw!r}."
        )
    return raw


def _opt_str(raw: object, field: str, pool_name: str, member_name: str) -> str | None:
    """Return *raw* as a string for *field*, or ``None`` when absent."""
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise _err(
            f"pool {pool_name!r} member {member_name!r}: {field!r} must be a "
            f"string; got {type(raw).__name__}."
        )
    return raw


def _parse_member(
    raw: object, pool_name: str, pool_family: Family, seen_names: set[str]
) -> ProviderAccount:
    """Parse one pool member into a :class:`ProviderAccount`.

    :raises OmnigentError: On any missing/invalid field, a duplicate name
        within the pool, a config dir that does not match the pool family,
        or an api_key member missing its ``api_key_ref``.
    """
    member = _require_mapping(raw, f"pool {pool_name!r} member")
    name = member.get("name")
    if not isinstance(name, str) or not name:
        raise _err(f"pool {pool_name!r}: every member needs a non-empty string 'name'.")
    if name in seen_names:
        raise _err(f"pool {pool_name!r}: duplicate member name {name!r}.")
    seen_names.add(name)

    kind = _parse_kind(member.get("kind"), pool_name, name)
    priority = _parse_priority(member.get("priority"), pool_name, name)
    claude_dir = _opt_str(member.get("claude_config_dir"), "claude_config_dir", pool_name, name)
    codex_dir = _opt_str(member.get("codex_config_dir"), "codex_config_dir", pool_name, name)
    api_key_ref = _opt_str(member.get("api_key_ref"), "api_key_ref", pool_name, name)
    oauth_token_ref = _opt_str(member.get("oauth_token_ref"), "oauth_token_ref", pool_name, name)

    if kind == "api_key" and not api_key_ref:
        raise _err(
            f"pool {pool_name!r} member {name!r}: an 'api_key' member requires 'api_key_ref'."
        )
    if kind == "api_key" and oauth_token_ref is not None:
        raise _err(
            f"pool {pool_name!r} member {name!r}: 'oauth_token_ref' is for a subscription "
            f"member (a headless OAuth token); an 'api_key' member uses 'api_key_ref'."
        )
    if pool_family == "anthropic" and codex_dir is not None:
        raise _err(
            f"pool {pool_name!r} member {name!r}: 'codex_config_dir' is invalid for an "
            f"anthropic pool (use 'claude_config_dir')."
        )
    if pool_family == "openai" and claude_dir is not None:
        raise _err(
            f"pool {pool_name!r} member {name!r}: 'claude_config_dir' is invalid for an "
            f"openai pool (use 'codex_config_dir')."
        )
    family_dir = claude_dir if pool_family == "anthropic" else codex_dir
    if oauth_token_ref is not None and family_dir is not None:
        raise _err(
            f"pool {pool_name!r} member {name!r}: a subscription authenticates by EITHER a "
            f"config dir OR 'oauth_token_ref', not both (launch and the poller would "
            f"otherwise auth as different accounts)."
        )

    return ProviderAccount(
        id=account_id_for(pool_name, name),
        name=name,
        family=pool_family,
        kind=kind,
        priority=priority,
        pool_id=pool_id_for(pool_name),
        claude_config_dir=claude_dir,
        codex_config_dir=codex_dir,
        api_key_ref=api_key_ref,
        oauth_token_ref=oauth_token_ref,
    )


def _parse_pool(name: str, raw: object) -> CredentialPool:
    """Parse one named pool into a :class:`CredentialPool`."""
    body = _require_mapping(raw, f"pool {name!r}")
    family = _parse_family(body.get("family"), name)
    failover = _parse_failover(body.get("failover"), name)
    rotation = _parse_rotation(body.get("rotation"), name)

    members_raw = body.get("members")
    if not isinstance(members_raw, list) or not members_raw:
        raise _err(f"pool {name!r}: 'members' must be a non-empty list.")

    seen: set[str] = set()
    members = tuple(_parse_member(m, name, family, seen) for m in members_raw)
    return CredentialPool(
        id=pool_id_for(name),
        name=name,
        family=family,
        failover_mode=failover,
        members=members,
        rotation_mode=rotation,
    )


def load_pools(config: Mapping[str, object]) -> dict[str, CredentialPool]:
    """Parse every pool in the config's ``pools:`` block.

    :param config: The full parsed config mapping (as returned by
        :func:`omnigent.onboarding.provider_config.load_config`).
    :returns: Map of pool name → :class:`CredentialPool`. Empty when no
        ``pools:`` block is present (backward compatible).
    :raises OmnigentError: If the block or any pool/member is malformed.
    """
    raw_block = config.get(POOLS_CONFIG_KEY)
    if raw_block is None:
        return {}
    block = _require_mapping(raw_block, "'pools'")
    return {name: _parse_pool(name, body) for name, body in block.items()}


def get_pool_for_family(pools: dict[str, CredentialPool], family: str) -> CredentialPool | None:
    """Return the first pool serving *family*, or ``None``.

    :param pools: Parsed pools (from :func:`load_pools`).
    :param family: ``"anthropic"`` or ``"openai"``.
    :returns: The lowest-named pool for the family (deterministic), or
        ``None`` when no pool serves it.
    """
    matching = sorted((p for p in pools.values() if p.family == family), key=lambda p: p.name)
    return matching[0] if matching else None


def find_account(
    pools: dict[str, CredentialPool], credential_id: str
) -> tuple[CredentialPool, ProviderAccount] | None:
    """Locate the pool + account for *credential_id* across all pools.

    :returns: The owning ``(pool, account)`` pair, or ``None`` when no
        account with that id exists.
    """
    for pool in pools.values():
        for member in pool.members:
            if member.id == credential_id:
                return pool, member
    return None
