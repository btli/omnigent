"""app.state.git_hosts / app.state.git_credential_store wiring."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from omnigent.git_hosts.base import HostConfig
from omnigent.git_hosts.config import load_git_hosts
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.git_credential_store import GitCredentialStore


@pytest.fixture()
def app_factory(db_uri: str, tmp_path: Path):
    """
    Factory building a minimal real app to probe ``app.state.git_hosts``.

    Mirrors ``tests/server/test_managed_hosts.py``'s ``_capability_probe_app``
    helper, reduced to the stores ``create_app`` requires unconditionally.

    :param db_uri: SQLite connection URI from the ``db_uri`` fixture.
    :param tmp_path: Per-test scratch dir for artifact/cache stores.
    :returns: A callable that builds a :class:`FastAPI` app, forwarding
        ``git_hosts`` and ``git_credential_store`` to :func:`create_app`.
    """

    def _build(
        *,
        git_hosts: tuple[HostConfig, ...] = (),
        git_credential_store: GitCredentialStore | None = None,
    ) -> FastAPI:
        artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
        return create_app(
            agent_store=SqlAlchemyAgentStore(db_uri),
            file_store=SqlAlchemyFileStore(db_uri),
            conversation_store=SqlAlchemyConversationStore(db_uri),
            artifact_store=artifact_store,
            agent_cache=AgentCache(artifact_store=artifact_store, cache_dir=tmp_path / "cache"),
            git_hosts=git_hosts,
            git_credential_store=git_credential_store,
        )

    return _build


def test_create_app_defaults_to_empty_git_hosts(app_factory) -> None:
    app = app_factory()
    assert app.state.git_hosts == ()


def test_create_app_stores_parsed_git_hosts(app_factory) -> None:
    hosts = load_git_hosts(
        [
            {
                "id": "acme",
                "provider": "forgejo",
                "web_host": "git.acme.com",
                "credential_source": "env:ACME_TOKEN",
            }
        ]
    )
    app = app_factory(git_hosts=hosts)
    assert app.state.git_hosts == hosts
    assert isinstance(app.state.git_hosts[0], HostConfig)


def test_git_credential_store_absent_by_default(app_factory) -> None:
    app = app_factory()
    assert getattr(app.state, "git_credential_store", None) is None


def test_git_credential_router_mounted_when_store_present(app_factory, tmp_path: Path) -> None:
    from cryptography.fernet import Fernet

    from omnigent.git_hosts.crypto import GitCredentialCipher

    store = GitCredentialStore(
        f"sqlite:///{tmp_path}/creds.db",
        GitCredentialCipher([Fernet.generate_key().decode()]),
    )
    app = app_factory(git_credential_store=store)
    assert app.state.git_credential_store is store
    assert any(r.path == "/v1/git-credentials" for r in app.routes)


def test_create_app_registers_request_validation_error_handler(app_factory) -> None:
    # Guards the sanitized-422 handler registration: without it, a malformed
    # git-credentials body (or any other route's body) would echo submitted
    # field values — including secrets — back in the 422 response. Assert the
    # handler *identity* — FastAPI always registers a default RequestValidationError
    # handler, so `in app.exception_handlers` would pass even if ours were removed.
    from omnigent.server.app import sanitized_validation_error_handler

    app = app_factory()
    assert app.exception_handlers.get(RequestValidationError) is sanitized_validation_error_handler
