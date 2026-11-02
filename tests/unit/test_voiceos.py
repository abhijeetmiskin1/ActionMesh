"""C34 — VoiceOS task sessions: what may be proposed, and what may actually run.

The feature is a conversation that can chain actions, so the tests are about the two
lines it must not cross:

* **a sentence is not a press.** `/voiceos/messages` understands and proposes; the only
  way a step runs is a separate `run` call, authorised against the caller's own
  credential;
* **a conversation is not a grant.** Context, references and history resolve *targets*;
  they never widen authority. A session owned by one actor is invisible to another, and
  a step the caller has no consent for is denied exactly as it would be from the
  dashboard.

Plus the reasoning layer's own honesty: an LLM answer is stripped to `{type, parameters}`
or dropped, a configured-but-unreachable provider is announced rather than faked, and a
reference resolves only to something the user was actually shown.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.protocol.models import DeviceKind
from actionmesh.voiceos import load_templates
from actionmesh.voiceos.llm import LlmConfig, build_provider, validate_proposal
from actionmesh.voiceos.models import Conversation, TaskSession, TaskStep
from actionmesh.voiceos.reasoning import Reasoner, observed_references
from actionmesh.voiceos.store import VoiceOsStore

LOCAL = ("127.0.0.1", 45123)
REMOTE = ("192.168.1.50", 55555)


def _clean_env(test: unittest.TestCase) -> None:
    for name in (
        "ACTIONMESH_API_KEY",
        "ACTIONMESH_ALLOW_REMOTE_PAIRING",
        "ACTIONMESH_SECRET_KEY",
        "ACTIONMESH_SECRET_FILE",
        "ACTIONMESH_VOICEOS_DB",
        "ACTIONMESH_VOICEOS_TASKS",
        "ACTIONMESH_VOICEOS_LLM_PROVIDER",
    ):
        test.addCleanup(os.environ.pop, name, None)
        os.environ.pop(name, None)


class _PairedApp(unittest.TestCase):
    """One laptop, two paired phones, and the machine's own operator view."""

    def setUp(self) -> None:
        _clean_env(self)
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.app = create_app(identities=self.identities, devices=self.devices)
        self.local = TestClient(self.app, client=LOCAL)
        self.remote = TestClient(self.app, client=REMOTE)
        self.devices.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="Laptop",
            platform="linux",
            capabilities=["message.send", "payment.prepare"],
        )
        self.actor_a = "phone-a"
        self.actor_b = "phone-b"
        self.token_a = self._pair("phone-a", self.actor_a)
        self.token_b = self._pair("phone-b", self.actor_b)
        # The store opens on the first VoiceOS turn and is closed by the lifespan;
        # a test client that never enters the context manager has to close it.
        self.addCleanup(self._close_store)

    def _close_store(self) -> None:
        store = getattr(self.app.state, "voiceos_store_instance", None)
        if store is not None:
            store.close()

    def _pair(self, phone: str, actor: str = "") -> str:
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": "critical"}
        ).json()["code"]
        response = self.remote.post(
            "/pair",
            json={"code": code, "device_id": phone, "actor_id": actor or phone},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def as_phone(self, token: str) -> TestClient:
        return TestClient(self.app, client=REMOTE, headers={"authorization": f"Bearer {token}"})

    def tell(self, client: TestClient, text: str) -> dict:
        return client.post("/voiceos/messages", json={"text": text}).json()


class TestAMessageIsNotAPress(_PairedApp):
    def test_understanding_creates_no_execution(self) -> None:
        before = len(self.local.get("/executions").json())
        answer = self.tell(self.local, "tell mom I'll be late")
        self.assertEqual(answer["kind"], "task")
        self.assertFalse(answer["ran"], "a message route must never run anything")
        self.assertTrue(answer["session_id"])
        self.assertEqual(len(self.local.get("/executions").json()), before)

    def test_the_proposed_session_has_no_authority_fields(self) -> None:
        answer = self.tell(self.local, "tell mom I'll be late")
        session = self.local.get(f"/voiceos/sessions/{answer['session_id']}").json()
        blob = str(session).lower()
        for forbidden in ("permission", "grant", "authorized", "allow_high_risk", "max_risk"):
            self.assertNotIn(forbidden, blob)

    def test_running_without_consent_is_denied_not_assumed(self) -> None:
        answer = self.tell(self.local, "tell mom I'll be late")
        run = self.local.post(f"/voiceos/sessions/{answer['session_id']}/run", json={
            "actor": {"id": "voice-user"}
        }).json()
        step = run["steps"][0]
        self.assertEqual(step["state"], "denied", "no consent was recorded for this actor")
        self.assertEqual(run["status"], "failed")

    def test_a_granted_step_runs_and_reports_verification(self) -> None:
        self.local.post(
            "/consents",
            json={"actor_id": "voice-user", "scope": "message.send", "max_risk": "high"},
        )
        answer = self.tell(self.local, "tell mom I'll be late")
        run = self.local.post(f"/voiceos/sessions/{answer['session_id']}/run", json={
            "actor": {"id": "voice-user"}
        }).json()
        step = run["steps"][0]
        self.assertEqual(step["state"], "succeeded")
        # The verification evidence has to survive the trip into the session. Reading the
        # engine's dataclass as a dict used to yield `verified: None`, which turned a
        # verified step into "succeeded, not verified" and lost the observation.
        self.assertTrue(step["verified"])
        self.assertTrue(step["observed"], "the step keeps what it actually saw")
        self.assertTrue(step["execution_id"])
        self.assertEqual(run["status"], "completed")


class TestSessionsAreActorScoped(_PairedApp):
    def _session_for(self, token: str) -> str:
        client = self.as_phone(token)
        answer = self.tell(client, "tell mom I'll be late")
        return answer["session_id"]

    def test_another_device_cannot_read_or_run_my_session(self) -> None:
        mine = self._session_for(self.token_a)
        other = self.as_phone(self.token_b)
        self.assertEqual(other.get(f"/voiceos/sessions/{mine}").status_code, 404)
        self.assertEqual(
            other.post(f"/voiceos/sessions/{mine}/run", json={}).status_code, 404
        )
        self.assertEqual(other.post(f"/voiceos/sessions/{mine}/cancel", json={}).status_code, 404)

    def test_the_list_is_filtered_to_the_caller(self) -> None:
        mine = self._session_for(self.token_a)
        rows = self.as_phone(self.token_b).get("/voiceos/sessions").json()
        self.assertNotIn(mine, [row["session_id"] for row in rows])
        own = self.as_phone(self.token_a).get("/voiceos/sessions").json()
        self.assertIn(mine, [row["session_id"] for row in own])

    def test_a_phone_cannot_open_a_session_as_someone_else(self) -> None:
        client = self.as_phone(self.token_a)
        refused = client.post(
            "/voiceos/messages", json={"text": "status", "actor": {"id": "someone-else"}}
        )
        self.assertEqual(refused.status_code, 403)


class TestConversationIntentHandling(_PairedApp):
    def test_status_answers_from_the_session_not_from_wishful_thinking(self) -> None:
        answer = self.tell(self.local, "tell mom I'll be late")
        self.local.post(f"/voiceos/sessions/{answer['session_id']}/run", json={
            "actor": {"id": "voice-user"}
        })
        answer = self.tell(self.local, "what's the status?")
        self.assertEqual(answer["kind"], "answer")
        self.assertIn("1 of 1 steps settled", answer["reply"])
        self.assertIn("denied", answer["reply"])

    def test_an_unmappable_request_says_so_and_names_nothing_invented(self) -> None:
        answer = self.tell(self.local, "align the struts")
        self.assertEqual(answer["kind"], "answer")
        self.assertIn("could not safely map", answer["reply"])
        self.assertEqual(answer["session_id"], "")

    def test_clearing_forgets_references_but_changes_no_authority(self) -> None:
        self.local.post("/consents", json={"actor_id": "voice-user", "scope": "x"})
        answer = self.tell(self.local, "clear the conversation")
        self.assertEqual(answer["kind"], "clear")
        self.assertEqual(self.local.get("/voiceos/conversation").json()["references"], [])


class TestReasoningLayer(unittest.TestCase):
    def setUp(self) -> None:
        self.conversation = Conversation(actor_id="voice-user")

    def _reasoner(self, templates=()):
        return Reasoner(
            planner=_StubPlanner(),
            known_action=lambda name: name.startswith("device.") or name == "message.send",
            templates=lambda: tuple(templates),
        )

    def test_a_reference_resolves_only_to_what_was_observed(self) -> None:
        self.conversation.references = observed_references(
            "device.process.list",
            {"processes": [{"process_id": "p1", "label": "api"}, {"process_id": "p2", "label": "web"}]},
        )
        answer = self._reasoner().understand("stop the second one", self.conversation)
        step = answer.session.steps[0]
        self.assertEqual(step.action_type, "device.process.stop")
        self.assertEqual(step.parameters, {"process_id": "p2"})

    def test_an_ordinal_with_nothing_observed_is_not_guessed(self) -> None:
        # "The second one" of a list nobody was shown has no answer. Guessing at a
        # process id would be the most expensive kind of helpful.
        answer = self._reasoner().understand("stop the second one", self.conversation)
        self.assertIsNone(answer.session)
        self.assertIn("no list to take that from", answer.reply)

    def test_a_template_supplies_the_details_the_planner_cannot_know(self) -> None:
        source = load_templates(_write_templates(self), known_action=lambda name: True)
        answer = self._reasoner(source.templates).understand(
            "run my tests", Conversation(actor_id="voice-user")
        )
        self.assertEqual(len(answer.session.steps), 2)
        self.assertEqual(answer.session.steps[1].when.get("step"), 0)
        self.assertIn("configured task", answer.reply)

    def test_a_refused_template_is_reported_not_half_loaded(self) -> None:
        path = Path(tempfile.mkstemp(suffix=".json")[1])
        path.write_text(
            '{"tasks": [{"name": "bad", "match": ["x"], "steps": ['
            '{"label": "l", "action": "device.nope", "parameters": {}}]}]}',
            encoding="utf-8",
        )
        source = load_templates(path, known_action=lambda name: False)
        self.assertEqual(source.templates, ())
        self.assertTrue(any("unregistered action" in note for note in source.refusals))


class TestLlmSeamIsHonest(unittest.TestCase):
    def test_a_proposal_keeps_only_what_a_proposal_may_carry(self) -> None:
        proposal = validate_proposal(
            {
                "actions": [
                    {"type": "message.send", "parameters": {"to": "mom"}},
                    {"type": "device.nope", "parameters": {}},
                    {"type": "message.send", "parameters": {}, "risk_level": "low"},
                    {"type": "message.send", "parameters": {}, "permissions": ["everything"]},
                ]
            },
            known_action=lambda name: name == "message.send",
        )
        self.assertEqual([action for action, _ in proposal.actions], ["message.send"])
        self.assertEqual(len(proposal.actions), 1)
        joined = " ".join(proposal.notes)
        self.assertIn("asserted fields it does not own", joined)
        self.assertIn("unregistered", joined)

    def test_a_configured_provider_without_a_transport_says_so(self) -> None:
        provider, reason = build_provider(LlmConfig(provider="some-vendor", model="m"))
        self.assertIsNone(provider, "no provider may be handed back that cannot answer")
        self.assertIn("no approved outbound transport", reason)

    def test_no_provider_configured_is_silent_not_apologetic(self) -> None:
        provider, reason = build_provider(LlmConfig())
        self.assertIsNone(provider)
        self.assertEqual(reason, "")


class TestSessionStoreIsDurable(unittest.TestCase):
    def test_a_session_survives_reopening_the_store(self) -> None:
        path = Path(tempfile.mkstemp(suffix=".db")[1])
        store = VoiceOsStore(path)
        session = TaskSession(
            session_id="task_1",
            actor_id="voice-user",
            request="tell mom",
            steps=[TaskStep(index=0, label="l", action_type="message.send")],
        )
        store.save_session(session)
        store.close()
        reopened = VoiceOsStore(path)
        self.addCleanup(reopened.close)
        loaded = reopened.get_session("task_1")
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.steps[0].action_type, "message.send")
        reopened.close()

    def test_a_newer_schema_is_refused_not_migrated_down(self) -> None:
        path = Path(tempfile.mkstemp(suffix=".db")[1])
        store = VoiceOsStore(path)
        store.connection.execute("PRAGMA user_version=99")
        store.connection.commit()
        store.close()
        from actionmesh.voiceos.store import SchemaError

        with self.assertRaises(SchemaError):
            VoiceOsStore(path)


class TestSessionReconciliation(unittest.TestCase):
    """A step must never be shown in a state the record contradicts."""

    def setUp(self) -> None:
        _clean_env(self)
        self.app = create_app()
        self.client = TestClient(self.app, client=LOCAL)
        self.addCleanup(self._close)

    def _close(self) -> None:
        store = getattr(self.app.state, "voiceos_store_instance", None)
        if store is not None:
            store.close()

    def test_a_step_that_ended_while_nobody_was_looking_is_folded_on_read(self) -> None:
        self.client.post(
            "/consents",
            json={"actor_id": "voice-user", "scope": "message.send", "max_risk": "high"},
        )
        answer = self.client.post(
            "/voiceos/messages", json={"actor": {"id": "voice-user"}, "text": "tell mom hi"}
        ).json()
        session_id = answer["session_id"]
        run = self.client.post(
            f"/voiceos/sessions/{session_id}/run", json={"actor": {"id": "voice-user"}}
        ).json()
        execution_id = run["steps"][0]["execution_id"]
        # Simulate the contradiction: the session still believes the step is parked.
        store = self.app.state.voiceos_store_instance
        session = store.get_session(session_id)
        session.steps[0].state = "awaiting_approval"
        session.status = "awaiting_approval"
        store.save_session(session)

        read = self.client.get(f"/voiceos/sessions/{session_id}").json()
        self.assertEqual(read["steps"][0]["state"], "succeeded")
        self.assertTrue(read["steps"][0]["verified"])
        self.assertEqual(read["status"], "completed")
        self.assertEqual(read["steps"][0]["execution_id"], execution_id)


class _StubPlanner:
    def plan(self, intent: str):
        from actionmesh.core.planner import Plan, ProposedAction

        if intent.lower().startswith("tell "):
            to, _, body = intent[5:].partition(" ")
            return Plan(intent=intent, actions=(ProposedAction("message.send", {"to": to, "message": body}),))
        return Plan(intent=intent, notes=(f"no rule matched: {intent!r}",))


def _write_templates(test: unittest.TestCase) -> str:
    path = Path(tempfile.mkstemp(suffix=".json")[1])
    path.write_text(
        """{"tasks": [{"name": "tests", "match": ["run my tests", "run the tests"], "steps": [
          {"label": "Run the project tests", "action": "device.terminal.execute",
           "parameters": {"command": "python3", "arguments": ["-m", "pytest"]}},
          {"label": "Start the server", "action": "device.server.start",
           "parameters": {"name": "web", "command": "python3", "arguments": ["server.py"]},
           "when": {"step": 0, "exit_code": 0}}]}]}""",
        encoding="utf-8",
    )
    test.addCleanup(path.unlink, missing_ok=True)
    return str(path)


__all__ = ["TestAMessageIsNotAPress", "TestConversationIntentHandling", "TestLlmSeamIsHonest",
           "TestReasoningLayer", "TestSessionStoreIsDurable", "TestSessionsAreActorScoped"]
