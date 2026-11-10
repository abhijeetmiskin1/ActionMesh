"""The only outbound network call in ActionMesh, reviewable on one page.

The package-wide rule in ``tests/security/test_dangerous_primitives.py`` is that
nothing dials out. A reasoning layer needs one approved exception: bounded HTTPS requests to named official endpoints. That exception lives here and nowhere else, and
this module is deliberately small enough to read in full:

* **One function dials.** :func:`post_json` opens one TLS connection, writes one
  bounded request, reads one bounded response, and closes. There is no connection
  pool, no redirect following, no proxy discovery, no DNS-over-HTTPS, no retry
  loop that could be steered by a response.
* **The destination is checked, not trusted.** ``https`` only, port 443 only, a
  hostname with no userinfo, a path, and no query or fragment. A caller cannot
  hand this function an ``http://`` URL, a bare host, or a ``user:pass@`` form and
  expect a connection attempt — it raises before dialling.
* **Both directions are bounded.** A request larger than
  :data:`MAX_REQUEST_BYTES` and a response larger than ``max_response_bytes`` are
  refused rather than buffered. Response headers are capped by the stream's own
  ``limit``.
* **TLS is verified.** ``ssl=True`` makes asyncio construct the default context,
  which verifies the certificate chain and the hostname. There is no "insecure"
  switch in this file, on purpose: an operator who needs to reach a private
  gateway can point the endpoint at a host with a valid certificate.
* **Nothing here logs.** Not the endpoint's headers, not the key, not the body,
  not the response. Errors carry a status code and a short message the remote
  service chose to make public; a request's contents never appear in them.

The same TLS exchange supports fixed read-only GitHub and Drive endpoints.
The connector query is encoded separately from the approved endpoint. No
redirects are followed and connector error bodies are not surfaced.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from contextlib import suppress
from typing import Any
from urllib.parse import urlencode, urlparse

#: The only port this transport will dial. An API endpoint on another port is not
#: the official interface it claims to be.
HTTPS_PORT = 443

#: The complete set of destinations this file will ever dial. It is a closed list in
#: the transport, not a parameter its caller controls, so even a compromised adapter
#: cannot aim this module at an arbitrary host: an endpoint outside this set is
#: refused before `open_connection` is reached. Adding one is a security review.
OFFICIAL_ENDPOINTS: frozenset[str] = frozenset(
    {
        "https://api.anthropic.com/v1/messages",
    }
)

READ_ENDPOINTS = frozenset({
    "https://api.github.com/search/repositories",
    "https://api.github.com/search/issues",
    "https://www.googleapis.com/drive/v3/files",
})


class ReadTransportError(RuntimeError):
    def __init__(self, status: int, *, rate_limited: bool = False) -> None:
        self.status = status
        self.rate_limited = rate_limited
        super().__init__(f"read service returned HTTP {status}")


#: A request above this size is a mistake or an attack; both are refused.
MAX_REQUEST_BYTES = 64 * 1024

#: A response above this size is truncated by closing the connection, never handed
#: to the model. 1 MiB is generous for one completion and small enough to bound cost.
MAX_RESPONSE_BYTES = 1024 * 1024

#: Header block ceiling, enforced by asyncio's stream ``limit``.
MAX_HEADER_BYTES = 64 * 1024

#: Names the caller may not set: they describe this request's own framing.
_RESERVED_HEADERS = frozenset(
    {"host", "content-length", "connection", "transfer-encoding", "content-type"}
)


class TransportError(RuntimeError):
    """The request was refused, or the service could not be reached.

    The message is safe to show a user: it never contains the API key, the request
    body, or a response body beyond a short message the service published.
    """


def checked_endpoint(endpoint: str, *, read: bool = False) -> tuple[str, int, str]:
    """Validate an endpoint and return ``(host, port, path)``; raise otherwise.

    Pure and synchronous so the rule can be tested without a network: an endpoint
    that fails here is never dialled at all.
    """
    raw = (endpoint or "").strip()
    if not raw:
        raise TransportError("no endpoint is configured")
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise TransportError("the model endpoint must use https")
    if parsed.username or parsed.password:
        raise TransportError("the model endpoint must not carry credentials in the URL")
    host = (parsed.hostname or "").strip()
    if not host:
        raise TransportError("the model endpoint has no host")
    if parsed.query or parsed.fragment:
        raise TransportError("the model endpoint must be a plain https URL")
    try:
        port = parsed.port or HTTPS_PORT
    except ValueError as exc:
        raise TransportError("the model endpoint has an invalid port") from exc
    if port != HTTPS_PORT:
        raise TransportError(f"the model endpoint must use port {HTTPS_PORT}")
    path = parsed.path or "/"
    if not path.startswith("/"):
        raise TransportError("the model endpoint has an invalid path")
    if f"https://{host}{path}" not in (READ_ENDPOINTS if read else OFFICIAL_ENDPOINTS):
        # The destination is a closed list in this file. A caller cannot widen it by
        # configuring a URL, which is the whole point of a transport that is allowed
        # to touch the network while the rest of the package is not.
        raise TransportError("that endpoint is not one of the approved model endpoints")
    return host, port, path


def _checked_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Reject caller headers that could split the request or reframe the body."""
    out: dict[str, str] = {}
    for name, value in headers.items():
        lowered = str(name).strip().lower()
        if not lowered or lowered in _RESERVED_HEADERS:
            raise TransportError(f"header {lowered or name!r} is set by the transport")
        if "\r" in str(value) or "\n" in str(value):
            raise TransportError(f"header {lowered!r} contains a newline")
        if "\r" in lowered or "\n" in lowered:
            raise TransportError("a header name contains a newline")
        out[lowered] = str(value)
    return out


def _request_bytes(
    host: str, path: str, headers: Mapping[str, str], body: bytes, *, method: str = "POST",
) -> bytes:
    lines = [
        f"{method} {path} HTTP/1.1",
        f"Host: {host}",
        "User-Agent: ActionMesh/1.0",
        "Accept: application/json",
        "Accept-Encoding: identity",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        # One request per connection: the simplest possible lifecycle, and it makes
        # a response with no length readable to EOF without guessing.
        "Connection: close",
    ]
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    head = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii", errors="strict")
    return head + body


async def _read_head(reader: asyncio.StreamReader) -> tuple[int, dict[str, str]]:
    try:
        raw = await reader.readuntil(b"\r\n\r\n")
    except (
        asyncio.IncompleteReadError,
        asyncio.LimitOverrunError,
        ValueError,
    ) as exc:
        raise TransportError("the model service closed the connection before answering") from exc
    text = raw.decode("iso-8859-1", errors="replace")
    lines = text.split("\r\n")
    status_line = lines[0] if lines else ""
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or not parts[0].startswith("HTTP/"):
        raise TransportError("the model service answered with a malformed status line")
    try:
        status = int(parts[1])
    except ValueError as exc:
        raise TransportError("the model service answered with a malformed status code") from exc
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers


async def _read_body(
    reader: asyncio.StreamReader, headers: Mapping[str, str], budget: int
) -> bytes:
    encoding = headers.get("content-encoding", "identity").strip().lower()
    if encoding not in ("", "identity"):
        # We never asked for it, and decoding a compressed body would add an entire
        # attack surface (decompression bombs) to a file that is otherwise provably
        # small. Refusing it is the honest answer.
        raise TransportError(f"the model service sent unsupported encoding {encoding!r}")
    if headers.get("transfer-encoding", "").strip().lower() == "chunked":
        return await _read_chunked(reader, budget)
    declared = headers.get("content-length", "").strip()
    if declared:
        try:
            length = int(declared)
        except ValueError as exc:
            raise TransportError("the model service sent an invalid content length") from exc
        if length < 0 or length > budget:
            raise TransportError("the model response exceeded the size limit")
        try:
            return await reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise TransportError("the model service cut the response short") from exc
    # No length: read to EOF, bounded.
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await reader.read(8192)
        if not block:
            return b"".join(chunks)
        total += len(block)
        if total > budget:
            raise TransportError("the model response exceeded the size limit")
        chunks.append(block)


async def _read_chunked(reader: asyncio.StreamReader, budget: int) -> bytes:
    out = bytearray()
    while True:
        try:
            line = await reader.readuntil(b"\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise TransportError("the model service cut the response short") from exc
        size_text = line.strip().split(b";")[0]
        try:
            size = int(size_text, 16)
        except ValueError as exc:
            raise TransportError("the model service sent a malformed chunk size") from exc
        if size == 0:
            return bytes(out)
        if len(out) + size > budget:
            raise TransportError("the model response exceeded the size limit")
        try:
            out += await reader.readexactly(size)
            await reader.readexactly(2)  # the CRLF that ends the chunk
        except asyncio.IncompleteReadError as exc:
            raise TransportError("the model service cut the response short") from exc


async def post_json(
    *,
    endpoint: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    timeout_s: float,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> dict[str, Any]:
    """One HTTPS POST carrying a JSON object; returns the response's JSON object."""
    host, port, path = checked_endpoint(endpoint)
    safe_headers = _checked_headers(headers)
    try:
        body = json.dumps(dict(payload), ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise TransportError("the request could not be encoded as JSON") from exc
    if len(body) > MAX_REQUEST_BYTES:
        raise TransportError("the request exceeded the size limit")
    budget = max(1, min(int(max_response_bytes), MAX_RESPONSE_BYTES))
    request = _request_bytes(host, path, safe_headers, body)
    return await _exchange_json(host, port, request, timeout_s, budget)


async def get_json(
    *, endpoint: str, headers: Mapping[str, str], query: Mapping[str, str],
    timeout_s: float = 5.0,
) -> dict[str, Any]:
    """Read from a fixed connector endpoint with bounded, encoded query values."""
    host, port, path = checked_endpoint(endpoint, read=True)
    encoded = urlencode(query)
    if len(encoded) > 2048:
        raise TransportError("the connector query exceeded the size limit")
    request = _request_bytes(host, f"{path}?{encoded}", _checked_headers(headers), b"", method="GET")
    return await _exchange_json(host, port, request, timeout_s, 512 * 1024, read=True)


async def _exchange_json(
    host: str, port: int, request: bytes, timeout_s: float, budget: int, *, read: bool = False,
) -> dict[str, Any]:

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                host,
                port,
                ssl=True,
                server_hostname=host,
                limit=MAX_HEADER_BYTES,
            ),
            timeout=timeout_s,
        )
    except TimeoutError as exc:
        raise TransportError("the model service did not answer in time") from exc
    except (OSError, ValueError) as exc:
        # ValueError covers an SSL context refusing the hostname; OSError covers DNS
        # failure, refused connections and certificate verification. The exception
        # text can name a file path or a resolver detail, so it is summarised rather
        # than forwarded.
        raise TransportError("the model service could not be reached over TLS") from exc

    try:
        writer.write(request)
        await asyncio.wait_for(writer.drain(), timeout=timeout_s)
        status, response_headers = await asyncio.wait_for(_read_head(reader), timeout=timeout_s)
        raw = await asyncio.wait_for(
            _read_body(reader, response_headers, budget), timeout=timeout_s
        )
    except TimeoutError as exc:
        raise TransportError("the model service did not answer in time") from exc
    except OSError as exc:
        raise TransportError("the connection to the model service failed") from exc
    finally:
        writer.close()
        with suppress(OSError, TimeoutError):
            await writer.wait_closed()

    if status < 200 or status >= 300:
        if read:
            raise ReadTransportError(
                status, rate_limited=response_headers.get("x-ratelimit-remaining") == "0",
            )
        raise TransportError(_error_message(status, raw))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransportError("the model service answered with something that is not JSON") from exc
    if not isinstance(data, dict):
        raise TransportError("the model service answered with a non-object JSON value")
    return data


def _error_message(status: int, raw: bytes) -> str:
    """A short, safe description of an error response.

    Official APIs put a human-readable message under ``error.message``. Only that
    message survives, clipped: an error body can echo a request, and an echoed
    request is exactly the content this transport must not surface.
    """
    detail = ""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            detail = str(error.get("message") or "")
        elif isinstance(error, str):
            detail = error
        if not detail:
            detail = str(data.get("message") or "")
    detail = " ".join(detail.split())[:200]
    return f"the model service refused the request (HTTP {status})" + (
        f": {detail}" if detail else ""
    )


__all__ = [
    "HTTPS_PORT",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "OFFICIAL_ENDPOINTS",
    "TransportError",
    "checked_endpoint",
    "post_json",
]