"""C41 — the command guide reports what the planner does, not what a label claims.

The guide on the Voice screen lists example phrasings. Before this route its
right-hand column was a hand-typed verdict, and it was wrong: several phrases matched
no rule at all, the page said "supported", and the next turn answered "I could not
map that". These tests pin the replacement: the page asks the running backend, and the
backend answers by running the phrase through the *same* planner a real turn uses.

Nothing here executes. ``/voiceos/guide`` is a measurement of a pure function, and the
last test proves it: asking about a phrase creates no session and no execution.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from actionmesh.api import GUIDE_MAX_PHRASES, create_app
from actionmesh.core.planner import Plan, ProposedAction

LOCAL = ("127.0.0.1", 54321)


class _Guide(unittest.TestCase):
    def setUp(self) -> None:
        for name in (
            "ACTIONMESH_VOICEOS_DB",
            "ACTIONMESH_VOICEOS_TASKS",
            "ACTIONMESH_VOICEOS_LLM_PROVIDER",
        ):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.app = create_app()
        self.client = TestClient(self.app, client=LOCAL)
        store = getattr(self.app.state, "voiceos_store_instance", None)
        if store is not None:
            self.addCleanup(store.close)

    def guide(self, phrases: list[str]) -> dict[str, dict[str, object]]:
        response = self.client.post("/voiceos/guide", json={"phrases": phrases})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual([entry["phrase"] for entry in body["phrases"]], phrases)
        return {entry["phrase"]: entry for entry in body["phrases"]}


class TestTheGuideIsMeasured(_Guide):
    def test_a_routable_phrase_reports_the_action_it_routes_to(self) -> None:
        entry = self.guide(["tell mom I'll be late"])["tell mom I'll be late"]
        self.assertEqual(entry["actions"], ["message.send"])
        self.assertEqual(entry["notes"], [])

    def test_a_phrase_with_no_rule_reports_no_action(self) -> None:
        # This is the exact phrase the guide used to call "supported" while the
        # planner had no rule for it.
        phrase = "show the running processes on this computer"
        entry = self.guide([phrase])[phrase]
        self.assertEqual(entry["actions"], [])
        self.assertTrue(
            any(str(note).startswith("no rule matched") for note in entry["notes"]),
            entry["notes"],
        )

    def test_a_known_capability_this_build_lacks_is_reported_as_dropped(self) -> None:
        # The rule matches, but no laptop agent is attached here, so the device action
        # is not registered. The distinction matters: the user is told to enable the
        # agent rather than that their sentence was unparseable.
        phrase = "show my projects"
        entry = self.guide([phrase])[phrase]
        self.assertEqual(entry["actions"], [])
        self.assertTrue(
            any(str(note).startswith("dropped unregistered action type") for note in entry["notes"]),
            entry["notes"],
        )

    def test_it_uses_the_same_planner_the_conversation_uses(self) -> None:
        # Not a copy, not a second rule table: the object on `app.state.planner` is the
        # one a turn reaches, so a change to the registry filter changes both answers.
        seen: list[str] = []

        class Recorder:
            def plan(self, intent: str) -> Plan:
                seen.append(intent)
                return Plan(intent=intent, actions=(ProposedAction("message.send", {"message": "x"}),))

        self.app.state.planner = Recorder()
        entry = self.guide(["anything at all"])["anything at all"]
        self.assertEqual(seen, ["anything at all"])
        self.assertEqual(entry["actions"], ["message.send"])


class TestTheGuideIsBounded(_Guide):
    def test_phrases_must_be_a_non_empty_list(self) -> None:
        for body in ({}, {"phrases": []}, {"phrases": "one"}, {"phrases": [1, 2]}):
            response = self.client.post("/voiceos/guide", json=body)
            self.assertEqual(response.status_code, 400, body)

    def test_too_many_phrases_are_refused(self) -> None:
        response = self.client.post(
            "/voiceos/guide",
            json={"phrases": ["tell mom hi"] * (GUIDE_MAX_PHRASES + 1)},
        )
        self.assertEqual(response.status_code, 400)

    def test_an_over_long_phrase_is_refused(self) -> None:
        response = self.client.post("/voiceos/guide", json={"phrases": ["tell mom " + "x" * 500]})
        self.assertEqual(response.status_code, 400)

    def test_blank_entries_are_refused_rather_than_measured(self) -> None:
        response = self.client.post("/voiceos/guide", json={"phrases": ["  "]})
        self.assertEqual(response.status_code, 400)


class TestTheGuideIsNotExecution(_Guide):
    def test_asking_about_a_phrase_creates_no_session_and_no_execution(self) -> None:
        before = len(self.client.get("/executions").json())
        entry = self.guide(["tell mom I'll be late"])["tell mom I'll be late"]
        # It routes, and still does not run.
        self.assertEqual(entry["actions"], ["message.send"])
        self.assertEqual(len(self.client.get("/executions").json()), before)
        self.assertEqual(self.client.get("/voiceos/sessions").json(), [])

    def test_it_never_proposes_an_unregistered_action(self) -> None:
        names = {defn.name for defn in self.app.state.engine.registry.list()}
        answer = self.client.post(
            "/voiceos/guide",
            json={"phrases": ["show my projects", "what processes are running", "tell mom hi"]},
        ).json()
        for entry in answer["phrases"]:
            for action in entry["actions"]:
                self.assertIn(action, names)


if __name__ == "__main__":
    unittest.main()