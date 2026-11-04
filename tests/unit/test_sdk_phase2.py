"""Phase 2 — provider parity and the OpenAI-compatible adapter seam.

Two questions this file answers:

* Does the Python provider-name set match what the codebase can honestly
  reach? ``actionmesh.voiceos.llm.AVAILABLE_PROVIDERS`` must be exactly
  ``{"anthropic", "desktop"}``: Anthropic has a direct reviewed transport,
  ``desktop`` routes through the Electron bridge (whose own
  ``implemented: true`` table in ``desktop/app/providers.js`` already
  covers openai, gemini and openai_compatible). Naming the other three as
  direct Python providers would claim an endpoint that does not exist in
  ``OFFICIAL_ENDPOINTS``.
* Does the OpenAI-compatible adapter enforce the shape the desktop side
  already enforces, before any opener is called? https-only, port 443, no
  userinfo/fragment, credential read only when invoked, bounded
  request/response, error bytes never echoed.

All adapter calls run through an injected fake opener. There is no network,
no key, and no claim of a live provider success: ``execute`` refuses with
``FailureKind.PROVIDER`` because the endpoint is not on the closed
transport list — that refusal is INTEGRATED behaviour, not a stub.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from typing import Any
from unittest import mock

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError
from actionmesh.providers.openai_compatible import (
    OpenAICompatibleProvider,
    compatible_endpoint,
    validate_compatible_base_url,
)
from actionmesh.providers.real_base import IntegrationConfig
from actionmesh.voiceos.llm import AVAILABLE_PROVIDERS, LlmConfig, build_provider
from actionmesh.voiceos.model_bridge import BridgeConfig

CRED_ENV = "ACTIONMESH_OPENAI_COMPATIBLE_API_KEY"


class _FakeResponse:
    """One bounded-reader response, like the real seam promises."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, budget: int) -> bytes:
        # Callers ask for budget+1; return at most what was requested.
        return self._body[:budget]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None


def _provider(opener: Any = None, base: str = "https://api.example.test/v1") -> OpenAICompatibleProvider:
    config = IntegrationConfig(
        provider_name="openai_compatible",
        base_url=base,
        credential_env=CRED_ENV,
    )
    return OpenAICompatibleProvider(config, opener=opener)


def _action(**parameters: Any) -> Action:
    return Action(type="chat.generate", provider="openai_compatible", parameters=parameters)


class TestPythonProviderNameParity(unittest.TestCase):
    def test_available_providers_match_the_real_python_transports(self) -> None:
        self.assertEqual(AVAILABLE_PROVIDERS, frozenset({"anthropic", "desktop"}))

    def test_anthropic_is_the_only_direct_python_provider(self) -> None:
        # No key, no bridge: anthropic must name the missing credential, not
        # silently resolve. The name being accepted is not a live claim.
        provider, reason = build_provider(LlmConfig(provider="anthropic"), environ={})
        self.assertIsNone(provider)
        self.assertIn("ACTIONMESH_VOICEOS_LLM_API_KEY", reason)

    def test_desktop_resolves_only_with_a_bridge(self) -> None:
        provider, reason = build_provider(
            LlmConfig(provider="desktop"),
            bridge=BridgeConfig(socket_path="/tmp/s.sock", token="tok"),
        )
        self.assertIsNotNone(provider)
        self.assertEqual(reason, "")
        provider, reason = build_provider(LlmConfig(provider="desktop"))
        self.assertIsNone(provider)
        self.assertIn("ACTIONMESH_PROVIDER_BRIDGE_SOCKET", reason)

    def test_openai_gemini_and_compatible_are_not_direct_python_providers(self) -> None:
        # They are implemented in providers.js behind the bridge; naming them
        # here would claim a direct API that the closed transport does not carry.
        for name in ("openai", "gemini", "openai_compatible"):
            provider, reason = build_provider(
                LlmConfig(provider=name),
                environ={CRED_ENV: "test"},
                bridge=BridgeConfig(socket_path="/tmp/s.sock", token="tok"),
            )
            self.assertIsNone(provider, name)
            self.assertIn("no approved outbound transport", reason, name)

    def test_a_model_answer_is_still_not_an_authority(self) -> None:
        # build_provider never lets a provider name become a grant; the
        # provider resolves to a proposer only. (One-line guard: if that
        # ever changes, this fails instead of the policy tests changing.)
        provider, _ = build_provider(
            LlmConfig(provider="desktop"),
            bridge=BridgeConfig(socket_path="/tmp/s.sock", token="tok"),
        )
        self.assertIsNotNone(provider)
        self.assertFalse(hasattr(provider, "approve"))


class TestCompatibleBaseUrlValidation(unittest.TestCase):
    def test_valid_https_base_url(self) -> None:
        host, port, path = validate_compatible_base_url("https://api.example.test/v1/")
        self.assertEqual((host, port), ("api.example.test", 443))
        self.assertEqual(path, "/v1")

    def test_endpoint_is_chat_completions_under_the_base(self) -> None:
        self.assertEqual(
            compatible_endpoint("https://api.example.test/v1"),
            "https://api.example.test/v1/chat/completions",
        )
        self.assertEqual(
            compatible_endpoint("https://api.example.test"),
            "https://api.example.test/chat/completions",
        )

    def test_rejects_http(self) -> None:
        with self.assertRaises(ProviderError) as raised:
            validate_compatible_base_url("http://api.example.test/v1")
        self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_rejects_non_443_port(self) -> None:
        with self.assertRaises(ProviderError):
            validate_compatible_base_url("https://api.example.test:8443/v1")

    def test_rejects_userinfo(self) -> None:
        with self.assertRaises(ProviderError):
            validate_compatible_base_url("https://user:pass@api.example.test/v1")

    def test_rejects_fragment_and_query(self) -> None:
        for url in ("https://api.example.test/v1#frag", "https://api.example.test/v1?x=1"):
            with self.assertRaises(ProviderError, msg=url):
                validate_compatible_base_url(url)

    def test_rejects_empty_and_hostless(self) -> None:
        for url in ("", "https:///v1"):
            with self.assertRaises(ProviderError, msg=repr(url)):
                validate_compatible_base_url(url)


class TestAdapterWithFakeOpener(unittest.TestCase):
    def test_simulated_flag_is_honest(self) -> None:
        self.assertFalse(_provider(opener=lambda *a, **k: _FakeResponse(b"{}")).simulated)

    def test_credential_is_required_before_the_opener(self) -> None:
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
        self.assertEqual(calls, [], "no credential, no call")

    def test_happy_path_posts_once_with_the_credential_header(self) -> None:
        seen: list[tuple[Any, float]] = []

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            seen.append((req, timeout))
            return _FakeResponse(b'{"choices":[{"message":{"content":"hello"}}]}')

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-test-value"}):
            data = _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(data["choices"][0]["message"]["content"], "hello")
        self.assertEqual(len(seen), 1)
        req, timeout = seen[0]
        self.assertEqual(req.method, "POST")
        self.assertEqual(
            req.full_url, "https://api.example.test/v1/chat/completions"
        )
        self.assertEqual(req.headers["Authorization"], "Bearer sk-test-value")
        self.assertNotIn("sk-test-value", str(timeout))
        self.assertNotIn("sk-test-value", repr(data) if "sk-test-value" in repr(data) else "")

    def test_response_is_bounded(self) -> None:
        # Trailing whitespace is valid JSON padding; without the bound the
        # 600 KiB of spaces would be buffered. Truncation at 256 KiB keeps
        # the document valid while proving the reader never buffered the rest.
        big = b'{"choices":[]}' + b" " * (600 * 1024)
        asked: list[int] = []

        class _Recording(_FakeResponse):
            def read(self, budget: int) -> bytes:
                asked.append(budget)
                return super().read(budget)

        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            return _Recording(big)

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-test-value"}):
            data = _provider(opener).generate(
                model="m", messages=[{"role": "user", "content": "hi"}]
            )
        self.assertEqual(data, {"choices": []})
        self.assertEqual(len(asked), 1)
        self.assertLessEqual(asked[0], 256 * 1024 + 1, "reader must never ask past the bound")

    def test_error_body_is_never_echoed(self) -> None:
        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            raise ConnectionError("gateway said: your key sk-test-value is bad")

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-test-value"}), self.assertRaises(ProviderError) as raised:
                _provider(opener).generate(
                    model="m", messages=[{"role": "user", "content": "hi"}]
                )
        self.assertEqual(raised.exception.kind, FailureKind.NETWORK)
        self.assertNotIn("sk-test-value", str(raised.exception))
        self.assertNotIn("gateway said", str(raised.exception))

    def test_non_json_answer_is_a_provider_failure_not_a_success(self) -> None:
        def opener(req: Any, *, timeout: float = 0.0) -> Any:
            return _FakeResponse(b"not json")

        with mock.patch.dict(os.environ, {CRED_ENV: "sk-test-value"}), self.assertRaises(ProviderError) as raised:
                _provider(opener).generate(
                    model="m", messages=[{"role": "user", "content": "hi"}]
                )
        self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)


class TestExecuteHonestRefusal(unittest.TestCase):
    def test_supports_only_its_own_provider(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        self.assertTrue(provider.supports(_action(model="m", messages=[])))
        self.assertFalse(
            provider.supports(Action(type="chat.generate", provider="mock"))
        )

    def test_execute_requires_a_model_and_messages(self) -> None:
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        for action in (
            _action(messages=[{"role": "user", "content": "hi"}]),
            _action(model="m"),
            _action(model="m", messages=[]),
            _action(model="m", messages=[{"role": "root", "content": "hi"}]),
        ):
            with self.assertRaises(ProviderError) as raised:
                asyncio.run(provider.execute(action))
            self.assertEqual(raised.exception.kind, FailureKind.INVALID_INPUT)

    def test_execute_without_a_credential_is_auth_not_success(self) -> None:
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

    def test_execute_with_a_credential_refuses_the_unapproved_endpoint(self) -> None:
        # INTEGRATED refusal: the closed transport lists one Anthropic URL,
        # so a user-supplied base URL cannot be dialled. No fake generation
        # result is returned; the caller sees FailureKind.PROVIDER.
        provider = _provider(opener=lambda *a, **k: _FakeResponse(b"{}"))
        with mock.patch.dict(os.environ, {CRED_ENV: "sk-test-value"}), self.assertRaises(
            ProviderError
        ) as raised:
            asyncio.run(
                provider.execute(
                    _action(model="m", messages=[{"role": "user", "content": "hi"}])
                )
            )
        self.assertEqual(raised.exception.kind, FailureKind.PROVIDER)
        self.assertIn("not an approved model endpoint", str(raised.exception))
        self.assertNotIn("sk-test-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
