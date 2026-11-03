"""C41 — `/voiceos/status` tells the truth about what will answer the next turn.

The route exists because "a provider object was constructed" and "the next turn will
reach a model" are different facts, and the UI must not confuse them. These tests pin
the three cases the distinction is for:

* nothing configured -> deterministic, and no reason to explain (there is nothing to
  apologise for);
* configured but the shell has no usable provider -> deterministic **with a reason**,
  because the user asked for a model and is not getting one;
* the shell confirms a usable provider -> `ai_model`.

The last case is still only a claim about configuration. A model that goes on to fail
per request says so in the turn's own notes; that path is covered by
``tests/unit/test_llm_generation.py`` and the integration suite.
"""

from __future__ import annotations

import os
import unittest
from typing import Any
from unittest import mock

from fastapi.testclient import TestClient

from actionmesh.api import create_app
from actionmesh.voiceos.model_bridge import BridgeUnavailable

LOCAL = ("127.0.0.1", 45123)

_ENV = (
    "ACTIONMESH_API_KEY",
    "ACTIONMESH_VOICEOS_LLM_PROVIDER",
    "ACTIONMESH_VOICEOS_LLM_MODEL",
    "ACTIONMESH_VOICEOS_LLM_API_KEY",
    "ACTIONMESH_PROVIDER_BRIDGE_SOCKET",
    "ACTIONMESH_PROVIDER_BRIDGE_TOKEN",
)

class VoiceOsStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        for name in _ENV:
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)

    def _app(self, **env: str) -> Any:
        os.environ.update(env)
        app = create_app()
        self.addCleanup(lambda: _close_voiceos(app))
        return app

    def _status(self, app: Any) -> dict[str, Any]:
        response = TestClient(app, client=LOCAL).get("/voiceos/status")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_no_provider_configured_is_deterministic_without_a_reason(self) -> None:
        body = self._status(self._app())
        self.assertEqual(body["mode"], "deterministic")
        self.assertEqual(body["mode_label"], "Deterministic fallback")
        # Nothing was asked for, so there is nothing to explain. A non-empty reason here
        # would train the user to ignore the field.
        self.assertEqual(body["reason"], "")

    def test_desktop_without_a_bridge_is_deterministic_and_says_why(self) -> None:
        body = self._status(self._app(ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop"))
        self.assertEqual(body["mode"], "deterministic")
        self.assertIn("bridge", body["reason"])
        # The reason names the environment variables an operator would set, and it goes
        # through the route untouched.
        self.assertIn("ACTIONMESH_PROVIDER_BRIDGE_SOCKET", body["reason"])

    def test_desktop_with_a_bridge_but_no_provider_is_still_deterministic(self) -> None:
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        reply = {
            "ok": True,
            "configured": False,
            "provider": "",
            "model": "",
            "implemented": False,
            "hasKey": False,
            "state": "not_configured",
        }
        with mock.patch("actionmesh.api.model_bridge.status", return_value=reply):
            body = self._status(app)
        self.assertEqual(body["mode"], "deterministic")
        self.assertIn("no usable provider", body["reason"])

    def test_desktop_with_a_usable_provider_is_ai_model(self) -> None:
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        reply = {
            "ok": True,
            "configured": True,
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "implemented": True,
            "hasKey": True,
            "state": "connected",
        }
        with mock.patch("actionmesh.api.model_bridge.status", return_value=reply):
            body = self._status(app)
        self.assertEqual(body["mode"], "ai_model")
        self.assertEqual(body["mode_label"], "AI model active")
        # The named provider and model come from the shell, which is the only process
        # that knows what the user actually chose.
        self.assertEqual(body["provider"], "anthropic")
        self.assertEqual(body["model"], "claude-sonnet-5-5")
        self.assertEqual(body["reason"], "")

    def test_a_provider_state_that_refuses_is_not_reported_as_active(self) -> None:
        # The live defect this pins: with an Anthropic key the provider rejected, the
        # shell answered `configured: true, implemented: true, state:
        # invalid_credentials`, this route said "AI model active", and the very next
        # turn's notes said the model did not answer and the deterministic planner
        # had answered instead. `configured` is not `usable`; `state` is the field
        # that knows the difference.
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        reply = {
            "ok": True,
            "configured": True,
            "provider": "anthropic",
            "model": "claude-haiku-4-5-20251001",
            "implemented": True,
            "hasKey": True,
            "state": "invalid_credentials",
            "detail": "The provider rejected the stored API key.",
        }
        with mock.patch("actionmesh.api.model_bridge.status", return_value=reply):
            body = self._status(app)
        self.assertEqual(body["mode"], "deterministic")
        self.assertEqual(body["mode_label"], "Deterministic fallback")
        # The reason says what the shell observed, so the user knows to replace the
        # key rather than to wonder why the chip disagrees with the last turn.
        self.assertIn("rejected the stored API key", body["reason"])
        self.assertEqual(body["state"], "invalid_credentials")

    def test_every_refusing_state_falls_back(self) -> None:
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        for state in (
            "missing_key",
            "invalid_credentials",
            "rate_limited",
            "timeout",
            "provider_unavailable",
            "malformed",
        ):
            with self.subTest(state=state):
                reply = {
                    "ok": True,
                    "configured": True,
                    "provider": "anthropic",
                    "model": "claude-sonnet-5-5",
                    "implemented": True,
                    "hasKey": True,
                    "state": state,
                }
                with mock.patch("actionmesh.api.model_bridge.status", return_value=reply):
                    body = self._status(app)
                self.assertEqual(body["mode"], "deterministic", state)
                self.assertTrue(body["reason"], state)

    def test_an_unverified_provider_is_active_but_says_it_is_unverified(self) -> None:
        # `not_configured` with a provider chosen means "configured, keyed, never
        # tested": the next turn really will call the model, so the mode is honest —
        # but the chip must not imply the connection has been proven. The caveat goes
        # in `reason`, which the UI renders as the chip's tooltip.
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        reply = {
            "ok": True,
            "configured": True,
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "implemented": True,
            "hasKey": True,
            "state": "not_configured",
        }
        with mock.patch("actionmesh.api.model_bridge.status", return_value=reply):
            body = self._status(app)
        self.assertEqual(body["mode"], "ai_model")
        self.assertIn("not been verified", body["reason"])

    def test_a_bridge_that_cannot_answer_is_deterministic_not_optimistic(self) -> None:
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN="token-not-a-key",
        )
        with mock.patch(
            "actionmesh.api.model_bridge.status",
            side_effect=BridgeUnavailable("the bridge socket could not be reached"),
        ):
            body = self._status(app)
        self.assertEqual(body["mode"], "deterministic")
        self.assertIn("could not be reached", body["reason"])

    def test_the_status_route_never_names_the_bridge_token(self) -> None:
        token = "token-canary-8c1d"
        app = self._app(
            ACTIONMESH_VOICEOS_LLM_PROVIDER="desktop",
            ACTIONMESH_PROVIDER_BRIDGE_SOCKET="/tmp/provider-bridge.sock",
            ACTIONMESH_PROVIDER_BRIDGE_TOKEN=token,
        )
        with mock.patch(
            "actionmesh.api.model_bridge.status",
            side_effect=BridgeUnavailable("the bridge socket could not be reached"),
        ):
            body = self._status(app)
        self.assertNotIn(token, repr(body))

def _close_voiceos(app: Any) -> None:
    store = getattr(app.state, "voiceos_store_instance", None)
    if store is not None:
        store.close()