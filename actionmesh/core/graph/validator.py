"""Graph validation, cycle detection and deterministic ordering (C4).

C4 does NOT execute anything. It produces a graph that is structurally sound and
deterministically ordered so C5 can schedule it without surprises.

Security posture
----------------
* A graph that fails validation is *rejected*; never partially repaired.
* Unrestricted loops are impossible: ``depends_on`` must form a DAG. Bounded
  repetition is expressed with an explicit ``RETRY`` node instead.
* Ordering is deterministic (ties broken by ``sorted()``), so identical graphs
  always produce identical execution order and identical serialization.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from actionmesh.core.graph.errors import (
    GraphCycleError,
    GraphValidationError,
    NodeConfigError,
)
from actionmesh.core.graph.nodeconfig import TERMINAL, summarize, validate_node
from actionmesh.protocol.models import ActionGraph, GraphNode, NodeType


def _index(nodes: Iterable[GraphNode]) -> dict[str, GraphNode]:
    idx: dict[str, GraphNode] = {}
    for node in nodes:
        if node.node_id in idx:
            raise GraphValidationError(f"duplicate node id: {node.node_id!r}")
        idx[node.node_id] = node
    return idx


def find_cycle(nodes: Iterable[GraphNode]) -> list[str] | None:
    """Return one dependency cycle as a node-id path, or ``None`` if acyclic.

    Iterative depth-first search with a colour map (white/grey/black), so a deep
    graph cannot exhaust the Python recursion limit.
    """
    idx = _index(nodes)
    WHITE, GREY, BLACK = 0, 1, 2
    colour = dict.fromkeys(idx, WHITE)
    parent: dict[str, str | None] = dict.fromkeys(idx, None)

    for start in sorted(idx):
        if colour[start] != WHITE:
            continue
        stack: list[tuple[str, list[str]]] = [(start, sorted(idx[start].depends_on))]
        colour[start] = GREY
        while stack:
            node_id, remaining = stack[-1]
            if not remaining:
                colour[node_id] = BLACK
                stack.pop()
                continue
            dep = remaining.pop(0)
            state = colour.get(dep)
            if state is None:
                raise GraphValidationError(f"unknown dependency: {dep!r}")
            if state == GREY:
                # Back edge -> reconstruct the cycle path.
                path = [dep]
                cur: str | None = node_id
                while cur is not None and cur != dep:
                    path.append(cur)
                    cur = parent[cur]
                path.append(dep)
                return list(reversed(path))
            if state == WHITE:
                colour[dep] = GREY
                parent[dep] = node_id
                stack.append((dep, sorted(idx[dep].depends_on)))
    return None


def validate_graph_structure(graph: ActionGraph) -> ActionGraph:
    """Full structural validation. Returns the graph unchanged on success.

    Checks: non-empty, unique ids, per-node config, dependency existence, no
    self-dependency, acyclicity, compensation targets, terminal reachability.
    """
    if not isinstance(graph, ActionGraph):
        raise GraphValidationError("graph must be an ActionGraph")
    if not graph.nodes:
        raise GraphValidationError("graph must contain at least one node")

    idx = _index(graph.nodes)

    for node in graph.nodes:
        validate_node(node)
        for dep in node.depends_on:
            if dep == node.node_id:
                raise GraphValidationError(f"{node.node_id}: node depends on itself")
            if dep not in idx:
                raise GraphValidationError(
                    f"{node.node_id}: unknown dependency {dep!r}"
                )

    cycle = find_cycle(graph.nodes)
    if cycle is not None:
        raise GraphCycleError("dependency cycle: " + " -> ".join(cycle))

    for node in graph.nodes:
        if node.node_type is NodeType.COMPENSATION:
            target = node.compensates
            assert target is not None  # guaranteed by validate_node
            if target not in idx:
                raise NodeConfigError(
                    f"{node.node_id}: compensates unknown node {target!r}"
                )
            if target == node.node_id:
                raise NodeConfigError(f"{node.node_id}: cannot compensate itself")
            if idx[target].node_type is NodeType.COMPENSATION:
                # An undo-of-an-undo would have to run after the undo it undoes,
                # which is backwards. C16 does not model nested compensation, so
                # accepting this graph would silently drop a declared undo — the
                # author would believe they had a rollback and have none.
                raise NodeConfigError(
                    f"{node.node_id}: cannot compensate another COMPENSATION node "
                    f"({target!r}); nested compensation is not supported"
                )

    if not any(n.node_type in TERMINAL for n in graph.nodes):
        raise GraphValidationError("graph must contain at least one END node")

    return graph


def topological_layers(graph: ActionGraph) -> list[list[str]]:
    """Deterministic Kahn layering.

    Every node in layer *i* depends only on nodes in layers ``< i``, so a whole
    layer may run concurrently. Ties break on ``sorted()`` for reproducibility.

    COMPENSATION nodes are **excluded**. They carry no ``depends_on`` (the undo
    only becomes meaningful once its target has run), so including them made
    Kahn treat every undo as a root node and the scheduler ran it as ordinary
    forward work — a create+delete pair firing in order and the engine
    reporting success for a net no-op. Rollback is C16's job, and it runs them
    in reverse order after a failure.

    Edges *pointing at* an excluded node are dropped rather than left dangling.
    An END node commonly depends on the compensation it terminates, and a
    dependency on something that is never scheduled would look like a cycle.
    """
    idx = _index(graph.nodes)
    scheduled = {nid for nid, n in idx.items() if n.node_type is not NodeType.COMPENSATION}
    remaining = {
        nid: set(n.depends_on) & scheduled
        for nid, n in idx.items()
        if nid in scheduled
    }
    layers: list[list[str]] = []

    while remaining:
        ready = sorted(nid for nid, deps in remaining.items() if not deps)
        if not ready:  # pragma: no cover - guarded by validate_graph_structure
            raise GraphCycleError("graph contains a cycle")
        layers.append(ready)
        for nid in ready:
            del remaining[nid]
        for deps in remaining.values():
            deps.difference_update(ready)

    return layers


def execution_order(graph: ActionGraph) -> list[str]:
    """Flatten :func:`topological_layers` into a deterministic node order."""
    return [nid for layer in topological_layers(graph) for nid in layer]


def dependents(graph: ActionGraph) -> dict[str, list[str]]:
    """Reverse dependency map — which nodes become runnable when *node* succeeds."""
    idx = _index(graph.nodes)
    rev: dict[str, list[str]] = {nid: [] for nid in idx}
    for node in graph.nodes:
        for dep in node.depends_on:
            rev[dep].append(node.node_id)
    return {k: sorted(v) for k, v in rev.items()}


def root_nodes(graph: ActionGraph) -> list[str]:
    """Nodes with no dependencies, in deterministic order."""
    return sorted(n.node_id for n in graph.nodes if not n.depends_on)


def compensation_map(graph: ActionGraph) -> dict[str, str]:
    """``{compensated_node_id: compensation_node_id}`` for rollback (C5/C6)."""
    out: dict[str, str] = {}
    for node in graph.nodes:
        if node.node_type is NodeType.COMPENSATION and node.compensates:
            out.setdefault(node.compensates, node.node_id)
    return out


def visualization(graph: ActionGraph) -> dict[str, Any]:
    """Deterministic, serializable graph view for the dashboard visualizer."""
    return {
        "graph_id": graph.graph_id,
        "intent": graph.intent,
        "layers": topological_layers(graph),
        "order": execution_order(graph),
        "roots": root_nodes(graph),
        "compensations": compensation_map(graph),
        "nodes": [summarize(n) for n in sorted(graph.nodes, key=lambda n: n.node_id)],
    }


__all__ = [
    "compensation_map",
    "dependents",
    "execution_order",
    "find_cycle",
    "root_nodes",
    "topological_layers",
    "validate_graph_structure",
    "visualization",
]

