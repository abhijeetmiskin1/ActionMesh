"""C2 — Action Registry: register / discover / resolve / version."""

from __future__ import annotations

from actionmesh.core.registry.defs import check_params, validate_definition, version_key
from actionmesh.core.registry.errors import (
    DefinitionError,
    DuplicateError,
    RegistryError,
    UnknownActionError,
)
from actionmesh.core.registry.registry import ActionRegistry, ResolvedAction, max_risk
from actionmesh.core.registry.seeds import seed_definitions

__all__ = [
    "ActionRegistry",
    "DefinitionError",
    "DuplicateError",
    "RegistryError",
    "ResolvedAction",
    "UnknownActionError",
    "check_params",
    "max_risk",
    "seed_definitions",
    "validate_definition",
    "version_key",
]
