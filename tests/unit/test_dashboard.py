"""C18 dashboard: planning must stay a proposal, all the way to the UI.

The control room gained a "Plan from natural language" panel. The dangerous
mistake would be to let the browser send its own policy when executing a plan —
that would let a proposal carry its own authority around the server's policy
engine. The tests pin that it does not.
"""

from __future__ import annotations

import unittest

from actionmesh.dashboard import page


class TestPlannerPanel(unittest.TestCase):
    def setUp(self) -> None:
        self.html = page()

    def _execute_plan_handler(self) -> str:
        start = self.html.index("document.querySelector('#executePlan')")
        end = self.html.index("const eventFeed")
        return self.html[start:end]

    def test_it_offers_an_intent_and_a_propose_control(self) -> None:
        self.assertIn('id="intent"', self.html)
        self.assertIn('id="plan"', self.html)
        self.assertIn("fetch('/plan'", self.html)

    def test_executing_a_plan_is_a_separate_disabled_step(self) -> None:
        # Planning and executing must not be one click.
        self.assertIn('id="executePlan"', self.html)
        self.assertIn("data-idle=\"Execute planned actions\" disabled", self.html)

    def test_the_panel_states_that_planning_does_not_execute(self) -> None:
        self.assertIn("The planner proposes; policy decides", self.html)

    def test_planned_execution_cannot_grant_itself_a_permission(self) -> None:
        # Policy rules are fine — the workspace declaring what it will consider is
        # the existing pattern. What must never happen is the browser handing the
        # server a permission or grant: those come only from the consent store.
        segment = self._execute_plan_handler()
        self.assertIn("/execute", segment)
        self.assertNotIn("permissions:", segment)
        self.assertNotIn("grants:", segment)

    def test_planned_policy_rules_cover_only_the_proposed_actions(self) -> None:
        # Bounded to the proposal: the browser cannot blanket-allow everything.
        self.assertIn(
            "plannedActions.map(action => ({action: action.type, decision: 'allow'}))",
            self._execute_plan_handler(),
        )

    def test_proposed_actions_are_rendered_without_inner_html(self) -> None:
        # Planner output is untrusted text; it must land via textContent.
        start = self.html.index("document.querySelector('#plan')")
        end = self.html.index("document.querySelector('#executePlan')")
        segment = self.html[start:end]
        self.assertIn("row.textContent = text", segment)
        self.assertNotIn("innerHTML", segment)


if __name__ == "__main__":
    unittest.main()