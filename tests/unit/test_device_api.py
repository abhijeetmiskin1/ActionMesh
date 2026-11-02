"""C19 device endpoints: presence the server derived, and a way to cut a device off.

The absence of `/devices/register` and `/devices/heartbeat` is tested on purpose.
Section 1 says the *laptop* registers itself and section 9 says a phone earns its
place by pairing; an HTTP surface that could do either would hand anyone who
reached the port the ability to mint a principal the audit trail then trusts.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceInfo, DeviceKind

LOCAL = ("127.0.0.1", 45123)  # revoking is the operator's act


def _app() -> object:
    identities = IdentityStore()
    return create_app(
        identities=identities,
        devices=DeviceRegistry(identities=identities),
    )


class TestDeviceListing(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.client = TestClient(self.app, client=LOCAL)
        self.registry: DeviceRegistry = self.app.state.devices  # type: ignore[attr-defined]

    def test_no_devices_yet(self) -> None:
        self.assertEqual(self.client.get("/devices").json(), [])

    def test_enrolled_device_is_listed_with_derived_presence(self) -> None:
        self.registry.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="My Laptop",
            platform="linux",
            os_version="Ubuntu 24.04",
            agent_version="0.1.0",
            capabilities=["message.send"],
        )
        body = self.client.get("/devices").json()
        self.assertEqual(len(body), 1)
        device = body[0]
        self.assertEqual(device["device_id"], "laptop-1")
        self.assertEqual(device["name"], "My Laptop")
        self.assertEqual(device["kind"], "laptop")
        self.assertEqual(device["platform"], "linux")
        self.assertEqual(device["os_version"], "Ubuntu 24.04")
        self.assertEqual(device["agent_version"], "0.1.0")
        self.assertEqual(device["capabilities"], ["message.send"])
        # Enrolled but never heard from.
        self.assertFalse(device["online"])
        self.assertIsNone(device["last_seen"])
        self.assertTrue(device["enrolled"])

    def test_heartbeat_reports_online(self) -> None:
        self.registry.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        self.registry.heartbeat("laptop-1")
        self.assertTrue(self.client.get("/devices/laptop-1").json()["online"])

    def test_stale_heartbeat_reports_offline(self) -> None:
        self.registry.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        self.registry.heartbeat("laptop-1", now=datetime.now(UTC) - timedelta(hours=1))
        self.assertFalse(self.client.get("/devices/laptop-1").json()["online"])

    def test_identity_handle_is_not_leaked(self) -> None:
        device = self.registry.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        self.assertTrue(device.identity_id)
        for body in (
            self.client.get("/devices").json()[0],
            self.client.get("/devices/laptop-1").json(),
        ):
            self.assertNotIn("identity_id", body)
            self.assertNotIn("secret", str(body).lower())
            self.assertNotIn("token", str(body).lower())

    def test_a_device_registered_without_an_identity(self) -> None:
        # No identity store configured is a legitimate deployment shape (a single
        # local user), so `enrolled` has to say which case the caller is in.
        plain = create_app(devices=DeviceRegistry())
        plain.state.devices.register(  # type: ignore[attr-defined]
            DeviceInfo(device_id="old-phone", name="Old Phone")
        )
        body = TestClient(plain, client=LOCAL).get("/devices/old-phone").json()
        self.assertFalse(body["enrolled"])
        self.assertEqual(body["kind"], "unknown")

    def test_unknown_device_is_404(self) -> None:
        self.assertEqual(self.client.get("/devices/nope").status_code, 404)


class TestDeviceRevocation(unittest.TestCase):
    def setUp(self) -> None:
        self.app = _app()
        self.client = TestClient(self.app, client=LOCAL)
        self.registry: DeviceRegistry = self.app.state.devices  # type: ignore[attr-defined]
        self.identities: IdentityStore = self.app.state.identities  # type: ignore[attr-defined]
        self.device = self.registry.enroll(
            device_id="phone-1",
            kind=DeviceKind.PHONE,
            capabilities=["message.send"],
        )
        self.registry.heartbeat("phone-1")

    def test_revoke_forgets_and_revokes(self) -> None:
        body = self.client.post(
            "/devices/phone-1/revoke", json={"reason": "phone lost on a train"}
        ).json()
        self.assertTrue(body["revoked"])
        self.assertEqual(body["identity_id"], self.device.identity_id)
        self.assertEqual(self.client.get("/devices").json(), [])
        with self.assertRaises(IdentityError):
            self.identities.resolve(self.device.identity_id)

    def test_revocation_is_audited(self) -> None:
        self.client.post("/devices/phone-1/revoke", json={"reason": "stolen"})
        audit = self.app.state.audit  # type: ignore[attr-defined]
        entry = audit.records[-1]
        self.assertEqual(entry.kind, "device")
        self.assertEqual(entry.subject, "phone-1")
        self.assertEqual(entry.result, "revoked")
        self.assertEqual(entry.reason, "stolen")
        # §10: the relationship is recorded — which device, what role, what it could do.
        self.assertEqual(entry.payload["identity_id"], self.device.identity_id)
        self.assertEqual(entry.payload["kind"], "phone")
        self.assertEqual(entry.payload["capabilities"], ["message.send"])
        self.assertTrue(audit.verify().ok)

    def test_reason_is_bounded(self) -> None:
        response = self.client.post("/devices/phone-1/revoke", json={"reason": "x" * 5000})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["revoked"])
        # A free-text field a client controls must not be able to grow the audit
        # trail without bound.
        self.assertEqual(
            len(self.app.state.audit.records[-1].reason), 200  # type: ignore[attr-defined]
        )

    def test_revoke_unknown_device_is_404(self) -> None:
        self.assertEqual(
            self.client.post("/devices/nope/revoke", json={}).status_code, 404
        )

    def test_revoke_needs_no_grant_and_sends_none(self) -> None:
        # A revocation is a withdrawal. It must not require, or carry, a permission.
        response = self.client.post("/devices/phone-1/revoke", json={})
        self.assertEqual(response.status_code, 200)


class TestNoSelfServiceAdmission(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(_app())

    def test_there_is_no_registration_route(self) -> None:
        for method, path in (
            ("post", "/devices"),
            ("put", "/devices"),
            ("post", "/devices/register"),
        ):
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(path, json={"device_id": "x"})
                self.assertIn(response.status_code, (404, 405))

    def test_there_is_no_heartbeat_route(self) -> None:
        for path in ("/devices/laptop-1/heartbeat", "/heartbeat", "/devices/heartbeat"):
            with self.subTest(path=path):
                self.assertIn(
                    self.client.post(path, json={}).status_code, (404, 405)
                )


if __name__ == "__main__":
    unittest.main()
