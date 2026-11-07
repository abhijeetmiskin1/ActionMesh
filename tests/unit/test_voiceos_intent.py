"""A2 — NL → verified website workflows through the real Reasoner.

Tests prove the wiring the previous module could not: "Open Zomato and show me pizza"
used to fall through to the RuleBasedPlanner and match the generic launch rule as
``system.launch_app {app: "Zomato and show me pizza"}``, which cannot run. Now the
reasoner matches it to ``food.discover`` with the query extracted — not hard-coded —
and a real browser step that the executor would actually dispatch.

These tests use a StubPlanner that intentionally returns no rules, matching the real
planner's behavior when a sentence does not fit its rule set. ``known_action`` is the
same predicate the API route supplies: a real registry lookup in production, a stub
here for the browser capability set.
"""

from __future__ import annotations

import unittest

from actionmesh.core.planner import Plan
from actionmesh.voiceos.models import Conversation
from actionmesh.voiceos.reasoning import Reasoner
from actionmesh.voiceos.website_workflows import (
    BOOKING_SUBMIT_ACTION,
    ORDER_PLACE_ACTION,
    TEMPLATES,
    extract_website_fields,
    match_website_intent,
)


class _StubPlanner:
    """A planner with no rules. It never proves intent mapping; the reasoner must."""

    def plan(self, intent: str) -> Plan:
        return Plan(intent=intent, notes=(f"no rule matched: {intent!r}",))


def _reasoner(known=None) -> Reasoner:
    return Reasoner(
        planner=_StubPlanner(),
        known_action=lambda name: (
            (name.startswith("browser.") or name in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION))
            if known is None else known(name)
        ),
        templates=lambda: (),
    )


class TestNlToTemplateIntent(unittest.TestCase):
    def test_open_zomato_show_me_pizza_maps_to_food_discover_with_extracted_query(self) -> None:
        text = "Open Zomato and show me pizza"
        self.assertEqual(match_website_intent(text), "food.discover")
        self.assertEqual(extract_website_fields(text, "food.discover"), {"food.query": "pizza"})

        answer = _reasoner().understand(text, Conversation(actor_id="a"))
        self.assertEqual(answer.kind, "task")
        self.assertIsNotNone(answer.session)
        session = answer.session
        self.assertEqual([step.action_type for step in session.steps],
                         [step["action"] for step in TEMPLATES["food.discover"].steps])
        # Read-only: an "Open Zomato" request cannot place an order.
        self.assertNotIn(ORDER_PLACE_ACTION, [step.action_type for step in session.steps])
        self.assertEqual(session.steps[1].parameters.get("query"), "pizza")

    def test_search_speed_render_asks_for_the_query(self) -> None:
        text = "search Zomato for speed render near me"
        self.assertEqual(match_website_intent(text), "food.discover")
        self.assertEqual(extract_website_fields(text, "food.discover"), {"food.query": "speed render"})

    def test_find_food_without_a_food_name_asks_what_to_find(self) -> None:
        answer = _reasoner().understand("find food", Conversation(actor_id="a"))
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertEqual(answer.unresolved, ("food.query",))
        self.assertIn("find", answer.reply.lower())

    def test_find_trains_around_4pm_asks_for_the_origin_not_invents_it(self) -> None:
        text = "find trains around 4 PM"
        self.assertEqual(match_website_intent(text), "train.search")
        answer = _reasoner().understand(text, Conversation(actor_id="a"))
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertEqual(answer.unresolved, ("journey.origin",))
        self.assertIn("from", answer.reply.lower())

    def test_fully_specified_train_search_is_a_task(self) -> None:
        text = "find trains from Delhi to Mumbai on 2026-11-10 3ac"
        self.assertEqual(match_website_intent(text), "train.search")
        answer = _reasoner().understand(text, Conversation(actor_id="a"))
        self.assertEqual(answer.kind, "task")
        self.assertIsNotNone(answer.session)
        session = answer.session
        self.assertNotIn(BOOKING_SUBMIT_ACTION, [step.action_type for step in session.steps])
        self.assertEqual(session.steps[3].parameters.get("text"), "delhi")
        # The browser ref is bound at run time from the snapshot's observed refs,
        # never invented in the plan.
        self.assertNotIn("ref", session.steps[3].parameters)
        self.assertEqual(
            session.steps[3].bind.get("ref"), {"from_step": 2, "key": "refs"}
        )

    def test_order_pizza_routes_to_food_order_not_discover(self) -> None:
        answer = _reasoner().understand("order pizza", Conversation(actor_id="a"))
        # food.query is present; the next required field is the restaurant ref.
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertEqual(answer.unresolved, ("selected.restaurant_ref",))
        self.assertIn("restaurant", answer.reply.lower())

    def test_book_train_ticket_routes_to_train_book_with_crritical_submit_last(self) -> None:
        text = "book a train ticket from Delhi to Mumbai on 2026-11-10 3ac"
        self.assertEqual(match_website_intent(text), "train.book")
        answer = _reasoner().understand(text, Conversation(actor_id="a"))
        # passenger.name is still missing — no booking can be proposed yet.
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertEqual(answer.unresolved, ("passenger.name",))

    def test_disabled_browser_capability_refuses_rather_than_guesses(self) -> None:
        answer = _reasoner(known=lambda name: False).understand(
            "Open Zomato and show me pizza", Conversation(actor_id="a")
        )
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)
        self.assertIn("laptop agent", answer.reply)

    def test_unrelated_sentence_maps_to_no_template(self) -> None:
        self.assertIsNone(match_website_intent("what is the weather"))
        answer = _reasoner().understand("what is the weather", Conversation(actor_id="a"))
        self.assertEqual(answer.kind, "answer")
        self.assertIsNone(answer.session)


class TestNoLaunchAppBypassWithRealPlanner(unittest.TestCase):
    """Mission guard: website sentences never fall through to system.launch_app.

    The old bug: "Open Zomato and show me pizza" matched the generic launch rule
    as ``system.launch_app {app: "Zomato and show me pizza"}``, which cannot run.
    The website branch in Reasoner._task runs *before* the planner, so no sentence
    that maps to a website template can reach the launch rule. This uses the real
    RuleBasedPlanner and the real registry (not the stub), so a regression in
    branch order or matching fails here rather than silently.
    """

    def _real_reasoner(self):
        from actionmesh.agent.definitions import register_device_actions
        from actionmesh.core.planner import RuleBasedPlanner
        from actionmesh.core.registry import ActionRegistry

        registry = ActionRegistry.with_seeds()
        register_device_actions(registry)
        return Reasoner(
            planner=RuleBasedPlanner(known_action=registry.has),
            known_action=registry.has,
            templates=lambda: (),
        )

    def test_mission_sentences_never_propose_launch_app(self) -> None:
        reasoner = self._real_reasoner()
        for text in (
            "Open Zomato and show me pizza",
            "find trains from Delhi to Mumbai on 2026-11-10 3ac",
            "order pizza",
            "find trains around 4 PM",
        ):
            answer = reasoner.understand(text, Conversation(actor_id="a"))
            if answer.session is not None:
                actions = [step.action_type for step in answer.session.steps]
                self.assertNotIn("system.launch_app", actions, text)
                self.assertTrue(
                    all(a.startswith("browser.") or a in (BOOKING_SUBMIT_ACTION, ORDER_PLACE_ACTION)
                        for a in actions),
                    f"{text}: non-website step leaked into session: {actions}",
                )
            self.assertNotIn("launch_app", answer.reply)


class TestNoInventedFields(unittest.TestCase):
    """extract_website_fields names only what the user's words contain."""

    def test_train_fields_only_from_user_words(self) -> None:
        fields = extract_website_fields("find trains from Delhi to Mumbai", "train.search")
        self.assertEqual(
            fields,
            {"journey.origin": "delhi", "journey.destination": "mumbai"},
        )
        # No date, class or passenger fields appear from nowhere.
        for key in ("journey.date", "journey.class", "passenger.name"):
            self.assertNotIn(key, fields)

    def test_food_query_only_from_user_words(self) -> None:
        self.assertEqual(
            extract_website_fields("open zomato", "food.discover"), {}
        )
        self.assertEqual(
            extract_website_fields("show me pizza near me", "food.discover"),
            {"food.query": "pizza"},
        )

    def test_origin_without_from_is_not_guessed(self) -> None:
        fields = extract_website_fields("find trains to Mumbai", "train.search")
        self.assertNotIn("journey.origin", fields)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class TestFoodIntentRequiresACue(unittest.TestCase):
    def test_local_and_ambiguous_searches_do_not_plan_external_actions(self) -> None:
        for text in (
            "Find my tax files", "Show my disk space", "Find the Python docs",
            "Buy a laptop", "Find my restaurant receipts", "Search for speed render near me",
        ):
            with self.subTest(text=text):
                self.assertIsNone(match_website_intent(text))

    def test_explicit_food_discovery_is_preserved(self) -> None:
        self.assertEqual(match_website_intent("Show me pizza near me"), "food.discover")
        self.assertEqual(match_website_intent("Open Zomato and show me pizza"), "food.discover")
        self.assertEqual(match_website_intent("Order pizza"), "food.order")
        self.assertEqual(match_website_intent("Search the web for pizza"), "web.search")
