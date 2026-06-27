"""Tests for the credential-label projection + namespace reservation."""

from __future__ import annotations

from omnigent.subscription_tokens.labels import (
    CREDENTIAL_ACCOUNT_LABEL,
    CREDENTIAL_FAMILY_LABEL,
    CREDENTIAL_KIND_LABEL,
    CREDENTIAL_LABEL_NAMESPACE,
    reserved_credential_keys,
    to_credential_labels,
)


def test_to_credential_labels_full_descriptor() -> None:
    """A full active-credential dict projects to the three labels."""
    active = {
        "id": "pacct_abc",
        "name": "claude-pro-1",
        "kind": "subscription",
        "family": "anthropic",
        "limit_status": "available",
    }
    assert to_credential_labels(active) == {
        CREDENTIAL_KIND_LABEL: "subscription",
        CREDENTIAL_FAMILY_LABEL: "anthropic",
        CREDENTIAL_ACCOUNT_LABEL: "pacct_abc",
    }


def test_to_credential_labels_none_and_empty() -> None:
    """``None`` / empty descriptor projects to no labels (never a blank)."""
    assert to_credential_labels(None) == {}
    assert to_credential_labels({}) == {}


def test_to_credential_labels_omits_missing_and_nonstring_fields() -> None:
    """Missing or non-string fields are omitted, so a stale label is left
    untouched rather than cleared/garbled — never a partial blank write."""
    # Only kind present (+ a non-str id that must be skipped, not stringified).
    partial = {"kind": "api_key", "id": 123, "family": ""}
    assert to_credential_labels(partial) == {CREDENTIAL_KIND_LABEL: "api_key"}


def test_reserved_credential_keys_filters_namespace() -> None:
    """Only ``credential.*`` keys are flagged; others pass through."""
    labels = {
        "team": "ml",
        CREDENTIAL_KIND_LABEL: "subscription",
        CREDENTIAL_ACCOUNT_LABEL: "pacct_abc",
        "cost_control.plan": "{}",
    }
    reserved = reserved_credential_keys(labels)
    assert set(reserved) == {CREDENTIAL_KIND_LABEL, CREDENTIAL_ACCOUNT_LABEL}
    assert all(key.startswith(CREDENTIAL_LABEL_NAMESPACE) for key in reserved)


def test_reserved_credential_keys_empty_when_none_present() -> None:
    """No ``credential.*`` keys → empty tuple (the common no-op path)."""
    assert reserved_credential_keys({"team": "ml"}) == ()
    assert reserved_credential_keys({}) == ()
