"""C19c pairing over HTTP: who may mint, who may spend, and what a token proves.

Three asymmetries carry the whole design and each is tested from the outside,
because that is the only level at which they are real:

* minting a code is loopback-only, spending one is not;
* a token names the actor, and the request body cannot overrule it;
* revoking a device cuts its token off, so forgetting a stolen phone is one call.
"""

from __future__ import annotations

import os
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.core.credentials import CredentialStore
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
        self.devices = DeviceRegistry(identities=self.identities)
        self.app = create_app(
            identities=self.identities, devices=self.devices
        )
        self.local = TestClient(self.app, client=LOCAL)
        self.remote = TestClient(self.app, client=REMOTE)
        self.devices.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="My Laptop",
            platform="linux",
            capabilities=["message.send"],
        )

    def pair(self, max_risk: str = "medium") -> tuple[str, dict]:
        """Mint on the laptop at a chosen ceiling, spend from the phone across the room."""
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": max_risk}
        ).json()["code"]
        response = self.remote.post(
            "/pair",
            json={"code": code, "device_id": "phone-1", "actor_id": "abhi", "name": "Pixel"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"], response.json()


class TestCodeMinting(_EnvPatch):
    def test_the_laptop_can_mint_a_code(self) -> None:
        body = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()
        self.assertRegex(body["code"], r"^\d{3}-\d{3}$")
        self.assertEqual(body["laptop_device_id"], "laptop-1")
        self.assertEqual(body["expires_in_s"], 300.0)
        self.assertIn("expires_at", body)

    def test_a_remote_client_cannot_mint_a_code(self) -> None:
        response = self.remote.post("/pairing/code", json={"device_id": "laptop-1"})
        self.assertEqual(response.status_code, 403)
        self.assertIn("laptop itself", response.json()["detail"])

    def test_a_remote_mint_can_be_widened_deliberately(self) -> None:
        os.environ["ACTIONMESH_ALLOW_REMOTE_PAIRING"] = "1"
        self.assertEqual(
            self.remote.post("/pairing/code", json={"device_id": "laptop-1"}).status_code,
            200,
        )

    def test_minting_needs_a_device_id(self) -> None:
        for bad in ({}, {"device_id": ""}, {"device_id": 5}):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.local.post("/pairing/code", json=bad).status_code, 400
                )

    def test_minting_for_an_unknown_device_is_refused(self) -> None:
        self.assertEqual(
            self.local.post("/pairing/code", json={"device_id": "ghost"}).status_code, 400
        )

    def test_durable_pairing_is_reported_honestly(self) -> None:
        # With no configured key the server is ephemeral, and a restart unpairs
        # everyone. Saying so beats letting the user find out.
        body = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()
        self.assertFalse(body["pairing_durable"])

        os.environ["ACTIONMESH_SECRET_KEY"] = "a-deployment-supplied-secret"
        durable_identities = IdentityStore()
        durable_app = create_app(
            identities=durable_identities,
            devices=DeviceRegistry(identities=durable_identities),
        )
        durable_app.state.devices.enroll(  # type: ignore[attr-defined]
            device_id="laptop-2", kind=DeviceKind.LAPTOP
        )
        durable = TestClient(durable_app, client=LOCAL)
        body = durable.post("/pairing/code", json={"device_id": "laptop-2"}).json()
        self.assertTrue(body["pairing_durable"])

    def test_revoke_is_loopback_only_and_burns_live_codes(self) -> None:
        self.local.post("/pairing/code", json={"device_id": "laptop-1"})
        self.assertEqual(
            self.remote.post("/pairing/revoke", json={}).status_code, 403
        )
        self.assertEqual(self.local.post("/pairing/revoke", json={}).json()["revoked"], 1)


class TestRedemption(_EnvPatch):
    def test_a_phone_pairs_from_the_other_side_of_the_room(self) -> None:
        _, body = self.pair()
        self.assertEqual(body["device_id"], "phone-1")
        self.assertEqual(body["actor_id"], "abhi")
        self.assertEqual(body["max_risk"], "medium")
        self.assertGreaterEqual(len(body["token"]), 40)
        self.assertIn("credential_id", body)
        self.assertIn("expires_at", body)

    def test_the_paired_phone_appears_in_the_device_list(self) -> None:
        self.pair()
        listed = self.local.get("/devices").json()
        by_id = {d["device_id"]: d for d in listed}
        self.assertIn("phone-1", by_id)
        self.assertEqual(by_id["phone-1"]["kind"], "phone")
        self.assertEqual(by_id["phone-1"]["capabilities"], [])

    def test_the_route_ignores_a_supplied_ceiling(self) -> None:
        # §14: a client cannot set its own risk level. Pairing has no such field, so
        # the extra key is not an elevation vector — it is nothing.
        code = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()["code"]
        response = self.remote.post(
            "/pair",
            json={
                "code": code,
                "device_id": "phone-9",
                "actor_id": "abhi",
                "max_risk": "critical",
                "permissions": ["*"],
                "grants": ["payment.execute"],
            },
        )
        # The ceiling is the one chosen at issuance (this app paired at default LOW
        # because the mint above carried no max_risk); the body's value is ignored.
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["max_risk"], "low")

    def test_a_bad_code_is_a_400_with_no_detail_leak(self) -> None:
        response = self.remote.post(
            "/pair", json={"code": "000-000", "device_id": "p", "actor_id": "a"}
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn(response.json()["detail"], ("invalid pairing code",))

    def test_malformed_input_is_rejected(self) -> None:
        for bad in ("12", "abcdef", "", None):
            with self.subTest(bad=bad):
                self.assertEqual(
                    self.remote.post(
                        "/pair",
                        json={"code": bad, "device_id": "p", "actor_id": "a"},
                    ).status_code,
                    400,
                )

    def test_a_missing_actor_is_rejected(self) -> None:
        code = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()["code"]
        response = self.remote.post("/pair", json={"code": code, "device_id": "phone-1"})
        self.assertEqual(response.status_code, 400)

    def test_replaying_the_ceremony_is_refused(self) -> None:
        code = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()["code"]
        first = self.remote.post(
            "/pair", json={"code": code, "device_id": "phone-1", "actor_id": "abhi"}
        )
        self.assertEqual(first.status_code, 200)
        replay = self.remote.post(
            "/pair", json={"code": code, "device_id": "phone-2", "actor_id": "abhi"}
        )
        self.assertEqual(replay.status_code, 400)
        self.assertIn("already used", replay.json()["detail"])


class TestTokenUse(_EnvPatch):
    GRAPH: dict[str, object] = {
        "nodes": [
            {
                "node_id": "a1",
                "action": {
                    "type": "message.send",
                    "parameters": {"message": "from the phone", "to": "mom"},
                },
            },
            {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
        ]
    }

    def _execute(
        self, token: str | None, *, actor_id: str | None = "abhi"
    ) -> dict:
        body: dict = {
            "graph": self.GRAPH,
            "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
        }
        if actor_id is not None:
            body["actor"] = {"id": actor_id}
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self.remote.post("/execute", json=body, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_a_paired_phone_can_execute_only_after_consent(self) -> None:
        token, _ = self.pair()
        # Paired, but consentless: the policy engine still denies.
        self.assertEqual(self._execute(token)["state"], "denied")

        self.local.post(
            "/consents",
            json={"actor_id": "abhi", "scope": "message.send", "max_risk": "high"},
        )
        result = self._execute(token)
        self.assertEqual(result["state"], "success", result)
        self.assertTrue(result["nodes"]["a1"]["verified"])

    def test_the_token_decides_the_actor(self) -> None:
        token, _ = self.pair()
        self.local.post(
            "/consents",
            json={"actor_id": "someone-else", "scope": "message.send", "max_risk": "high"},
        )
        # Consent for another principal cannot be reached through this credential:
        # the actor comes from the token, and the body does not get a vote.
        self.assertEqual(self._execute(token, actor_id=None)["state"], "denied")

    def test_a_body_actor_that_disagrees_is_rejected(self) -> None:
        token, _ = self.pair()
        headers = {"Authorization": f"Bearer {token}"}
        response = self.remote.post(
            "/execute",
            json={
                "graph": self.GRAPH,
                "actor": {"id": "root"},
                "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not match the paired device", response.json()["detail"])

    def test_a_forbidden_authorization_scheme_is_refused(self) -> None:
        for header in (
            {"Authorization": "Basic dXNlcjpwYXNz"},
            {"Authorization": "Bearer"},
            {"Authorization": "Bearer   "},
        ):
            with self.subTest(header=header):
                response = self.remote.post(
                    "/execute", json={"graph": self.GRAPH}, headers=header
                )
                self.assertEqual(response.status_code, 401)

    def test_an_unknown_token_gets_one_generic_answer(self) -> None:
        response = self.remote.post(
            "/execute",
            json={"graph": self.GRAPH},
            headers={"Authorization": "Bearer nope-not-a-token"},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "unauthorized")

    def test_revoking_the_device_kills_the_token(self) -> None:
        token, _ = self.pair()
        self.local.post(
            "/consents",
            json={"actor_id": "abhi", "scope": "message.send", "max_risk": "high"},
        )
        self.assertEqual(self._execute(token)["state"], "success")
        self.local.post("/devices/phone-1/revoke", json={"reason": "stolen"})
        self.assertEqual(
            self.remote.post(
                "/execute",
                json={"graph": self.GRAPH},
                headers={"Authorization": f"Bearer {token}"},
            ).status_code,
            401,
        )

    def test_an_expired_token_is_refused(self) -> None:
        credentials: CredentialStore = self.app.state.credentials  # type: ignore[attr-defined]
        token, body = self.pair()
        credential = credentials.get(body["credential_id"])
        assert credential is not None
        # Backdate the stored record rather than re-issuing: expiry is a property of
        # what the server holds, not of what the phone was handed.
        credential.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        response = self.remote.post(
            "/execute",
            json={"graph": self.GRAPH},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 401)


class TestTokenAlongsideApiKey(unittest.TestCase):
    """A device token says *which* device. It does not say the port is public."""

    def setUp(self) -> None:
        self.addCleanup(os.environ.pop, "ACTIONMESH_API_KEY", None)
        os.environ["ACTIONMESH_API_KEY"] = "shared-edge-key"
        identities = IdentityStore()
        self.app = create_app(
            identities=identities, devices=DeviceRegistry(identities=identities)
        )
        self.local = TestClient(
            self.app, client=LOCAL, headers={"x-api-key": "shared-edge-key"}
        )
        self.remote = TestClient(
            self.app, client=REMOTE, headers={"x-api-key": "shared-edge-key"}
        )
        self.app.state.devices.enroll(  # type: ignore[attr-defined]
            device_id="laptop-1", kind=DeviceKind.LAPTOP
        )
        code = self.local.post("/pairing/code", json={"device_id": "laptop-1"}).json()["code"]
        paired = self.remote.post(
            "/pair", json={"code": code, "device_id": "phone-1", "actor_id": "abhi"}
        )
        self.assertEqual(paired.status_code, 200, paired.text)
        self.token = paired.json()["token"]

    def test_a_token_alone_is_not_enough_without_the_key(self) -> None:
        no_key = TestClient(self.app, client=REMOTE)
        response = no_key.get(
            "/devices", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 401)

    def test_the_api_key_alone_still_works(self) -> None:
        self.assertEqual(self.remote.get("/devices").status_code, 200)

    def test_both_together_work(self) -> None:
        response = self.remote.get(
            "/devices/phone-1", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["device_id"], "phone-1")

    def test_health_stays_open(self) -> None:
        self.assertEqual(
            TestClient(self.app, client=REMOTE).get("/health").status_code, 200
        )


if __name__ == "__main__":
    unittest.main()
