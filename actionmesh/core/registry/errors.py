"""Action Registry errors (C2)."""

from __future__ import annotations


class RegistryError(Exception):
    """Base class for registry failures."""


class DefinitionError(RegistryError):
    """An action definition is malformed and must not be registered."""


class DuplicateError(RegistryError):
    """The same (name, version) is already registered."""


class UnknownActionError(RegistryError):
    """No definition exists for the requested action (or version)."""


__all__ = [
    "DefinitionError",
    "DuplicateError",
    "RegistryError",
    "UnknownActionError",
]
