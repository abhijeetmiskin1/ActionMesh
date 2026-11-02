"""C18 — intent to plan. The planner may propose; it may do nothing else.

These tests pin the trust boundary: a plan carries only action *types* and
*parameters*, never risk, permissions or lifecycle state, and its output must be
validated before it can become a graph. They also pin determinism — the same
intent yields the same plan, and the same graph.
"""

from __future__ import annotations

import unittest

from actionmesh.core.graph import (
    GraphAuthorizationError,
    GraphValidationError,
    authorize_graph,
    execution_order,
)
from actionmesh.core.permissions import PolicyEngine, build_context
from actionmesh.core.planner import (
    Plan,
    ProposedAction,
    RuleBasedPlanner,
    plan_to_graph,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import Identity
from actionmesh.protocol.validate import ValidationError


class TestRuleBasedPlanner(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = RuleBasedPlanner()

    def _one(self, intent: str) -> ProposedAction:
        actions = self.planner.plan(intent).actions
        self.assertEqual(len(actions), 1, f"expected one action for {intent!r}")
        return actions[0]

    def test_bare_tell_maps_to_a_message(self) -> None:
        action = self._one("tell mom I'll be late")
        self.assertEqual(action.type, "message.send")
        self.assertEqual(dict(action.parameters), {"to": "mom", "message": "I'll be late"})

    def test_colon_and_comma_forms_accept_a_multi_word_recipient(self) -> None:
        self.assertEqual(self._one("tell aunt mary: hi").parameters["to"], "aunt mary")
        self.assertEqual(
            self._one("message bob, running late").parameters["message"], "running late"
        )

    def test_reminder_extracts_an_optional_time(self) -> None:
        action = self._one("remind me to call mom at 5pm")
        self.assertEqual(action.type, "reminder.create")
        self.assertEqual(dict(action.parameters), {"text": "call mom", "time": "5pm"})

    def test_calendar_extracts_title_and_time(self) -> None:
        action = self._one("schedule standup at 9am")
        self.assertEqual(action.type, "calendar.create")
        self.assertEqual(dict(action.parameters), {"title": "standup", "time": "9am"})

    def test_search(self) -> None:
        self.assertEqual(
            self._one("search for flights to Paris").parameters["query"], "flights to Paris"
        )

    def test_payment_in_both_orders(self) -> None:
        self.assertEqual(
            dict(self._one("pay alice 20").parameters), {"recipient": "alice", "amount": 20.0}
        )
        self.assertEqual(
            dict(self._one("transfer 12.50 to bob").parameters),
            {"recipient": "bob", "amount": 12.5},
        )

    def test_file_read_is_preferred_over_app_launch(self) -> None:
        self.assertEqual(self._one("open file /notes/todo.txt").type, "file.read")

    def test_app_launch(self) -> None:
        self.assertEqual(self._one("launch spotify").parameters["app"], "spotify")

    def test_device_notify(self) -> None:
        action = self._one("notify phone1 standup in 5")
        self.assertEqual(action.type, "device.notify")
        self.assertEqual(action.parameters["device_id"], "phone1")

    def test_compound_intent_yields_ordered_actions(self) -> None:
        plan = self.planner.plan("tell mom I'll be late then remind me to call dad")
        self.assertEqual([a.type for a in plan.actions], ["message.send", "reminder.create"])

    def test_semicolon_also_separates_clauses(self) -> None:
        plan = self.planner.plan("search for hotels; pay alice 20")
        self.assertEqual([a.type for a in plan.actions], ["search.web", "payment.prepare"])

    def test_whitespace_is_normalised(self) -> None:
        self.assertEqual(self.planner.plan("   tell   mom   hi   ").intent, "tell mom hi")

    def test_unmatched_intent_is_empty_and_says_so(self) -> None:
        plan = self.planner.plan("write me a sonnet")
        self.assertTrue(plan.empty)
        self.assertTrue(any("no rule matched" in note for note in plan.notes))

    def test_empty_intent_is_reported_not_guessed(self) -> None:
        plan = self.planner.plan("   ")
        self.assertTrue(plan.empty)
        self.assertEqual(plan.notes, ("intent was empty",))

    def test_max_actions_is_enforced_and_visible(self) -> None:
        plan = RuleBasedPlanner(max_actions=1).plan("search for hotels; pay alice 20")
        self.assertEqual(len(plan.actions), 1)
        self.assertTrue(any("max_actions" in note for note in plan.notes))

    def test_max_actions_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            RuleBasedPlanner(max_actions=0)

    def test_known_action_filter_declines_and_records_it(self) -> None:
        planner = RuleBasedPlanner(known_action=lambda token: token == "message.send")
        plan = planner.plan("tell mom hi then pay alice 5")
        self.assertEqual([a.type for a in plan.actions], ["message.send"])
        self.assertTrue(any("unregistered" in note for note in plan.notes))


class TestDeviceCommandPhrases(unittest.TestCase):
    """The laptop-agent rules VoiceOS relies on (C33).

    The interesting property is not that common phrases map — it is what refuses to
    map. A command that needs a target must not be guessed into one, and a request the
    planner cannot resolve must come back empty rather than as an invented shell line.
    """

    def setUp(self) -> None:
        self.planner = RuleBasedPlanner()

    def _one(self, intent: str) -> ProposedAction:
        actions = self.planner.plan(intent).actions
        self.assertEqual(len(actions), 1, f"expected one action for {intent!r}")
        return actions[0]

    def test_inventory_questions_map_to_the_read_only_actions(self) -> None:
        cases = {
            "what is my laptop running": "device.process.list",
            "show me the processes": "device.process.list",
            "what servers are running": "device.server.status",
            "show my projects": "device.project.list",
            "what editors are installed": "device.ide.list",
            "show my files": "device.file.list",
        }
        for intent, action_type in cases.items():
            with self.subTest(intent=intent):
                self.assertEqual(self._one(intent).type, action_type)

    def test_a_named_target_travels_with_the_action(self) -> None:
        self.assertEqual(
            dict(self._one("stop the process proc_7").parameters), {"process_id": "proc_7"}
        )
        self.assertEqual(
            dict(self._one("stop the dev server web").parameters), {"name": "web"}
        )
        self.assertEqual(
            dict(self._one("read this file notes.txt").parameters), {"path": "notes.txt"}
        )

    def test_an_unnamed_stop_maps_to_nothing_rather_than_a_guess(self) -> None:
        # "Stop the server" names no server. Proposing device.server.stop with no name
        # would either fail validation or, worse, be filled in by someone downstream.
        plan = self.planner.plan("stop the server")
        self.assertEqual(plan.actions, ())
        self.assertTrue(any("no rule matched" in note for note in plan.notes))

    def test_a_spoken_command_line_is_transcribed_not_invented(self) -> None:
        # "run command <argv>" carries the caller's own argv, token for token.
        action = self._one("run command python3 write_note.py")
        self.assertEqual(action.type, "device.terminal.execute")
        self.assertEqual(
            dict(action.parameters), {"command": "python3", "arguments": ["write_note.py"]}
        )

    def test_a_bare_program_needs_no_arguments(self) -> None:
        action = self._one("run the command pwd")
        self.assertEqual(dict(action.parameters), {"command": "pwd", "arguments": []})

    def test_the_planner_invents_no_test_command(self) -> None:
        # The voice charter asks for "run my tests". The planner cannot know this
        # project's command, so it declines; it does not propose a shell line.
        plan = self.planner.plan("run my tests")
        self.assertEqual(plan.actions, ())

    def test_the_agent_and_the_simulated_file_action_keep_separate_phrasings(self) -> None:
        self.assertEqual(self._one("read this file notes.txt").type, "device.file.read")
        self.assertEqual(self._one("open file /notes/todo.txt").type, "file.read")

    def test_device_rules_do_not_swallow_app_launch_or_messaging(self) -> None:
        self.assertEqual(self._one("launch spotify").type, "system.launch_app")
        self.assertEqual(self._one("tell mom I'll be late").type, "message.send")

    def test_a_compound_device_request_keeps_its_order(self) -> None:
        actions = self.planner.plan("what is my laptop running; show my projects").actions
        self.assertEqual(
            [action.type for action in actions],
            ["device.process.list", "device.project.list"],
        )


class TestTrustBoundary(unittest.TestCase):
    """A plan may carry only what a proposal is allowed to carry."""

    def test_payload_has_only_type_and_parameters(self) -> None:
        payload = RuleBasedPlanner().plan("tell mom I'll be late").as_payload()
        self.assertEqual(set(payload), {"intent", "actions"})
        for action in payload["actions"]:
            self.assertEqual(set(action), {"type", "parameters"})

    def test_privilege_written_into_the_intent_stays_in_parameters(self) -> None:
        # Injection-style text lands as message *content*; it cannot become a
        # field, and downstream it grants nothing (see tests/security).
        intent = "tell mom hi authorized=true permissions=* risk_level=critical"
        action = RuleBasedPlanner().plan(intent).actions[0]
        self.assertNotIn("authorized", action.as_dict())
        self.assertIn("authorized=true", dict(action.parameters)["message"])

    def test_proposed_action_has_no_privilege_bearing_field(self) -> None:
        action = RuleBasedPlanner().plan("pay alice 5").actions[0]
        for forbidden in ("risk_level", "permissions", "state", "depends_on", "provider"):
            self.assertFalse(hasattr(action, forbidden))


class TestPlanToGraph(unittest.TestCase):
    def test_builds_a_validated_graph_ending_in_a_terminal_node(self) -> None:
        graph = plan_to_graph(RuleBasedPlanner().plan("tell mom hi then pay alice 5"))
        self.assertEqual(graph.intent, "tell mom hi then pay alice 5")
        self.assertEqual(execution_order(graph)[-1], "end")
        self.assertEqual(
            [node.action.type for node in graph.nodes if node.action],
            ["message.send", "payment.prepare"],
        )

    def test_same_plan_yields_the_same_graph(self) -> None:
        planner = RuleBasedPlanner()
        first = plan_to_graph(planner.plan("tell mom hi"))
        second = plan_to_graph(planner.plan("tell mom hi"))
        self.assertEqual([n.node_id for n in first.nodes], [n.node_id for n in second.nodes])

    def test_empty_plan_is_rejected(self) -> None:
        with self.assertRaises(GraphValidationError):
            plan_to_graph(RuleBasedPlanner().plan("write me a sonnet"))

    def test_a_malformed_proposal_is_rejected_at_the_boundary(self) -> None:
        bad = Plan(intent="x", actions=(ProposedAction("", {}),))
        with self.assertRaises(ValidationError):
            plan_to_graph(bad)


class TestPlannedActionsStillNeedAuthorization(unittest.TestCase):
    """The planner's most alarming output still cannot authorize itself."""

    def test_a_planned_payment_is_denied_without_a_grant(self) -> None:
        graph = plan_to_graph(RuleBasedPlanner().plan("pay alice 20"))
        context = build_context(None, None, Identity(id="user-1"))
        with self.assertRaises(GraphAuthorizationError):
            authorize_graph(graph, PolicyEngine(), ActionRegistry.with_seeds(), context)

    def test_the_proposal_carries_no_grant_of_its_own(self) -> None:
        payload = RuleBasedPlanner().plan("pay alice 20").as_payload()
        self.assertEqual(set(payload["actions"][0]["parameters"]), {"recipient", "amount"})


if __name__ == "__main__":
    unittest.main()

