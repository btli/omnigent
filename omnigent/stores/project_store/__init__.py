"""Project store contract."""

from __future__ import annotations

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from omnigent.entities import Project, ProjectBackfillResult
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.conversation_store import ConversationStore


class _Unset(Enum):
    VALUE = "unset"


UNSET = _Unset.VALUE


class ProjectInputError(OmnigentError):
    """Project input validation error rendered as HTTP 422 by the API."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.INVALID_INPUT)

    @property
    def http_status(self) -> int:
        return 422


class ProjectStore(ABC):
    """Persistence contract for flat owner-scoped projects."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        owner_principal_id: str,
        name: str,
        *,
        description: str | None = None,
        defaults_json: dict[str, Any] | None = None,
        defaults_schema_version: int = 1,
    ) -> Project:
        """Create a project. Name collisions raise ``OmnigentError``."""
        ...

    @abstractmethod
    def get(self, project_id: str, owner_principal_id: str) -> Project | None:
        """Get an owned project, hiding other owners and workspaces."""
        ...

    @abstractmethod
    def get_for_use(self, project_id: str, owner_principal_id: str) -> Project | None:
        """Get a project for attachment, rejecting archived projects."""
        ...

    @abstractmethod
    def get_by_name(self, name: str, owner_principal_id: str) -> Project | None:
        """Get an owned project by normalized name, including archived rows."""
        ...

    @abstractmethod
    def get_or_create_by_name(self, owner_principal_id: str, name: str) -> Project:
        """Return a named project, creating it unless an archived row exists."""
        ...

    @abstractmethod
    def list(self, owner_principal_id: str, *, include_archived: bool = False) -> list[Project]:
        """List projects owned by one principal."""
        ...

    @abstractmethod
    def rename(
        self,
        project_id: str,
        owner_principal_id: str,
        name: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        """Compare-and-set a project's name."""
        ...

    @abstractmethod
    def update(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
        description: str | None | _Unset = UNSET,
        defaults_json: dict[str, Any] | None | _Unset = UNSET,
    ) -> Project | None:
        """Compare-and-set mutable project fields."""
        ...

    @abstractmethod
    def archive(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        """Soft-archive a project."""
        ...

    @abstractmethod
    def restore(
        self,
        project_id: str,
        owner_principal_id: str,
        *,
        expected_row_version: int | None,
    ) -> Project | None:
        """Restore a soft-archived project."""
        ...

    @abstractmethod
    def backfill_legacy_labels(
        self, conversation_store: ConversationStore
    ) -> ProjectBackfillResult:
        """Migrate legacy labels or return a non-mutating mapping plan."""
        ...
