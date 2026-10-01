"""C4 — Action Graph: structure, ordering, serialization."""

from __future__ import annotations

import json
import unittest

from actionmesh.core.graph import (
    GraphBuilder,
    GraphCycleError,
    GraphValidationError,
    NodeConfigError,
    compensation_map,
    dependents,
    execution_order,
    find_cycle,
    graph_from_actions,
    graph_from_dict,
    graph_from_json,
    graph_to_dict,
    graph_to_json,
    root_nodes,
    topological_layers,
    visualization,
)
from actionmesh.protocol.models import (
    Action,
    ActionGraph,
    GraphNode,
    NodeType,
)
from actionmesh.protocol.validate import ValidationError


def msg(text: str = "hi") -> dict[str, object]:
    return {"type": "message.send", "parameters": {"message": text}}


def cal(title: str = "e") -> dict[str, object]:
    return {"type": "calendar.create", "parameters": {"title": title}}


def _builder() -> GraphBuilder:
    b = GraphBuilder(intent="test graph")
    b.action(msg("hello"), node_id="a1")
    b.action(cal("standup"), node_id="a2", depends_on=["a1"])
    b.end(depends_on=["a2"])
    return b


class TestGraphBuilder(unittest.TestCase):
    def test_builds_valid_linear_graph(self) -> None:
        graph = _builder().build()
        self.assertEqual(graph.intent, "test graph")
        self.assertEqual([n.node_id for n in graph.nodes], ["a1", "a2", "end"])

    def test_graph_from_actions_builds_sequence(self) -> None:
        graph = graph_from_actions([msg(), cal()], intent="seq")
        self.assertEqual(len(graph.nodes), 3)
        self.assertEqual(execution_order(graph)[-1], "end")

    def test_graph_from_zero_actions_rejected(self) -> None:
        with self.assertRaises(GraphValidationError):
            graph_from_actions([])

    def test_action_dict_is_validated_as_untrusted(self) -> None:
        with self.assertRaises(ValidationError):
            GraphBuilder().action({"type": "message.send", "parameters": "not-a-dict"})

    def test_action_without_type_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            GraphBuilder().action({"parameters": {}})

    def test_nodes_property_exposes_accumulated_nodes(self) -> None:
        self.assertEqual(len(_builder().nodes), 3)

    def test_auto_ids_are_deterministic(self) -> None:
        b1, b2 = GraphBuilder("x"), GraphBuilder("x")
        b1.action(msg())
        b2.action(msg())
        self.assertEqual(b1.nodes[0].node_id, b2.nodes[0].node_id)

    def test_end_node_id_is_conventional(self) -> None:
        self.assertEqual(_builder().nodes[-1].node_id, "end")


class TestGraphValidation(unittest.TestCase):
    def test_duplicate_node_ids_rejected(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="dup")
        b.action(msg("b"), node_id="dup")
        b.end()
        with self.assertRaises(GraphValidationError):
            b.build()

    def test_unknown_dependency_rejected(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1", depends_on=["ghost"])
        b.end()
        with self.assertRaises(GraphValidationError):
            b.build()

    def test_condition_requires_expression(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.condition("   ", node_id="c1", depends_on=["a1"])
        b.end(depends_on=["c1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_wait_requires_positive_seconds(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.wait(0, node_id="w1", depends_on=["a1"])
        b.end(depends_on=["w1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_wait_seconds_bounded(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.wait(10**9, node_id="w1", depends_on=["a1"])
        b.end(depends_on=["w1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_retry_attempts_bounded(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.retry("a1", max_attempts=999, node_id="r1")
        b.end(depends_on=["r1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_retry_requires_a_target(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.add(GraphNode(node_id="r1", node_type=NodeType.RETRY, max_attempts=2))
        b.end(depends_on=["a1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_compensation_requires_target(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.add(
            GraphNode(
                node_id="c1",
                node_type=NodeType.COMPENSATION,
                compensation=Action(type="message.send"),
            )
        )
        b.end(depends_on=["c1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_compensation_requires_compensating_action(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.add(GraphNode(node_id="c1", node_type=NodeType.COMPENSATION, compensates="a1"))
        b.end(depends_on=["c1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_compensation_of_unknown_node_rejected(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.compensate("ghost", msg("undo"), node_id="c1")
        b.end(depends_on=["c1"])
        with self.assertRaises(NodeConfigError):
            b.build()

    def test_approval_node_must_guard_something(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.approval(node_id="ap1")
        b.end(depends_on=["a1"])
        with self.assertRaises(NodeConfigError):
            b.build()


class TestCycleDetection(unittest.TestCase):
    def test_two_node_cycle_rejected(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1", depends_on=["a2"])
        b.action(msg("b"), node_id="a2", depends_on=["a1"])
        b.end()
        with self.assertRaises(GraphCycleError):
            b.build()

    def test_three_node_cycle_rejected(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1", depends_on=["a3"])
        b.action(msg("b"), node_id="a2", depends_on=["a1"])
        b.action(msg("c"), node_id="a3", depends_on=["a2"])
        b.end()
        with self.assertRaises(GraphCycleError):
            b.build()

    def test_find_cycle_returns_none_for_dag(self) -> None:
        self.assertIsNone(find_cycle(_builder().build().nodes))

    def test_find_cycle_returns_closed_path(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1", depends_on=["a2"])
        b.action(msg("b"), node_id="a2", depends_on=["a1"])
        b.end()
        cycle = find_cycle(b.nodes)
        self.assertIsNotNone(cycle)
        assert cycle is not None
        self.assertEqual(cycle[0], cycle[-1])



class TestOrdering(unittest.TestCase):
    def test_layers_group_independent_nodes(self) -> None:
        b = GraphBuilder()
        b.action(msg(), node_id="a1")
        b.action(msg("b"), node_id="a2")
        b.end(depends_on=["a1", "a2"])
        layers = topological_layers(b.build())
        self.assertEqual(layers[0], ["a1", "a2"])
        self.assertEqual(layers[-1], ["end"])

    def test_order_is_deterministic_across_insertion_order(self) -> None:
        b2 = GraphBuilder(intent="test graph")
        b2.end(node_id="end", depends_on=["a2"])
        b2.action(cal("standup"), node_id="a2", depends_on=["a1"])
        b2.action(msg("hello"), node_id="a1")
        self.assertEqual(execution_order(_builder().build()), execution_order(b2.build()))

    def test_dependents_reverse_map(self) -> None:
        rev = dependents(_builder().build())
        self.assertEqual(rev["a1"], ["a2"])
        self.assertEqual(rev["a2"], ["end"])

    def test_root_nodes(self) -> None:
        self.assertEqual(root_nodes(_builder().build()), ["a1"])

    def test_compensation_map(self) -> None:
        b = GraphBuilder()
        b.action(cal("event"), node_id="a1")
        b.compensate("a1", cal("undo"), node_id="c1")
        b.end(depends_on=["a1"])
        self.assertEqual(compensation_map(b.build()), {"a1": "c1"})

    def test_visualization_is_json_safe_and_ordered(self) -> None:
        graph = _builder().build()
        view = visualization(graph)
        json.dumps(view)
        self.assertEqual(view["order"], execution_order(graph))
        self.assertEqual(view["roots"], ["a1"])


class TestDeepChain(unittest.TestCase):
    def test_deep_chain_does_not_recurse(self) -> None:
        b = GraphBuilder()
        prev: list[str] = []
        for i in range(2000):
            prev = [b.action(msg(), node_id=f"n{i}", depends_on=prev).node_id]
        b.end(depends_on=prev)
        self.assertEqual(len(execution_order(b.build())), 2001)


class TestSerialization(unittest.TestCase):
    def _rich_graph(self) -> ActionGraph:
        b = GraphBuilder(intent="round trip")
        b.action(msg("hello"), node_id="a1", label="greeting")
        b.wait(30.0, node_id="w1", depends_on=["a1"])
        b.retry("a1", max_attempts=4, node_id="r1")
        b.action(cal("e"), node_id="a2", depends_on=["w1", "r1"])
        b.compensate("a2", cal("undo"), node_id="c1")
        b.end(depends_on=["a2", "c1"])
        return b.build()

    def test_round_trip_preserves_semantics(self) -> None:
        original = self._rich_graph()
        restored = graph_from_json(graph_to_json(original))
        self.assertEqual(restored.intent, original.intent)
        self.assertEqual(restored.graph_id, original.graph_id)
        # Serialization is canonical (sorted by node_id), so compare ids as sets
        # and compare *semantics* via the deterministic execution order.
        self.assertEqual(
            sorted(n.node_id for n in restored.nodes),
            sorted(n.node_id for n in original.nodes),
        )
        self.assertEqual(execution_order(restored), execution_order(original))
        for before, after in zip(
            sorted(original.nodes, key=lambda n: n.node_id),
            sorted(restored.nodes, key=lambda n: n.node_id),
            strict=True,
        ):
            self.assertEqual(before.node_type, after.node_type)
            self.assertEqual(before.depends_on, after.depends_on)
            self.assertEqual(before.label, after.label)
            self.assertEqual(before.condition, after.condition)
            self.assertEqual(before.wait_s, after.wait_s)
            self.assertEqual(before.max_attempts, after.max_attempts)
            self.assertEqual(before.compensates, after.compensates)
            self.assertEqual(
                before.action.model_dump() if before.action else None,
                after.action.model_dump() if after.action else None,
            )

    def test_repeated_serialization_is_byte_identical(self) -> None:
        graph = self._rich_graph()
        self.assertEqual(graph_to_json(graph), graph_to_json(graph))

    def test_node_order_is_canonical(self) -> None:
        b2 = GraphBuilder(intent="test graph")
        b2.end(node_id="end", depends_on=["a2"])
        b2.action(cal("standup"), node_id="a2", depends_on=["a1"])
        b2.action(msg("hello"), node_id="a1")
        self.assertEqual(
            [n["node_id"] for n in graph_to_dict(_builder().build())["nodes"]],
            [n["node_id"] for n in graph_to_dict(b2.build())["nodes"]],
        )



class TestMalformedSerialization(unittest.TestCase):
    def test_unknown_node_type_rejected(self) -> None:
        payload = {
            "nodes": [
                {"node_id": "a1", "node_type": "teleport", "action": msg()},
                {"node_id": "end", "node_type": "end"},
            ]
        }
        with self.assertRaises(GraphValidationError):
            graph_from_dict(payload)

    def test_malformed_graph_rejected(self) -> None:
        for payload in (
            "not a dict",
            {"nodes": "nope"},
            {"nodes": [{"node_id": "a1", "node_type": "action", "depends_on": [1]}]},
            {"nodes": [{"node_id": "a1", "node_type": "action", "metadata": []}]},
            {"nodes": [{"node_id": "a1", "node_type": "action", "wait_s": "soon"}]},
        ):
            with self.subTest(payload=payload), self.assertRaises(GraphValidationError):
                graph_from_dict(payload)

    def test_invalid_json_rejected(self) -> None:
        with self.assertRaises(GraphValidationError):
            graph_from_json("{not json")

    def test_cyclic_serialized_graph_rejected(self) -> None:
        payload = {
            "nodes": [
                {"node_id": "a1", "node_type": "action", "action": msg(), "depends_on": ["a2"]},
                {
                    "node_id": "a2",
                    "node_type": "action",
                    "action": msg("y"),
                    "depends_on": ["a1"],
                },
                {"node_id": "end", "node_type": "end"},
            ]
        }
        with self.assertRaises(GraphCycleError):
            graph_from_dict(payload)

    def test_validate_can_be_skipped_only_explicitly(self) -> None:
        payload = graph_to_dict(_builder().build())
        payload["nodes"] = []  # structurally invalid
        with self.assertRaises(GraphValidationError):
            graph_from_dict(payload)
        self.assertEqual(graph_from_dict(payload, validate=False).nodes, [])


class TestFailureClassification(unittest.TestCase):
    def test_known_failure_kinds(self) -> None:
        from actionmesh.protocol.graphs import classify_failure

        for text, expected in (
            ("rate limit exceeded", "rate_limit"),
            ("connection reset", "network"),
            ("timed out", "timeout"),
            ("unauthorized", "authorization"),
            ("invalid payload", "invalid_input"),
            ("provider exploded", "provider"),
            ("something odd", "unknown"),
        ):
            with self.subTest(text=text):
                self.assertEqual(classify_failure(text).value, expected)


if __name__ == "__main__":
    unittest.main()

