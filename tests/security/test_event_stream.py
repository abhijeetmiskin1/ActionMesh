"""C19d: the realtime stream is observational and device-scoped.

A stream is the one surface a phone reads continuously, so it is the one place where
"hard to guess" is not good enough -- watching someone else's execution has to be
refused by the server, not merely unlikely. These drive the real HTTP/WS endpoints.
"""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from actionmesh.api import create_app
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.protocol.models import DeviceKind, MeshEvent

LOCAL = ("127.0.0.1", 45123)
REMOTE = ("192.168.1.50", 55555)


class _Fixture(unittest.TestCase):
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

    def pair(self, actor: str = "abhi", phone: str = "phone-1") -> str:
        grant = self.local.post(
            "/consents",
            json={
                "actor_id": actor,
                "scope": "device.control",
                "max_risk": "critical",
                "reason": "fixture consent before pairing",
            },
        )
        self.assertEqual(grant.status_code, 200, grant.text)
        code = self.local.post(
            "/pairing/code", json={"device_id": "laptop-1", "max_risk": "high"}
        ).json()["code"]
        response = self.remote.post(
            "/pair", json={"code": code, "device_id": phone, "actor_id": actor}
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["token"]

    def hub(self):
        return self.app.state.engine.events

    def first_frame(self, *, client=None, headers=None, params=None):
        with (client or self.remote).websocket_connect(
            "/ws/events", headers=headers or {}, params=params or {}
        ) as websocket:
            return websocket.receive_json()


class TestStreamTicket(_Fixture):
    def test_a_ticket_needs_a_device_credential(self) -> None:
        self.assertEqual(self.remote.get("/events/stream-ticket").status_code, 401)

    def test_a_credential_exchanges_for_an_actor_bound_ticket(self) -> None:
        token = self.pair(actor="abhi")
        response = self.remote.get(
            "/events/stream-ticket", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["actor"], "abhi")
        self.assertGreaterEqual(len(body["ticket"]), 20)
        self.assertGreater(body["expires_in_s"], 0)

    def test_the_events_endpoint_rejects_a_bogus_ticket_before_streaming(self) -> None:
        # The 401 must be raised *before* the streaming body, so a caller learns the
        # ticket was bad instead of getting an empty stream that looks like silence.
        response = self.local.get("/events", params={"ticket": "not-a-real-ticket"})
        self.assertEqual(response.status_code, 401)

    def test_a_revoked_device_cannot_mint_a_ticket(self) -> None:
        token = self.pair(actor="abhi", phone="phone-1")
        paired = self.local.get("/devices").json()
        phone_id = next(d["device_id"] for d in paired if d["kind"] == "phone")
        self.local.post(f"/devices/{phone_id}/revoke", json={"reason": "lost"})
        response = self.remote.get(
            "/events/stream-ticket", headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(response.status_code, 401)


class TestWebsocketScoping(_Fixture):
    def test_a_device_sees_only_its_own_actor_events(self) -> None:
        hub = self.hub()
        hub.publish(MeshEvent(event_type="theirs", execution_id="e-m", actor="mallory"))
        token = self.pair(actor="abhi")
        hub.publish(MeshEvent(event_type="mine", execution_id="e-a", actor="abhi"))
        frame = self.first_frame(headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(frame["event_type"], "mine")
        self.assertEqual(frame["actor"], "abhi")

    def test_an_invalid_bearer_does_not_get_a_stream_when_a_key_gates_the_port(self) -> None:
        with patch.dict(os.environ, {"ACTIONMESH_API_KEY": "shared"}):
            app = create_app()
            client = TestClient(app, client=REMOTE)
            with self.assertRaises(WebSocketDisconnect), client.websocket_connect(
                "/ws/events", headers={"Authorization": "Bearer nope-not-a-token"}
            ):
                pass

    def test_a_no_credential_local_view_still_sees_events(self) -> None:
        # The trusted operator view is loopback-only; a remote anonymous caller is
        # denied (see TestStreamTicket / the WS boundary).
        hub = self.hub()
        hub.publish(MeshEvent(event_type="any", execution_id="e-x", actor="someone"))
        frame = self.first_frame(client=self.local)
        self.assertEqual(frame["event_type"], "any")


class TestEventSizeGuard(_Fixture):
    def test_an_oversized_payload_is_replaced_not_streamed_whole(self) -> None:
        hub = self.hub()
        hub.publish(
            MeshEvent(
                event_type="big",
                execution_id="e-big",
                actor="abhi",
                payload={"blob": "x" * 40000},
            )
        )
        frame = self.first_frame(client=self.local)
        self.assertEqual(frame["event_type"], "big")
        self.assertTrue(frame["payload"].get("truncated"))
        self.assertNotIn("blob", frame["payload"])


class TestStreamDenial(_Fixture):
    def test_a_remote_anonymous_http_stream_is_denied(self) -> None:
        # The §5 property, on the HTTP path: a phone whose token was rejected and
        # cleared must NOT fall through to the unscoped operator view. Off-loopback
        # with no credential, the stream is refused outright.
        response = self.remote.get("/events")
        self.assertEqual(response.status_code, 401)

    def test_an_invalid_ticket_is_denied_even_over_loopback(self) -> None:
        # Being on the machine does not rescue a bad ticket; it is not a bypass.
        response = self.local.get("/events", params={"ticket": "bogus"})
        self.assertEqual(response.status_code, 401)


class TestApprovalEventCarriesNoAuthority(_Fixture):
    def _ask_execution(self) -> str:
        self.local.post(
            "/consents",
            json={"actor_id": "operator", "scope": "message.send", "max_risk": "high"},
        )
        response = self.local.post(
            "/execute",
            json={
                "actor": {"id": "operator"},
                "graph": {
                    "nodes": [
                        {
                            "node_id": "n1",
                            "action": {"type": "message.send", "parameters": {"to": "mom", "message": "x"}},
                        },
                        {"node_id": "end", "node_type": "end", "depends_on": ["n1"]},
                    ]
                },
                "policy": {"rules": [{"action": "message.send", "decision": "ask"}]},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["execution_id"]

    def test_approval_required_names_the_request_without_granting_it(self) -> None:
        self._ask_execution()
        events = [e for e in self.hub().history() if e.event_type == "approval.required"]
        self.assertEqual(len(events), 1)
        payload = events[0].payload
        self.assertEqual(
            set(payload), {"node_id", "action_type", "risk_level", "reasons"}
        )
        for forbidden in ("authorized", "grant", "grants", "permission", "decision"):
            self.assertNotIn(forbidden, payload)

    def test_injected_authority_fields_in_an_approval_are_inert(self) -> None:
        execution_id = self._ask_execution()
        waiting = self.local.get("/approvals").json()
        action_id = waiting[0]["actions"][0]["action_id"]
        # A client tries to authorize itself by smuggling fields into the approval.
        response = self.local.post(
            "/approvals",
            json={
                "execution_id": execution_id,
                "action_id": action_id,
                "status": "approved",
                "authorized": True,
                "role": "admin",
                "grants": ["everything"],
                "decision": "allow",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        # The only thing that mattered was status=approved on the real action id;
        # the injected fields neither appear nor change the outcome beyond that.
        body = response.json()
        self.assertEqual(body["status"], "approved")
        self.assertNotIn("role", body)
        self.assertNotIn("grants", body)


if __name__ == "__main__":
    unittest.main()
