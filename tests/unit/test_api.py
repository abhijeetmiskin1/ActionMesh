"""C8 REST API tests."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app())

    def test_health(self) -> None:
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["protocol"], "AMP/1.0")

    def test_capabilities_are_discoverable(self) -> None:
        response = self.client.get("/capabilities", params={"provider": "mock"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 16)

    def test_execute_requires_authorization_context(self) -> None:
        response = self.client.post(
            "/execute",
            json={"graph": {"nodes": [
                {"node_id": "a1", "action": {
                    "type": "message.send", "parameters": {"message": "hello"}
                }},
                {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
            ]}},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "denied")

    def test_execute_runs_authorized_graph_and_can_be_looked_up(self) -> None:
        consent = self.client.post(
            "/consents",
            json={"actor_id": "user-1", "scope": "message.send", "max_risk": "high"},
        )
        self.assertEqual(consent.status_code, 200)
        response = self.client.post(
            "/execute",
            json={
                "graph": {
                    "intent": "send hello",
                    "nodes": [
                        {"node_id": "a1", "action": {
                            "type": "message.send",
                            "parameters": {"message": "hello", "to": "mom"},
                        }},
                        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                    ],
                },
                "actor": {"id": "user-1"},
                "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["state"], "success")
        lookup = self.client.get(f"/executions/{body['execution_id']}")
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.json()["state"], "success")
        event_types = [
            event.event_type
            for event in self.client.app.state.engine.events.history(body["execution_id"])
        ]
        self.assertEqual(
            event_types,
            ["execution.started", "node.completed", "node.completed", "execution.completed"],
        )

    def test_request_body_cannot_inject_permission_grants(self) -> None:
        response = self.client.post(
            "/execute",
            json={
                "graph": {"nodes": [{"node_id": "end", "node_type": "end"}]},
                "grants": [{"scope": "message.send"}],
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_consent_is_scoped_to_the_granting_actor(self) -> None:
        self.client.post(
            "/consents",
            json={"actor_id": "owner", "scope": "message.send", "max_risk": "high"},
        )
        response = self.client.post(
            "/execute",
            json={
                "actor": {"id": "different-user"},
                "graph": {"nodes": [
                    {"node_id": "message", "action": {
                        "type": "message.send", "parameters": {"message": "hello"}
                    }},
                    {"node_id": "end", "node_type": "end", "depends_on": ["message"]},
                ]},
                "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "denied")

    def test_request_cannot_opt_into_high_risk_execution(self) -> None:
        consent = self.client.post(
            "/consents",
            json={"actor_id": "high-risk-user", "scope": "file.share", "max_risk": "high"},
        )
        self.assertEqual(consent.status_code, 200)
        response = self.client.post(
            "/execute",
            json={
                "actor": {"id": "high-risk-user"},
                "allow_high_risk": True,
                "policy": {"rules": [{"action": "file.share", "decision": "allow"}]},
                "graph": {"nodes": [
                    {"node_id": "file", "action": {
                        "type": "file.share",
                        "parameters": {"path": "/a", "target": "bob"},
                    }},
                    {"node_id": "end", "node_type": "end", "depends_on": ["file"]},
                ]},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "waiting_permission")

    def test_malformed_graph_is_rejected(self) -> None:
        response = self.client.post("/execute", json={"graph": {"nodes": "nope"}})
        self.assertEqual(response.status_code, 400)

    def test_unknown_execution_is_not_found(self) -> None:
        response = self.client.get("/executions/missing")
        self.assertEqual(response.status_code, 404)

    def test_execution_and_consent_lists_are_scoped(self) -> None:
        executions = self.client.get("/executions")
        self.assertEqual(executions.status_code, 200)
        consents = self.client.get("/consents", params={"actor_id": "user-1"})
        self.assertEqual(consents.status_code, 200)
        self.assertTrue(all(item["granted_by"] == "user-1" for item in consents.json()))

    def test_approval_and_cancel_endpoints(self) -> None:
        approval = self.client.post(
            "/approvals", json={"action_id": "act-1", "status": "approved"}
        )
        self.assertEqual(approval.status_code, 200)
        self.assertEqual(approval.json()["status"], "approved")
        cancel = self.client.post("/executions/missing/cancel")
        self.assertEqual(cancel.status_code, 404)


class TestPlanEndpoint(unittest.TestCase):
    """``POST /plan`` proposes; it never authorizes, approves or executes."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.client = TestClient(create_app())

    def test_plan_proposes_actions_without_running_them(self) -> None:
        before = len(self.client.get("/executions").json())
        response = self.client.post("/plan", json={"intent": "tell mom I'll be late"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([item["type"] for item in body["actions"]], ["message.send"])
        self.assertEqual(set(body["actions"][0]), {"type", "parameters"})
        # Proposing is not doing.
        self.assertEqual(len(self.client.get("/executions").json()), before)

    def test_plan_explains_an_unmatched_intent_instead_of_guessing(self) -> None:
        body = self.client.post("/plan", json={"intent": "write me a sonnet"}).json()
        self.assertEqual(body["actions"], [])
        self.assertTrue(any("no rule matched" in note for note in body["notes"]))

    def test_plan_requires_a_non_empty_string_intent(self) -> None:
        for payload in ({"intent": "   "}, {"intent": 7}, {}):
            with self.subTest(payload=payload):
                self.assertEqual(self.client.post("/plan", json=payload).status_code, 400)


if __name__ == "__main__":
    unittest.main()
