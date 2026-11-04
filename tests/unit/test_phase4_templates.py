"""Phase 4 (Agent 5) — new read-only web.search template + booking honesty.

Proves the new ``web.search`` template and the P1-2 boundary:

* every step action of every template (incl. web.search) is in the real
  ``IMPLEMENTED`` set (runtime check, not a stub);
* every ``{{field}}`` placeholder is a required_field or a submit review
  field (journey/passenger/fare/items/total) — nothing invented;
* web.search is read-only: only browser.open/search/snapshot/inspect, no
  submit, no invented results/prices;
* unselected refs ask clarification (selected.* / order.* never defaulted);
* build_workflow never executes submit — it only builds values.

Labels: web.search = FIXTURE-TESTED; booking.submit/order.place = NOT-IMPLEMENTED
(provider PERMANENT refusal; build path never executes them).
"""

from __future__ import annotations

import re
import unittest

from actionmesh.agent.definitions import IMPLEMENTED
from actionmesh.voiceos.website_workflows import (
    BOOKING_SUBMIT_ACTION,
    CRITICAL_SUBMIT_ACTIONS,
    TEMPLATES,
    build_workflow,
    clarification_field,
    extract_website_fields,
    match_website_intent,
    resolve_template,
    submit_step,
    substitute,
)

_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}")
# Fields that appear only on submit steps as the reviewed operation
# (journey/passenger/fare/items/total) — built from required fields at
# runtime, never user-supplied verbatim nor invented.
_SUBMIT_REVIEW_FIELDS = {"journey", "passenger", "fare", "items", "total",
                         "order.items", "order.total"}


def _placeholders(template) -> set[str]:
    ph: set[str] = set()
    for step in template.steps:
        for value in step.get("parameters", {}).values():
            if isinstance(value, str):
                ph.update(_PLACEHOLDER.findall(value))
    return ph


class TestEveryTemplateStepIsImplemented(unittest.TestCase):
    def test_all_steps_in_real_implemented(self) -> None:
        for name, template in TEMPLATES.items():
            for i, step in enumerate(template.steps):
                self.assertIn(
                    step["action"], IMPLEMENTED,
                    f"{name} step {i} names {step['action']!r} not in IMPLEMENTED",
                )

    def test_web_search_template_exists(self) -> None:
        self.assertIn("web.search", TEMPLATES)
        self.assertEqual(resolve_template("web.search").name, "web.search")


class TestPlaceholderCoverage(unittest.TestCase):
    def test_every_placeholder_is_required_or_submit_review(self) -> None:
        for name, template in TEMPLATES.items():
            if name == "site.workflow":
                continue  # generic DSL shape; placeholders are caller-supplied
            uncovered = _placeholders(template) - set(template.required_fields) - _SUBMIT_REVIEW_FIELDS
            self.assertEqual(uncovered, set(), f"{name}: uncovered placeholders {sorted(uncovered)}")

    def test_web_search_placeholders_all_required(self) -> None:
        template = TEMPLATES["web.search"]
        self.assertEqual(_placeholders(template), set(template.required_fields))
        self.assertEqual(_placeholders(template), {"web.query"})


class TestWebSearchIsReadOnly(unittest.TestCase):
    def setUp(self) -> None:
        self.template = TEMPLATES["web.search"]

    def test_no_submit(self) -> None:
        self.assertIsNone(submit_step(self.template))
        actions = {s["action"] for s in self.template.steps}
        self.assertEqual(actions & set(CRITICAL_SUBMIT_ACTIONS), set())

    def test_only_read_only_browser_actions(self) -> None:
        allowed = {"browser.open", "browser.search", "browser.snapshot", "browser.inspect"}
        actions = {s["action"] for s in self.template.steps}
        self.assertTrue(actions <= allowed, f"unexpected actions: {actions - allowed}")

    def test_actually_searches_and_reads_back(self) -> None:
        actions = [s["action"] for s in self.template.steps]
        self.assertIn("browser.search", actions)
        self.assertIn("browser.snapshot", actions)

    def test_no_invented_result_language(self) -> None:
        forbidden = ("booked", "booking confirmed", "reference created", "order placed",
                     "price is", "rate is", "temperature is")
        for step in self.template.steps:
            words = " ".join(
                [step["label"], *(str(v) for v in step.get("parameters", {}).values())]
            ).lower()
            for phrase in forbidden:
                self.assertNotIn(phrase, words)

    def test_match_and_extract_round_trip(self) -> None:
        self.assertEqual(match_website_intent("search the web for photosynthesis"), "web.search")
        fields = extract_website_fields("search the web for photosynthesis", "web.search")
        self.assertEqual(fields, {"web.query": "photosynthesis"})

    def test_bare_search_asks_clarification_no_invented_query(self) -> None:
        fields = extract_website_fields("search the web", "web.search")
        self.assertEqual(fields, {})
        self.assertEqual(clarification_field(self.template, fields), "web.query")


class TestWebIntentDoesNotShadow(unittest.TestCase):
    """The web template must not steal intents other paths already own.

    Regression pins (Phase 4 full suite): the first broad ``_WEB_SEARCH_RX``
    matched "search for ..." and "look up ...", so

    * "search for speed render near me" (a food discovery request) became
      ``web.search`` instead of ``food.discover``, and
    * "look up actionmesh" (a generic search the LLM planner resolves to the
      registered ``search.web`` action) was intercepted by ``_website_step``
      before the planner ever ran.

    The web template now requires explicit web/internet/Google wording.
    """

    def test_search_speed_render_is_food_discover_not_web_search(self) -> None:
        text = "search for speed render near me"
        self.assertEqual(match_website_intent(text), "food.discover")
        self.assertEqual(
            extract_website_fields(text, "food.discover"), {"food.query": "speed render"}
        )

    def test_look_up_falls_through_to_the_planner_not_web_template(self) -> None:
        self.assertIsNone(match_website_intent("look up actionmesh"))

    def test_explicit_web_wording_still_matches(self) -> None:
        self.assertEqual(match_website_intent("search the web for cats"), "web.search")
        self.assertEqual(match_website_intent("search the internet for cats"), "web.search")
        self.assertEqual(match_website_intent("web search for cats"), "web.search")
        self.assertEqual(
            extract_website_fields("search the web for cats", "web.search"),
            {"web.query": "cats"},
        )
        # A bare "google X" is web, not food.
        self.assertEqual(match_website_intent("google cats"), "web.search")

    def test_missing_placeholder_substitutes_empty_fail_closed(self) -> None:
        self.assertEqual(substitute("{{web.query}}", {}), "")
        self.assertEqual(substitute("{{web.query}}", {"web.query": "x"}), "x")


class TestUnselectedRefsAskClarification(unittest.TestCase):
    def test_food_order_without_item_ref_asks(self) -> None:
        template = TEMPLATES["food.order"]
        self.assertEqual(
            clarification_field(template, {"food.query": "pizza"}),
            "selected.restaurant_ref",
        )
        self.assertEqual(
            clarification_field(template, {"food.query": "pizza",
                                           "selected.restaurant_ref": "r1"}),
            "selected.item_ref",
        )

    def test_train_book_without_train_ref_asks(self) -> None:
        template = TEMPLATES["train.book"]
        provided = {f: "x" for f in template.required_fields if f != "selected.train_ref"}
        self.assertEqual(clarification_field(template, provided), "selected.train_ref")

    def test_no_default_ref_in_build(self) -> None:
        template = TEMPLATES["food.order"]
        workflow = build_workflow(
            template, actor_id="owner", known_action=lambda a: a in IMPLEMENTED,
            parameters={"food.query": "pizza"},
        )
        click_steps = [s for s in workflow.steps if s.action_type == "browser.click"]
        for step in click_steps:
            self.assertEqual(step.parameters.get("ref"), "", "unselected ref must stay empty, never defaulted")


class TestSubmitNeverExecutesInBuildPath(unittest.TestCase):
    def test_build_only_builds_values(self) -> None:
        for name in ("train.book", "food.order"):
            template = TEMPLATES[name]
            params = dict.fromkeys(template.required_fields, "x")
            workflow = build_workflow(
                template, actor_id="owner", known_action=lambda a: a in IMPLEMENTED,
                parameters=params,
            )
            submit = [s for s in workflow.steps if s.action_type in CRITICAL_SUBMIT_ACTIONS]
            self.assertEqual(len(submit), 1, name)
            step = submit[0]
            # Built (parameters present) but not executed: no result/state attached.
            self.assertTrue(hasattr(step, "parameters"))
            self.assertFalse(hasattr(step, "result") and step.result,
                             f"{name}: build path must not attach a result")
            self.assertFalse(hasattr(step, "booking_reference"), f"{name}: no invented reference")
            self.assertFalse(hasattr(step, "payment_status"), f"{name}: no invented payment status")

    def test_submit_params_come_from_required_fields_only(self) -> None:
        # fare/journey/passenger placeholders substitute from parameters;
        # when absent they become "" (fail-closed), never a computed default.
        template = TEMPLATES["train.book"]
        workflow = build_workflow(
            template, actor_id="owner", known_action=lambda a: a in IMPLEMENTED,
            parameters={"journey.origin": "NZM"},
        )
        submit = next(s for s in workflow.steps if s.action_type == BOOKING_SUBMIT_ACTION)
        for key in ("journey", "passenger", "fare"):
            self.assertEqual(submit.parameters.get(key), "",
                             f"{key} must be '' when no value supplied, got {submit.parameters.get(key)!r}")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
