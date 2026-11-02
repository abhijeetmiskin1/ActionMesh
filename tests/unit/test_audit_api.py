"""C15 audit trail over the HTTP boundary.

The point of these tests is that the *service* audits itself: an ordinary
`/execute` call has to land in the chain without anyone passing `audit=`.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.audit import AuditIntegrityError, AuditLog

LOCAL = ("127.0.0.1", 45123)  # the machine's own operator view

GRAPH = {
    "nodes": [
        {
            "node_id": "a1",
            "action": {"type": "message.send", "parameters": {"message": "audited"}},
        },
        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
    ]
}


def execute(client: TestClient) -> None:
    """Grant the permission, then run a real graph through the API."""
    consent = client.post(
        "/consents",
        json={"actor_id": "user-1", "scope": "message.send", "max_risk": "high"},
    )
    assert consent.status_code == 200, consent.text
    response = client.post("/execute", json={"graph": GRAPH})
    assert response.status_code == 200, response.text


class TestAuditEndpoints(unittest.TestCase):
    def setUp(self) -> None:
        # A fresh app per test, so chains cannot leak between tests.
        self.client = TestClient(create_app(), client=LOCAL)

    def test_chain_is_intact_before_anything_runs(self) -> None:
        response = self.client.get("/audit/verify")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["broken_at"], None)

    def test_execute_produces_audit_records_without_any_opt_in(self) -> None:
        before = self.client.get("/audit").json()["chain"]["records"]
        execute(self.client)
        body = self.client.get("/audit").json()
        self.assertGreater(body["chain"]["records"], before)
        self.assertTrue(body["chain"]["ok"])
        self.assertTrue(any(r["kind"] == "event" for r in body["records"]))

    def test_limit_is_validated(self) -> None:
        self.assertEqual(
            self.client.get("/audit", params={"limit": 0}).status_code, 400
        )
        self.assertEqual(
            self.client.get("/audit", params={"limit": 1001}).status_code, 400
        )

    def test_limit_caps_the_returned_records(self) -> None:
        execute(self.client)
        body = self.client.get("/audit", params={"limit": 2}).json()
        self.assertLessEqual(len(body["records"]), 2)

    def test_head_agrees_between_the_two_endpoints(self) -> None:
        execute(self.client)
        self.assertEqual(
            self.client.get("/audit").json()["head"],
            self.client.get("/audit/verify").json()["head"],
        )

    def test_injected_audit_log_is_used(self) -> None:
        log = AuditLog()
        client = TestClient(create_app(audit=log), client=LOCAL)
        execute(client)
        self.assertTrue(log.verify())
        self.assertGreater(len(log), 0)
        self.assertEqual(client.get("/audit").json()["chain"]["records"], len(log))

    def test_a_denied_execution_is_audited(self) -> None:
        # No consent is granted, so authorization fails. The denial is the most
        # security-relevant outcome there is — it must not be the one that
        # leaves no trace.
        response = self.client.post("/execute", json={"graph": GRAPH})
        self.assertEqual(response.json()["state"], "denied")
        records = self.client.get("/audit").json()["records"]
        self.assertTrue(
            any(r["result"] == "execution.denied" for r in records),
            f"denial missing from the audit trail: {records}",
        )
        self.assertTrue(self.client.get("/audit/verify").json()["ok"])


class TestAuditPersistenceViaApi(unittest.TestCase):
    @staticmethod
    def _client_with_db(path: Path) -> TestClient:
        with patch.dict(os.environ, {"ACTIONMESH_AUDIT_DB": str(path)}):
            return TestClient(create_app(), client=LOCAL)

    def test_env_var_persists_the_chain_across_app_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            first = self._client_with_db(path)
            execute(first)
            first.app.state.audit_backend.close()

            # A brand new app reads the same chain back and still verifies.
            second = self._client_with_db(path)
            body = second.get("/audit").json()
            second.app.state.audit_backend.close()
            self.assertTrue(body["chain"]["ok"])
            self.assertGreater(body["chain"]["records"], 0)

    def test_tampered_persisted_chain_stops_startup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            client = self._client_with_db(path)
            execute(client)
            client.app.state.audit_backend.close()

            raw = sqlite3.connect(str(path))
            raw.execute("UPDATE audit_records SET result = 'forged'")
            raw.commit()
            raw.close()

            # The rejected backend is closed internally, so the store is not
            # left locked on the way out.
            with (
                patch.dict(os.environ, {"ACTIONMESH_AUDIT_DB": str(path)}),
                self.assertRaises(AuditIntegrityError),
            ):
                create_app()

    def test_blank_env_var_falls_back_to_in_memory(self) -> None:
        with patch.dict(os.environ, {"ACTIONMESH_AUDIT_DB": "   "}):
            app = create_app()
        self.assertIsNone(app.state.audit.backend)
        self.assertIsNone(app.state.audit_backend)
