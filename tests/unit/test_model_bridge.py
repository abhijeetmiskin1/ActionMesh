"""C41 — the bridge client: one frame in, one frame out, and no token anywhere.

These tests run against a real Unix-socket server in a thread rather than a mock. The
client's whole job is wire behaviour — framing, bounds, deadlines, EOF — and a mocked
``socket`` object would let every one of those be wrong while the tests passed.

What they pin:

* the request frame is what the contract says it is (op, purpose, token);
* only a newline-terminated frame is a frame, and only one is read;
* an ``{ok:false}`` answer becomes a :class:`BridgeError` carrying its state;
* a missing socket, a silent peer and an oversized reply all fail *quickly*, bounded
  either by the timeout or by the size ceiling — never by buffering;
* the token appears in no exception text and no return value.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from actionmesh.voiceos.model_bridge import (
    MAX_FRAME_BYTES,
    BridgeConfig,
    BridgeError,
    BridgeUnavailable,
    generate,
    status,
)

TOKEN = "per-launch-token-not-a-key"


class _Server:
    """A one-connection-at-a-time Unix-socket server that answers with a callable.

    ``responder(raw_request_bytes) -> bytes`` is run in a thread per connection; it may
    return ``None`` to mean "close without answering", which is how the truncated and
    silent-peer cases are produced. Every received frame is recorded so a test can
    assert what the client actually wrote.
    """

    def __init__(
        self,
        responder: Callable[[bytes], bytes | None],
        *,
        hold: bool = False,
    ) -> None:
        self._responder = responder
        # ``hold`` keeps the connection open and silent after the request: the case
        # where the peer is alive but mute, which only the client's timeout can end.
        self._hold = hold
        self.received: list[bytes] = []
        self.path = str(Path(tempfile.mkdtemp(prefix="bridge-")) / "provider-bridge.sock")
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(4)
        self._sock.settimeout(0.2)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            with conn:
                chunks: list[bytes] = []
                while True:
                    try:
                        chunk = conn.recv(8192)
                    except OSError:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if b"\n" in chunk:
                        break
                raw = b"".join(chunks)
                self.received.append(raw)
                answer = self._responder(raw)
                if answer is not None:
                    with suppress(OSError):
                        conn.sendall(answer)
                elif self._hold:
                    # Stay open and say nothing until the client gives up on us.
                    with suppress(OSError):
                        conn.recv(1)

    def close(self) -> None:
        self._stop.set()
        self._sock.close()
        self._thread.join(timeout=2)
        with suppress(OSError):
            os.unlink(self.path)


def _ok(content: object) -> bytes:
    return json.dumps({"ok": True, "content": content}).encode("utf-8") + b"\n"


class TestGenerateRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self.content = {"actions": [{"type": "device.process.list", "parameters": {}}]}
        self.server = _Server(lambda _raw: _ok(self.content))
        self.config = BridgeConfig(socket_path=self.server.path, token=TOKEN, timeout_s=5.0)

    def tearDown(self) -> None:
        self.server.close()

    def test_generate_returns_the_content_object(self) -> None:
        self.assertEqual(
            generate(self.config, purpose="plan", system="s", user="u"), self.content
        )

    def test_the_request_frame_is_the_contract_shape(self) -> None:
        generate(self.config, purpose="plan", system="SYSTEM", user="USER", timeout_s=7.0)
        frame = json.loads(self.server.received[0].decode("utf-8"))
        self.assertEqual(frame["token"], TOKEN)
        self.assertEqual(frame["op"], "generate")
        self.assertEqual(frame["purpose"], "plan")
        self.assertEqual(frame["system"], "SYSTEM")
        self.assertEqual(frame["user"], "USER")
        self.assertEqual(frame["timeout_s"], 7.0)

    def test_the_frame_is_newline_terminated_and_read_once(self) -> None:
        generate(self.config, purpose="plan", system="s", user="u")
        self.assertEqual(len(self.server.received), 1)
        self.assertTrue(self.server.received[0].endswith(b"\n"))

    def test_status_round_trips(self) -> None:
        answer = {"ok": True, "configured": True, "provider": "desktop", "hasKey": True}
        server = _Server(lambda _raw: json.dumps(answer).encode("utf-8") + b"\n")
        try:
            config = BridgeConfig(socket_path=server.path, token=TOKEN, timeout_s=5.0)
            self.assertEqual(status(config), answer)
            frame = json.loads(server.received[0].decode("utf-8"))
            self.assertEqual(frame, {"token": TOKEN, "op": "status"})
        finally:
            server.close()


class TestFailureIsHonest(unittest.TestCase):
    def _config(self, path: str, timeout_s: float = 2.0) -> BridgeConfig:
        return BridgeConfig(socket_path=path, token=TOKEN, timeout_s=timeout_s)

    def test_a_refused_generation_carries_its_state(self) -> None:
        server = _Server(
            lambda _raw: json.dumps(
                {"ok": False, "state": "missing_key", "detail": "No API key is stored."}
            ).encode("utf-8")
            + b"\n"
        )
        try:
            with self.assertRaises(BridgeError) as raised:
                generate(self._config(server.path), purpose="plan", system="s", user="u")
            self.assertEqual(raised.exception.state, "missing_key")
            self.assertIn("No API key is stored.", raised.exception.detail)
        finally:
            server.close()

    def test_a_missing_socket_is_unavailable_not_an_answer(self) -> None:
        path = str(Path(tempfile.mkdtemp()) / "absent.sock")
        with self.assertRaises(BridgeUnavailable):
            generate(self._config(path), purpose="plan", system="s", user="u")

    def test_a_truncated_response_does_not_hang(self) -> None:
        # Half a frame, no newline, then EOF.
        server = _Server(lambda _raw: b'{"ok": true, "content": {"actions": [')
        started = time.monotonic()
        try:
            with self.assertRaises((BridgeUnavailable, BridgeError)):
                generate(self._config(server.path), purpose="plan", system="s", user="u")
        finally:
            server.close()
        self.assertLess(time.monotonic() - started, 3.0, "the client must not wait on EOF")

    def test_a_silent_peer_times_out(self) -> None:
        server = _Server(lambda _raw: None, hold=True)
        started = time.monotonic()
        try:
            with self.assertRaises(BridgeUnavailable):
                generate(
                    self._config(server.path, timeout_s=0.5),
                    purpose="plan",
                    system="s",
                    user="u",
                )
        finally:
            server.close()
        self.assertLess(time.monotonic() - started, 3.0)

    def test_an_oversized_response_is_refused(self) -> None:
        huge = b'{"ok": true, "content": {"actions": ["' + b"x" * (MAX_FRAME_BYTES + 1024)
        server = _Server(lambda _raw: huge + b'"]}}\n')
        try:
            with self.assertRaises(BridgeError) as raised:
                generate(self._config(server.path), purpose="plan", system="s", user="u")
            self.assertIn("size limit", str(raised.exception))
        finally:
            server.close()

    def test_non_json_is_malformed(self) -> None:
        server = _Server(lambda _raw: b"<html>not json</html>\n")
        try:
            with self.assertRaises(BridgeError) as raised:
                generate(self._config(server.path), purpose="plan", system="s", user="u")
            self.assertEqual(raised.exception.state, "malformed")
        finally:
            server.close()

    def test_ok_without_content_is_malformed_not_an_empty_proposal(self) -> None:
        server = _Server(lambda _raw: b'{"ok": true}\n')
        try:
            with self.assertRaises(BridgeError) as raised:
                generate(self._config(server.path), purpose="plan", system="s", user="u")
            self.assertEqual(raised.exception.state, "malformed")
        finally:
            server.close()

    def test_an_unconfigured_bridge_never_dials(self) -> None:
        with self.assertRaises(BridgeUnavailable):
            generate(BridgeConfig(socket_path="", token=""), purpose="plan", system="s", user="u")


class TestTheTokenNeverEscapes(unittest.TestCase):
    """The token authorises one socket conversation. It is not observable anywhere else."""

    def test_the_token_is_in_neither_an_error_nor_a_return_value(self) -> None:
        content = {"finding": "disk full", "confidence": "high"}
        server = _Server(lambda _raw: _ok(content))
        try:
            config = BridgeConfig(socket_path=server.path, token=TOKEN, timeout_s=5.0)
            answer = generate(config, purpose="diagnose", system="s", user="u")
            self.assertNotIn(TOKEN, json.dumps(answer))

            refused = _Server(
                lambda _raw: json.dumps(
                    {"ok": False, "state": "provider_unavailable", "detail": "down"}
                ).encode("utf-8")
                + b"\n"
            )
            try:
                bad = BridgeConfig(socket_path=refused.path, token=TOKEN, timeout_s=5.0)
                with self.assertRaises(BridgeError) as raised:
                    generate(bad, purpose="plan", system="s", user="u")
                self.assertNotIn(TOKEN, str(raised.exception))
                self.assertNotIn(TOKEN, raised.exception.detail)
            finally:
                refused.close()
        finally:
            server.close()

    def test_an_unreachable_bridge_error_does_not_name_the_token(self) -> None:
        path = str(Path(tempfile.mkdtemp()) / "absent.sock")
        with self.assertRaises(BridgeUnavailable) as raised:
            generate(
                BridgeConfig(socket_path=path, token=TOKEN),
                purpose="plan",
                system="s",
                user="u",
            )
        self.assertNotIn(TOKEN, str(raised.exception))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()