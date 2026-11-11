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

import asyncio
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.core.permissions import PermissionGrant, PolicyContext
from actionmesh.protocol.models import (
    DeviceKind,
    Identity,
    Policy,
    PolicyDecision,
    PolicyRule,
    RiskLevel,
)
from actionmesh.voiceos import load_templates
from actionmesh.voiceos.llm import LlmConfig, build_provider, validate_proposal
from actionmesh.voiceos.models import Conversation, TaskSession, TaskStep
from actionmesh.voiceos.reasoning import Reasoner, observed_references
from actionmesh.voiceos.store import VoiceOsStore
from tests.unit.test_executor import run

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
            # The owner's choice at enrolment, and it has to be a real one: the
            # registry default is LOW (fail closed), which would refuse the medium-risk
            # action this fixture advertises and make every run test a ceiling test.
            risk_ceiling=RiskLevel.HIGH,
        )
        # A device is offline until something proves it is reachable -- `enroll` only
        # credits the registration, never a heartbeat. A laptop agent sends one at
        # startup, and C36's whole point is that presence is not faked, so the fixture
        # does what a running agent does instead of asserting optimism.
        self.devices.heartbeat("laptop-1")
        # And this control process *is* that laptop's control service: with an agent
        # enabled, `create_app` sets exactly this, and routing then knows which resolved
        # device the in-process providers serve. An injected engine is deliberately not
        # told, so the test states the topology it is testing instead of leaving it to
        # a resemblance check that would happily run a Mac's command here.
        self.app.state.engine.local_device_id = "laptop-1"
        self.actor_a = "phone-a"
        self.actor_b = "phone-b"
        # CredentialStore.issue() refuses minting above the actor's consented
        # ceiling. The operator (this laptop) grants the phone actors a
        # critical consent explicitly before pairing, so no credential is
        # minted above what the operator actually consented to.
        for actor in (self.actor_a, self.actor_b):
            grant = self.local.post(
                "/consents",
                json={
                    "actor_id": actor,
                    "scope": "device.control",
                    "max_risk": "critical",
                    "reason": "fixture consent before pairing",
                },
            )
            self.assertEqual(grant.status_code, 200, grant.text)
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


class TestTheActiveTargetIsAlwaysStated(_PairedApp):
    """C36: which computer a request is about, said before anything is authorised.

    The failure these guard against is not a crash. It is a plan that runs somewhere
    the user did not mean, and the only defence that works for a reader is that the
    target was named *before* the press -- and that a target that cannot be reached
    is reported as such instead of being silently swapped for one that can.
    """

    def test_every_answer_names_the_computer_it_is_about(self) -> None:
        answer = self.tell(self.local, "tell mom I'll be late")
        self.assertEqual(answer["target_device"], "laptop-1")
        self.assertEqual(answer["target_name"], "Laptop")
        self.assertTrue(answer["target_online"])

    def test_the_plan_says_the_target_in_words(self) -> None:
        answer = self.tell(self.local, "tell mom I'll be late")
        self.assertIn("laptop-1", answer["reply"])
        session = self.local.get(f"/voiceos/sessions/{answer['session_id']}").json()
        self.assertEqual(session["device_id"], "laptop-1")
        self.assertEqual(session["steps"][0]["device_id"], "laptop-1")

    def test_switching_persists_and_is_reported_by_the_conversation(self) -> None:
        self.devices.enroll(
            device_id="mac-1", kind=DeviceKind.LAPTOP, name="MacBook", platform="macos"
        )
        self.devices.heartbeat("mac-1")
        answer = self.tell(self.local, "switch to the MacBook")
        self.assertEqual(answer["kind"], "device")
        self.assertEqual(answer["target_device"], "mac-1")
        view = self.local.get("/voiceos/conversation").json()
        self.assertEqual(view["target_device"], "mac-1")
        self.assertEqual(view["device_id"], "mac-1")
        # And the next request is aimed there.
        self.assertEqual(self.tell(self.local, "status")["target_device"], "mac-1")

    def test_an_offline_target_is_refused_not_redirected(self) -> None:
        """Demo G. The Mac is registered, selected, and asleep -- and stays the target."""
        self.devices.enroll(
            device_id="mac-1", kind=DeviceKind.LAPTOP, name="MacBook", platform="macos"
        )
        # No heartbeat: it is not reachable. laptop-1 *is* online, which is the trap.
        # Consent for the action exists, so the *only* thing standing between this
        # request and the message is the device being unreachable. Without it the run
        # would stop at policy and prove nothing about routing.
        self.local.post(
            "/consents",
            json={"actor_id": "voice-user", "scope": "message.send", "max_risk": "high"},
        )
        answer = self.tell(self.local, "switch to the MacBook")
        self.assertEqual(answer["target_device"], "mac-1")
        self.assertIn("offline", answer["reply"])
        proposal = self.tell(self.local, "tell mom I'll be late")
        self.assertEqual(proposal["target_device"], "mac-1")
        run = self.local.post(
            f"/voiceos/sessions/{proposal['session_id']}/run",
            json={"actor": {"id": "voice-user"}},
        ).json()
        step = run["steps"][0]
        self.assertEqual(step["state"], "denied")
        self.assertIn("offline", step["error"])
        self.assertEqual(step["device_id"], "mac-1", "no step is re-addressed")

    def test_naming_a_phone_explains_it_is_not_an_execution_target(self) -> None:
        # Uniquely named on purpose: "phone-a" also matches "phone-b" as an id prefix,
        # which is a genuine tie and would (correctly) ask instead of refusing.
        self.devices.enroll(
            device_id="tablet-9", kind=DeviceKind.PHONE, name="Pixel Tablet", platform="android"
        )
        answer = self.tell(self.local, "switch to the Pixel Tablet")
        self.assertEqual(answer["kind"], "answer")
        self.assertIn("control surface", answer["reply"])
        self.assertIn("cannot run actions", answer["reply"])

    def test_an_ambiguous_switch_asks_instead_of_picking(self) -> None:
        for device_id, name in (("mac-1", "MacBook Pro"), ("mac-2", "MacBook Pro")):
            self.devices.enroll(
                device_id=device_id, kind=DeviceKind.LAPTOP, name=name, platform="macos"
            )
            self.devices.heartbeat(device_id)
        answer = self.tell(self.local, "switch to the MacBook Pro")
        self.assertEqual(answer["kind"], "answer")
        self.assertIn("Which computer did you mean", answer["reply"])
        # The conversation's target did not move to either of them.
        self.assertEqual(self.local.get("/voiceos/conversation").json()["target_device"],
                         "laptop-1")

    def test_a_step_with_no_selected_computer_is_refused_not_run_locally(self) -> None:
        """Once a device plane exists, "unselected" is a refusal -- never "here"."""
        from actionmesh.voiceos.models import TaskSession, TaskStep
        from actionmesh.voiceos.orchestrator import Orchestrator

        session = TaskSession(
            session_id="task_x",
            actor_id="voice-user",
            request="send one message",
            steps=[TaskStep(index=0, label="one", action_type="message.send",
                            parameters={"to": "mom", "message": "hi"})],
        )
        context = PolicyContext(
            actor=Identity(id="voice-user"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=Policy(
                rules=[PolicyRule(action="message.send", decision=PolicyDecision.ALLOW)]
            ),
        )
        outcome = run(Orchestrator(engine=self.app.state.engine).advance(session, context))
        step = outcome["session"].steps[0]
        self.assertEqual(step.state, "denied")
        self.assertEqual(step.execution_id, "", "nothing was dispatched at all")
        self.assertIn("no computer is selected", step.error)


class TestReasoningLayer(unittest.TestCase):
    def setUp(self) -> None:
        self.conversation = Conversation(actor_id="voice-user")

    def _reasoner(self, templates=()):
        return Reasoner(
            planner=_StubPlanner(),
            known_action=lambda name: name.startswith("device.") or name == "message.send",
            templates=lambda: tuple(templates),
        )

    def test_jarvis_opens_the_registered_browser_and_never_falls_back_to_simulation(self) -> None:
        command = "Hey Jarvis, open the browser."
        unavailable = self._reasoner().understand(command, self.conversation)
        self.assertEqual(unavailable.kind, "answer")
        self.assertIn("browser drive", unavailable.reply)
        reasoner = Reasoner(
            planner=_StubPlanner(), known_action=lambda name: name == "browser.open",
            templates=lambda: (),
        )
        planned = reasoner.understand(command, self.conversation)
        assert planned.session is not None
        self.assertEqual(planned.session.steps[0].action_type, "browser.open")
        self.assertEqual(planned.session.steps[0].parameters, {"url": "https://duckduckgo.com/"})

    def test_named_services_use_the_browser_action(self) -> None:
        reasoner = Reasoner(
            planner=_StubPlanner(), known_action=lambda name: name == "browser.open",
            templates=lambda: (),
        )
        for service, url in (
            ("GitHub", "https://github.com/"),
            ("Google Drive", "https://drive.google.com/"),
            ("Google Calendar", "https://calendar.google.com/"),
        ):
            command = f"Hey Jarvis, open {service}."
            with self.subTest(service=service):
                answer = reasoner.understand(command, self.conversation)
                assert answer.session is not None
                self.assertEqual(answer.session.steps[0].action_type, "browser.open")
                self.assertEqual(answer.session.steps[0].parameters, {"url": url})
                self.assertIsNone(self._reasoner().understand(command, self.conversation).session)

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


class TestAnthropicProviderIsReallyWired(unittest.TestCase):
    """C37: the adapter produces a real Messages API request and consumes a real reply.

    These run against a stubbed transport, not the network: they prove the *wire
    format* and the trust boundary, which is the part that can be verified without a
    credential. The final test pins the honest claim — with no key present, the build
    must say so rather than hand back a provider that cannot answer.
    """

    def _provider(self, **kwargs: Any) -> Any:
        from actionmesh.voiceos.llm import AnthropicProvider

        catalog = kwargs.pop("catalog", lambda: (("device.process.list", "list processes"),))
        return AnthropicProvider(
            name="anthropic",
            model="claude-sonnet-5-5",
            api_key="test-key-not-used",
            catalog=catalog,
            **kwargs,
        )

    def test_the_request_is_a_pinned_messages_call_with_a_forced_tool(self) -> None:
        payload = self._provider().payload("show me the running processes")
        self.assertEqual(payload["model"], "claude-sonnet-5-5")
        self.assertEqual(payload["max_tokens"], 1024)
        # A forced tool call is what makes the answer machine-readable without asking
        # the model to please emit JSON.
        self.assertEqual(
            payload["tool_choice"],
            {"type": "tool", "name": "propose_plan"},
        )
        self.assertEqual([tool["name"] for tool in payload["tools"]], ["propose_plan"])
        message = payload["messages"][0]
        self.assertEqual(message["role"], "user")
        self.assertIn("show me the running processes", message["content"])
        self.assertIn("device.process.list", message["content"])
        # Nothing beyond the request and the catalogue leaves the process.
        self.assertNotIn("stdout", message["content"])
        self.assertNotIn("password", message["content"].lower())

    def test_the_catalogue_sends_names_and_descriptions_only(self) -> None:
        from actionmesh.voiceos.llm import (
            MAX_CATALOG_ACTIONS,
            _catalogue_text,
            build_catalogue,
        )

        class _Action:
            def __init__(self, name: str, description: str) -> None:
                self.name = name
                self.description = description

        describe = build_catalogue(
            [_Action("device.file.read", "Read one file\nfrom a computer")]
        )
        self.assertEqual(describe(), [("device.file.read", "Read one file from a computer")])

        # Only the description is normalised; a long one is clipped rather than
        # silently dropped.
        clipped = build_catalogue([_Action("a.b", "x" * 400)])()
        self.assertEqual(len(clipped[0][1]), 160)

        # The truncation applies where the text is rendered, which is what reaches
        # the model.
        rows = [(f"a.{i}", "d") for i in range(MAX_CATALOG_ACTIONS + 5)]
        text = _catalogue_text(rows)
        self.assertEqual(len(text.splitlines()), MAX_CATALOG_ACTIONS)
        self.assertIn("6 more registered actions omitted", text.splitlines()[-1])

    def test_a_tool_use_block_is_the_only_accepted_answer(self) -> None:
        from actionmesh.voiceos.llm import LlmError, _tool_input

        answer = {
            "content": [
                {"type": "text", "text": "Sure, here is JSON..."},
                {
                    "type": "tool_use",
                    "name": "propose_plan",
                    "input": {"actions": [{"type": "device.process.list", "parameters": {}}]},
                },
            ]
        }
        self.assertEqual(
            _tool_input(answer),
            {"actions": [{"type": "device.process.list", "parameters": {}}]},
        )
        # Prose despite the forced tool is a malformed answer, not a proposal.
        with self.assertRaises(LlmError):
            _tool_input({"content": [{"type": "text", "text": "{\"actions\": []}"}]})
        with self.assertRaises(LlmError):
            _tool_input({"content": []})

    def test_the_model_cannot_assert_its_own_authority(self) -> None:
        """End to end through the real adapter: a hostile answer is neutered."""
        from actionmesh.core.planner import RuleBasedPlanner
        from actionmesh.voiceos.llm import LlmPlanner, _tool_input

        hostile = _tool_input(
            {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "propose_plan",
                        "input": {
                            "actions": [
                                {
                                    "type": "device.terminal.execute",
                                    "parameters": {"command": "rm -rf /"},
                                    "risk_level": "low",
                                    "approved": True,
                                }
                            ]
                        },
                    }
                ]
            }
        )
        planner = LlmPlanner(
            provider=_StubProvider(answer=hostile),
            fallback=RuleBasedPlanner(),
            known_action=lambda _: True,
        )
        plan = planner.plan("delete everything")
        # The whole entry is dropped, not partially honoured: parameters survive only
        # when the model stayed inside the fields it owns.
        self.assertEqual(plan.actions, ())
        self.assertIn("asserted fields it does not own", " ".join(plan.notes))
        self.assertIn("deterministic planner answered instead", " ".join(plan.notes))

    def test_a_model_proposal_is_a_plan_but_never_a_grant(self) -> None:
        from actionmesh.core.planner import RuleBasedPlanner
        from actionmesh.voiceos.llm import LlmPlanner

        planner = LlmPlanner(
            provider=_StubProvider(
                answer={
                    "actions": [
                        {"type": "device.process.list", "parameters": {"limit": 20}}
                    ]
                }
            ),
            fallback=RuleBasedPlanner(),
            known_action=lambda name: name == "device.process.list",
        )
        plan = planner.plan("what is running?")
        self.assertEqual(len(plan.actions), 1)
        self.assertEqual(plan.actions[0].type, "device.process.list")
        # A Plan has no authority fields at all; this is the boundary, restated.
        self.assertFalse(hasattr(plan.actions[0], "risk_level"))
        self.assertFalse(hasattr(plan.actions[0], "approved"))

    def test_a_failing_provider_falls_back_and_says_so(self) -> None:
        from actionmesh.core.planner import RuleBasedPlanner
        from actionmesh.voiceos.llm import LlmError, LlmPlanner

        planner = LlmPlanner(
            provider=_StubProvider(error=LlmError("the model service did not answer in time")),
            fallback=RuleBasedPlanner(),
            known_action=lambda _: True,
        )
        plan = planner.plan("show processes")
        self.assertIn("the model did not answer this request", " ".join(plan.notes))
        self.assertIn("deterministic planner answered instead", " ".join(plan.notes))

    def test_no_credential_means_no_provider_not_a_silent_one(self) -> None:
        from actionmesh.voiceos.llm import ENV_API_KEY, ENV_API_KEY_FALLBACK

        with mock.patch.dict(os.environ, {}, clear=False):
            for name in (ENV_API_KEY, ENV_API_KEY_FALLBACK):
                os.environ.pop(name, None)
            provider, reason = build_provider(LlmConfig(provider="anthropic"))
        self.assertIsNone(provider)
        self.assertIn("no credential is present", reason)

    def test_a_present_credential_yields_a_provider(self) -> None:
        from actionmesh.voiceos.llm import ENV_API_KEY

        provider, reason = build_provider(
            LlmConfig(provider="anthropic"), environ={ENV_API_KEY: "sk-test"}
        )
        self.assertIsNotNone(provider)
        self.assertEqual(reason, "")
        self.assertEqual(provider.model, "claude-sonnet-5-5")

    def test_the_key_never_appears_in_a_reason_or_a_payload(self) -> None:
        from actionmesh.voiceos.llm import ENV_API_KEY, AnthropicProvider

        provider, reason = build_provider(
            LlmConfig(provider="anthropic"), environ={ENV_API_KEY: "sk-secret-value"}
        )
        self.assertNotIn("sk-secret-value", reason)
        assert isinstance(provider, AnthropicProvider)
        import json as _json

        self.assertNotIn("sk-secret-value", _json.dumps(provider.payload("hi")))


class _StubProvider:
    """A provider that answers from a fixed dict, or raises, without any network."""

    name = "stub"

    def __init__(self, *, answer: Any = None, error: Exception | None = None) -> None:
        self._answer = answer
        self._error = error

    def propose(self, request: str) -> Any:
        if self._error is not None:
            raise self._error
        return self._answer


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

    def test_a_persisted_plan_reloads_with_its_bind_and_analysis_intact(self) -> None:
        """A stored session must come back as the *same plan*, not a lesser one.

        Two fields are absent from the client view on purpose and were consequently
        absent from the stored payload too, because both paths went through
        ``to_dict``. The read that follows "which server is running?" then reloaded
        with no ``bind`` and was denied for a missing ``process_id``, and the
        explanation placeholder reloaded as an ordinary step with an empty
        ``action_type``, which the graph validator rejected with a 500. Found by
        running the diagnosis plan against the live app.
        """
        path = Path(tempfile.mkstemp(suffix=".db")[1])
        store = VoiceOsStore(path)
        session = TaskSession(
            session_id="task_diag",
            actor_id="voice-user",
            request="check my backend",
            steps=[
                TaskStep(index=0, label="status", action_type="device.server.status"),
                TaskStep(
                    index=1,
                    label="read",
                    action_type="device.process.logs",
                    bind={"process_id": {"from_step": 0, "key": "servers", "field": "process_id"}},
                ),
                TaskStep(
                    index=2,
                    label="explain",
                    action_type="",
                    analysis={"question": "check my backend", "computer": "laptop-1"},
                ),
            ],
        )
        store.save_session(session)
        store.close()
        reopened = VoiceOsStore(path)
        self.addCleanup(reopened.close)
        loaded = reopened.get_session("task_diag")
        assert loaded is not None
        self.assertEqual(
            loaded.steps[1].bind,
            {"process_id": {"from_step": 0, "key": "servers", "field": "process_id"}},
        )
        self.assertEqual(loaded.steps[2].analysis["computer"], "laptop-1")
        # The placeholder must not become runnable just because it was persisted.
        self.assertIs(loaded.pending_step(), loaded.steps[0])

    def test_the_client_view_still_withholds_internal_fields(self) -> None:
        # `to_storage` narrows the gap between the two views; it must not erase it.
        # `analysis` carries the question and the recorded answer, neither of which is
        # part of a step's capability, and a client has no business writing `bind`.
        step = TaskStep(
            index=2,
            label="explain",
            action_type="",
            bind={"process_id": {"from_step": 0}},
            analysis={"question": "q", "computer": "laptop-1"},
        )
        view = step.to_dict()
        self.assertNotIn("analysis", view)
        self.assertNotIn("bind", view)
        self.assertIn("analysis", step.to_storage())
        self.assertIn("bind", step.to_storage())


class TestStepIsNamedBeforeItRuns(unittest.TestCase):
    """A crash during a step must leave a session that points at the run.

    The failure this guards against is subtle and one-directional. If a step's execution
    id is only written when the step finishes, then the crash that strands the step
    leaves nothing behind to ask about: the record exists, the session says `pending`,
    and the next press runs the work a second time. Writing the id before the await
    turns that into a stranded `running` step -- true, and harmless.
    """

    def setUp(self) -> None:
        from actionmesh.core.executor import ExecutionEngine
        from actionmesh.core.permissions import PolicyEngine
        from actionmesh.core.registry import ActionRegistry
        from actionmesh.core.storage import SQLiteStore
        from actionmesh.providers import MockProvider
        from actionmesh.voiceos.orchestrator import Orchestrator

        _clean_env(self)
        self.directory = tempfile.mkdtemp(prefix="am_voiceos_")
        self.addCleanup(shutil.rmtree, self.directory, True)
        self.store = SQLiteStore(str(Path(self.directory) / "executions.db"))
        self.addCleanup(self.store.close)
        self.engine = ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [MockProvider()],
            store=self.store,
            owner_id="proc-test",
        )
        self.orchestrator = Orchestrator(engine=self.engine)
        self.session = TaskSession(
            session_id="task_x",
            actor_id="voice-user",
            request="send one message",
            steps=[TaskStep(index=0, label="one", action_type="message.send",
                            parameters={"to": "mom", "message": "hi"})],
        )
        self.context = PolicyContext(
            actor=Identity(id="voice-user"),
            grants=[
                PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)
            ],
            policy=Policy(
                rules=[PolicyRule(action="message.send", decision=PolicyDecision.ALLOW)]
            ),
        )

    def test_stable_step_idempotency_key_reaches_the_existing_action_graph(self) -> None:
        step = TaskStep(
            index=0, label="restart service", action_type="device.server.start",
            parameters={"name": "api"}, idempotency_key="repair-attempt-1",
        )
        graph = self.orchestrator._graph(self.session, step)
        action_node = next(node for node in graph.nodes if node.action is not None)
        assert action_node.action is not None
        self.assertEqual(action_node.action.idempotency_key, "repair-attempt-1")

    def test_automatic_execution_does_not_continue_after_unverified_result(self) -> None:
        from actionmesh.agent.definitions import register_device_actions
        from actionmesh.core.executor.engine import ExecutionResult
        from actionmesh.protocol.models import ActionState

        register_device_actions(self.engine.registry)
        self.session.steps = [
            TaskStep(index=index, label="List files", action_type="device.file.list")
            for index in range(2)
        ]
        with mock.patch.object(
            self.orchestrator, "_run_step", return_value=ExecutionResult(
                execution_id="check", state=ActionState.REQUIRES_USER, simulated=False,
            ),
        ) as execute:
            run(self.orchestrator.advance(self.session, self.context, stop_on_unverified=True))
        self.assertEqual(execute.await_count, 1)
        self.assertEqual(self.session.steps[1].state, "pending")
        self.assertEqual(self.orchestrator.fold_records(self.session).status, "needs_review")

    def test_resolved_failed_approval_never_dispatches_the_next_step(self) -> None:
        from actionmesh.protocol.models import ActionState

        first = self.session.steps[0]
        first.state = "awaiting_approval"
        first.execution_id = "waiting"
        self.session.status = "awaiting_approval"
        self.session.steps.append(TaskStep(index=1, label="Later", action_type="message.send"))
        record = mock.Mock(state=ActionState.DENIED, results={})
        with (
            mock.patch.object(self.engine, "record", return_value=record),
            mock.patch.object(self.orchestrator, "_run_step") as execute,
        ):
            run(self.orchestrator.advance(self.session, self.context))
            run(self.orchestrator.advance(self.session, self.context))
        execute.assert_not_awaited()
        self.assertEqual(self.session.status, "failed")
        self.assertEqual(self.session.steps[1].state, "pending")

    def test_automatic_execution_pauses_before_simulated_actions(self) -> None:
        with mock.patch.object(self.orchestrator, "_run_step") as execute:
            run(self.orchestrator.advance(self.session, self.context, stop_on_unverified=True))
        execute.assert_not_awaited()
        self.assertEqual(self.session.status, "needs_review")
        self.assertIn("simulation", self.session.summary)

    def test_the_execution_id_is_recorded_while_the_step_is_still_running(self) -> None:
        seen: list[tuple[str, str]] = []
        original = self.orchestrator.engine.run_execution

        async def watch_inside_the_run(graph, context):
            # This is the window a SIGKILL lands in. What the checkpoint wrote is all a
            # restarted process will have when the step never returns.
            seen.append((self.session.steps[0].state, self.session.steps[0].execution_id))
            return await original(graph, context)

        self.orchestrator.engine.run_execution = watch_inside_the_run  # type: ignore[method-assign]

        def checkpoint(session: TaskSession) -> None:
            seen.append(("checkpoint", session.steps[0].execution_id))

        run(self.orchestrator.advance(self.session, self.context, checkpoint=checkpoint))

        state, execution_id = seen[0]
        self.assertEqual(state, "checkpoint")
        self.assertTrue(execution_id, "the step was awaited with no durable name")
        self.assertEqual(self.session.steps[0].execution_id, execution_id)
        self.assertIsNotNone(self.engine.record(execution_id))
        # Named and run are the same execution, not two ids that happen to be adjacent.
        self.assertEqual(seen[1][1], execution_id)

    def test_a_fresh_orchestrator_can_fold_the_named_step_after_a_restart(self) -> None:
        """The engine that ran it is gone; the record and the session are not."""
        from actionmesh.voiceos.orchestrator import Orchestrator

        run(self.orchestrator.advance(self.session, self.context))
        execution_id = self.session.steps[0].execution_id
        self.assertTrue(execution_id)
        self.assertEqual(self.session.steps[0].state, "succeeded")

        # Stand in for the crash: the session still says the step is in flight even
        # though the execution finished before anyone could fold it.
        self.session.steps[0].state = "running"
        self.session.status = "running"
        folded = Orchestrator(engine=self.engine).fold_records(self.session)
        self.assertEqual(folded.steps[0].state, "succeeded")
        self.assertEqual(folded.status, "completed")


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

    def test_overlapping_runs_cannot_dispatch_the_same_session_twice(self) -> None:
        async def scenario() -> None:
            entered = asyncio.Event()
            release = asyncio.Event()
            orchestrator = self.app.state.voiceos_orchestrator
            original = orchestrator.advance

            async def paused(*args: Any, **kwargs: Any) -> dict[str, Any]:
                entered.set()
                await release.wait()
                return await original(*args, **kwargs)

            transport = ASGITransport(app=self.app, client=LOCAL)
            async with AsyncClient(transport=transport, base_url="http://testserver") as client:
                actor = {"id": "voice-user"}
                response = await client.post(
                    "/voiceos/messages", json={"actor": actor, "text": "tell mom hi"},
                )
                session_id = response.json()["session_id"]
                route = f"/voiceos/sessions/{session_id}"
                with mock.patch.object(orchestrator, "advance", side_effect=paused) as advance:
                    first = asyncio.create_task(client.post(f"{route}/run", json={"actor": actor}))
                    try:
                        await asyncio.wait_for(entered.wait(), timeout=2)
                        duplicate = await client.post(f"{route}/run", json={"actor": actor})
                        self.assertEqual(duplicate.status_code, 409)
                        self.assertEqual((await client.get(route)).status_code, 200)
                    finally:
                        release.set()
                        completed = await first
                    self.assertEqual(completed.status_code, 200)
                    advance.assert_awaited_once()

        asyncio.run(scenario())

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
        session.automatic = True
        store.save_session(session)
        listed = self.client.get("/voiceos/sessions").json()
        row = next(item for item in listed if item["session_id"] == session_id)
        self.assertEqual(row["automatic_approval_execution_id"], execution_id)

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
