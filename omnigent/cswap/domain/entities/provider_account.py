"""The :class:`ProviderAccount` entity.

One credential slot in a multi-subscription pool. **Distinct from**
:mod:`omnigent.entities.account` (web-UI users) — this is an AI provider
credential. It never holds a raw secret: subscriptions point at an
isolated CLI config directory (whose ``.credentials.json`` holds the OAuth
token), and API keys point at a secret reference resolved lazily via
:func:`omnigent.onboarding.provider_config.resolve_secret`.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.cswap.domain.value_objects.enums import (
    SUBSCRIPTION,
    AccountKind,
    Family,
)


@dataclass(frozen=True)
class ProviderAccount:
    """An AI provider credential participating in a pool.

    :param id: Stable account id, e.g. ``"pacct_<hex>"``.
    :param name: Human label, unique within a pool, e.g. ``"claude-pro-1"``.
    :param family: The provider family served (``anthropic`` / ``openai``).
    :param kind: ``subscription`` (CLI login) or ``api_key``.
    :param priority: Selection priority within the pool; lower preferred.
    :param pool_id: The owning pool's id, or ``None`` for an ungrouped
        account.
    :param claude_config_dir: For a Claude subscription: the isolated
        ``CLAUDE_CONFIG_DIR`` holding this account's OAuth credentials.
    :param codex_config_dir: For a Codex subscription: the isolated
        ``CODEX_HOME`` holding this account's login.
    :param api_key_ref: For an api_key account: the secret reference
        (``env:VAR`` / ``keychain:NAME`` / ``$VAR``).
    :param is_active: Soft-delete / disable flag.
    """

    id: str
    name: str
    family: Family
    kind: AccountKind
    priority: int
    pool_id: str | None = None
    claude_config_dir: str | None = None
    codex_config_dir: str | None = None
    api_key_ref: str | None = None
    is_active: bool = True

    @property
    def is_subscription(self) -> bool:
        """Whether this account is a subscription (CLI-login) credential."""
        return self.kind == SUBSCRIPTION

    def config_dir(self) -> str | None:
        """Return the isolated config dir for this subscription, if any.

        :returns: :attr:`claude_config_dir` for an Anthropic subscription,
            :attr:`codex_config_dir` for an OpenAI subscription, else
            ``None``.
        """
        if not self.is_subscription:
            return None
        return self.claude_config_dir if self.family == "anthropic" else self.codex_config_dir
