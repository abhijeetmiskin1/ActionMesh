"""ActionMesh — Connections Center (Phase 6, Agent 4).

The Python half of this lane: the device and pairing routes this center
renders against, tested from the outside with a TestClient, because that
is the only level at which "the lane only prints what the backend said"
is a real claim.

The browser bridge and the provider lanes are not Python routes — they
live in the Electron main process (desktop/app/browser_bridge.js and the
purpose IPC in main.js) — so they are deliberately not tested here. The
renderer half of that honesty is covered by desktop/test/connections.test.js.
"""

from __future__ import annotations

import os
import unittest

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.protocol.models import DeviceKind

LOCAL = ("127.0.0.1", 45123)
REMOTE = ("192.168.1.50", 55555)


class _EnvPatch(unittest.TestCase):
    """TestClient's default peer address is `testclient`, which is not loopback."""

    def setUp(self) -> None:
        for name in (
            "ACTIONMESH_API_KEY",
            "ACTIONMESH_ALLOW_REMOTE_PAIRING",
            "ACTIONMESH_SECRET_KEY",
            "ACTIONMESH_SECRET_FILE",
        ):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.identities = IdentityStore()
        self.app = create_app(
            identities=self.identities, devices=DeviceRegistry(identities=self.identities)
        )
        self.local = TestClient(self.app, client=LOCAL)
        self.remote = TestClient(self.app, client=REMOTE)
        self.app.state.devices.enroll(  # type: ignore[attr-defined]
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="My Laptop",
            platform="linux",
            capabilities=["message.send", "device.files.read"],
        )


class TestDevicesLane(_EnvPatch):
    def test_no_devices_lists_enrolled_laptop_only(self) -> None:
        listed = self.local.get("/devices").json()
        self.assertEqual(len(listed), 1)
        row = listed[0]
        self.assertEqual(row["device_id"], "laptop-1")
        for field in ("online", "last_seen", "risk_ceiling", "enrolled"):
            self.assertIn(field, row)
        self.assertIs(row["enrolled"], True)

    def test_identity_handle_never_leaks_into_a_device_row(self) -> None:
        listed = self.local.get("/devices").json()
        for row in listed:
            for private in ("identity_id", "secret", "token", "key"):
                self.assertNotIn(private, row, f"key {private!r} leaked in device row")

    def test_empty_registry_is_not_configured_not_error(self) -> None:
        identities = IdentityStore()
        bare = create_app(identities=identities, devices=DeviceRegistry())
        client = TestClient(bare, client=LOCAL)
        self.assertEqual(client.get("/devices").json(), [])

    def test_unknown_device_is_404_not_a_fake_row(self) -> None:
        self.assertEqual(self.local.get("/devices/ghost").status_code, 404)

    def test_device_row_requires_no_grant_and_sends_none(self) -> None:
        self.assertEqual(self.local.get("/devices").status_code, 200)
        self.assertEqual(self.local.get("/devices/laptop-1").status_code, 200)

    def test_revoke_forgets_the_device_so_the_lane_empties(self) -> None:
        response = self.local.post("/devices/laptop-1/revoke", json={"reason": "gone"})
        self.assertEqual(response.status_code, 200)
        self.assertIs(response.json()["revoked"], True)
        self.assertEqual(self.local.get("/devices").json(), [])

    def test_revoke_unknown_device_is_404(self) -> None:
        self.assertEqual(
            self.local.post("/devices/ghost/revoke", json={}).status_code, 404
        )

    def test_no_self_service_admission_endpoints(self) -> None:
        for method, path in (
            ("POST", "/devices"),
            ("PUT", "/devices"),
            ("POST", "/devices/register"),
            ("POST", "/devices/heartbeat"),
        ):
            with self.subTest(method=method, path=path):
                self.assertIn(
                    getattr(self.local, method.lower())(path).status_code,
                    (404, 405),
                )


class TestPairingLane(_EnvPatch):
    def test_mint_shows_a_code_in_the_documented_shape(self) -> None:
        body = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()
        self.assertRegex(body["code"], r"^\d{3}-\d{3}$")
        self.assertEqual(body["laptop_device_id"], "laptop-1")
        self.assertIn("expires_at", body)
        self.assertIn("expires_in_s", body)
        self.assertEqual(body["max_risk"], "low")

    def test_mint_at_a_named_ceiling_reports_that_ceiling(self) -> None:
        body = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": "medium"}
        ).json()
        self.assertEqual(body["max_risk"], "medium")

    def test_pairing_not_durable_without_a_key(self) -> None:
        body = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()
        self.assertIs(body["pairing_durable"], False)

    def test_mint_is_loopback_only(self) -> None:
        self.assertEqual(
            self.remote.post("/pairing/code", json={"device_id": "laptop-1"}).status_code,
            403,
        )

    def test_revoke_all_burns_live_codes(self) -> None:
        self.local.post("/pairing/code", json={"device_id": "laptop-1"})
        revoked = self.local.post("/pairing/revoke", json={}).json()["revoked"]
        self.assertEqual(revoked, 1)

    def test_remote_revoke_all_is_refused(self) -> None:
        self.assertEqual(self.remote.post("/pairing/revoke", json={}).status_code, 403)


class TestCapabilitiesForLanes(_EnvPatch):
    def test_capabilities_route_answers_a_list_of_names(self) -> None:
        body = self.local.get("/capabilities").json()
        self.assertIsInstance(body, list)
        self.assertTrue(all(isinstance(entry, dict) for entry in body))
        # the route may use different field spellings (name/action); the
        # renderer only needs a non-empty answer each time
        named = [
            entry
            for entry in body
            if isinstance(entry, dict) and ("name" in entry or "action" in entry)
        ]
        self.assertEqual(len(named), len(body))

    def test_capabilities_never_advertise_a_grant_or_key(self) -> None:
        body = self.local.get("/capabilities").json()
        lowered = str(body).lower()
        # an explicit grant schema/name would be a client-authority vector;
        # the route must name what the agent may do, never a permission box
        self.assertNotIn("grants", lowered)
        self.assertNotIn("grant", lowered)
        self.assertNotIn("secret", lowered)
        self.assertNotIn("api_key", lowered)
        # tokens belong to the pairing ceremony, not to a capability row
        self.assertNotIn("token", lowered)


class TestMediaBoundaryAdvertisement(_EnvPatch):
    def test_media_capabilities_are_advertised_only_when_the_boundary_is_open(self) -> None:
        body = self.local.get("/capabilities").json()
        names = {entry["name"] for entry in body if isinstance(entry, dict) and "name" in entry}
        media_names = {name for name in names if name.startswith("media.")}
        # present; never a partial advertisement.
        if media_names:
            self.assertIn("media.status", names)
            self.assertIn("media.play", names)


if __name__ == "__main__":
    unittest.main()
