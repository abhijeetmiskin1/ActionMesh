"""C15 release smoke test."""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app


class TestReleaseSmoke(unittest.TestCase):
    def test_dashboard_and_execution_workflow(self) -> None:
        client = TestClient(create_app())
        self.assertEqual(client.get("/health").status_code, 200)
        dashboard = client.get("/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn("ActionMesh", dashboard.text)
        self.assertIn("Recent executions", dashboard.text)
        self.assertIn("EventSource('/events')", dashboard.text)
        self.assertEqual(client.get("/capabilities").status_code, 200)
        self.assertEqual(client.get("/metrics").status_code, 200)


if __name__ == "__main__":
    unittest.main()