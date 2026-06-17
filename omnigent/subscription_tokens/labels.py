"""Conversation labels that project the session's bound credential.

The launch path binds a session to one :class:`ProviderAccount` (see
:mod:`omnigent.subscription_tokens.integration`). This module names the
conversation labels that mirror that binding into the policy engine's label
space, so guardrail policies can *govern* on which credential a session runs
(e.g. "a personal subscription may not touch restricted data without
approval") and the active-credential UI can read it from the same source of
truth.

The labels are **engine-owned**: their only writer is the policy-engine build
step that seeds them from the binding (see
:func:`omnigent.runtime.policies.builder.build_policy_engine`). Clients cannot
set them — the session routes reject the ``credential.*`` namespace in
client-supplied label writes (mirroring the ``cost_control.*`` reservation in
:mod:`omnigent.cost_plan`) so a caller cannot forge ``credential.kind`` to slip
restricted work past a credential-governance policy.
"""

from __future__ import annotations

from collections.abc import Mapping

# Engine-owned namespace: the binding-derived credential labels. Rejected in
# client-supplied label writes (see the session routes), so the value a policy
# reads always reflects the real bound account, never a forged one.
CREDENTIAL_LABEL_NAMESPACE = "credential."

# The bound account's kind ("subscription" | "api_key" — ``AccountKind``).
CREDENTIAL_KIND_LABEL = "credential.kind"
# The bound account's provider family ("anthropic" | "openai" — ``Family``).
CREDENTIAL_FAMILY_LABEL = "credential.family"
# The bound account's stable pool-member id (schemaless — arbitrary id string).
CREDENTIAL_ACCOUNT_LABEL = "credential.account"


def reserved_credential_keys(labels: Mapping[str, str]) -> tuple[str, ...]:
    """Return the ``credential.*`` keys present in *labels*.

    Mirrors :func:`omnigent.cost_plan.reserved_cost_control_keys`. The session
    routes call this to reject any client attempt to seed or write an
    engine-owned credential label.

    :param labels: A label mapping, e.g. ``{"team": "ml"}``.
    :returns: The keys under :data:`CREDENTIAL_LABEL_NAMESPACE`, in iteration
        order (``()`` when none).
    """
    return tuple(key for key in labels if key.startswith(CREDENTIAL_LABEL_NAMESPACE))


def to_credential_labels(active: Mapping[str, object] | None) -> dict[str, str]:
    """Project an active-credential descriptor into engine label writes.

    :param active: The dict from
        :func:`omnigent.subscription_tokens.integration.active_credential_for_session`
        (``{"id", "name", "kind", "family", "limit_status"}``), or ``None``
        when the session has no binding / no pool is configured.
    :returns: ``{credential.kind, credential.family, credential.account}`` for
        the fields present as non-empty strings, or ``{}`` when *active* is
        ``None`` / carries none of them. Never emits a blank value — a missing
        field is omitted so a stale label is left untouched rather than cleared.
    """
    if not active:
        return {}
    out: dict[str, str] = {}
    kind = active.get("kind")
    if isinstance(kind, str) and kind:
        out[CREDENTIAL_KIND_LABEL] = kind
    family = active.get("family")
    if isinstance(family, str) and family:
        out[CREDENTIAL_FAMILY_LABEL] = family
    account_id = active.get("id")
    if isinstance(account_id, str) and account_id:
        out[CREDENTIAL_ACCOUNT_LABEL] = account_id
    return out
