"""Deterministic graph serialization / deserialization (C4).

Round-trip guarantee
--------------------
``graph_from_json(graph_to_json(g))`` reproduces a graph that is *semantically
equal* to ``g``: same node ids, types, dependencies, conditions, waits, retry
bounds, compensation links and action payloads.

Node ordering is normalized (sorted by ``node_id``) so two logically identical
graphs serialize to the same node order — which is what makes graph caching and
idempotent replays safe. Per-action identity (``action_id`` /
``idempotency_key``) is preserved verbatim: it is identity, not semantics.
"""

from __future__ import annotations

import json
from contextlib import suppress
from datetime import datetime
from typing import Any

from actionmesh.core.graph.errors import GraphValidationError
from actionmesh.core.graph.validator import validate_graph_structure
from actionmesh.protocol.models import Action, ActionGraph, GraphNode, NodeType
from actionmesh.protocol.validate import ValidationError, validate_action


def _action_to_dict(action: Action | None) -> dict[str, Any] | None:
    return action.model_dump() if action is not None else None


def _action_from_dict(raw: Any, path: str) -> Action:
    """Rebuild an ``Action`` from wire data through the same validation as HTTP.

    ``Action(**raw)`` looks like deserialization but is not: the dataclass applies no
    coercion, so ``"risk_level": "low"`` stays a bare ``str``. Nothing downstream
    checks for that -- ``registry.resolve`` returns early for an existing ``Action``
    and the policy engine compares enums -- so the first symptom is an
    ``AttributeError`` deep inside resolution, or worse a comparison that is quietly
    always false. Deserialization *is* a trust boundary, so it validates here.

    ``validate_action`` also deliberately refuses to take identity from untrusted
    input; serialization guarantees identity is preserved, so those fields are put
    back afterwards from the wire value when it is well-formed.
    """
    try:
        action = validate_action(raw, path)
    except ValidationError as exc:
        raise GraphValidationError(f"{path} is malformed: {exc}") from exc
    if isinstance(raw, dict):
        action_id = raw.get("action_id")
        if isinstance(action_id, str) and action_id:
            action.action_id = action_id
        idempotency = raw.get("idempotency_key")
        if isinstance(idempotency, str) and idempotency:
            action.idempotency_key = idempotency
        created = raw.get("created_at")
        if isinstance(created, str) and created:
            # A timestamp nobody can parse is not identity worth failing over; the
            # action itself already validated, and its id is what dedupes.
            with suppress(ValueError):
                action.created_at = datetime.fromisoformat(created)
    return action


def graph_to_dict(graph: ActionGraph) -> dict[str, Any]:
    """Serialize to a canonical, JSON-safe dict (nodes sorted by id)."""
    if not isinstance(graph, ActionGraph):
        raise GraphValidationError("graph must be an ActionGraph")
    return {
        "graph_id": graph.graph_id,
        "intent": graph.intent,
        # C36 — routing survives serialization. Emitted even when empty so the key's
        # absence can never be read as "someone stripped the target".
        "target_device_id": graph.target_device_id,
        "created_at": graph.created_at.isoformat(),
        "nodes": [
            {
                "node_id": n.node_id,
                "node_type": n.node_type.value,
                "action": _action_to_dict(n.action),
                "compensation": _action_to_dict(n.compensation),
                "depends_on": list(n.depends_on),
                "label": n.label,
                "condition": n.condition,
                "wait_s": n.wait_s,
                "max_attempts": n.max_attempts,
                "compensates": n.compensates,
                "metadata": dict(n.metadata),
            }
            for n in sorted(graph.nodes, key=lambda n: n.node_id)
        ],
    }



def _node_from_dict(raw: Any, path: str) -> GraphNode:
    if not isinstance(raw, dict):
        raise GraphValidationError(f"{path} must be an object")
    raw_type = str(raw.get("node_type", NodeType.ACTION.value))
    try:
        node_type = NodeType(raw_type)
    except ValueError as exc:
        raise GraphValidationError(f"{path}.node_type unknown: {raw_type!r}") from exc
    depends = raw.get("depends_on", [])
    if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
        raise GraphValidationError(f"{path}.depends_on must be a list of strings")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise GraphValidationError(f"{path}.metadata must be an object")
    try:
        return GraphNode(
            node_id=str(raw.get("node_id", "")),
            node_type=node_type,
            action=(
                _action_from_dict(raw["action"], f"{path}.action")
                if isinstance(raw.get("action"), dict)
                else None
            ),
            compensation=(
                _action_from_dict(raw["compensation"], f"{path}.compensation")
                if isinstance(raw.get("compensation"), dict)
                else None
            ),
            depends_on=list(depends),
            label=str(raw.get("label", "")),
            condition=str(raw.get("condition", "")),
            wait_s=float(raw.get("wait_s", 0.0)),
            max_attempts=int(raw.get("max_attempts", 1)),
            compensates=raw.get("compensates"),
            metadata=dict(metadata),
        )
    except (TypeError, ValueError) as exc:
        raise GraphValidationError(f"{path} is malformed: {exc}") from exc


def graph_from_dict(raw: Any, *, validate: bool = True) -> ActionGraph:
    """Deserialize untrusted graph JSON. Rejects malformed input."""
    if not isinstance(raw, dict):
        raise GraphValidationError("graph payload must be an object")
    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list):
        raise GraphValidationError("graph.nodes must be a list")
    nodes = [
        _node_from_dict(item, f"graph.nodes[{i}]") for i, item in enumerate(nodes_raw)
    ]
    target = raw.get("target_device_id", "")
    if not isinstance(target, str):
        raise GraphValidationError("graph.target_device_id must be a string")
    graph = ActionGraph(
        intent=str(raw.get("intent", "")), nodes=nodes, target_device_id=target.strip()
    )
    graph_id = raw.get("graph_id")
    if isinstance(graph_id, str) and graph_id:
        graph.graph_id = graph_id
    if validate:
        validate_graph_structure(graph)
    return graph


def graph_from_json(payload: str, *, validate: bool = True) -> ActionGraph:
    try:
        raw = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise GraphValidationError(f"graph JSON is invalid: {exc}") from exc
    return graph_from_dict(raw, validate=validate)


__all__ = [
    "graph_from_dict",
    "graph_from_json",
    "graph_to_dict",
    "graph_to_json",
]


def graph_to_json(graph: ActionGraph, *, indent: int | None = None) -> str:
    return json.dumps(graph_to_dict(graph), indent=indent, sort_keys=True)
