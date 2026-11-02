"""P11: deployment modes are real -- readiness, fail-closed production startup, and
execution state that survives a restart."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.config import ConfigError

LOCAL = ("127.0.0.1", 45123)  # the machine's own operator view


class TestReadiness(unittest.TestCase):
    def test_ready_reports_mode_and_devices_without_gating_on_connectivity(self) -> None:
        client = TestClient(create_app(), client=LOCAL)
        body = client.get("/ready").json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["mode"], "development")
        self.assertFalse(body["durable_execution"])
        self.assertEqual(body["online_devices"], 0)

    def test_ready_is_503_when_the_durable_store_is_unreachable(self) -> None:
        app = create_app()
        app.state.engine.store = _BrokenStore()
        response = TestClient(app, client=LOCAL).get("/ready")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "degraded")

    def test_health_never_leaks_configuration(self) -> None:
        body = TestClient(create_app(), client=LOCAL).get("/health").json()
        self.assertEqual(set(body), {"status", "protocol"})


class _BrokenStore:
    def healthy(self) -> bool:
        return False


class TestProductionStartup(unittest.TestCase):
    def test_production_without_durable_config_does_not_start(self) -> None:
        with (
            patch.dict("os.environ", {"ACTIONMESH_ENV": "production"}, clear=True),
            self.assertRaises(ConfigError),
        ):
            create_app()

    def test_production_starts_when_every_safeguard_is_present(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            env = {
                "ACTIONMESH_ENV": "production",
                "ACTIONMESH_API_KEY": "edge-key",
                "ACTIONMESH_SECRET_KEY": "a-long-durable-secret",
                "ACTIONMESH_DATABASE_URL": f"sqlite:///{Path(directory) / 'app.db'}",
                "ACTIONMESH_AUDIT_DB": str(Path(directory) / "audit.db"),
                "ACTIONMESH_PUBLIC_BASE_URL": "https://mesh.example",
                "ACTIONMESH_HOST": "127.0.0.1",
            }
            with patch.dict("os.environ", env, clear=True):
                app = create_app()
            self.addCleanup(app.state.engine.store.close)
            self.addCleanup(app.state.audit_backend.close)
            body = TestClient(app, client=LOCAL, headers={"x-api-key": "edge-key"}).get("/ready").json()
            self.assertEqual(body["mode"], "production")
            self.assertTrue(body["durable_execution"])
            self.assertTrue(body["durable_audit"])


class TestDurableAcrossRestart(unittest.TestCase):
    def test_a_completed_execution_is_readable_after_the_process_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = f"sqlite:///{Path(directory) / 'app.db'}"
            with patch.dict("os.environ", {"ACTIONMESH_DATABASE_URL": db}, clear=True):
                first = create_app()
                client = TestClient(first, client=LOCAL)
                client.post(
                    "/consents",
                    json={"actor_id": "op", "scope": "message.send", "max_risk": "high"},
                )
                response = client.post(
                    "/execute",
                    json={
                        "actor": {"id": "op"},
                        "graph": {
                            "nodes": [
                                {
                                    "node_id": "m",
                                    "action": {
                                        "type": "message.send",
                                        "parameters": {"to": "mom", "message": "hi"},
                                    },
                                },
                                {"node_id": "end", "node_type": "end", "depends_on": ["m"]},
                            ]
                        },
                        "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
                    },
                )
                execution_id = response.json()["execution_id"]
                first.state.engine.store.close()

                # A brand-new process: nothing in memory, only the file on disk.
                second = create_app()
                self.addCleanup(second.state.engine.store.close)
                recovered = TestClient(second, client=LOCAL).get(f"/executions/{execution_id}")
                self.assertEqual(recovered.status_code, 200)
                self.assertEqual(recovered.json()["state"], "success")


if __name__ == "__main__":
    unittest.main()
