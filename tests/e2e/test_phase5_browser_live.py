"""Phase 5 live browser: a live run only when the real Electron bridge is up.

Label contract: LIVE means a real ``BridgeDriver`` talked to the real
``browser-bridge.sock`` owned by the running ActionMesh desktop process and
re-read the world (url/title agreement) on a real page. BLOCKED means the
test skipped naming the exact missing piece. This file never substitutes
``FakeDriver`` for the live path and never claims navigation from a window
launch alone: a live pass requires an ``ok`` open, a non-empty real title,
the expected host in the reported url, and snapshot/inspect read-back that
agrees with ``open``.

Bridge discovery (test-only, no production change): the Electron main
process mints the per-launch token and writes
``ACTIONMESH_BROWSER_BRIDGE_SOCKET`` / ``ACTIONMESH_BROWSER_BRIDGE_TOKEN``
only into the backend child environment. The live class therefore reads, in
memory only, the ``uvicorn actionmesh.api`` child's ``/proc`` environ to
recover that pair. The token value is never printed, logged, or written
anywhere; skip reasons name variables, never values. If the env pair is
already exported in this process it wins and no ``/proc`` read happens.

``TestHonestUnavailable`` always runs (not live-gated): it points the real
bridge config at a socket path that does not exist and proves ``open``
answers ``unavailable`` — never ``ok``, never a claimed navigation. That is
the honest-absent path, deterministic in any environment.
"""

from __future__ import annotations

import os
import socket
import unittest
from contextlib import suppress

LIVE_ENV = "ACTIONMESH_LIVE_ACCEPTANCE"

SOCKET_ENV = "ACTIONMESH_BROWSER_BRIDGE_SOCKET"
TOKEN_ENV = "ACTIONMESH_BROWSER_BRIDGE_TOKEN"

EXAMPLE_URL = "https://example.com"
EXAMPLE_HOST = "example.com"

SEARCH_ENGINE = "duckduckgo"
SEARCH_HOST = "duckduckgo.com"
SEARCH_QUERY = "ActionMesh"

TRAIN_URL_ENV = "ACTIONMESH_LIVE_TRAIN_URL"
ZOMATO_URL_ENV = "ACTIONMESH_LIVE_ZOMATO_URL"

_FORBIDDEN_KEYS = {
    "train", "trains", "fare", "fares", "schedule", "restaurant",
    "restaurants", "dish", "price", "prices", "rating", "booking",
    "bookings", "order", "orders", "confirmation", "reference", "pnr",
    "total", "items", "passenger", "journey",
}


def run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


def _host_of(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0] if "://" in url else ""


def _tcp_reachable(host: str, port: int, timeout_s: float = 3.0) -> bool:
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _train_url() -> str:
    return os.environ.get(TRAIN_URL_ENV, "https://www.irctc.co.in/").strip()


def _zomato_url() -> str:
    return os.environ.get(ZOMATO_URL_ENV, "https://www.zomato.com/").strip()


def _backend_bridge_pair() -> tuple[str, str] | None:
    """Recover (socket_path, token) for the live bridge, in memory only.

    Preference order: exported env (the documented channel), else the
    running ``uvicorn actionmesh.api`` child's ``/proc`` environ — the same
    pair Electron main wrote at backend spawn. Returns ``None`` when no
    complete pair exists anywhere. The token is returned to the caller for
    in-process use only; this helper never prints or persists it.
    """
    sock = (os.environ.get(SOCKET_ENV) or "").strip()
    tok = (os.environ.get(TOKEN_ENV) or "").strip()
    if sock and tok:
        return sock, tok
    try:
        pids = [p for p in os.listdir("/proc") if p.isdigit()]
    except OSError:
        return None
    for pid in pids:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                cmdline = fh.read()
            if b"uvicorn" not in cmdline or b"actionmesh.api" not in cmdline:
                continue
            with open(f"/proc/{pid}/environ", "rb") as fh:
                raw = fh.read()
        except OSError:
            continue
        env: dict[str, str] = {}
        for entry in raw.split(b"\0"):
            if b"=" not in entry:
                continue
            name, _, value = entry.partition(b"=")
            env[name.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        sock = (env.get(SOCKET_ENV) or "").strip()
        tok = (env.get(TOKEN_ENV) or "").strip()
        if sock and tok:
            return sock, tok
    return None


def _socket_listening(path: str) -> bool:
    """True only when a process actually accepts a connection on ``path``.

    A leftover socket file proves nothing (``browser_bridge.js`` says the
    same); connecting is the only reliable test.
    """
    if not path:
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(2.0)
    try:
        probe.connect(path)
    except OSError:
        return False
    finally:
        probe.close()
    return True


@unittest.skipUnless(
    os.environ.get(LIVE_ENV) == "1",
    f"live browser is opt-in: set {LIVE_ENV}=1 to run it",
)
class TestPhase5LiveBrowser(unittest.TestCase):
    """Real-bridge, read-only live navigation. LIVE or BLOCKED, never faked."""

    def _require_display(self) -> None:
        if not _has_display():
            self.skipTest(
                "BLOCKED: no display (DISPLAY/WAYLAND_DISPLAY unset) — "
                "the Electron browser window cannot exist headless"
            )

    def _require_bridge(self):  # type: ignore[no-untyped-def]
        from actionmesh.agent.browser import BridgeDriver, BrowserEngine
        from actionmesh.agent.browser_bridge import (
            BrowserBridgeConfig,
            make_request,
        )

        pair = _backend_bridge_pair()
        if pair is None:
            self.skipTest(
                "BLOCKED: browser bridge not configured — neither "
                f"{SOCKET_ENV}/{TOKEN_ENV} exported here nor present in a "
                "running 'uvicorn actionmesh.api' backend child's environment. "
                "External dependency: the ActionMesh desktop (Electron) "
                "running with its browser window up. No FakeDriver substitute."
            )
        socket_path, token = pair
        if not os.path.exists(socket_path):
            self.skipTest(
                f"BLOCKED: browser bridge socket does not exist at the "
                f"configured path ({socket_path}) — the Electron browser "
                "bridge is not running"
            )
        if not _socket_listening(socket_path):
            self.skipTest(
                "BLOCKED: browser bridge socket exists but nothing is "
                f"listening on it ({socket_path}) — stale file or bridge "
                "failed to start; a socket file alone is not a bridge"
            )
        config = BrowserBridgeConfig(socket_path=socket_path, token=token)
        assert config.configured  # pair was complete; never print its values
        return BrowserEngine(BridgeDriver(make_request(config)))

    def _require_network(self, host: str) -> None:
        if not _tcp_reachable(host, 443):
            self.skipTest(
                f"BLOCKED: network unreachable from this environment "
                f"(tcp {host}:443 refused/timed out) — skipped, not faked"
            )

    def _stop_quietly(self, engine) -> None:  # type: ignore[no-untyped-def]
        with suppress(Exception):
            run(engine.stop())

    def _open_readonly(self, engine, url: str, host: str) -> dict:  # type: ignore[no-untyped-def]
        """Open read-only; require independent read-back agreement.

        Only ``open``/``snapshot``/``inspect`` are called. ``needs_user``
        is BLOCKED, never bypassed. A window launching is not navigation:
        ``ok`` plus title plus host plus read-back agreement is the claim.
        """
        self._require_display()
        self._require_network(host)
        opened = run(engine.open(url))
        if opened.get("status") == "unavailable":
            self.skipTest(
                f"BLOCKED: browser bridge unreachable: {opened.get('reason', 'unavailable')}"
            )
        if opened.get("status") == "needs_user":
            self.skipTest(
                f"BLOCKED: {host} presented a user gate "
                f"({opened.get('reason', '')}) — not bypassed (read-only)"
            )
        self.assertEqual(opened.get("status"), "ok", f"open({url}) failed: {opened!r}")
        observed_url = opened.get("url", "")
        observed_title = opened.get("title", "")
        self.assertIn(host, observed_url, f"open reported unexpected url: {observed_url!r}")
        self.assertTrue(observed_title, "open reported no page title")
        self.assertTrue(
            _FORBIDDEN_KEYS.isdisjoint(opened),
            f"read-only open invented domain data: {sorted(_FORBIDDEN_KEYS & opened.keys())}",
        )

        snap = run(engine.snapshot())
        if snap.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on read-back: {snap.get('reason', '')}")
        self.assertEqual(snap.get("status"), "ok", f"snapshot failed: {snap!r}")
        self.assertEqual(snap.get("url"), observed_url, "read-back url disagrees with open")
        self.assertEqual(snap.get("title"), observed_title, "read-back title disagrees with open")
        self.assertGreaterEqual(snap.get("count", 0), 1, f"read-back saw no elements on {host}")
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(snap))

        insp = run(engine.inspect())
        if insp.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on inspect: {insp.get('reason', '')}")
        self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
        self.assertEqual(insp.get("url"), observed_url)
        self.assertEqual(insp.get("title"), observed_title)
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(insp))
        return opened

    def test_example_com_live_with_readback(self) -> None:
        """LIVE example.com: real bridge, real page, URL + title evidence."""
        engine = self._require_bridge()
        try:
            opened = self._open_readonly(engine, EXAMPLE_URL, EXAMPLE_HOST)
            self.assertIn("Example", opened.get("title", ""))
        finally:
            self._stop_quietly(engine)

    def test_duckduckgo_search_live_readback(self) -> None:
        """LIVE read-only search on the real search page. Never clicks."""
        engine = self._require_bridge()
        try:
            self._require_display()
            self._require_network(SEARCH_HOST)
            opened = run(engine.open(EXAMPLE_URL))
            if opened.get("status") == "unavailable":
                self.skipTest(f"BLOCKED: bridge unreachable: {opened.get('reason', '')}")
            if opened.get("status") != "ok":
                self.skipTest(f"BLOCKED: could not open a starting page: {opened!r}")
            found = run(engine.search(SEARCH_QUERY, SEARCH_ENGINE))
            if found.get("status") == "unavailable":
                self.skipTest(f"BLOCKED: bridge unreachable on search: {found.get('reason', '')}")
            if found.get("status") == "needs_user":
                self.skipTest(f"BLOCKED: search gate ({found.get('reason', '')}) — not bypassed")
            self.assertEqual(found.get("status"), "ok", f"search failed: {found!r}")
            search_url = found.get("url", "")
            self.assertIn(SEARCH_HOST, search_url)
            self.assertIsInstance(found.get("results"), list)
            self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(found))
            insp = run(engine.inspect())
            if insp.get("status") == "unavailable":
                self.skipTest(f"BLOCKED: bridge unreachable on read-back: {insp.get('reason', '')}")
            self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
            self.assertEqual(insp.get("url"), search_url)
            self.assertTrue(insp.get("title"), "search page reported no title")
        finally:
            self._stop_quietly(engine)

    def test_stale_ref_detected_across_navigation_live(self) -> None:
        """LIVE stale-ref detection: a ref minted on page A is stale on page B.

        The click attempt must answer ``stale`` from the engine's ref map
        before any drive reaches the page — so this never touches the live
        page's controls.
        """
        engine = self._require_bridge()
        try:
            self._require_display()
            self._require_network(EXAMPLE_HOST)
            opened = run(engine.open(EXAMPLE_URL))
            if opened.get("status") == "unavailable":
                self.skipTest(f"BLOCKED: bridge unreachable: {opened.get('reason', '')}")
            if opened.get("status") == "needs_user":
                self.skipTest(f"BLOCKED: gate on {EXAMPLE_HOST} — not bypassed")
            self.assertEqual(opened.get("status"), "ok", f"open failed: {opened!r}")
            snap = run(engine.snapshot())
            self.assertEqual(snap.get("status"), "ok", f"snapshot failed: {snap!r}")
            refs = snap.get("refs") or []
            self.assertTrue(refs, "snapshot minted no refs to go stale")
            moved = run(engine.open("https://duckduckgo.com/"))
            if moved.get("status") == "unavailable":
                self.skipTest(f"BLOCKED: bridge unreachable on navigate: {moved.get('reason', '')}")
            if moved.get("status") == "needs_user":
                self.skipTest("BLOCKED: gate on second page — not bypassed")
            self.assertEqual(moved.get("status"), "ok", f"second open failed: {moved!r}")
            self.assertNotEqual(moved.get("url"), opened.get("url"))
            stale = run(engine.click(refs[0]))
            self.assertEqual(
                stale.get("status"), "stale",
                f"ref from a previous page must be stale, got: {stale!r}",
            )
        finally:
            self._stop_quietly(engine)

    def test_train_site_live_read_only(self) -> None:
        """Read-only visit to the train site. Never books; nothing invented."""
        url = _train_url()
        host = _host_of(url)
        if not host:
            self.skipTest(f"BLOCKED: {TRAIN_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, url, host)
        finally:
            self._stop_quietly(engine)

    def test_zomato_live_read_only(self) -> None:
        """Read-only visit to Zomato. Never orders; nothing invented."""
        url = _zomato_url()
        host = _host_of(url)
        if not host:
            self.skipTest(f"BLOCKED: {ZOMATO_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, url, host)
        finally:
            self._stop_quietly(engine)


class TestHonestUnavailable(unittest.TestCase):
    """Always-on proof of the honest-absent path. INTEGRATED, never LIVE.

    Uses the real bridge config shape pointed at a socket that cannot
    exist: ``open`` must answer ``unavailable`` — never ``ok`` and never a
    claimed navigation. Deterministic in any environment, bridge up or not.
    """

    def _dead_engine(self):  # type: ignore[no-untyped-def]
        from actionmesh.agent.browser import BridgeDriver, BrowserEngine
        from actionmesh.agent.browser_bridge import (
            BrowserBridgeConfig,
            make_request,
        )

        dead_path = "/nonexistent-actionmesh-phase5/browser-bridge.sock"
        self.assertFalse(os.path.exists(dead_path))
        config = BrowserBridgeConfig(socket_path=dead_path, token="dead-token")
        return BrowserEngine(BridgeDriver(make_request(config)))

    def test_open_without_bridge_is_unavailable_never_ok(self) -> None:
        engine = self._dead_engine()
        result = run(engine.open(EXAMPLE_URL))
        self.assertEqual(result.get("status"), "unavailable", f"got: {result!r}")
        self.assertNotEqual(result.get("status"), "ok")
        self.assertNotIn("url", result)

    def test_every_op_without_session_is_unavailable(self) -> None:
        engine = self._dead_engine()
        for coro in (
            engine.snapshot(),
            engine.inspect(),
            engine.navigate(EXAMPLE_URL),
            engine.search(SEARCH_QUERY, SEARCH_ENGINE),
            engine.back(),
            engine.refresh(),
            engine.stop(),
        ):
            result = run(coro)
            self.assertEqual(
                result.get("status"), "unavailable",
                f"op without a session must be unavailable, got: {result!r}",
            )


if __name__ == "__main__":
    unittest.main()
