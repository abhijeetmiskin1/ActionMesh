"""C12 developer SDK tests."""

from __future__ import annotations

import unittest

from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import execution_order
from actionmesh.core.permissions import PermissionGrant, PolicyEngine, policy_from_rules
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import Action, ActionGraph, GraphNode, Identity, NodeType, RiskLevel
from actionmesh.providers import default_mock_providers
from actionmesh.sdk import ActionMeshClient


class TestSdk(unittest.TestCase):
    def setUp(self) -> None:
        registry = ActionRegistry.with_seeds()
        self.client = ActionMeshClient(
            ExecutionEngine(registry, PolicyEngine(), default_mock_providers())
        )

    def test_capabilities_are_typed_dicts(self) -> None:
        self.assertEqual(len(self.client.capabilities()), 16)

    def test_execute_uses_core_authorization(self) -> None:
        graph = ActionGraph(nodes=[
            GraphNode(
                node_id="a1",
                action=Action(type="message.send", parameters={"message": "hi"}),
            ),
            GraphNode(node_id="end", node_type=NodeType.END, depends_on=["a1"]),
        ])
        result = self.client.execute(
            graph,
            actor=Identity(id="user-1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=policy_from_rules([{"action": "message.send", "decision": "allow"}]),
        )
        self.assertEqual(result.state.value, "success")

    def test_missing_actor_is_rejected(self) -> None:
        graph = ActionGraph(nodes=[GraphNode(node_id="end", node_type=NodeType.END)])
        with self.assertRaises(ValueError):
            self.client.execute(graph, actor={})

    def test_plan_proposes_without_claiming_privileges(self) -> None:
        plan = self.client.plan("tell mom I'll be late")
        self.assertEqual([action.type for action in plan.actions], ["message.send"])
        self.assertEqual(set(plan.as_payload()["actions"][0]), {"type", "parameters"})

    def test_plan_graph_is_validated_and_ends_in_a_terminal_node(self) -> None:
        graph = self.client.plan_graph("remind me to call mom at 5pm")
        self.assertEqual(
            [node.action.type for node in graph.nodes if node.action], ["reminder.create"]
        )
        self.assertEqual(execution_order(graph)[-1], "end")

    def test_an_injected_planner_is_injectable(self) -> None:
        # The SDK takes a Planner, so a real model can replace the rule-based one
        # without touching the SDK or anything downstream.
        class AlwaysPayment:
            def plan(self, intent: str):
                from actionmesh.core.planner import Plan, ProposedAction

                return Plan(intent=intent, actions=(ProposedAction("payment.prepare", {}),))

        client = ActionMeshClient(
            ExecutionEngine(ActionRegistry.with_seeds(), PolicyEngine(), default_mock_providers()),
            planner=AlwaysPayment(),
        )
        self.assertEqual([a.type for a in client.plan("hi").actions], ["payment.prepare"])


if __name__ == "__main__":
    unittest.main()