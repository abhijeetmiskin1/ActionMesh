"""Phase 5 Worker E — end-to-end journey tests through the real HTTP surface.

FIXTURE-TESTED ONLY: every test runs against an in-process ``create_app()``
with the mock provider. No laptop agent, real browser, real MPRIS player,
real vendor, or live LLM is involved. Labels are kept distinct on purpose:

* a ``success`` here means the *pipeline* (plan -> policy -> execute ->
  verify -> audit) behaved, with a simulated provider — it is not live
  verification of any OS/browser/media effect;
* ``booking.submit`` / ``order.place`` are never executed through the mock
  path; their fail-closed refusal is proved at the ``LaptopProvider``
  layer, where the handler actually lives;
* an unwired LLM provider is proved to return no provider (never a fake
  success), with the deterministic planner as the only answer.

Journeys pin the contract across seams that single-layer tests cannot:
the same intent proposed by ``/plan`` must be the action ``/execute``
can run (and ``/voiceos/messages`` must propose the same step without
running it); a denied intent must leave audit + execution state clean;
audit must verify after a real run; a provider that cannot answer must
say so in user-visible text.
"""

from __future__ import annotations

import asyncio
import unittest

from fastapi.testclient import TestClient

from actionmesh.agent.definitions import PROVIDER
from actionmesh.agent.provider import LaptopProvider
from actionmesh.api import create_app
from actionmesh.protocol.models import Action
from actionmesh.providers.base import ProviderError
from actionmesh.voiceos.llm import LlmConfig, build_provider

LOCAL = ("127.0.0.1", 45123)

INTENT = "tell mom I'll be late"
# Canonical (action_type, parameters) both /plan and VoiceOS derive from the
# same deterministic planner for this intent; pin them so drift is a failure.
EXPECTED_PLAN = ("message.send", {"to": "mom", "message": "I'll be late"})


def _client() -> TestClient:
    return TestClient(create_app(), client=LOCAL)


def _execute_message(client: TestClient, actor: str = "journey-user") -> dict:
    return client.post(
        "/execute",
        json={
            "actor": {"id": actor},
            "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            "graph": {
                "intent": "send hello",
                "nodes": [
                    {
                        "node_id": "a1",
                        "action": {
                            "type": "message.send",
                            "parameters": {"message": "hello", "to": "mom"},
                        },
                    },
                    {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                ],
            },
        },
    ).json()


class TestIntentPlanPolicyExecuteVerifyAudit(unittest.TestCase):
    """The full pipeline on one intent, end to end, in order."""

    def setUp(self) -> None:
        self.client = _client()
        self.client.post(
            "/consents",
            json={
                "actor_id": "journey-user",
                "scope": "message.send",
                "max_risk": "high",
            },
        )

    def test_plan_proposes_exactly_what_execute_runs_and_audit_remembers(self) -> None:
        # 1. Plan: proposes, never runs.
        before = len(self.client.get("/executions").json())
        plan = self.client.post("/plan", json={"intent": INTENT}).json()
        self.assertEqual(len(plan["actions"]), 1)
        action = plan["actions"][0]
        self.assertEqual((action["type"], action["parameters"]), EXPECTED_PLAN)
        self.assertEqual(len(self.client.get("/executions").json()), before)

        # 2. Policy + execute: the planned action, run under consent + allow.
        result = _execute_message(self.client)
        self.assertEqual(result["state"], "success")
        node = result["nodes"]["a1"]
        self.assertEqual(node["action_type"], EXPECTED_PLAN[0])
        self.assertEqual(node["state"], "success")

        # 3. Verify: independent observed-state confirmation, not the
        # executor certifying itself. `verified` is a separate verdict
        # from `state`, and its method/detail must name the observation.
        self.assertTrue(node["verified"])
        self.assertEqual(node["verification"]["method"], "observed_state")
        self.assertTrue(node["verification"]["verified"])
        self.assertIn("observed state matches", node["verification"]["detail"])

        # 4. Audit: the run is recorded and the chain still verifies.
        record = self.client.get(
            f"/executions/{result['execution_id']}"
        ).json()
        self.assertEqual(record["state"], "success")
        self.assertEqual(record["node_states"]["a1"], "success")
        chain = self.client.get("/audit/verify").json()
        self.assertTrue(chain["ok"], chain)
        self.assertGreaterEqual(chain["records"], 4)
        trail = self.client.get("/audit").json()
        kinds = [r["result"] for r in trail["records"]]
        self.assertIn("execution.started", kinds)
        self.assertIn("authorization.decided", kinds)
        self.assertIn("execution.completed", kinds)

    def test_the_same_intent_via_voice_proposes_the_same_step_without_running(self) -> None:
        # VoiceOS is a client on the same pipeline: it proposes the same
        # deterministic step /plan derived, and a message alone runs nothing.
        before = len(self.client.get("/executions").json())
        answer = self.client.post(
            "/voiceos/messages", json={"text": INTENT}
        ).json()
        self.assertEqual(answer["kind"], "task")
        self.assertFalse(answer["ran"])
        session = self.client.get(
            f"/voiceos/sessions/{answer['session_id']}"
        ).json()
        self.assertEqual(len(session["steps"]), 1)
        step = session["steps"][0]
        self.assertEqual(step["action_type"], EXPECTED_PLAN[0])
        self.assertEqual(step["parameters"], EXPECTED_PLAN[1])
        self.assertEqual(step["state"], "pending")
        # And /plan independently derives the identical step.
        plan = self.client.post("/plan", json={"intent": INTENT}).json()
        self.assertEqual(plan["actions"][0]["type"], step["action_type"])
        self.assertEqual(plan["actions"][0]["parameters"], step["parameters"])
        self.assertEqual(len(self.client.get("/executions").json()), before)


class TestRejectedIntentRunsNothing(unittest.TestCase):
    """Policy is the only authorization producer: no consent, no execution."""

    def test_denied_execute_leaves_no_execution_and_no_success_claim(self) -> None:
        client = _client()
        body = client.post(
            "/execute",
            json={
                "actor": {"id": "nobody"},
                "policy": {
                    "rules": [{"action": "message.send", "decision": "allow"}]
                },
                "graph": {
                    "nodes": [
                        {
                            "node_id": "a1",
                            "action": {
                                "type": "message.send",
                                "parameters": {"message": "hi"},
                            },
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                    ]
                },
            },
        ).json()
        self.assertEqual(body["state"], "denied")
        self.assertEqual(body["nodes"], {})
        self.assertIn("missing permission", body["error"])
        # Denied means not recorded as a run and never reported as success.
        self.assertEqual(client.get("/executions").json(), [])
        # The denial itself is auditable, and the chain still verifies.
        self.assertTrue(client.get("/audit/verify").json()["ok"])


class TestProviderUnavailableIsNotFakeSuccess(unittest.TestCase):
    """An unwired provider returns no provider and says why, in plain text."""

    def test_unwired_provider_yields_no_provider_and_a_user_visible_reason(self) -> None:
        provider, reason = build_provider(
            LlmConfig(provider="some-vendor", model="m")
        )
        self.assertIsNone(provider, "no provider may be handed back that cannot answer")
        self.assertIn("no approved outbound transport", reason)
        self.assertIn("some-vendor", reason)

    def test_anthropic_without_a_credential_is_unavailable_not_silent(self) -> None:
        provider, reason = build_provider(
            LlmConfig(provider="anthropic"),
            environ={},
        )
        self.assertIsNone(provider)
        self.assertIn("no credential is present", reason)
        self.assertIn("deterministic planner answered", reason)

    def test_booking_and_ordering_refuse_fail_closed_at_the_provider(self) -> None:
        # The CRITICAL submit handlers exist (SUPPORTED == IMPLEMENTED) but
        # must raise PERMANENT (no payment backend) and record no effect —
        # never fabricate a confirmation (see provider._submit docstring).
        provider = LaptopProvider(None)
        for op, params in (
            ("booking.submit", {"journey": "A->B", "passenger": "Ada", "fare": "10"}),
            ("order.place", {"items": "tea", "total": "5"}),
        ):
            with self.subTest(op=op):
                action = Action(
                    type=op, provider=PROVIDER, parameters=dict(params)
                )
                with self.assertRaises(ProviderError) as caught:
                    asyncio.run(provider.execute(action))
                self.assertIn("no payment backend", str(caught.exception))
                self.assertIsNone(provider.observe(action))
                self.assertNotIn(action.action_id, provider._effects)


if __name__ == "__main__":
    unittest.main()
