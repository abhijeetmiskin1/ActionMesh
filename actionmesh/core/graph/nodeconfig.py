"""Per-node-type configuration rules (C4).

Each :class:`~actionmesh.protocol.models.NodeType` declares what it must carry,
what it must not carry, and is validated in isolation here. C5 reads these
already-validated fields; it never re-parses untrusted input.
"""

from __future__ import annotations

from typing import Any

from actionmesh.core.graph.errors import NodeConfigError
from actionmesh.protocol.models import GraphNode, NodeType

#: Node types whose forward execution is a real side effect, so they must carry
#: an action. COMPENSATION is excluded: it carries its undo-action in
#: ``GraphNode.compensation`` and only runs during rollback.
CARRIES_ACTION = frozenset({NodeType.ACTION, NodeType.SEQUENCE, NodeType.PARALLEL})

#: Human-facing node types.
TERMINAL = frozenset({NodeType.END})

#: Bounds chosen so an untrusted graph cannot exhaust resources.
MAX_WAIT_S = 24 * 60 * 60.0
MAX_ATTEMPTS = 10


def validate_node(node: GraphNode) -> None:
    """Validate one node in isolation. Raises :class:`NodeConfigError`."""
    if not isinstance(node.node_id, str) or not node.node_id:
        raise NodeConfigError("node_id must be a non-empty string")

    ntype = node.node_type
    if not isinstance(ntype, NodeType):
        raise NodeConfigError(f"{node.node_id}: unknown node type {ntype!r}")

    if ntype in CARRIES_ACTION and node.action is None:
        raise NodeConfigError(f"{node.node_id}: {ntype.value} node requires an action")

    if ntype is NodeType.END and node.action is not None:
        raise NodeConfigError(f"{node.node_id}: END node must not carry an action")

    if ntype is NodeType.CONDITION and not node.condition.strip():
        raise NodeConfigError(f"{node.node_id}: CONDITION node requires a condition")

    if ntype is NodeType.WAIT:
        if node.wait_s <= 0:
            raise NodeConfigError(f"{node.node_id}: WAIT node requires wait_s > 0")
        if node.wait_s > MAX_WAIT_S:
            raise NodeConfigError(f"{node.node_id}: wait_s exceeds {MAX_WAIT_S}s")

    if ntype is NodeType.RETRY:
        if not 1 <= node.max_attempts <= MAX_ATTEMPTS:
            raise NodeConfigError(
                f"{node.node_id}: RETRY max_attempts must be 1..{MAX_ATTEMPTS}"
            )
        if not node.depends_on:
            raise NodeConfigError(
                f"{node.node_id}: RETRY node must depend on its target"
            )

    if ntype is NodeType.COMPENSATION:
        if not node.compensates:
            raise NodeConfigError(
                f"{node.node_id}: COMPENSATION node must name the node it undoes"
            )
        if node.compensation is None:
            raise NodeConfigError(
                f"{node.node_id}: COMPENSATION node requires a compensating action"
            )

    if ntype is NodeType.APPROVAL and not node.depends_on:
        raise NodeConfigError(
            f"{node.node_id}: APPROVAL node must guard at least one node"
        )


def summarize(node: GraphNode) -> dict[str, Any]:
    """Small, serializable node description for the API/CLI/graph visualizer."""
    return {
        "node_id": node.node_id,
        "node_type": node.node_type.value,
        "label": node.label or (node.action.type if node.action else node.node_type.value),
        "depends_on": list(node.depends_on),
        "condition": node.condition or None,
        "wait_s": node.wait_s or None,
        "max_attempts": node.max_attempts if node.node_type is NodeType.RETRY else None,
        "compensates": node.compensates,
        "action": node.action.model_dump() if node.action else None,
    }


__all__ = [
    "CARRIES_ACTION",
    "MAX_ATTEMPTS",
    "MAX_WAIT_S",
    "TERMINAL",
    "summarize",
    "validate_node",
]
