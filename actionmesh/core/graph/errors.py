"""C4 — Action Graph errors. Every failure is raised, never silently repaired."""

from __future__ import annotations


class GraphError(Exception):
    """Base class for all action-graph failures. Fail-closed."""


class GraphValidationError(GraphError):
    """Structural problem: duplicate ids, dangling deps, bad node config."""


class GraphCycleError(GraphValidationError):
    """The dependency graph contains a cycle.

    Unrestricted loops are forbidden. Bounded repetition is expressed with a
    RETRY node (an explicit, bounded ``max_attempts``), never with a cycle.
    """


class NodeConfigError(GraphValidationError):
    """A node is missing required config, or carries config it may not have."""


class GraphAuthorizationError(GraphError):
    """A graph node cannot be authorized. The graph is rejected as a whole."""


__all__ = [
    "GraphAuthorizationError",
    "GraphCycleError",
    "GraphError",
    "GraphValidationError",
    "NodeConfigError",
]
