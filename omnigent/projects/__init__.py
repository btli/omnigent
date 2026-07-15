"""Per-project defaults models and resolution."""

from omnigent.projects.defaults import (
    DEFAULTS_SCHEMA_VERSION,
    ProjectDefaultsBundle,
    validate_defaults_bundle,
)
from omnigent.projects.resolver import ResolvedProjectDefaults, resolve_project_defaults

__all__ = [
    "DEFAULTS_SCHEMA_VERSION",
    "ProjectDefaultsBundle",
    "ResolvedProjectDefaults",
    "resolve_project_defaults",
    "validate_defaults_bundle",
]
