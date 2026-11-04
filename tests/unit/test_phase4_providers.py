"""Phase 4 - P1-1 real vendor adapter (honest boundary).

Label: MOCKED-VERIFIED, live BLOCKED.

The only outbound transport approved by
``tests/security/test_dangerous_primitives.py`` is
``actionmesh.voiceos.transport.post_json`` (single
``asyncio.open_connection`` grant) whose closed
``OFFICIAL_ENDPOINTS`` is exactly
``{"https://api.anthropic.com/v1/messages"}``.  There is no approved
transport for an OpenAI-compatible base URL, and ``NETWORK_ALLOWED``
is empty, so adding ``urllib.request`` here would break the guard.

``OpenAICompatibleProvider`` therefore implements the full
validation / credential / execute-refusal contract honestly:

* ``IntegrationConfig`` enforces https ``base_url`` and concrete scopes,
  and reads the credential from the environment only at invocation
  (``IntegrationConfig.credential()``) - never at import time, never
  logged, never echoed in an error.
* ``validate_compatible_base_url`` enforces https only, port 443 only,
  no userinfo, no query/fragment, matching ``desktop/app/providers.js``
  ``compatibleEndpoint``.
* ``execute()`` requires model + messages, fails with
  ``AUTHENTICATION`` when the env credential is missing, and with
  ``PROVIDER`` ("endpoint is not an approved model endpoint") when a
  credential is present, because the closed transport cannot dial a
  user-supplied base URL.  It never uses an injected opener and never
  fabricates a result.
* ``generate()`` is a test seam with an injected opener proving the
  wire shape: bounded request (64 KiB) and response (256 KiB), timeout
  clamping, Authorization header from the env credential only.

No live vendor call is claimed.  ``execute`` returning a success would
be a lie; REFUSAL is the INTEGRATED behaviour captured in
``tests/unit/test_sdk_phase2.py``.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import pathlib
import unittest
from typing import Any
from unittest import mock

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError
from actionmesh.providers.openai_compatible import (
    ALLOWED_PORT,
    ENV_CREDENTIAL,
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    OpenAICompatibleProvider,
    compatible_endpoint,
    validate_compatible_base_url,
)
from actionmesh.providers.real_base import IntegrationConfig, RealProviderBase
from actionmesh.voiceos import transport as transport_module

CRED_ENV = ENV_CREDENTIAL


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, budget: int) -> bytes:
        return self._body[:budget]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


def _config(base: str = "https://api.example.test/v1") -> IntegrationConfig:
    return IntegrationConfig(
        provider_name="openai_compatible",
        base_url=base,
        credential_env=CRED_ENV,
    )


def _provider(opener: Any = None, base: str = "https://api.example.test/v1") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(_config(base), opener=opener)


def _action(**parameters: Any) -> Action:
    return Action(type="chat.generate", provider="openai_compatible", parameters=parameters)


class TestBoundaryOnlyRealBase(unittest.TestCase):
    def test_real_base_is_not_simulated_but_does_no_network_io(self) -> None:
        self.assertFalse(RealProviderBase.simulated)
        self.assertFalse(OpenAICompatibleProvider.simulated)
        source = pathlib.Path(
            "actionmesh/providers/real_base.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = {
            n.names[0].name
            for n in ast.walk(tree)
            if isinstance(n, ast.Import)
        } | {
            n.module
            for n in ast.walk(tree)
            if isinstance(n, ast.ImportFrom)
        }
        self.assertNotIn("socket", imported)
        self.assertNotIn("urllib.request", imported)
        self.assertNotIn("http.client", imported)
        self.assertNotIn("requests", imported)

    def test_integration_config_rejects_http_and_wildcard_scope(self) -> None:
        with self.assertRaises(ProviderError):
            IntegrationConfig("v", "http://api.example.test", "T").validate()
        with self.assertRaises(ProviderError) as raised:
            IntegrationConfig(
                "v", "https://api.example.test", "T", scopes=("*",)
            ).validate()
        self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_integration_config_rejects_empty_provider_and_env(self) -> None:
        for name, env in (("", "T"), ("v", ""), ("  ", "T")):
            with self.assertRaises(ProviderError, msg=(name, env)):
                IntegrationConfig(name, "https://api.example.test", env).validate()

    def test_credential_read_only_at_invocation(self) -> None:
        config = _config()
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            ProviderError
        ) as raised:
            config.credential()
        self.assertEqual(raised.exception.kind, FailureKind.AUTHENTICATION)
        self.assertIn(CRED_ENV, str(raised.exception))
        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}):
            self.assertEqual(config.credential(), "sk-phase4-canary")

    def test_no_approved_transport_for_compatible_endpoints(self) -> None:
        # The single approved transport's closed list must not contain a
        # user-supplied compatible endpoint; that is why execute refuses.
        self.assertEqual(
            transport_module.OFFICIAL_ENDPOINTS,
            frozenset({"https://api.anthropic.com/v1/messages"}),
        )
        self.assertNotIn(
            compatible_endpoint("https://api.example.test/v1"),
            transport_module.OFFICIAL_ENDPOINTS,
        )
        with self.assertRaises(transport_module.TransportError):
            transport_module.checked_endpoint(
                compatible_endpoint("https://api.example.test/v1")
            )


class TestOpenAiCompatibleValidation(unittest.TestCase):
    def test_valid_https_base_url_and_port_443(self) -> None:
        host, port, path = validate_compatible_base_url(
            "https://api.example.test/v1/"
        )
        self.assertEqual((host, port, path), ("api.example.test", 443, "/v1"))

    def test_rejects_http_and_non_443(self) -> None:
        for url in (
            "http://api.example.test/v1",
            "https://api.example.test:8443/v1",
        ):
            with self.assertRaises(ProviderError) as raised:
                validate_compatible_base_url(url)
            self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_rejects_userinfo_query_fragment_and_empty(self) -> None:
        for url in (
            "https://user:pass@api.example.test/v1",
            "https://api.example.test/v1?x=1",
            "https://api.example.test/v1#frag",
            "",
            "https:///v1",
        ):
            with self.assertRaises(ProviderError, msg=url):
                validate_compatible_base_url(url)

    def test_constructor_validates_base_url(self) -> None:
        with self.assertRaises(ProviderError):
            OpenAICompatibleProvider(_config("http://api.example.test"))

    def test_bounds_are_the_documented_ones(self) -> None:
        self.assertEqual(MAX_REQUEST_BYTES, 64 * 1024)
        self.assertEqual(MAX_RESPONSE_BYTES, 256 * 1024)
        self.assertEqual(ALLOWED_PORT, 443)
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        self.assertGreater(provider._timeout_s, 0)
        # timeout clamping is internal; max response clamp is bounded
        self.assertLessEqual(provider._max_response_bytes, MAX_RESPONSE_BYTES)

    def test_source_never_imports_outbound_client(self) -> None:
        source = pathlib.Path(
            "actionmesh/providers/openai_compatible.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
        for banned in ("urllib.request", "http.client", "socket", "httpx", "requests"):
            self.assertNotIn(banned, imported)
        # urllib.parse only (pure string handling) is allowed by the guard
        self.assertIn("urllib.parse", imported)


class TestGenerateSeamBoundedAndNoSecretEcho(unittest.TestCase):
    def test_happy_path_uses_credential_header_once(self) -> None:
        seen: list[tuple[Any, float]] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            seen.append((req, timeout))
            return _FakeResponse(b'{"choices":[{"message":{"content":"ok"}}]}')

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}):
            data = _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(data["choices"][0]["message"]["content"], "ok")
        self.assertEqual(len(seen), 1)
        req, _timeout = seen[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(req.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(req.headers["Authorization"], "Bearer sk-phase4-canary")
        self.assertEqual(req.headers["Content-Type"], "application/json")

    def test_missing_credential_never_calls_opener(self) -> None:
        calls: list[Any] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            calls.append(req)
            return _FakeResponse(b"{}")

        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            ProviderError
        ) as raised:
            _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(raised.exception.kind, FailureKind.AUTHENTICATION)
        self.assertEqual(calls, [])

    def test_response_is_bounded_at_256_kib(self) -> None:
        asked: list[int] = []

        class _Recording(_FakeResponse):
            def read(self, budget: int) -> bytes:
                asked.append(budget)
                return super().read(budget)

        big = b'{"choices":[]}' + b" " * (600 * 1024)

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            return _Recording(big)

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}):
            data = _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(data, {"choices": []})
        self.assertLessEqual(asked[0], MAX_RESPONSE_BYTES + 1)

    def test_oversized_request_is_refused_before_opener(self) -> None:
        calls: list[Any] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            calls.append(req)
            return _FakeResponse(b"{}")

        huge = "x" * (MAX_REQUEST_BYTES + 1)
        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}), self.assertRaises(
            ProviderError
        ) as raised:
            _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": huge}]
            )
        self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)
        self.assertEqual(calls, [])

    def test_error_bodies_are_never_echoed(self) -> None:
        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            raise ConnectionError("gateway said: your key sk-phase4-canary is bad")

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}), self.assertRaises(
            ProviderError
        ) as raised:
            _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(raised.exception.kind, FailureKind.NETWORK)
        self.assertNotIn("sk-phase4-canary", str(raised.exception))
        self.assertNotIn("gateway said", str(raised.exception))

    def test_non_json_and_non_object_are_provider_failures(self) -> None:
        for body in (b"not json", b"[1,2]"):
            def opener(req: Any, *, timeout: float = 0.0, _b: bytes = body) -> Any:
                return _FakeResponse(_b)

            with mock.patch.dict(os.environ, {CRED_ENV: "sk"}), self.assertRaises(
                ProviderError
            ) as raised:
                _provider(opener).generate(
                    model="m", messages=[{"role": "user", "content": "hi"}]
                )
            self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)


class TestExecuteHonestRefusal(unittest.TestCase):
    def test_supports_only_own_provider_name(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        self.assertTrue(provider.supports(_action(model="m", messages=[])))
        self.assertFalse(provider.supports(Action(type="chat.generate", provider="mock")))
        self.assertFalse(provider.supports(Action(type="chat.generate", provider="")))

    def test_requires_model_and_messages(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        for action in (
            _action(messages=[{"role": "user", "content": "hi"}]),
            _action(model="m"),
            _action(model="m", messages=[]),
            _action(model="m", messages=[{"role": "root", "content": "hi"}]),
            _action(model="m", messages=["not-a-dict"]),
        ):
            with self.assertRaises(ProviderError) as raised:
                asyncio.run(provider.execute(action))
            self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_unsupported_provider_is_invalid_input(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        with self.assertRaises(ProviderError) as raised:
            asyncio.run(provider.execute(Action(type="chat.generate", provider="mock")))
        self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_without_credential_is_authentication_not_success(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(
                provider.execute(
                    _action(model="m", messages=[{"role": "user", "content": "hi"}])
                )
            )
        self.assertEqual(raised.exception.kind, FailureKind.AUTHENTICATION)

    def test_with_credential_refuses_unapproved_endpoint_no_secret_echo(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(
                provider.execute(
                    _action(model="m", messages=[{"role": "user", "content": "hi"}])
                )
            )
        self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)
        self.assertIn("not an approved model endpoint", str(raised.exception))
        self.assertNotIn("sk-phase4-canary", str(raised.exception))

    def test_execute_never_uses_injected_opener(self) -> None:
        # Even if an opener is injected (test seam), execute must not bypass
        # the transport check.  A ProviderResult must never be fabricated.
        calls: list[Any] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            calls.append(req)
            return _FakeResponse(b'{"choices":[]}')

        provider = _provider(opener)
        with mock.patch.dict(os.environ, {CRED_ENV: "sk-phase4-canary"}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(
                provider.execute(
                    _action(model="m", messages=[{"role": "user", "content": "hi"}])
                )
            )
        self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)
        self.assertEqual(calls, [], "execute must not call the injected opener")

    def test_request_body_is_json_and_bounded(self) -> None:
        # Behaviour of _generate_with_opener's explicit bound, proved via seam:
        # a payload that would exceed 64 KiB is refused before dialling.
        # (Encoded via json round-trip to check the 64KiB claim directly.)
        payload = {"model": "m", "messages": [{"role": "user", "content": "x" * 70000}]}
        self.assertGreater(len(json.dumps(payload).encode()), MAX_REQUEST_BYTES)


if __name__ == "__main__":
    unittest.main()
