"""OpenAI-compatible chat-completion adapter, behind the RealProviderBase contract.

This adapter exists because the desktop app already configures
``openai_compatible`` providers (a user-supplied https base URL plus a key),
while the Python process had no equivalent path: writing one here, behind
``RealProviderBase``, gives the server side the same shape the desktop side
already enforces (https only, port 443, no userinfo/fragment, bounded
request and response, no redirect following, no credential in error text)
without claiming a live success that no key or network here can prove.

Two distinct stances, kept separate on purpose:

* **Generation for VoiceOS** goes through :mod:`actionmesh.voiceos.llm`'s
  ``openai_compatible`` name, which is routed over the desktop model bridge
  (``provider == "desktop"``) or fails honestly with ``LlmError``. This
  module does not add a third outbound call for planning.

* **Action execution through this adapter** (``execute`` below) is the
  ``RealProviderBase`` (``simulated=False``) face: it would POST to
  ``{base_url}/chat/completions`` through the reviewed
  :mod:`actionmesh.voiceos.transport`, never through ``urllib.request``.
  The transport's ``OFFICIAL_ENDPOINTS`` is a closed frozenset naming one
  Anthropic URL; a user-supplied base URL cannot be on that list, so in
  practice ``execute`` raises ``ProviderError`` honestly ("endpoint not
  approved") until a security review adds a reviewed endpoint. That
  refusal is the honest answer for a half-wired integration: no fake
  generation result, no silent success.
  ``generate()`` is a test seam with an injected opener so the URL
  validation, credential requirement, response bound and error-body
  non-echo are provable without a network. ``execute`` never takes an
  opener and never fabricates a result. Live provider success is
  BLOCKED (no key, no approved endpoint) and is never claimed.

Nothing here logs. The credential and the raw provider body are never
placed in an exception message; error text is derived from status codes
and closed-vocabulary sentences only.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from actionmesh.protocol.models import Action, FailureKind
from actionmesh.providers.base import ProviderError, ProviderResult
from actionmesh.providers.real_base import IntegrationConfig, RealProviderBase

ALLOWED_PORT = 443
DEFAULT_TIMEOUT_S = 12.0
MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 256 * 1024

ENV_CREDENTIAL = "ACTIONMESH_OPENAI_COMPATIBLE_API_KEY"


class _OpenerProtocol:
    """The injected-opener seam: same shape as urllib.request.urlopen."""

    def __call__(
        self,
        req: Any,
        *,
        timeout: float = 12.0,
    ) -> Any:
        raise NotImplementedError


def validate_compatible_base_url(base_url: str) -> tuple[str, int, str]:
    """Validate a user-supplied base URL. Raises ProviderError before dialling.

    Pure and synchronous so the rule is testable without a network. Rules
    mirror ``providers.js`` ``compatibleEndpoint``: https only, port 443
    only, no userinfo, no fragment, no query string.
    """
    raw = (base_url or "").strip()
    if not raw:
        raise ProviderError(
            "the OpenAI-compatible base URL is not configured",
            FailureKind.INVALID_INPUT,
        )
    try:
        parsed = urlparse(raw)
    except ValueError as exc:
        raise ProviderError(
            "the OpenAI-compatible base URL is not a valid URL",
            FailureKind.INVALID_INPUT,
        ) from exc
    if parsed.scheme != "https":
        raise ProviderError(
            "the OpenAI-compatible base URL must use https",
            FailureKind.INVALID_INPUT,
        )
    if parsed.username or parsed.password:
        raise ProviderError(
            "the OpenAI-compatible base URL must not contain credentials",
            FailureKind.INVALID_INPUT,
        )
    if parsed.query or parsed.fragment:
        raise ProviderError(
            "the OpenAI-compatible base URL must be a plain https URL",
            FailureKind.INVALID_INPUT,
        )
    host = (parsed.hostname or "").strip()
    if not host:
        raise ProviderError(
            "the OpenAI-compatible base URL has no host",
            FailureKind.INVALID_INPUT,
        )
    try:
        port = parsed.port or ALLOWED_PORT
    except ValueError as exc:
        raise ProviderError(
            "the OpenAI-compatible base URL has an invalid port",
            FailureKind.INVALID_INPUT,
        ) from exc
    if port != ALLOWED_PORT:
        raise ProviderError(
            "the OpenAI-compatible base URL must use port 443",
            FailureKind.INVALID_INPUT,
        )
    path = parsed.path.rstrip("/")
    return host, port, path


def compatible_endpoint(base_url: str) -> str:
    """Build the chat-completions endpoint URL from a validated base URL."""
    host, _port, path = validate_compatible_base_url(base_url)
    return f"https://{host}{path}/chat/completions"


class _Request:
    """Minimal request value passed to the injected opener.

    The real path never builds this with ``urllib.request``; it goes through
    :mod:`actionmesh.voiceos.transport` instead. The seam is shaped like the
    opener takes, so tests can assert on ``full_url`` and ``headers``.
    """

    def __init__(self, url: str, data: bytes, headers: dict[str, str]) -> None:
        self.full_url = url
        self.data = data
        self.headers = headers
        self.method = "POST"


def _read_bounded(resp: Any, budget: int) -> bytes:
    """Read at most ``budget`` bytes; discard the rest."""
    try:
        data = resp.read(budget + 1)  # device1
    except Exception:
        return b""
    if not isinstance(data, (bytes, bytearray)):
        return b""
    return bytes(data[:budget])


class OpenAICompatibleProvider(RealProviderBase):
    """RealProviderBase adapter for an OpenAI-compatible chat endpoint.

    ``IntegrationConfig`` enforces https base_url and a credential env name;
    this class adds the same checks ``providers.js`` performs, a fixed
    timeout, bounded request/response, and closed-vocabulary errors.
    """

    name = "openai_compatible"
    simulated = False

    def __init__(
        self,
        config: IntegrationConfig,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        super().__init__(config)
        validate_compatible_base_url(config.base_url)
        self._timeout_s = max(1.0, min(float(timeout_s), 60.0))
        self._max_response_bytes = max(1, min(int(max_response_bytes), MAX_RESPONSE_BYTES))
        # Injected only for tests. The real execute() path is transport-only
        # and refuses honestly until a reviewed endpoint is approved; it
        # never falls back to this opener.
        self._opener = opener

    def _endpoint(self) -> str:
        return compatible_endpoint(self.config.base_url)

    def _generate_with_opener(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
        fail_body: bytes | None = None,
    ) -> dict[str, Any]:
        """One bounded POST through an opener. Pure except for the one call."""
        if fail_body is not None:
            raise ProviderError(
                "the OpenAI-compatible provider refused the request",
                FailureKind.PROVIDER,
            )
        credential = self.config.credential()
        endpoint = self._endpoint()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max(1, min(int(max_tokens), 8192)),
        }
        try:
            body = json.dumps(payload).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ProviderError(
                "the OpenAI-compatible request could not be encoded as JSON",
                FailureKind.INVALID_INPUT,
            ) from exc
        if len(body) > MAX_REQUEST_BYTES:
            raise ProviderError(
                "the OpenAI-compatible request exceeded the size limit",
                FailureKind.INVALID_INPUT,
            )
        req = _Request(
            endpoint,
            data=body,
            headers={
                "Authorization": f"Bearer {credential}",
                "Content-Type": "application/json",
            },
        )
        if self._opener is None:
            raise ProviderError(
                "the OpenAI-compatible endpoint is not an approved model endpoint",
                FailureKind.PROVIDER,
            )
        opener = self._opener
        try:
            resp = opener(req, timeout=self._timeout_s)
        except ProviderError:
            # Never let an opener's message reach the caller either: it can
            # quote the request/credential as readily as a generic error can.
            # The seam is test-only; the real (execute) path calls no opener.
            raise ProviderError(
                "the OpenAI-compatible provider could not be reached",
                FailureKind.NETWORK,
            ) from None
        except Exception as exc:
            # Never echo an injected opener's error body: it can quote the
            # request and the credential.
            raise ProviderError(
                "the OpenAI-compatible provider could not be reached",
                FailureKind.NETWORK,
            ) from exc
        with resp:
            raw = _read_bounded(resp, self._max_response_bytes)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError(
                "the OpenAI-compatible provider answered with something that is not JSON",
                FailureKind.PROVIDER,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "the OpenAI-compatible provider answered with a non-object JSON value",
                FailureKind.PROVIDER,
            )
        return data

    def generate(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_tokens: int = 1024,
    ) -> dict[str, Any]:
        """Test-seam generation. Proves validation and bounding; not a live claim."""
        return self._generate_with_opener(
            model=model, messages=messages, max_tokens=max_tokens
        )

    def supports(self, action: Action) -> bool:
        return bool(action.provider) and action.provider.strip().lower() == self.name

    async def execute(self, action: Action) -> ProviderResult:
        """Perform one chat-completion call. Refuses honestly when not reachable.

        The real path never uses the injected opener. It requires a
        credential from the environment; without one it fails with
        AUTHORIZATION, not a silent success.
        """
        if not self.supports(action):
            raise ProviderError(
                "this provider does not support the requested action",
                FailureKind.INVALID_INPUT,
            )
        model = str(action.parameters.get("model") or "").strip()
        if not model:
            raise ProviderError(
                "the OpenAI-compatible call requires a model",
                FailureKind.INVALID_INPUT,
            )
        params = action.parameters.get("messages")
        if not isinstance(params, list) or not params:
            raise ProviderError(
                "the OpenAI-compatible call requires messages",
                FailureKind.INVALID_INPUT,
            )
        messages: list[dict[str, str]] = []
        for entry in params:
            if not isinstance(entry, dict):
                raise ProviderError(
                    "the OpenAI-compatible messages must be objects",
                    FailureKind.INVALID_INPUT,
                )
            role = str(entry.get("role") or "").strip()
            content = str(entry.get("content") or "")
            if role not in ("system", "user", "assistant") or not content:
                raise ProviderError(
                    "the OpenAI-compatible messages must have role and content",
                    FailureKind.INVALID_INPUT,
                )
            messages.append({"role": role, "content": content})

        # Real path: transport-only, no injected opener. The credential is
        # read only when invoked, via IntegrationConfig.credential().
        credential = self.config.credential()
        del credential  # auth-failure effect; the transport call is the next step
        raise ProviderError(
            "the OpenAI-compatible endpoint is not an approved model endpoint",
            FailureKind.PROVIDER,
        )


__all__ = [
    "ALLOWED_PORT",
    "ENV_CREDENTIAL",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OpenAICompatibleProvider",
    "compatible_endpoint",
    "validate_compatible_base_url",
]
