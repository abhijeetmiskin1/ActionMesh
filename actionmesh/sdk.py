"""C12 developer SDK for the local ActionMesh execution boundary."""

from __future__ import annotations

import asyncio
from typing import Any

from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph.serialization import graph_from_dict
from actionmesh.core.permissions.context import PermissionGrant, build_context
from actionmesh.core.planner import Plan, Planner, RuleBasedPlanner, plan_to_graph
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import ActionGraph, Identity, Policy
from actionmesh.protocol.validate import validate_action, validate_identity


class ActionMeshClient:
    """Small typed facade; all execution still passes through the core engine."""

    def __init__(self, engine: ExecutionEngine, planner: Planner | None = None) -> None:
        self.engine = engine
        #: Intent -> untrusted candidate plan. The SDK proposes; the policy
        #: engine still decides, and nothing here executes anything.
        self.planner = planner if planner is not None else RuleBasedPlanner()

    @property
    def registry(self) -> ActionRegistry:
        return self.engine.registry

    def capabilities(self, *, provider: str | None = None) -> list[dict[str, Any]]:
        return [cap.model_dump() for cap in self.registry.discover(provider=provider)]

    def plan(self, intent: str) -> Plan:
        """Propose a plan from natural-language intent. Authorizes nothing."""
        return self.planner.plan(intent)

    def plan_graph(self, intent: str) -> ActionGraph:
        """Plan, then cross the trust boundary into a validated graph."""
        return plan_to_graph(self.planner.plan(intent))

    def execute(
        self,
        graph: ActionGraph | dict[str, Any],
        *,
        actor: Identity | dict[str, Any],
        grants: list[PermissionGrant] | None = None,
        policy: Policy | None = None,
        allow_high_risk: bool = False,
    ):
        trusted_graph = graph_from_dict(graph) if isinstance(graph, dict) else graph
        for node in trusted_graph.nodes:
            if node.action is not None:
                node.action = validate_action(node.action)
            if node.compensation is not None:
                node.compensation = validate_action(node.compensation)
        trusted_actor = validate_identity(actor)
        if trusted_actor is None:
            raise ValueError("actor is required")
        context = build_context(
            None,
            None,
            trusted_actor,
            grants=grants,
            policy=policy,
            allow_high_risk=allow_high_risk,
        )
        return asyncio.run(self.engine.execute(trusted_graph, context))

    def approve(self, action_id: str) -> None:
        from actionmesh.protocol.models import ApprovalRequest

        self.engine.submit_approval(ApprovalRequest(action_id=action_id, status="approved"))

    def cancel(self, execution_id: str) -> None:
        self.engine.cancel(execution_id)


__all__ = ["ActionMeshClient"]