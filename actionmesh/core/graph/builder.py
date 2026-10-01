"""Graph builder — the ergonomic way to assemble a validated graph (C4).

``GraphBuilder`` accepts *untrusted* input (typically LLM output) and produces a
graph that has passed :func:`validate_graph_structure`. It never silently drops a
node: an unbuildable plan is an error, not a smaller graph.

Authorization is deliberately NOT done here. C4 validates structure; the C3
:class:`~actionmesh.core.permissions.PolicyEngine` decides authorization, and
:mod:`actionmesh.core.graph.authorization` combines both for pre-flight checks.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from actionmesh.core.graph.errors import GraphValidationError
from actionmesh.core.graph.validator import validate_graph_structure
from actionmesh.protocol.models import Action, ActionGraph, GraphNode, NodeType
from actionmesh.protocol.validate import validate_action


def _payload(action: Action | dict[str, Any]) -> Action:
    """Validate dict input as untrusted before it becomes protocol data."""
    return validate_action(action) if isinstance(action, dict) else action


class GraphBuilder:
    """Accumulate nodes, then :meth:`build` a validated :class:`ActionGraph`."""

    def __init__(self, intent: str = "") -> None:
        self._intent = intent or ""
        self._nodes: list[GraphNode] = []

    def add(self, node: GraphNode) -> GraphNode:
        self._nodes.append(node)
        return node

    def _auto(self, prefix: str, node_id: str | None) -> str:
        """Deterministic auto id: ``<prefix>_<n>`` counting *this prefix* only.

        Counting per prefix (rather than by total node count) keeps ids
        independent of insertion order, so two logically identical graphs get
        identical ids and therefore serialize identically.
        """
        if node_id:
            return node_id
        used = sum(1 for n in self._nodes if n.node_id.startswith(f"{prefix}_"))
        return f"{prefix}_{used + 1}"

    def _add_action_node(
        self,
        node_type: NodeType,
        prefix: str,
        action: Action | dict[str, Any],
        node_id: str | None,
        depends_on: Sequence[str],
        label: str,
    ) -> GraphNode:
        return self.add(
            GraphNode(
                node_id=self._auto(prefix, node_id),
                node_type=node_type,
                action=_payload(action),
                depends_on=list(depends_on),
                label=label,
            )
        )

    def action(
        self,
        action: Action | dict[str, Any],
        *,
        node_id: str | None = None,
        depends_on: Sequence[str] = (),
        label: str = "",
    ) -> GraphNode:
        """Add an ACTION node."""
        return self._add_action_node(
            NodeType.ACTION, "act", action, node_id, depends_on, label
        )

    def sequence(
        self,
        action: Action | dict[str, Any],
        *,
        node_id: str | None = None,
        depends_on: Sequence[str] = (),
        label: str = "",
    ) -> GraphNode:
        return self._add_action_node(
            NodeType.SEQUENCE, "seq", action, node_id, depends_on, label
        )

    def parallel(
        self,
        action: Action | dict[str, Any],
        *,
        node_id: str | None = None,
        depends_on: Sequence[str] = (),
        label: str = "",
    ) -> GraphNode:
        return self._add_action_node(
            NodeType.PARALLEL, "par", action, node_id, depends_on, label
        )

    def approval(
        self, *, node_id: str | None = None, guards: Sequence[str] = (),
        label: str = "",
    ) -> GraphNode:
        return self.add(
            GraphNode(
                node_id=self._auto("apr", node_id),
                node_type=NodeType.APPROVAL,
                depends_on=list(guards),
                label=label,
            )
        )

    def condition(
        self, expression: str, *, node_id: str | None = None,
        depends_on: Sequence[str] = (), label: str = "",
    ) -> GraphNode:
        return self.add(
            GraphNode(
                node_id=self._auto("cond", node_id),
                node_type=NodeType.CONDITION,
                condition=expression,
                depends_on=list(depends_on),
                label=label,
            )
        )

    def wait(
        self, seconds: float, *, node_id: str | None = None,
        depends_on: Sequence[str] = (), label: str = "",
    ) -> GraphNode:
        return self.add(
            GraphNode(
                node_id=self._auto("wait", node_id),
                node_type=NodeType.WAIT,
                wait_s=seconds,
                depends_on=list(depends_on),
                label=label,
            )
        )

    def retry(
        self, target: str, *, max_attempts: int = 3,
        node_id: str | None = None, label: str = "",
    ) -> GraphNode:
        return self.add(
            GraphNode(
                node_id=self._auto("retry", node_id),
                node_type=NodeType.RETRY,
                max_attempts=max_attempts,
                depends_on=[target],
                label=label,
            )
        )

    def compensate(
        self, target: str, action: Action | dict[str, Any], *,
        node_id: str | None = None, label: str = "",
    ) -> GraphNode:
        """Add a COMPENSATION node that undoes ``target``.

        The undo-action is stored in both ``compensation`` (the semantic home,
        and what C5/C6 run during rollback) and ``action`` (so generic side-effect
        tooling sees a payload without special-casing).
        """
        payload = _payload(action)
        return self.add(
            GraphNode(
                node_id=self._auto("comp", node_id),
                node_type=NodeType.COMPENSATION,
                action=payload,
                compensation=payload,
                compensates=target,
                label=label,
            )
        )

    def end(
        self, *, node_id: str | None = None,
        depends_on: Sequence[str] = (), label: str = "",
    ) -> GraphNode:
        # END is a singleton per graph, so its id is the conventional "end"
        # unless overridden. Keeps serialized graphs readable.
        return self.add(
            GraphNode(
                node_id=node_id or "end",
                node_type=NodeType.END,
                depends_on=list(depends_on),
                label=label,
            )
        )

    # -- assembly ---------------------------------------------------------
    def build(self) -> ActionGraph:
        """Validate and return the graph. Raises on any structural problem."""
        return validate_graph_structure(
            ActionGraph(intent=self._intent, nodes=list(self._nodes))
        )

    @property
    def nodes(self) -> tuple[GraphNode, ...]:
        return tuple(self._nodes)


def graph_from_actions(
    actions: Iterable[Action | dict[str, Any]], *, intent: str = ""
) -> ActionGraph:
    """Build a linear SEQUENCE graph from an ordered action list + END node."""
    builder = GraphBuilder(intent=intent)
    previous: list[str] = []
    for item in actions:
        previous = [builder.action(item, depends_on=previous).node_id]
    if not previous:
        raise GraphValidationError("cannot build a graph from zero actions")
    builder.end(depends_on=previous)
    return builder.build()


__all__ = ["GraphBuilder", "graph_from_actions"]

