"""Phase 2 Agent 2 — ref-binding into the CRITICAL submit, and the IMPLEMENTED audit.

Gap 4 close-out: ``train.book`` / ``food.order`` templates carry symbolic refs
(``selected.train_ref`` / ``selected.restaurant_ref``) that the user picks from
snapshot results. :func:`build_workflow` must bind a *provided* selection into
the submit step's declared parameters (``booking.submit`` journey /
``order.place`` items) **without executing the submit**, and must leave the
submit unbound when the ref is absent (fail-closed: the clarifier asks, no
session is created, and ``booking.submit``/``order.place`` never succeed — the
provider handler refuses with PERMANENT when no payment backend is configured).

Acceptance label for these tests: **fixture-tested** — they exercise the
template → workflow build path with a real registry; they do not claim any
live booking, order, restaurant or train result.
"""

from __future__ import annotations

import re
import unittest

from actionmesh.agent.definitions import IMPLEMENTED, register_device_actions
from actionmesh.core.registry import ActionRegistry
from actionmesh.voiceos.website_workflows import (
    BOOKING_SUBMIT_ACTION,
    ORDER_PLACE_ACTION,
    TEMPLATES,
    build_workflow,
    clarification_field,
    submit_step,
)

_JOURNEY = {
    "journey.origin": "NZM",
    "journey.destination": "CSMT",
    "journey.date": "2026-11-10",
    "journey.class": "3AC",
    "passenger.name": "Asha",
}


def _registry() -> ActionRegistry:
    reg = ActionRegistry.with_seeds()
    register_device_actions(reg)
    return reg


class TestSelectedRefBindsIntoSubmit(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = _registry()

    def test_food_order_provided_ref_binds_into_submit_items(self) -> None:
        wf = build_workflow(
            TEMPLATES["food.order"],
            actor_id="u1",
            known_action=self.reg.has,
            parameters={
                "food.query": "pizza",
                "selected.restaurant_ref": "r7",
                "order.items": "pizza x1",
                "order.total": "299",
            },
        )
        clicks = [s for s in wf.steps if s.action_type == "browser.click"]
        self.assertEqual(clicks[0].parameters["ref"], "r7")
        submit = [s for s in wf.steps if s.action_type == ORDER_PLACE_ACTION]
        self.assertEqual(len(submit), 1)
        # The user-selected ref reaches the declared `items` param so the
        # CRITICAL gate reviews the exact selection. Nothing executes here —
        # build_workflow only builds a Workflow; the provider's
        # `_submit` still refuses with PERMANENT (no payment backend).
        self.assertEqual(submit[0].parameters["items"], "r7")
        self.assertEqual(submit[0].parameters["total"], "299")
        # The unselected item ref stays fail-closed: "" (never a default).
        self.assertEqual(clicks[1].parameters["ref"], "")

    def test_train_book_provided_ref_binds_into_submit_journey(self) -> None:
        wf = build_workflow(
            TEMPLATES["train.book"],
            actor_id="u1",
            known_action=self.reg.has,
            parameters={
                **_JOURNEY,
                "selected.train_ref": "t9",
                "journey": "NZM-CSMT",
                "passenger": "Asha",
                "fare": "150",
            },
        )
        clicks = [s for s in wf.steps if s.action_type == "browser.click"]
        self.assertEqual(clicks[0].parameters["ref"], "t9")
        submit = [s for s in wf.steps if s.action_type == BOOKING_SUBMIT_ACTION]
        self.assertEqual(len(submit), 1)
        self.assertEqual(submit[0].parameters["journey"], "t9")
        self.assertEqual(submit[0].parameters["passenger"], "Asha")
        self.assertEqual(submit[0].parameters["fare"], "150")

    def test_absent_ref_leaves_submit_unbound_and_clarifier_asks(self) -> None:
        # No selection: the click ref substitutes to "" and the submit is not
        # fed the ref. The clarifier (not substitution) is what stops the run.
        template = TEMPLATES["train.book"]
        self.assertEqual(
            clarification_field(template, _JOURNEY), "selected.train_ref"
        )
        wf = build_workflow(
            template,
            actor_id="u1",
            known_action=self.reg.has,
            parameters={
                **_JOURNEY,
                "journey": "NZM-CSMT",
                "passenger": "Asha",
                "fare": "150",
            },
        )
        clicks = [s for s in wf.steps if s.action_type == "browser.click"]
        self.assertEqual(clicks[0].parameters["ref"], "")
        submit = next(s for s in wf.steps if s.action_type == BOOKING_SUBMIT_ACTION)
        self.assertEqual(submit.parameters["journey"], "NZM-CSMT")  # not "" from ref

    def test_food_order_without_ref_asks(self) -> None:
        self.assertEqual(
            clarification_field(TEMPLATES["food.order"], {"food.query": "pizza"}),
            "selected.restaurant_ref",
        )

    def test_search_templates_do_not_bind(self) -> None:
        # Read-only templates have no submit step; passing a ref changes nothing.
        for name in ("train.search", "food.discover"):
            build_workflow(
                TEMPLATES[name],
                actor_id="u1",
                known_action=self.reg.has,
                parameters={
                    "food.query": "pizza",
                    **_JOURNEY,
                    "selected.restaurant_ref": "r7",
                    "selected.train_ref": "t9",
                },
            )
            self.assertIsNone(submit_step(TEMPLATES[name]))


class TestEveryTemplateActionIsImplemented(unittest.TestCase):
    def test_all_template_steps_are_in_IMPLEMENTED(self) -> None:
        self.assertEqual(len(IMPLEMENTED), 61)
        for name, template in TEMPLATES.items():
            for step in template.steps:
                self.assertIn(
                    step["action"],
                    IMPLEMENTED,
                    f"{name} names {step['action']!r} which has no handler",
                )

    def test_all_template_actions_registered_in_real_registry(self) -> None:
        reg = _registry()
        for name, template in TEMPLATES.items():
            for step in template.steps:
                self.assertTrue(
                    reg.has(step["action"]),
                    f"{name} step names unregistered {step['action']!r}",
                )

    def test_every_placeholder_is_required_or_submit_review_field(self) -> None:
        # Every {{field}} a template substitutes must be either (a) a required
        # field the clarifier asks for, or (b) a submit-step review field the
        # caller supplies at build time (journey/passenger/fare, items/total).
        # A placeholder in neither set would substitute to "" silently.
        review = {"journey", "passenger", "fare", "order.items", "order.total", "selected.item_ref"}
        for name, template in TEMPLATES.items():
            if name == "site.workflow":
                continue  # generic by design; operator fills at build time
            placeholders = {
                match
                for step in template.steps
                for value in step.get("parameters", {}).values()
                for match in re.findall(r"\{\{\s*([A-Za-z0-9_.]+)\s*\}\}", str(value))
            }
            unexplained = placeholders - set(template.required_fields) - review
            self.assertEqual(unexplained, set(), f"{name} unexplained: {unexplained}")


class TestSubmitNeverExecutesInBuildPath(unittest.TestCase):
    """build_workflow only builds; the provider refuses submits with PERMANENT."""

    def test_build_does_not_call_provider(self) -> None:
        # build_workflow takes no provider/engine at all, so by construction it
        # cannot execute. This asserts the signature stays that way.
        import inspect

        params = list(inspect.signature(build_workflow).parameters)
        for forbidden in ("provider", "engine", "executor"):
            self.assertNotIn(forbidden, params)

    def test_selected_ref_does_not_invent_submit_success(self) -> None:
        # Binding a ref sets a parameter string; it never produces a
        # booking/order reference or a success claim.
        wf = build_workflow(
            TEMPLATES["food.order"],
            actor_id="u1",
            known_action=_registry().has,
            parameters={"food.query": "pizza", "selected.restaurant_ref": "r7",
                        "order.items": "pizza x1", "order.total": "299"},
        )
        submit = next(s for s in wf.steps if s.action_type == ORDER_PLACE_ACTION)
        self.assertEqual(submit.parameters["items"], "r7")
        self.assertNotIn("reference", {k.lower() for k in submit.parameters})
        self.assertNotIn("booked", str(submit.parameters).lower())
        self.assertNotIn("confirmed", str(submit.parameters).lower())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
