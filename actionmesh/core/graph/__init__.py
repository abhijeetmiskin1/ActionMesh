"""C4 — Action Graph: build, validate, order, serialize, pre-flight authorize.

C4 produces a structurally sound, deterministically ordered graph and proves it
may proceed. It never executes an action and never talks to a provider.

Layering (earlier phases are consumed, never reimplemented)::

    Registry (C2)  ─┐
    Policy  (C3)  ─┼─> authorize_graph() ─> C5 executor
    Structure(C4)  ─┘
"""

from __future__ import annotations

from actionmesh.core.graph.authorization import (
    GraphAuthorization,
    NodeAuthorization,
    authorize_graph,
)
from actionmesh.core.graph.builder import GraphBuilder, graph_from_actions
from actionmesh.core.graph.errors import (
    GraphAuthorizationError,
    GraphCycleError,
    GraphError,
    GraphValidationError,
    NodeConfigError,
)
from actionmesh.core.graph.nodeconfig import (
    CARRIES_ACTION,
    MAX_ATTEMPTS,
    MAX_WAIT_S,
    summarize,
    validate_node,
)
from actionmesh.core.graph.serialization import (
    graph_from_dict,
    graph_from_json,
    graph_to_dict,
    graph_to_json,
)
from actionmesh.core.graph.validator import (
    compensation_map,
    dependents,
    execution_order,
    find_cycle,
    root_nodes,
    topological_layers,
    validate_graph_structure,
    visualization,
)

__all__ = [
    "CARRIES_ACTION",
    "MAX_ATTEMPTS",
    "MAX_WAIT_S",
    "GraphAuthorization",
    "GraphAuthorizationError",
    "GraphBuilder",
    "GraphCycleError",
    "GraphError",
    "GraphValidationError",
    "NodeAuthorization",
    "NodeConfigError",
    "authorize_graph",
    "compensation_map",
    "dependents",
    "execution_order",
    "find_cycle",
    "graph_from_actions",
    "graph_from_dict",
    "graph_from_json",
    "graph_to_dict",
    "graph_to_json",
    "root_nodes",
    "summarize",
    "topological_layers",
    "validate_graph_structure",
    "validate_node",
    "visualization",
]
