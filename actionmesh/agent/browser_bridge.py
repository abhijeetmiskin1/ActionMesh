"""Style mirror of :mod:`actionmesh.voiceos.model_bridge` for the browser channel.

Calls there are blocking because the caller is a worker thread. Calls here are
coroutines because the browser driver is awaited on the event loop; everything
else is the same shape: one frame in, one frame out, both directions bounded,
and the per-launch token in the frame and in nothing else.

Two wire facts this module owns:

* The Python side sends a frame that has no token field (``encode`` builds the
  bare command); the frame written here carries ``token``. ``browser_bridge.js``
  answers ``{"ok": false, "detail": ...}`` for a bad token or an unknown op,
  *not* the model bridge's ``{"ok": false, "state": ...}`` triple, so an error
  reply is raised here with a fixed detail rather than handed to
  ``CommandResult.from_wire`` (which would read ``status`` as ``""`` and let a
  refusal look like a snapshot). Success replies from ``handleDrive`` carry
  ``status`` and no ``ok`` field, so ``ok is False`` — not "not True" — is the
  only error test.
* One connection per request, server closes after the reply. ``readline`` is
  bounded by an explicit length check against :data:`MAX_FRAME_BYTES` after
  every read, because a peer that never says newline must not grow the buffer
  without limit.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from actionmesh.agent.browser_ipc import MAX_FRAME_BYTES

#: Where main wrote the drive socket and its per-launch token. Mirrors the
#: backend's env names; a half-filled pair is "not configured", never an error.
ENV_BROWSER_BRIDGE_SOCKET = "ACTIONMESH_BROWSER_BRIDGE_SOCKET"
ENV_BROWSER_BRIDGE_TOKEN = "ACTIONMESH_BROWSER_BRIDGE_TOKEN"

#: Longer than the window's own 30 s idle ceiling is useless; a drive that has
#: not answered by then is a bridge that stopped serving.
REPLY_TIMEOUT_S = 30.0


class BrowserBridgeUnavailable(RuntimeError):
    """The socket could not be reached. The message names the path only."""


class BrowserBridgeError(RuntimeError):
    """The bridge answered ``{"ok": false}`` with a fixed, token-free detail."""

    def __init__(self, detail: str = "") -> None:
        self.detail = str(detail or "the browser bridge declined the request")
        super().__init__(self.detail)


@dataclass(frozen=True)
class BrowserBridgeConfig:
    socket_path: str
    token: str
    timeout_s: float = REPLY_TIMEOUT_S

    @property
    def configured(self) -> bool:
        return bool(self.socket_path.strip()) and bool(self.token.strip())


def browser_bridge_config_from_env(
    environ: dict[str, str] | None = None,
) -> BrowserBridgeConfig:
    """The drive socket, from the child's environment, or an unconfigured value.

    Same contract as :func:`actionmesh.voiceos.llm.bridge_config_from_env`:
    both names are written by Electron main at spawn; a missing pair yields
    ``configured == False`` and the caller falls back to ``FakeDriver``.
    """
    import os

    env = os.environ if environ is None else environ
    return BrowserBridgeConfig(
        socket_path=(env.get(ENV_BROWSER_BRIDGE_SOCKET) or "").strip(),
        token=(env.get(ENV_BROWSER_BRIDGE_TOKEN) or "").strip(),
    )


def _unavailable(config: BrowserBridgeConfig, cause: BaseException) -> BrowserBridgeUnavailable:
    path = (config.socket_path or "").strip()
    if isinstance(cause, FileNotFoundError):
        what = "the browser bridge socket does not exist"
    elif isinstance(cause, ConnectionRefusedError):
        what = "the browser bridge refused the connection"
    elif isinstance(cause, TimeoutError):
        what = "the browser bridge did not answer in time"
    elif isinstance(cause, PermissionError):
        what = "the browser bridge socket is not reachable by this user"
    else:
        what = "the browser bridge socket could not be reached"
    return BrowserBridgeUnavailable(f"{what} ({path})" if path else what)


async def _exchange(config: BrowserBridgeConfig, frame: dict[str, Any]) -> dict[str, Any]:
    """Write one frame, read one reply line, close. The only place bytes move."""
    try:
        body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise BrowserBridgeError("the drive frame could not be encoded") from exc
    if len(body) > MAX_FRAME_BYTES:
        raise BrowserBridgeError("the drive frame exceeded the size limit")

    path = (config.socket_path or "").strip()
    if not path:
        raise BrowserBridgeUnavailable("no browser bridge socket is configured")

    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(path),
            timeout=config.timeout_s,
        )
    except (FileNotFoundError, ConnectionRefusedError, TimeoutError, PermissionError, OSError) as exc:
        raise _unavailable(config, exc) from exc

    try:
        writer.write(body + b"\n")
        await writer.drain()
        raw = await asyncio.wait_for(reader.readline(), timeout=config.timeout_s)
    except (TimeoutError, OSError) as exc:
        raise _unavailable(config, exc) from exc
    finally:
        with suppress(Exception):
            writer.close()

    if not raw or not raw.endswith(b"\n"):
        # EOF before the newline (``readline`` only returns unterminated bytes
        # on EOF): the peer stopped mid-reply.
        raise BrowserBridgeError("the browser bridge closed the connection without completing a reply")
    if len(raw) > MAX_FRAME_BYTES + 1:
        raise BrowserBridgeError("the browser bridge reply exceeded the size limit")
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BrowserBridgeError("the browser bridge answered with something that is not JSON") from exc
    if not isinstance(answer, dict):
        raise BrowserBridgeError("the browser bridge answered with a non-object JSON value")
    if answer.get("ok") is False:
        raise BrowserBridgeError(str(answer.get("detail") or "the browser bridge declined the request"))
    return answer


def make_request(config: BrowserBridgeConfig) -> Any:
    """The ``request`` callable :class:`BridgeDriver` expects.

    ``BridgeDriver._drive`` hands this an encoded bare command line. The token
    is injected here — the only place it is written — the frame is written,
    and the parsed reply dict is returned; error replies raise.
    """

    async def request(line: str) -> dict[str, Any]:
        try:
            frame = json.loads(line)
        except (TypeError, json.JSONDecodeError) as exc:
            raise BrowserBridgeError("the drive frame could not be encoded") from exc
        if not isinstance(frame, dict):
            raise BrowserBridgeError("the drive frame was not a JSON object")
        frame["token"] = config.token
        return await _exchange(config, frame)

    return request


__all__ = [
    "ENV_BROWSER_BRIDGE_SOCKET",
    "ENV_BROWSER_BRIDGE_TOKEN",
    "BrowserBridgeConfig",
    "BrowserBridgeError",
    "BrowserBridgeUnavailable",
    "browser_bridge_config_from_env",
    "make_request",
]
