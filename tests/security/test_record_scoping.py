"""C24/C25: the read surface and the decision surface are as scoped as the stream.

P10 pinned the *event* channel to the authenticated actor. These tests pin the
rest of the surface to the same rule, because a phone that cannot watch another
device's execution but can fetch its record — or read its pending approval, or
cancel it, or **decide it** — has been separated by a curtain rather than a wall.
The decision half matters most: approving is the human-in-the-loop, so who may do
it is the entire guarantee.
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


class _TwoDevices(unittest.TestCase):
    """Two paired phones with one execution each, plus the machine's own operator."""

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
        self.app = create_app(identities=self.identities, devices=self.devices)
        self.local = TestClient(self.app, client=LOCAL)
        self.remote = TestClient(self.app, client=REMOTE)
        self.devices.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="Laptop",
            platform="linux",
            capabilities=["message.send"],
        )
        self.token_a = self.pair(phone="phone-a", actor="alice")
        self.token_b = self.pair(phone="phone-b", actor="bob")
        self.id_a = self.run_execution(self.token_a, "alice", "hello from alice")
        self.id_b = self.run_execution(self.token_b, "bob", "hello from bob")

    def pair(self, *, phone: str, actor: str) -> str:
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": "high"}
        ).json()["code"]
        response = self.remote.post(
            "/pair", json={"code": code, "device_id": phone, "actor_id": actor}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def bearer(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def run_execution(self, token: str, actor: str, message: str) -> str:
        self.local.post(
            "/consents",
            json={"actor_id": actor, "scope": "message.send", "max_risk": "high"},
        )
        response = self.remote.post(
            "/execute",
            headers=self.bearer(token),
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "a1",
                            "action": {
                                "type": "message.send",
                                "parameters": {"message": message},
                            },
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                    ]
                },
                "policy": {"rules": [{"action": "message.send", "decision": "allow"}]},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["execution_id"]


class TestRecordScoping(_TwoDevices):
    def test_a_device_sees_only_its_own_executions(self) -> None:
        listed = self.remote.get("/executions", headers=self.bearer(self.token_a)).json()
        ids = [record["execution_id"] for record in listed]
        self.assertIn(self.id_a, ids)
        self.assertNotIn(self.id_b, ids)

    def test_another_device_s_execution_is_not_readable(self) -> None:
        # 404, not 403: confirming that the id exists is already a disclosure.
        response = self.remote.get(
            f"/executions/{self.id_b}", headers=self.bearer(self.token_a)
        )
        self.assertEqual(response.status_code, 404)

    def test_the_machine_operator_still_sees_everything(self) -> None:
        # The loopback operator is the trusted local view; scoping must not narrow it.
        ids = [record["execution_id"] for record in self.local.get("/executions").json()]
        self.assertIn(self.id_a, ids)
        self.assertIn(self.id_b, ids)
        self.assertEqual(self.local.get(f"/executions/{self.id_b}").status_code, 200)

    def test_a_remote_caller_with_no_credential_is_denied(self) -> None:
        for path in ("/executions", f"/executions/{self.id_a}", "/audit", "/approvals"):
            with self.subTest(path=path):
                self.assertEqual(self.remote.get(path).status_code, 401)


class TestCancelAuthorization(_TwoDevices):
    def test_a_device_cannot_cancel_another_device_s_execution(self) -> None:
        response = self.remote.post(
            f"/executions/{self.id_a}/cancel", headers=self.bearer(self.token_b)
        )
        self.assertEqual(response.status_code, 404)
        record = self.local.get(f"/executions/{self.id_a}").json()
        self.assertNotEqual(record.get("state"), "cancelled")

    def test_cancel_reports_a_request_rather_than_a_result(self) -> None:
        # A cancel is a request, never a result: answering "cancelled" before the owner
        # has stopped is the executor grading its own homework. C35 added the other half
        # of the same honesty -- `id_b` is already finished, so the route reports that the
        # run is over instead of pretending the press did something, and the record keeps
        # the state the executor actually witnessed.
        response = self.remote.post(
            f"/executions/{self.id_b}/cancel", headers=self.bearer(self.token_b)
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "terminal")
        self.assertNotEqual(response.json()["status"], "cancelled")
        record = self.local.get(f"/executions/{self.id_b}").json()
        self.assertNotEqual(record["state"], "cancelled")

    def test_a_cancel_writes_the_flag_the_owner_acts_on(self) -> None:
        # Not "this request set a local variable": the durable record is what any other
        # process reads, which is how a phone's Cancel reaches a run it did not start.
        parked = self.remote.post(
            "/execute",
            headers=self.bearer(self.token_a),
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "n",
                            "action": {
                                "type": "message.send",
                                "parameters": {"message": "hold the parcel"},
                            },
                        },
                        {"node_id": "e", "node_type": "end", "depends_on": ["n"]},
                    ]
                },
                "policy": {"rules": [{"action": "message.send", "decision": "ask"}]},
            },
        ).json()
        self.assertEqual(parked["state"], "waiting_permission")
        answer = self.remote.post(
            f"/executions/{parked['execution_id']}/cancel",
            headers=self.bearer(self.token_a),
        )
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["status"], "requested")
        stored = self.local.get(f"/executions/{parked['execution_id']}").json()
        self.assertTrue(stored["cancel_requested"])
        second = self.remote.post(
            f"/executions/{parked['execution_id']}/cancel",
            headers=self.bearer(self.token_a),
        )
        self.assertEqual(second.json()["status"], "already_requested")


class TestApprovalAndAuditScoping(_TwoDevices):
    def test_a_pending_approval_is_invisible_to_another_actor(self) -> None:
        parked = self.remote.post(
            "/execute",
            headers=self.bearer(self.token_a),
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "a1",
                            "action": {
                                "type": "message.send",
                                "parameters": {"message": "secret draft to carol"},
                            },
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                    ]
                },
                "policy": {"rules": [{"action": "message.send", "decision": "ask"}]},
            },
        ).json()
        self.assertEqual(parked["state"], "waiting_permission")
        body = self.remote.get("/approvals", headers=self.bearer(self.token_b)).text
        self.assertNotIn("a1", body)
        self.assertNotIn("secret draft to carol", body)
        mine = self.remote.get("/approvals", headers=self.bearer(self.token_a)).json()
        self.assertEqual([entry["actor"] for entry in mine], ["alice"])

    def test_the_audit_trail_is_scoped_to_its_reader(self) -> None:
        audit = self.remote.get("/audit", headers=self.bearer(self.token_a)).json()
        actors = {record.get("actor") for record in audit["records"]}
        self.assertNotIn("bob", actors)
        self.assertTrue(actors)
        self.assertIn("chain", audit)


class TestWriteScoping(_TwoDevices):
    """The decision side. Reading another actor's work is a leak; deciding on it is a
    takeover -- the human-in-the-loop invariant is only worth as much as the server's
    willingness to check whose human is being asked."""

    def parked(self) -> dict[str, str]:
        body = self.remote.post(
            "/execute",
            headers=self.bearer(self.token_a),
            json={
                "graph": {
                    "nodes": [
                        {
                            "node_id": "a1",
                            "action": {
                                "type": "message.send",
                                "parameters": {"message": "alice's own draft"},
                            },
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
                    ]
                },
                "policy": {"rules": [{"action": "message.send", "decision": "ask"}]},
            },
        ).json()
        self.assertEqual(body["state"], "waiting_permission")
        entry = next(
            item
            for item in self.remote.get(
                "/approvals", headers=self.bearer(self.token_a)
            ).json()
            if item["execution_id"] == body["execution_id"]
        )
        return {
            "execution_id": body["execution_id"],
            "action_id": entry["actions"][0]["action_id"],
        }

    def test_a_device_cannot_decide_another_actor_s_action(self) -> None:
        parked = self.parked()
        response = self.remote.post(
            "/approvals",
            headers=self.bearer(self.token_b),
            json={
                "action_id": parked["action_id"],
                "status": "approved",
                "execution_id": parked["execution_id"],
            },
        )
        self.assertEqual(response.status_code, 403, response.text)
        waiting = self.remote.get("/approvals", headers=self.bearer(self.token_a)).json()
        self.assertEqual([entry["execution_id"] for entry in waiting], [parked["execution_id"]])

    def test_the_owner_can_still_decide_its_own_action(self) -> None:
        parked = self.parked()
        response = self.remote.post(
            "/approvals",
            headers=self.bearer(self.token_a),
            json={
                "action_id": parked["action_id"],
                "status": "approved",
                "execution_id": parked["execution_id"],
            },
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_the_machine_operator_can_decide_any_action(self) -> None:
        parked = self.parked()
        response = self.local.post(
            "/approvals",
            json={"action_id": parked["action_id"], "status": "approved"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_device_cannot_grant_consent_to_someone_else(self) -> None:
        response = self.remote.post(
            "/consents",
            headers=self.bearer(self.token_b),
            json={"actor_id": "alice", "scope": "system.shell", "max_risk": "critical"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        mine = self.remote.get(
            "/consents?actor_id=alice", headers=self.bearer(self.token_a)
        ).json()
        self.assertNotIn("system.shell", [record["scope"] for record in mine])

    def test_a_device_cannot_read_another_actor_s_consents(self) -> None:
        response = self.remote.get(
            "/consents?actor_id=alice", headers=self.bearer(self.token_b)
        )
        self.assertEqual(response.status_code, 403, response.text)
        own = self.remote.get(
            "/consents?actor_id=bob", headers=self.bearer(self.token_b)
        )
        self.assertEqual(own.status_code, 200, own.text)

    def test_a_device_cannot_revoke_another_device(self) -> None:
        response = self.remote.post(
            "/devices/phone-a/revoke", headers=self.bearer(self.token_b)
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertIn(
            "phone-a",
            [device["device_id"] for device in self.local.get("/devices").json()],
        )

    def test_a_device_can_revoke_itself(self) -> None:
        response = self.remote.post(
            "/devices/phone-b/revoke", headers=self.bearer(self.token_b)
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_a_device_may_ask_for_its_own_consents_without_naming_itself(self) -> None:
        # The server already knows whose credential this is, and the dashboard needs to
        # read its own list on every refresh. Requiring the caller to name itself -- and
        # then refusing the answer when the name is wrong -- is a trap, not a check.
        mine = self.remote.get("/consents", headers=self.bearer(self.token_b))
        self.assertEqual(mine.status_code, 200, mine.text)
        self.assertEqual(
            sorted(record["scope"] for record in mine.json()), ["message.send"]
        )

    def test_the_operator_must_still_name_the_actor_it_means(self) -> None:
        self.assertEqual(self.local.get("/consents").status_code, 400)

    def test_a_device_may_record_its_own_consent_without_naming_itself(self) -> None:
        response = self.remote.post(
            "/consents",
            headers=self.bearer(self.token_b),
            json={"scope": "file.read", "max_risk": "high"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["granted_by"], "bob")


if __name__ == "__main__":
    unittest.main()
