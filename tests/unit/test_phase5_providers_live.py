"""Phase 5 - P1-1 live vendor adapter: transport decision, adversarially probed.

Label: MOCKED-VERIFIED / BLOCKED. No live vendor call is claimed.

TRANSPORT DECISION (evidence, checked 2026-11-05 in this process)
------------------------------------------------------------------
* ``tests/security/test_dangerous_primitives.py`` is the guard:
  ``NETWORK_ALLOWED == frozenset()``, ``MEMBER_ALLOWLIST`` grants exactly
  one outbound member - ``asyncio.open_connection`` in
  ``actionmesh/voiceos/transport.py`` - plus AF_UNIX bridge members.
  No other module may import a network stack (``socket``,
  ``urllib.request``, ``http.client``, ``httpx`` ...), and that file
  asserts ``OFFICIAL_ENDPOINTS == {"https://api.anthropic.com/v1/
  messages"}`` exactly.
* ``actionmesh/voiceos/transport.OFFICIAL_ENDPOINTS`` is that same
  one-entry closed frozenset. ``checked_endpoint`` refuses anything
  outside it *before* any dial. A user-supplied OpenAI-compatible
  ``base_url`` can therefore never be dialled through the only
  approved transport, and there is no second approved transport to
  dial it through.
* ``actionmesh/voiceos/llm.AVAILABLE_PROVIDERS == {"anthropic",
  "desktop"}`` deliberately excludes ``openai`` / ``gemini`` /
  ``openai_compatible``: those have no direct Python transport and
  are reached, when at all, over the C41 desktop bridge
  (``provider == "desktop"``, key held by Electron main, never by
  this process). Adding a name here would claim a direct API the
  closed transport does not carry.
* Credential presence in this process (booleans only, no values
  read or printed): ``ACTIONMESH_VOICEOS_LLM_API_KEY`` absent,
  ``ANTHROPIC_API_KEY`` absent, ``ACTIONMESH_OPENAI_COMPATIBLE_API_KEY``
  absent, ``ACTIONMESH_VOICEOS_LLM_PROVIDER`` absent. So even the
  Anthropic path - the one approved endpoint - has no credential
  configured here, and ``build_provider(provider="anthropic")``
  honestly returns ``(None, "no credential is present ...")``.
  ``/voiceos/status`` (``mode`` / ``provider`` / ``provider_state``
  fields) reports ``deterministic`` in that state.

Conclusion: there is NO legitimately approved + credentialed live
path for an OpenAI-compatible base URL in this codebase. Inventing
one would mean importing ``urllib.request`` (guard failure),
widening ``OFFICIAL_ENDPOINTS`` without a security review, or
reaching for the desktop bridge's key - all forbidden. The adapter
therefore keeps its honest refusal: ``execute()`` raises
``ProviderError`` (``PROVIDER`` when a credential is present,
``AUTHENTICATION`` when it is not) and never calls any opener,
never fabricates a ``ProviderResult``.

WHAT APPROVAL WOULD BE NEEDED TO GO LIVE (design note)
------------------------------------------------------
Going live is a security-review decision, not a coding shortcut.
All of the following, minimally, in that order:

1. Transport allowlist entry. ``tests/security/
   test_dangerous_primitives.py``: add the exact member grant the
   new call needs (the file, member by member - never a widened
   ``NETWORK_ALLOWED``, which would also exempt that file from the
   pickle/socket/subprocess import bans - and update the exact-dict
   assertion in
   ``test_the_execution_exemption_is_exactly_one_function`` so the
   widening is a loud, reviewed diff line).
2. ``OFFICIAL_ENDPOINTS`` addition in
   ``actionmesh/voiceos/transport.py``. A *closed* frozenset entry
   naming the exact vendor endpoint (https, port 443, no userinfo,
   no query/fragment, no redirects - the transport already refuses
   all of those). If the endpoint is user-supplied, the review must
   say why a caller-controlled host is safe against SSRF, or the
   entry must be a fixed official host only. Update
   ``test_the_model_transport_dials_only_approved_endpoints`` to
   pin the new closed set.
3. Credential lane. A named environment variable read only at
   invocation via ``IntegrationConfig.credential()`` (already the
   shape here: ``ACTIONMESH_OPENAI_COMPATIBLE_API_KEY``), never
   logged, never echoed in an error, never written to a file.
   ``AVAILABLE_PROVIDERS`` may gain ``"openai_compatible"`` only
   together with a real adapter branch in ``build_provider`` -
   the trailing ``LlmError("... listed but not implemented")``
   exists to catch exactly that mismatch.
4. Bounds and adversarial proof, mirroring what this file proves
   for the refusal: 64 KiB request / 256 KiB response caps, 12 s
   timeout, closed-vocabulary errors that cannot quote the request
   or the key, no secret in logs/events/audit, refusal without a
   credential. Only then may a real request returning a real
   response be labelled LIVE.

Until 1-4 land, ``execute()`` refusing is not a stub - it is the
integrated behaviour, and any success it returned would be a lie.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import pathlib
import unittest
from typing import Any
from unittest import mock

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError
from actionmesh.providers.openai_compatible import (
    ENV_CREDENTIAL,
    OpenAICompatibleProvider,
    compatible_endpoint,
)
from actionmesh.providers.real_base import IntegrationConfig
from actionmesh.voiceos import transport as transport_module
from actionmesh.voiceos.llm import (
    AVAILABLE_PROVIDERS,
    LlmConfig,
    build_provider,
)

CANARY = "sk-phase5-canary-SECRET-VALUE"


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, budget: int) -> bytes:
        return self._body[:budget]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


def _provider(opener: Any = None) -> OpenAICompatibleProvider:
    config = IntegrationConfig(
        provider_name="openai_compatible",
        base_url="https://api.example.test/v1",
        credential_env=ENV_CREDENTIAL,
    )
    return OpenAICompatibleProvider(config, opener=opener)


def _action() -> Action:
    return Action(
        type="chat.generate",
        provider="openai_compatible",
        parameters={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
    )


class TestNoApprovedTransportExists(unittest.TestCase):
    def test_official_endpoints_is_the_one_anthropic_url(self) -> None:
        self.assertEqual(
            transport_module.OFFICIAL_ENDPOINTS,
            frozenset({"https://api.anthropic.com/v1/messages"}),
        )

    def test_compatible_endpoint_is_not_approved_and_transport_refuses_it(
        self,
    ) -> None:
        endpoint = compatible_endpoint("https://api.example.test/v1")
        self.assertNotIn(endpoint, transport_module.OFFICIAL_ENDPOINTS)
        with self.assertRaises(transport_module.TransportError):
            transport_module.checked_endpoint(endpoint)

    def test_available_providers_unchanged_no_openai_compatible(self) -> None:
        # The provider-list fix this task allowed is NOT needed: the
        # exclusion of openai_compatible is the honest fact, pinned here
        # so a drive-by addition fails loudly.
        self.assertEqual(AVAILABLE_PROVIDERS, frozenset({"anthropic", "desktop"}))

    def test_anthropic_without_credential_returns_no_provider(self) -> None:
        # Proves the one approved endpoint is not usable live here:
        # no credential record exists, so the builder declines rather
        # than dialling. Only field-level facts, no secret material.
        provider, note = build_provider(
            LlmConfig(provider="anthropic", model="m"),
            environ={},
        )
        self.assertIsNone(provider)
        self.assertIn("no credential is present", note)

    def test_adapter_source_imports_no_outbound_client(self) -> None:
        source = pathlib.Path(
            "actionmesh/providers/openai_compatible.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in (
            "urllib.request",
            "http.client",
            "socket",
            "ssl",
            "httpx",
            "requests",
        ):
            self.assertNotIn(banned, imported)


class TestExecuteRefusalAdversarial(unittest.TestCase):
    def test_opener_never_called_with_credential(self) -> None:
        calls: list[Any] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            calls.append(req)
            raise AssertionError("execute() must never call an opener")

        provider = _provider(opener)
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(provider.execute(_action()))
        self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)
        self.assertEqual(calls, [])

    def test_opener_never_called_without_credential(self) -> None:
        calls: list[Any] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            calls.append(req)
            return _FakeResponse(b'{"choices":[]}')

        provider = _provider(opener)
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(provider.execute(_action()))
        self.assertEqual(raised.exception.kind, FailureKind.AUTHENTICATION)
        self.assertEqual(calls, [])

    def test_error_never_contains_the_secret(self) -> None:
        provider = _provider(lambda *a, **k: _FakeResponse(b"{}"))
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(provider.execute(_action()))
        text = str(raised.exception)
        self.assertNotIn(CANARY, text)
        self.assertNotIn("sk-phase5", text)
        self.assertIn("not an approved model endpoint", text)

    def test_secret_in_opener_error_body_still_never_surfaces(self) -> None:
        # Even if a future live path called an opener whose error quoted
        # the key, today's refusal must not be reachable through it at
        # all: the opener is dead code on the execute path.
        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            raise ConnectionError(f"key {CANARY} rejected by gateway")

        provider = _provider(opener)
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(provider.execute(_action()))
        self.assertNotIn(CANARY, str(raised.exception))
        self.assertNotIn("gateway", str(raised.exception))

    def test_secret_not_written_to_caplog(self) -> None:
        provider = _provider(lambda *a, **k: _FakeResponse(b"{}"))
        logger = logging.getLogger("actionmesh.providers.openai_compatible")
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ), self.assertLogs(logger, level="DEBUG") as logs:
            asyncio.run(provider.execute(_action()))
        self.assertNotIn(CANARY, "\n".join(logs.output))

    def test_secret_not_in_action_parameters_echo(self) -> None:
        # A model-generated parameter that *is* the secret must not be
        # echoed back by the refusal either.
        action = Action(
            type="chat.generate",
            provider="openai_compatible",
            parameters={
                "model": "m",
                "messages": [{"role": "user", "content": f"my key is {CANARY}"}],
            },
        )
        provider = _provider(lambda *a, **k: _FakeResponse(b"{}"))
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(provider.execute(action))
        self.assertNotIn(CANARY, str(raised.exception))

    def test_opener_provider_error_body_still_never_surfaces(self) -> None:
        # The seam's own ProviderError passthrough must not leak either: an
        # opener that raises ProviderError quoting the credential must be
        # reduced to the closed-vocabulary NETWORK message.
        from actionmesh.providers.base import ProviderError as _PE

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            raise _PE(f"gateway rejected key {CANARY}")

        provider = _provider(opener)
        with mock.patch.dict(os.environ, {ENV_CREDENTIAL: CANARY}), self.assertRaises(
            ProviderError
        ) as raised:
            provider.generate(model="m", messages=[{"role": "user", "content": "hi"}])
        self.assertEqual(raised.exception.kind, FailureKind.NETWORK)
        self.assertNotIn(CANARY, str(raised.exception))
        self.assertNotIn("gateway", str(raised.exception))
        self.assertIn("could not be reached", str(raised.exception))

    def test_no_provider_result_is_ever_fabricated(self) -> None:
        # execute() either raises or returns a real ProviderResult;
        # on the refusal path there is no result object to inspect for
        # a phantom "success": the raise IS the outcome.
        provider = _provider(lambda *a, **k: _FakeResponse(b'{"choices":[]}'))
        with mock.patch.dict(
            os.environ, {ENV_CREDENTIAL: CANARY}
        ), self.assertRaises(ProviderError):
            asyncio.run(provider.execute(_action()))


if __name__ == "__main__":
    unittest.main()
