"""Graph pre-flight authorization (C4 bridging C2 Registry + C3 Policy).

This module proves a *graph* is safe to hand to the executor. It composes three
earlier phases without duplicating them:

* **C2 Registry** — every action must resolve to a registered definition.
  Unknown actions are rejected; the definition's risk/permissions are
  authoritative (they may raise, never lower, the declared risk).
* **C3 PolicyEngine** — each executable action is evaluated for the actor.
* **C4 structure** — already validated by ``validate_graph_structure``.

Guarantees
----------
* An unknown action can never enter a runnable graph.
* A node cannot execute unless the C3 engine explicitly authorizes it.
* ``DENY`` blocks the entire graph. ``ASK`` does *not* block: the node is
  recorded as requiring approval, and C5 must not run it until approval exists.
* Authorization is all-or-nothing — partial authorization is never returned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.graph.errors import GraphAuthorizationError
from actionmesh.core.graph.validator import validate_graph_structure
from actionmesh.core.permissions.context import PolicyContext, build_context
from actionmesh.core.permissions.engine import PolicyEngine
from actionmesh.core.permissions.risk import risk_rank
from actionmesh.core.registry.registry import ActionRegistry
from actionmesh.protocol.models import ActionGraph, GraphNode, PolicyDecision, RiskLevel


@dataclass(frozen=True)
class NodeAuthorization:
    """Per-node authorization verdict."""

    node_id: str
    action_type: str
    decision: str
    risk: RiskLevel
    permissions: tuple[str, ...] = ()
    requires_approval: bool = False
    authorized: bool = False
    reason: str = ""


@dataclass(frozen=True)
class GraphAuthorization:
    """Result of authorizing an entire graph. All-or-nothing."""

    graph_id: str
    ok: bool
    nodes: tuple[NodeAuthorization, ...] = field(default_factory=tuple)
    blocking: tuple[str, ...] = field(default_factory=tuple)
    highest_risk: RiskLevel = RiskLevel.LOW

    @property
    def needs_approval(self) -> tuple[str, ...]:
        return tuple(n.node_id for n in self.nodes if n.requires_approval and n.authorized)

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "ok": self.ok,
            "highest_risk": self.highest_risk.value,
            "needs_approval": list(self.needs_approval),
            "nodes": [
                {
                    "node_id": n.node_id,
                    "action_type": n.action_type,
                    "decision": n.decision,
                    "risk": n.risk.value,
                    "permissions": list(n.permissions),
                    "requires_approval": n.requires_approval,
                    "authorized": n.authorized,
                    "reason": n.reason,
                }
                for n in self.nodes
            ],
        }



def _node_actions(node: GraphNode) -> list[tuple[str, Any]]:
    """Actions a node would run: its own action plus any compensation action."""
    out: list[tuple[str, Any]] = []
    if node.action is not None:
        out.append((node.action.type, node.action))
    if node.compensation is not None:
        out.append((node.compensation.type, node.compensation))
    return out


def authorize_graph(
    graph: ActionGraph,
    engine: PolicyEngine,
    registry: ActionRegistry,
    context: PolicyContext,
) -> GraphAuthorization:
    """Authorize every executable node, or reject the whole graph.

    Raises :class:`GraphAuthorizationError` if any node cannot be resolved or is
    denied. On success the caller must still honour ``requires_approval`` before
    executing (C5's responsibility).
    """
    validate_graph_structure(graph)

    results: list[NodeAuthorization] = []
    blocking: set[str] = set()
    highest = RiskLevel.LOW

    for node in sorted(graph.nodes, key=lambda n: n.node_id):
        pairs = _node_actions(node)
        if not pairs:
            # Control nodes (END, WAIT, CONDITION, APPROVAL) cause no side effect
            # of their own; their dependencies gate them.
            results.append(
                NodeAuthorization(
                    node_id=node.node_id,
                    action_type=node.node_type.value,
                    decision=PolicyDecision.ALLOW.value,
                    risk=RiskLevel.LOW,
                    authorized=True,
                    reason="control node: no side effect",
                )
            )
            continue

        permissions: set[str] = set()
        node_risk = RiskLevel.LOW
        needs_approval = False
        verdict: NodeAuthorization | None = None

        for action_type, action in pairs:
            try:
                resolved = registry.resolve(action)
            except Exception as exc:  # unknown action / invalid params -> block
                verdict = NodeAuthorization(
                    node_id=node.node_id,
                    action_type=action_type,
                    decision=PolicyDecision.DENY.value,
                    risk=RiskLevel.HIGH,
                    authorized=False,
                    reason=f"registry rejected {action_type!r}: {exc}",
                )
                break

            permissions.update(resolved.definition.permissions)
            risk = RiskLevel(resolved.effective_risk)
            if risk_rank(risk) > risk_rank(node_risk):
                node_risk = risk

            outcome = engine.evaluate(
                build_context(
                    resolved.action,
                    resolved.definition,
                    context.actor,
                    grants=context.grants,
                    policy=context.policy,
                    approvals=context.approvals,
                    allow_high_risk=context.allow_high_risk,
                    now=context.now,
                )
            )

            # DENY is the only blocking decision. ASK means "permitted pending
            # user approval": the node is recorded as needing approval and C5
            # must not run it until that approval actually exists.
            if outcome.decision is PolicyDecision.DENY:
                verdict = NodeAuthorization(
                    node_id=node.node_id,
                    action_type=action_type,
                    decision=outcome.decision.value,
                    risk=risk,
                    permissions=tuple(sorted(permissions)),
                    requires_approval=False,
                    authorized=False,
                    reason="; ".join(outcome.reasons) or "policy denied",
                )
                break

            if outcome.decision is PolicyDecision.ASK or outcome.requires_approval:
                needs_approval = True

        if verdict is not None:
            blocking.add(node.node_id)
            results.append(verdict)
            if risk_rank(verdict.risk) > risk_rank(highest):
                highest = verdict.risk
            continue

        results.append(
            NodeAuthorization(
                node_id=node.node_id,
                action_type=pairs[0][0],
                decision=(
                    PolicyDecision.ASK.value
                    if needs_approval
                    else PolicyDecision.ALLOW.value
                ),
                risk=node_risk,
                permissions=tuple(sorted(permissions)),
                requires_approval=needs_approval,
                authorized=True,
                reason=(
                    "authorized pending user approval"
                    if needs_approval
                    else "policy allowed"
                ),
            )
        )
        if risk_rank(node_risk) > risk_rank(highest):
            highest = node_risk

    if blocking:
        raise GraphAuthorizationError(
            "graph nodes not authorized: " + ", ".join(sorted(blocking))
        )

    return GraphAuthorization(
        graph_id=graph.graph_id,
        ok=True,
        nodes=tuple(sorted(results, key=lambda n: n.node_id)),
        blocking=(),
        highest_risk=highest,
    )


__all__ = [
    "GraphAuthorization",
    "NodeAuthorization",
    "authorize_graph",
]
