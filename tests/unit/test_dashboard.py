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


class TestWebViewCompatibility(unittest.TestCase):
    """The dashboard must render inside the Chrome 83 WebView the app ships on.

    ``Element.replaceChildren`` is a Chrome 86+ API. The Android 11 system
    WebView is Chrome 83 and throws a TypeError on it, and because that render
    sat inside the same try-block as the fetches, the catch handler reported the
    service as "Offline" while every request was answering 200. These tests pin
    the renderer to the DOM subset an old WebView actually has, so a list added
    later cannot quietly reintroduce the failure.
    """

    def setUp(self) -> None:
        self.html = page()

    def _helper(self) -> str:
        start = self.html.index("const replaceAll = (element, nodes) => {")
        end = self.html.index("const renderList =")
        return self.html[start:end]

    def test_the_page_never_calls_replace_children(self) -> None:
        # A comment may name the API; no call may.
        self.assertNotIn(".replaceChildren(", self.html)

    def test_children_are_replaced_through_a_webview_safe_helper(self) -> None:
        self.assertIn("const replaceAll = (element, nodes) => {", self.html)

    def test_the_helper_clears_and_appends_without_replace_children(self) -> None:
        segment = self._helper()
        self.assertIn("element.textContent = '';", segment)
        self.assertIn("element.appendChild(node)", segment)

    def test_every_dynamic_list_renders_through_the_helper(self) -> None:
        # capabilities, executions and the proposed plan each called the
        # unsupported API directly before; each must go through the helper now.
        for marker in (
            "replaceAll(actions,",
            "renderList('#executions'",
            "replaceAll(planFeed,",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, self.html)

    def test_rendered_rows_still_use_text_content(self) -> None:
        # The helper is a compatibility shim, not a licence to trust markup.
        segment = self.html[self.html.index("const renderList ="):]
        self.assertIn("row.textContent =", segment)
        self.assertNotIn("innerHTML", segment)


if __name__ == "__main__":
    unittest.main()