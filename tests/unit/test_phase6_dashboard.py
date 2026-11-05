"""Phase 6 Agent 1: dashboard helpers verified against real routes.

No backend code was changed in this phase; these tests pin the contracts
that desktop/ui/activity.js depends on, so the module's documented
assumptions stay true (the activity.js test file mirrors them in JS).
"""

from __future__ import annotations

import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "actionmesh" / "api.py"
ACTIVITY = Path(__file__).resolve().parents[2] / "desktop" / "ui" / "activity.js"


class TestDashboardContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.api_src = BACKEND.read_text(encoding="utf-8")
        cls.activity_src = ACTIVITY.read_text(encoding="utf-8")

    def test_ready_route_exists(self) -> None:
        self.assertIn('@app.get("/ready"', self.api_src)

    def test_devices_approvals_executions_automations_audit_routes_exist(self) -> None:
        for marker in (
            '@app.get("/devices"',
            '@app.get("/approvals"',
            '@app.get("/executions"',
            '@app.get("/automations"',
            '@app.get("/audit"',
            '@app.get("/audit/verify"',
        ):
            self.assertIn(marker, self.api_src)

    def test_dashboard_route_is_html_not_json(self) -> None:
        # activity.js must not treat /dashboard (HTMLResponse) as stats data.
        start = self.api_src.index('@app.get("/dashboard"')
        segment = self.api_src[start : start + 400]
        self.assertIn("HTMLResponse", segment)
        self.assertNotIn("'/dashboard'", self.activity_src)
        self.assertNotIn('"/dashboard"', self.activity_src)

    def test_activity_js_uses_bridge_only(self) -> None:
        self.assertIn("actionmesh.request", self.activity_src)
        self.assertNotIn("fetch(", self.activity_src)
        self.assertNotIn("XMLHttpRequest", self.activity_src)
        self.assertNotIn(".innerHTML", self.activity_src)

    def test_activity_js_exports_and_routes(self) -> None:
        for name in ("loadDashboard", "renderDashboard", "initActivity"):
            self.assertIn(name, self.activity_src)
        for route in ("/ready", "/devices", "/approvals", "/executions?limit=8",
                      "/voiceos/sessions?limit=8", "/automations", "/audit?limit=8"):
            self.assertIn(route, self.activity_src)


if __name__ == "__main__":
    unittest.main()
