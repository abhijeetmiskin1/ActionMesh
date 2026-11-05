"""Phase 6 Agent 5 — saved tasks through the real /automations API (TestClient).

Covers the contract the desktop saved-tasks module relies on, end to end over
HTTP (no fake bridge): saving grants nothing, every run is re-authorised at
run time, and only phrases a registered operator template actually matches
draft steps — everything else is honestly ``source=none`` with a reason, so
the UI never offers a save button for a plan that does not exist.

Built-in examples (desktop/ui/savedtasks.js EXAMPLES) are verified here
against the real planner with a tasks file installed: each example phrase
must draft `template:*` steps, and a phrase that does not route must be cut
from the list rather than shown as a working example.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.protocol.models import DeviceKind, RiskLevel

LOCAL = ("127.0.0.1", 45123)

# A tasks file the planner may use. search.web is a real registry action
# (actionmesh/core/registry/seeds.py), so this template is accepted at load.
TASKS = {
    "tasks": [
        {
            "name": "daily-check",
            "match": ["daily check", "check the disk"],
            "steps": [
                {
                    "label": "Search disk",
                    "action": "search.web",
                    "parameters": {"query": "disk"},
                }
            ],
        }
    ]
}

# Must mirror desktop/ui/savedtasks.js EXAMPLES exactly.
EXAMPLE_PHRASES = [
    "every day at 9am daily check",
    "check the disk",
    "every 30 minutes check the disk",
    "when a run fails check the disk",
]


def _clean_env(test: unittest.TestCase) -> None:
    for name in (
        "ACTIONMESH_API_KEY",
        "ACTIONMESH_ALLOW_REMOTE_PAIRING",
        "ACTIONMESH_SECRET_KEY",
        "ACTIONMESH_SECRET_FILE",
        "ACTIONMESH_VOICEOS_DB",
        "ACTIONMESH_VOICEOS_TASKS",
        "ACTIONMESH_VOICEOS_LLM_PROVIDER",
        "ACTIONMESH_AUTOMATIONS_DB",
    ):
        test.addCleanup(os.environ.pop, name, None)
        os.environ.pop(name, None)


class _SavedTasksApp(unittest.TestCase):
    def setUp(self) -> None:
        _clean_env(self)
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.app = create_app(identities=self.identities, devices=self.devices)
        self.client = TestClient(self.app, client=LOCAL)
        self.devices.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="Laptop",
            platform="linux",
            capabilities=["message.send", "reminder.create"],
            risk_ceiling=RiskLevel.HIGH,
        )
        self.devices.heartbeat("laptop-1")
        self.app.state.engine.local_device_id = "laptop-1"
        self.addCleanup(self._close)

    def _close(self) -> None:
        for name in ("automation_store_instance", "voiceos_store_instance"):
            store = getattr(self.app.state, name, None)
            if store is not None:
                store.close()

    def _install_tasks(self) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "tasks.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(TASKS, fh)
        os.environ["ACTIONMESH_VOICEOS_TASKS"] = path

    def _draft(self, text: str) -> dict:
        response = self.client.post(
            "/automations/draft", json={"text": text, "actor": {"id": "voiceos-local"}}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def _save(self, body: dict) -> dict:
        response = self.client.post("/automations", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()


class TestSavedTasksDraftContract(_SavedTasksApp):
    def test_builtin_examples_all_draft_real_steps(self) -> None:
        self._install_tasks()
        for phrase in EXAMPLE_PHRASES:
            draft = self._draft(phrase)
            self.assertTrue(
                str(draft.get("source", "")).startswith("template:"),
                f"example {phrase!r} must draft from a template, got source={draft.get('source')!r}",
            )
            self.assertGreater(len(draft.get("steps", [])), 0, phrase)
            self.assertTrue(draft.get("proposal"), f"example {phrase!r} must carry a proposal")

    def test_example_triggers_are_parsed_alongside_the_template(self) -> None:
        self._install_tasks()
        daily = self._draft("every day at 9am daily check")
        self.assertEqual(daily["schedule"], {"kind": "daily", "at": "09:00"})
        interval = self._draft("every 30 minutes check the disk")
        self.assertEqual(interval["schedule"], {"kind": "interval", "seconds": 1800})
        on_fail = self._draft("when a run fails check the disk")
        self.assertEqual(on_fail["event"], {"event_type": "execution.completed", "state": "failed"})

    def test_a_phrase_with_no_template_drafts_nothing_and_says_why(self) -> None:
        self._install_tasks()
        draft = self._draft("list processes")
        self.assertEqual(draft["source"], "none")
        self.assertEqual(draft["steps"], [])
        self.assertIn("no operator template", draft["reason"])

    def test_without_a_tasks_file_every_phrase_is_none(self) -> None:
        for phrase in EXAMPLE_PHRASES:
            draft = self._draft(phrase)
            self.assertEqual(draft["source"], "none", phrase)
            self.assertEqual(draft["steps"], [], phrase)

    def test_drafting_alone_stores_nothing(self) -> None:
        self._install_tasks()
        self._draft("check the disk")
        self.assertEqual(self.client.get("/automations").json()["workflows"], [])

    def test_a_draft_proposes_a_disarmed_plan(self) -> None:
        self._install_tasks()
        draft = self._draft("every day at 9am daily check")
        self.assertIs(draft["proposal"]["enabled"], False)

    def test_saving_a_draft_cannot_start_it_unattended(self) -> None:
        """A saved draft is stored disarmed; arming it is a separate, explicit act.

        The sentence named a schedule, so the plan is storable with one — but a plan
        that began firing at 09:00 the moment somebody pressed "save" would be an
        unattended action nobody switched on. Saving starts nothing; the trigger is
        armed later, by a person, and until then there is no due time at all.
        """
        self._install_tasks()
        draft = self._draft("every day at 9am daily check")
        saved = self._save(draft["proposal"])
        self.assertIs(saved["enabled"], False)
        self.assertEqual(saved["next_run_at"], "", "a disarmed plan has no due time")

        armed = self.client.post(
            f"/automations/{saved['workflow_id']}/enabled", json={"enabled": True}
        )
        self.assertEqual(armed.status_code, 200, armed.text)
        self.assertIs(armed.json()["enabled"], True)
        self.assertTrue(armed.json()["next_run_at"], "arming a daily plan gives it a due time")

    def test_a_disarmed_saved_plan_still_runs_when_a_person_presses_run(self) -> None:
        """Disarming stops triggers, not people: "run now" is a press, not a schedule."""
        self._install_tasks()
        draft = self._draft("every day at 9am daily check")
        saved = self._save(draft["proposal"])
        self.assertIs(saved["enabled"], False)
        run = self.client.post(f"/automations/{saved['workflow_id']}/run", json={})
        self.assertEqual(run.status_code, 200, run.text)
        self.assertIn(run.json()["state"], {"succeeded", "denied", "partial", "failed"})


class TestSavedTasksCrudAndRun(_SavedTasksApp):
    def _body(self) -> dict:
        return {
            "name": "nightly check",
            "steps": [
                {
                    "label": "Search disk",
                    "action": "search.web",
                    "parameters": {"query": "disk"},
                }
            ],
        }

    def test_save_list_get_rename_run_delete(self) -> None:
        saved = self._save(self._body())
        self.assertIn("Nothing has run", saved["runs_nothing"])

        workflows = self.client.get("/automations").json()["workflows"]
        self.assertEqual(len(workflows), 1)
        self.assertEqual(workflows[0]["name"], "nightly check")

        got = self.client.get(f"/automations/{saved['workflow_id']}").json()
        self.assertEqual(got["workflow_id"], saved["workflow_id"])

        renamed = self.client.put(
            f"/automations/{saved['workflow_id']}",
            json={**self._body(), "name": "morning check"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "morning check")

        run = self.client.post(f"/automations/{saved['workflow_id']}/run", json={})
        self.assertEqual(run.status_code, 200, run.text)
        self.assertEqual(run.json()["state"], "denied", "no consent was ever granted")

        deleted = self.client.delete(f"/automations/{saved['workflow_id']}")
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(self.client.get("/automations").json()["workflows"], [])

    def test_saving_grants_nothing_and_a_bodies_grants_field_is_refused(self) -> None:
        refused = self.client.post(
            "/automations", json={**self._body(), "grants": [{"scope": "search.web"}]}
        )
        self.assertEqual(refused.status_code, 400)
        self.assertIn("cannot carry permissions", refused.text)

        refused = self.client.post(
            "/automations", json={**self._body(), "permissions": ["search.web"]}
        )
        self.assertEqual(refused.status_code, 400)

    def test_saving_a_draft_proposal_round_trips_through_run(self) -> None:
        self._install_tasks()
        draft = self._draft("check the disk")
        proposal = draft["proposal"]
        self.assertTrue(proposal["steps"])

        saved = self._save(proposal)
        self.assertEqual(saved["steps"][0]["action_type"], "search.web")

        run = self.client.post(f"/automations/{saved['workflow_id']}/run", json={}).json()
        self.assertEqual(run["state"], "denied", "a saved plan is authorised again at run")

    def test_enabled_toggles_the_saved_task(self) -> None:
        saved = self._save(self._body())
        toggled = self.client.post(
            f"/automations/{saved['workflow_id']}/enabled", json={"enabled": True}
        )
        self.assertEqual(toggled.status_code, 200, toggled.text)
        self.assertTrue(toggled.json()["enabled"])

    def test_an_edit_cannot_transfer_ownership(self) -> None:
        saved = self._save(self._body())
        response = self.client.put(
            f"/automations/{saved['workflow_id']}",
            json={**self._body(), "actor_id": "someone-else"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["actor_id"], "voiceos-local")


if __name__ == "__main__":
    unittest.main()
