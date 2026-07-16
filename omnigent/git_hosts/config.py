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
_ALLOWED_KEYS = frozenset(
    {
        "id",
        "provider",
        "web_host",
        "credential_source",
        "api_base",
        "ssh_host",
        "ssh_port",
        "ca_bundle",
    }
)


def _require_str(entry: dict[str, Any], key: str, index: int) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"git_hosts[{index}].{key} is required and must be a non-empty string")
    return value


def load_git_hosts(raw: object) -> tuple[HostConfig, ...]:
    """Parse the server-config ``git_hosts:`` value into validated records.

    :param raw: The raw value (``None``, or a list of mappings).
    :returns: A tuple of validated :class:`HostConfig`; duplicate hosts raise.
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
        unknown = set(entry) - _ALLOWED_KEYS
        if unknown:
            raise ValueError(f"git_hosts[{index}] has unknown keys: {sorted(unknown)}")
        for key in _REQUIRED:
            _require_str(entry, key, index)
        provider = entry["provider"]
        if provider not in available_providers():
            raise ValueError(
                f"git_hosts[{index}].provider: unknown git host provider {provider!r}; "
                f"known: {available_providers()}"
            )
        web_host = _require_str(entry, "web_host", index).lower()
        if any(c in web_host for c in ":/@") or any(c.isspace() for c in web_host):
            raise ValueError(
                f"git_hosts[{index}].web_host must be a bare hostname (no port, path, or "
                "userinfo); custom ports are not supported yet"
            )
        if web_host in seen:
            raise ValueError(f"git_hosts[{index}].web_host: duplicate host {web_host!r}")
        seen.add(web_host)

        api_base_raw = entry.get("api_base")
        if api_base_raw is not None and (not isinstance(api_base_raw, str) or not api_base_raw):
            raise ValueError(f"git_hosts[{index}].api_base must be a non-empty string when set")
        api_base = api_base_raw or get_git_host(provider).default_api_base(web_host)

        ssh_port_raw = entry.get("ssh_port")
        if ssh_port_raw is not None and (
            isinstance(ssh_port_raw, bool) or not isinstance(ssh_port_raw, int)
        ):
            raise ValueError(f"git_hosts[{index}].ssh_port must be an integer when set")

        ssh_host_raw = entry.get("ssh_host")
        if ssh_host_raw is not None and (not isinstance(ssh_host_raw, str) or not ssh_host_raw):
            raise ValueError(f"git_hosts[{index}].ssh_host must be a non-empty string when set")

        ca_bundle_raw = entry.get("ca_bundle")
        if ca_bundle_raw is not None and (not isinstance(ca_bundle_raw, str) or not ca_bundle_raw):
            raise ValueError(f"git_hosts[{index}].ca_bundle must be a non-empty string when set")

        hosts.append(
            HostConfig(
                id=entry["id"],
                provider=provider,
                web_host=web_host,
                api_base=api_base,
                credential_source=entry["credential_source"],
                ssh_host=ssh_host_raw,
                ssh_port=ssh_port_raw,
                ca_bundle=ca_bundle_raw,
            )
        )
    return tuple(hosts)
