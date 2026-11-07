"""The client half of the C41 model bridge: a socket, not a credential.

VoiceOS plans in a Python process. The provider credential lives in the Electron main
process's keyring and must never reach that Python process. So the Python side asks
main to make the call over a private Unix-domain socket, and receives back only the
model's answer — which :func:`actionmesh.voiceos.llm.validate_proposal` then treats as
untrusted input, exactly as it treats every other model answer.

That is the whole security story of this file, and it is why the module is this small:

* **One frame in, one frame out.** Newline-delimited JSON, one connection per request,
  the server closes. There is no request pipelining, no retry loop and no way for a
  response to steer a second call.
* **Both directions are bounded.** A request above :data:`MAX_FRAME_BYTES` is refused
  before it is written; a response is accumulated only up to the same ceiling. A peer
  that keeps talking is disconnected rather than buffered.
* **Blocking, with a deadline.** ``socket.settimeout`` on connect and on every read. The
  callers already run this off the event loop (``LlmPlanner.plan`` is invoked in a
  worker thread), so ``asyncio`` here would be machinery without a purpose.
* **The token is a capability, not a secret this module owns.** It arrives in the
  child's environment, is written into the request frame, and appears in no exception,
  no log line and no return value. An operator reading a failure learns *that* the
  bridge refused, never *what* it refused with.

This module deliberately does **not** touch
:mod:`actionmesh.voiceos.transport`. That module's job is the one approved outbound
HTTPS call; this one reaches a socket in the user's own profile directory, and the
network call itself happens in main. Adding an endpoint to the transport's closed list
would be the wrong shape entirely.
"""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass
from typing import Any

#: The server's own ceiling (contract §3). A frame larger than this is a mistake or an
#: attack; the connection is dropped rather than the bytes buffered.
MAX_FRAME_BYTES = 256 * 1024

#: How much one ``recv`` may return. Any positive size works; this one is small enough
#: that the ceiling is checked often and large enough not to spin.
_RECV_CHUNK = 8192


class BridgeUnavailable(RuntimeError):
    """The bridge is not running, or the socket could not be reached.

    The message is safe to show a user: it names the socket path and the kind of
    failure, never the token, never a request or response body.
    """


class BridgeError(RuntimeError):
    """The bridge answered ``{"ok": false}``, or answered in a shape it must not.

    ``state`` is the server's machine-readable reason (contract §3: one of the same
    eight values ``providers.generate`` reports); ``detail`` is the sentence it chose
    to make public. Neither carries key material.
    """

    def __init__(self, state: str, detail: str = "") -> None:
        self.state = str(state or "provider_unavailable")
        self.detail = str(detail or "")
        super().__init__(f"the model bridge declined the request: {self.state}"
                         + (f" ({self.detail})" if self.detail else ""))


@dataclass(frozen=True)
class BridgeConfig:
    """Where the bridge listens and what authorises one conversation with it.

    Both values come from the child process's environment, written by the Electron main
    process at spawn time. ``configured`` is the single question the builders ask: a
    half-filled pair is not a configuration, it is a mistake, and treating it as "off"
    keeps the deterministic planner answering with a reason instead of dialling a path
    that cannot work.
    """

    socket_path: str
    token: str
    timeout_s: float = 20.0

    @property
    def configured(self) -> bool:
        return bool(self.socket_path.strip()) and bool(self.token.strip())


def _unavailable(socket_path: str, cause: BaseException | None = None) -> BridgeUnavailable:
    """Classify a transport failure without forwarding its possibly-noisy text."""
    if isinstance(cause, FileNotFoundError):
        what = "the bridge socket does not exist"
    elif isinstance(cause, ConnectionRefusedError):
        what = "the bridge refused the connection"
    elif isinstance(cause, TimeoutError):
        what = "the bridge did not answer in time"
    elif isinstance(cause, PermissionError):
        what = "the bridge socket is not reachable by this user"
    else:
        what = "the bridge socket could not be reached"
    return BridgeUnavailable(f"{what} ({socket_path})" if socket_path else what)


def _connect(config: BridgeConfig, timeout_s: float) -> socket.socket:
    path = (config.socket_path or "").strip()
    if not path:
        raise BridgeUnavailable("no model bridge socket is configured")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(timeout_s)
        sock.connect(path)
    except OSError as exc:
        sock.close()
        raise _unavailable(path, exc) from exc
    return sock


def _exchange(config: BridgeConfig, frame: dict[str, Any], timeout_s: float) -> dict[str, Any]:
    """Write one frame, read one frame, close. The only place bytes move."""
    try:
        body = json.dumps(frame, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        # Not a transport problem and not the peer's fault: the caller handed this
        # module something that is not JSON, and that is a programming error.
        raise BridgeError("malformed", "the request could not be encoded") from exc
    if len(body) > MAX_FRAME_BYTES:
        raise BridgeError("malformed", "the request exceeded the size limit")

    sock = _connect(config, timeout_s)
    try:
        try:
            sock.sendall(body + b"\n")
        except OSError as exc:
            raise _unavailable((config.socket_path or "").strip(), exc) from exc

        chunks: list[bytes] = []
        total = 0
        while True:
            try:
                chunk = sock.recv(_RECV_CHUNK)
            except TimeoutError as exc:
                raise _unavailable((config.socket_path or "").strip(), exc) from exc
            except OSError as exc:
                raise _unavailable((config.socket_path or "").strip(), exc) from exc
            if not chunk:
                # EOF before the newline: the peer stopped mid-frame. Half an answer
                # is not an answer, and guessing at it is the "looks like it worked"
                # shorthand this module exists to avoid.
                raise BridgeError(
                    "malformed", "the bridge closed the connection without completing a response"
                )
            total += len(chunk)
            if total > MAX_FRAME_BYTES:
                raise BridgeError("malformed", "the response exceeded the size limit")
            newline = chunk.find(b"\n")
            if newline >= 0:
                chunks.append(chunk[:newline])
                break
            chunks.append(chunk)
    finally:
        sock.close()

    raw = b"".join(chunks)
    try:
        answer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BridgeError("malformed", "the bridge answered with something that is not JSON") from exc
    if not isinstance(answer, dict):
        raise BridgeError("malformed", "the bridge answered with a non-object JSON value")
    if not answer.get("ok"):
        raise BridgeError(
            answer.get("state") or "provider_unavailable", str(answer.get("detail") or "")
        )
    return answer


def generate(
    config: BridgeConfig,
    *,
    purpose: str,
    system: str,
    user: str,
    timeout_s: float | None = None,
) -> dict[str, Any]:
    """Ask the bridge to make one model call; return the parsed ``content`` object.

    ``purpose`` is ``"plan"`` or ``"diagnose"`` (contract §1). The returned mapping is
    the model's answer **verbatim** — this function does not validate, filter or
    reshape it. Authority lives in ``validate_proposal``, and keeping the untrusted
    shape intact is what makes that validator testable.
    """
    timeout = config.timeout_s if timeout_s is None else timeout_s
    frame = {
        "token": config.token,
        "op": "generate",
        "purpose": purpose,
        "system": system,
        "user": user,
        "timeout_s": timeout,
    }
    answer = _exchange(config, frame, timeout)
    content = answer.get("content")
    if not isinstance(content, dict):
        # `content` is present only when ok is true, and is an object by contract. A
        # bridge that says ok without one is malformed, and passing `None` upward
        # would turn a protocol violation into "the model proposed nothing".
        raise BridgeError("malformed", "the bridge answered ok without a content object")
    return content


def status(config: BridgeConfig, *, timeout_s: float | None = None) -> dict[str, Any]:
    """Ask the bridge what it is configured with; return its status frame.

    The answer is *reported*, not assumed: ``state`` is the last verified state, and
    ``hasKey`` is a boolean — no key material ever appears in it.
    """
    timeout = config.timeout_s if timeout_s is None else timeout_s
    return _exchange(config, {"token": config.token, "op": "status"}, timeout)


MAX_WEB_AUDIO_BYTES = 128 * 1024


def audio_status(config: BridgeConfig) -> dict[str, Any]:
    answer = _exchange(config, {"token": config.token, "op": "audio_status"}, 5.0)
    return {"configured": answer.get("configured") is True, "max_bytes": MAX_WEB_AUDIO_BYTES}


def transcribe(config: BridgeConfig, audio: bytes) -> str:
    if not audio or len(audio) > MAX_WEB_AUDIO_BYTES:
        raise BridgeError("malformed", "the recording is empty or too large")
    answer = _exchange(config, {"token": config.token, "op": "transcribe",
        "audio": base64.b64encode(audio).decode("ascii")}, 60.0)
    text = answer.get("text")
    if not isinstance(text, str) or len(text) > 16000:
        raise BridgeError("malformed", "the bridge returned an invalid transcript")
    return text.strip()


__all__ = [
    "MAX_FRAME_BYTES",
    "MAX_WEB_AUDIO_BYTES",
    "BridgeConfig",
    "BridgeError",
    "BridgeUnavailable",
    "audio_status",
    "generate",
    "status",
    "transcribe",
]