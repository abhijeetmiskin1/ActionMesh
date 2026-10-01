"""Graph + plan validation and failure classification (stdlib-only).

C1-level untrusted graph validation. Structural DAG semantics (cycle detection,
node config rules, deterministic ordering) live in
:mod:`actionmesh.core.graph` (C4) so that this module stays a pure trust-boundary
validator.
"""

from __future__ import annotations

from itertools import pairwise
from typing import Any

from actionmesh.protocol.models import ActionGraph, FailureKind, GraphNode, NodeType
from actionmesh.protocol.validate import (
    ValidationError,
    _coerce_enum,
    _fail,
    require_type,
    validate_action,
)


def validate_graph(raw: Any, path: str = "graph") -> ActionGraph:
    """Validate an untrusted action graph: nodes, deps, no unknown references."""
    if isinstance(raw, ActionGraph):
        raw = raw.model_dump()
    require_type(raw, dict, path)

    nodes_raw = raw.get("nodes")
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise _fail("graph.nodes must be a non-empty list", f"{path}.nodes", nodes_raw)

    nodes: list[GraphNode] = []
    for i, item in enumerate(nodes_raw):
        npath = f"{path}.nodes[{i}]"
        if isinstance(item, GraphNode):
            item = item.model_dump()
        require_type(item, dict, npath)
        try:
            ntype = _coerce_enum(item.get("node_type", NodeType.ACTION), NodeType, f"{npath}.type")
        except ValidationError as exc:
            raise _fail(exc.reason, exc.path, exc.value) from exc
        action = None
        if item.get("action") is not None:
            action = validate_action(item["action"], f"{npath}.action")
        depends = item.get("depends_on", [])
        if not isinstance(depends, list) or not all(isinstance(d, str) for d in depends):
            raise _fail("depends_on must be a list of node ids", f"{npath}.depends_on", depends)
        node = GraphNode(node_type=ntype, action=action, depends_on=list(depends))
        if isinstance(item.get("node_id"), str) and item["node_id"]:
            node.node_id = item["node_id"]
        nodes.append(node)

    ids = {n.node_id for n in nodes}
    if len(ids) != len(nodes):
        raise _fail("graph node ids must be unique", f"{path}.nodes", None)
    for node in nodes:
        for dep in node.depends_on:
            if dep not in ids:
                raise _fail(f"unknown dependency {dep!r}", f"{path}.nodes", dep)
            if dep == node.node_id:
                raise _fail("node cannot depend on itself", f"{path}.nodes", dep)

    return ActionGraph(intent=str(raw.get("intent", "")), nodes=nodes)


def validate_plan(raw: Any, path: str = "plan") -> ActionGraph:
    """Validate an untrusted LLM plan ``{intent, actions[]}`` into a graph.

    The LLM proposes an ordered list; it does not choose the dependency
    structure, permissions, risk or authorization. Those are decided downstream.
    """
    require_type(raw, dict, path)
    intent = raw.get("intent", "")
    if not isinstance(intent, str):
        raise _fail("plan.intent must be a string", f"{path}.intent", intent)
    actions = raw.get("actions", [])
    if not isinstance(actions, list):
        raise _fail("plan.actions must be a list", f"{path}.actions", actions)
    nodes = [
        GraphNode(action=validate_action(item, f"{path}.actions[{i}]"))
        for i, item in enumerate(actions)
    ]
    for prev, cur in pairwise(nodes):
        cur.depends_on.append(prev.node_id)
    return ActionGraph(intent=intent, nodes=nodes)


def classify_failure(reason: str) -> FailureKind:
    """Map a free-form failure reason onto a FailureKind for retry policy."""
    text = reason.lower()
    for keyword, kind in (
        ("rate limit", FailureKind.RATE_LIMIT),
        ("429", FailureKind.RATE_LIMIT),
        ("timeout", FailureKind.TIMEOUT),
        ("timed out", FailureKind.TIMEOUT),
        ("network", FailureKind.NETWORK),
        ("connection", FailureKind.NETWORK),
        ("authenticat", FailureKind.AUTHENTICATION),
        ("unauthorized", FailureKind.AUTHORIZATION),
        ("forbidden", FailureKind.AUTHORIZATION),
        ("denied", FailureKind.AUTHORIZATION),
        ("invalid", FailureKind.INVALID_INPUT),
        ("bad request", FailureKind.INVALID_INPUT),
    ):
        if keyword in text:
            return kind
    if "provider" in text:
        return FailureKind.PROVIDER
    return FailureKind.UNKNOWN


__all__ = ["classify_failure", "validate_graph", "validate_plan"]
