"""C14 transport hardening tests."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from actionmesh.api import create_app
from actionmesh.protocol.models import MeshEvent


class TestApiKeyProtection(unittest.TestCase):
    def test_configured_api_key_protects_non_health_routes(self) -> None:
        with patch.dict(os.environ, {"ACTIONMESH_API_KEY": "secret"}):
            client = TestClient(create_app())
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/capabilities").status_code, 401)
            self.assertEqual(
                client.get("/capabilities", headers={"x-api-key": "secret"}).status_code,
                200,
            )

    def test_security_headers_are_present(self) -> None:
        response = TestClient(create_app()).get("/health")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["x-frame-options"], "DENY")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    def test_oversized_request_is_rejected_before_routing(self) -> None:
        with patch.dict(os.environ, {"ACTIONMESH_MAX_REQUEST_BYTES": "1024"}):
            client = TestClient(create_app())
            response = client.get("/health", headers={"content-length": "1025"})
        self.assertEqual(response.status_code, 413)

    def test_websocket_api_key_uses_constant_time_comparison(self) -> None:
        with (
            patch.dict(os.environ, {"ACTIONMESH_API_KEY": "secret"}),
            patch("actionmesh.api.hmac.compare_digest", return_value=False) as compare,
        ):
            client = TestClient(create_app())
            with self.assertRaises(WebSocketDisconnect), client.websocket_connect(
                "/ws/events", headers={"x-api-key": "wrong"}
            ):
                pass
        compare.assert_called_once_with("wrong", "secret")

    def test_authorized_websocket_receives_live_event(self) -> None:
        # The unscoped operator view is a loopback-only trust; a remote anonymous
        # caller is denied (see test below). This is the laptop's own dashboard.
        client = TestClient(create_app(), client=("127.0.0.1", 50000))
        with client.websocket_connect("/ws/events") as websocket:
            event = MeshEvent(event_type="live", execution_id="exec-live")
            client.app.state.engine.events.publish(event)
            received = websocket.receive_json()
        self.assertEqual(received["event_type"], "live")
        self.assertEqual(received["execution_id"], "exec-live")

    def test_a_remote_anonymous_websocket_is_denied(self) -> None:
        # Losing/never-having a credential must not yield the operator view from off
        # the machine. TestClient's default peer is non-loopback.
        client = TestClient(create_app())
        with self.assertRaises(WebSocketDisconnect), client.websocket_connect("/ws/events"):
            pass

    def test_websocket_replays_and_filters_history(self) -> None:
        client = TestClient(create_app(), client=("127.0.0.1", 50000))
        client.app.state.engine.events.publish(
            MeshEvent(event_type="target", execution_id="exec-target")
        )
        client.app.state.engine.events.publish(
            MeshEvent(event_type="other", execution_id="exec-other")
        )
        with client.websocket_connect(
            "/ws/events", params={"execution_id": "exec-target"}
        ) as websocket:
            received = websocket.receive_json()
        self.assertEqual(received["event_type"], "target")
        self.assertEqual(received["execution_id"], "exec-target")



if __name__ == "__main__":
    unittest.main()