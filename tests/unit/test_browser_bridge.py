"""The browser drive bridge client: wire shape over a real socket.

**The shape, not the product.** These tests speak the exact bytes
``desktop/app/browser_bridge.js`` speaks — one newline-delimited JSON frame in
with a ``token`` field, one reply line back — against a real ``asyncio``
Unix-socket server in a tmp directory. The client under test knows nothing of
Electron; it injects the token, reads one line, and must turn an ``{ok: false}``
refusal into a raised error rather than a blank ``status``.

Env-only contract: the socket path and token arrive in the child's environment
(``ACTIONMESH_BROWSER_BRIDGE_SOCKET`` / ``..._TOKEN``), written by Electron
main at spawn. No file, no argv, no credential.
"""

from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from actionmesh.agent.browser_bridge import (
    BrowserBridgeConfig,
    BrowserBridgeError,
    BrowserBridgeUnavailable,
    browser_bridge_config_from_env,
    make_request,
)

pytestmark = pytest.mark.anyio


@pytest.fixture()
def anyio_backend() -> str:
    return "asyncio"


def _config(tmp_path, token: str = "test-token-32-bytes") -> BrowserBridgeConfig:
    return BrowserBridgeConfig(socket_path=str(tmp_path / "browser-bridge.sock"), token=token)


async def _run_server(sock_path: str, respond):
    """Start a one-connection server that reads one frame line and replies
    whatever ``respond`` (frame -> reply dict) returns, verbatim."""
    seen: list[dict] = []

    async def _on_client(reader, writer):
        raw = await reader.readline()
        frame = json.loads(raw.decode("utf-8"))
        seen.append(frame)
        reply = await respond(frame) if inspect.iscoroutinefunction(respond) else respond(frame)
        writer.write(json.dumps(reply).encode("utf-8") + b"\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_on_client, path=sock_path)
    return server, seen


async def test_env_reading_round_trips(tmp_path) -> None:
    env = {
        "ACTIONMESH_BROWSER_BRIDGE_SOCKET": str(tmp_path / "x.sock"),
        "ACTIONMESH_BROWSER_BRIDGE_TOKEN": "t0ken",
    }
    config = browser_bridge_config_from_env(env)
    assert config.socket_path == str(tmp_path / "x.sock")
    assert config.token == "t0ken"
    assert config.configured

    assert not browser_bridge_config_from_env({}).configured
    assert not browser_bridge_config_from_env({"ACTIONMESH_BROWSER_BRIDGE_SOCKET": "/x"}).configured


async def test_success_reply_passes_through_and_token_is_injected(tmp_path) -> None:
    config = _config(tmp_path)
    server, seen = await _run_server(
        config.socket_path,
        lambda frame: {"request_id": "open", "status": "ok", "url": "https://example.test/", "title": "Example"},
    )
    try:
        reply = await make_request(config)('{"op":"open","request_id":"open","url":"https://example.test/"}')
    finally:
        server.close()
        await server.wait_closed()

    assert reply["status"] == "ok"
    assert seen[0]["token"] == "test-token-32-bytes"
    assert seen[0]["op"] == "open"


async def test_error_reply_raises_with_fixed_detail(tmp_path) -> None:
    config = _config(tmp_path)
    server, _seen = await _run_server(
        config.socket_path,
        lambda frame: {"ok": False, "detail": "The request token was not accepted."},
    )
    try:
        with pytest.raises(BrowserBridgeError, match="token was not accepted"):
            await make_request(config)('{"op":"open","request_id":"open","url":"https://example.test/"}')
    finally:
        server.close()
        await server.wait_closed()


async def test_absent_socket_is_unavailable(tmp_path) -> None:
    config = _config(tmp_path)
    with pytest.raises(BrowserBridgeUnavailable):
        await make_request(config)('{"op":"open","request_id":"open","url":"https://example.test/"}')


async def test_non_json_reply_raises(tmp_path) -> None:
    config = _config(tmp_path)

    async def _garble(reader, writer):
        await reader.readline()
        writer.write(b"not json\n")
        await writer.drain()
        writer.close()

    server = await asyncio.start_unix_server(_garble, path=config.socket_path)
    try:
        with pytest.raises(BrowserBridgeError):
            await make_request(config)('{"op":"open","request_id":"open","url":"https://example.test/"}')
    finally:
        server.close()
        await server.wait_closed()


async def test_provider_wires_bridge_driver_when_configured(tmp_path) -> None:
    from actionmesh.agent.provider import LaptopProvider

    config = _config(tmp_path)
    provider = LaptopProvider(browser_bridge=config)
    assert type(provider.browser._driver).__name__ == "BridgeDriver"

    bare = LaptopProvider()
    assert type(bare.browser._driver).__name__ == "FakeDriver"

    # A half-filled pair is "not configured", never a dead dial.
    half = LaptopProvider(browser_bridge=BrowserBridgeConfig(socket_path=str(tmp_path / "x.sock"), token=""))
    assert type(half.browser._driver).__name__ == "FakeDriver"

async def test_bridge_driver_snapshot_records_carry_driver_id(tmp_path) -> None:
    """The driver contract promises snapshot records keyed by ``id`` (the
    driver-local element id the engine maps opaque refs onto). The IPC wire
    reports the window-local stamp as ``ref`` instead; ``BridgeDriver`` must
    rename it. Regression test: this was the bug where every record landed
    without an ``id``, so ``BrowserEngine._rebuild_refs`` dropped them all
    and snapshot/inspect reported an empty page (live Zomato returned 67
    raw records but ref_map_size 0)."""
    from actionmesh.agent.browser import BridgeDriver

    server, _seen = await _run_server(
        str(tmp_path / "browser-bridge.sock"),
        lambda _frame: {
            "status": "ok",
            "request_id": "snapshot",
            "epoch": 4,
            "elements": [
                {"ref": 1, "role": "button", "name": "Order on app", "tag": "button"},
                {"ref": 2, "role": "link", "name": "Pizza", "tag": "a", "href_host": "zomato.com"},
            ],
        },
    )
    try:
        drv = BridgeDriver(make_request(_config(tmp_path)))
        _epoch, records = await drv.snapshot_elements(200)
    finally:
        server.close()
        await server.wait_closed()
    assert [r["id"] for r in records] == [1, 2]
    assert all("ref" not in r for r in records)


async def test_bridge_driver_type_forwards_witnessed_inserted(tmp_path) -> None:
    """The ``inserted`` count the window captured around the write must reach the
    driver verbatim, and an unwitnessed reply stays ``None`` — never a 0, which
    teams with ``changed: true`` to read as a confirmed zero insertion."""
    from actionmesh.agent.browser import BridgeDriver

    for reply_inserted, expected in (
        (5, 5),
        (None, None),
    ):
        server, _seen = await _run_server(
            str(tmp_path / "browser-bridge.sock"),
            lambda _frame, _reply=reply_inserted: {
                "status": "ok",
                "request_id": "type",
                "epoch": 4,
                "changed": True,
                **({} if _reply is None else {"inserted": _reply}),
            },
        )
        try:
            drv = BridgeDriver(make_request(_config(tmp_path)))
            outcome = await drv.type_text(7, "Hello")
        finally:
            server.close()
            await server.wait_closed()
        assert outcome["inserted"] == expected

async def test_engine_snapshot_counts_bridge_elements(tmp_path) -> None:
    """End to end through the engine seam: a snapshot reply whose elements
    carry the window-local stamp must still yield a non-empty ref map."""
    from actionmesh.agent.browser import BridgeDriver, BrowserEngine

    async def _respond(frame):
        op = frame.get("op")
        if op == "open":
            return {"status": "ok", "request_id": "open", "epoch": 1,
                    "url": "https://example.test/", "title": "Example"}
        if op == "snapshot":
            return {"status": "ok", "request_id": "snapshot", "epoch": 1,
                    "elements": [
                        {"ref": 1, "role": "button", "name": "Order", "tag": "button"},
                        {"ref": 2, "role": "link", "name": "Pizza", "tag": "a"},
                    ]}
        if op == "state":
            return {"status": "ok", "request_id": "state", "epoch": 1,
                    "url": "https://example.test/", "title": "Example"}
        return {"status": "error", "request_id": op}

    server, _seen = await _run_server(str(tmp_path / "browser-bridge.sock"), _respond)
    try:
        drv = BridgeDriver(make_request(_config(tmp_path)))
        engine = BrowserEngine(drv)
        opened = await engine.open("https://example.test/")
        assert opened["status"] == "ok"
        assert opened["ref_map_size"] == 2
        snap = await engine.snapshot()
        assert snap["status"] == "ok"
        assert snap["count"] == 2
    finally:
        server.close()
        await server.wait_closed()
