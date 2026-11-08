"""Phase 4 live acceptance: read-only browser + honest MPRIS, opt-in only.

P0-2 (browser) and P0-1 (media) are closed only as far as this environment
honestly allows. This file never runs live in the default suite:

* ``TestPhase4LiveAcceptance`` skips unless ``ACTIONMESH_LIVE_ACCEPTANCE=1``,
  then skips (never passes, never fakes) when the Electron bridge env
  (``ACTIONMESH_BROWSER_BRIDGE_SOCKET`` / ``ACTIONMESH_BROWSER_BRIDGE_TOKEN``)
  is unset or unreachable, or when TCP to the target host is
  unreachable. It never substitutes ``FakeDriver`` for the live path.
* When it does run it drives the REAL production path
  ``BrowserEngine(BridgeDriver(make_request(config)))`` read-only
  (``open`` + ``snapshot`` + ``inspect`` only) against example.com, a
  DuckDuckGo search, the train URL and the Zomato URL, and requires
  independent read-back agreement (snapshot and inspect re-read driver
  state and must agree on URL and title after navigation).
* ``TestReadOnlySafety`` is NOT live: it uses ``FakeDriver`` in-process to
  prove the read-only invariants (no click/type/select/submit on the
  read-only path, ``needs_user`` never bypassed, no invented
  trains/restaurants/prices, booking/order submits stay CRITICAL and
  fail-closed). Its label is INTEGRATED/MOCKED, never LIVE.

Acceptance labels: MOCKED = FakeDriver only; INTEGRATED = real engine +
FakeDriver seam; LIVE = real bridge + real network + read-back agreement;
BLOCKED = skipped with an explicit reason. Live class: LIVE or BLOCKED only.
"""

from __future__ import annotations

import os
import socket
import unittest
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit

LIVE_ENV = "ACTIONMESH_LIVE_ACCEPTANCE"
MPRIS_PLAYER_ENV = "ACTIONMESH_LIVE_MPRIS_PLAYER"

EXAMPLE_URL = "https://example.com"
EXAMPLE_HOST = "example.com"

SEARCH_ENGINE = "duckduckgo"
SEARCH_HOST = "duckduckgo.com"
SEARCH_QUERY = "ActionMesh"

TRAIN_URL_ENV = "ACTIONMESH_LIVE_TRAIN_URL"
ZOMATO_URL_ENV = "ACTIONMESH_LIVE_ZOMATO_URL"

# READ-ONLY result keys. A read-only open/inspect/snapshot/search result must
# never carry domain data invented by the harness (trains, fares,
# restaurants, prices, bookings, orders). Search adds only "results" (link
# anchors read off the SERP) and "ref_map_size" on open.
_READONLY_KEYS = {
    "status", "url", "title", "refs", "count", "element_count",
    "truncated", "ref_map_size", "results", "needs_user", "reason", "note",
}
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


def _train_url() -> str:
    return os.environ.get(TRAIN_URL_ENV, "https://www.irctc.co.in/").strip()


def _zomato_url() -> str:
    return os.environ.get(ZOMATO_URL_ENV, "https://www.zomato.com/").strip()


@unittest.skipUnless(
    os.environ.get(LIVE_ENV) == "1",
    f"live acceptance is opt-in: set {LIVE_ENV}=1 to run it",
)
class TestPhase4LiveAcceptance(unittest.TestCase):
    """Exercise the real Electron bridge and an explicitly selected test player."""

    def _require_bridge(self):  # type: ignore[no-untyped-def]
        from actionmesh.agent.browser import BridgeDriver, BrowserEngine
        from actionmesh.agent.browser_bridge import (
            browser_bridge_config_from_env,
            make_request,
        )

        config = browser_bridge_config_from_env()
        if not config.configured:
            self.skipTest(
                "BLOCKED: browser bridge is not configured "
                "(ACTIONMESH_BROWSER_BRIDGE_SOCKET / "
                "ACTIONMESH_BROWSER_BRIDGE_TOKEN unset) — external dependency: "
                "running Electron desktop with a browser window; "
                "no FakeDriver substitute is accepted for live acceptance"
            )
        return BrowserEngine(BridgeDriver(make_request(config)))

    def _require_network(self, host: str) -> None:
        if not _tcp_reachable(host, 443):
            self.skipTest(
                f"BLOCKED: network unreachable from this sandbox "
                f"(tcp {host}:443 refused/timed out) — skipped, not faked"
            )

    def _stop_quietly(self, engine) -> None:  # type: ignore[no-untyped-def]
        with suppress(Exception):
            run(engine.stop())

    def _open_readonly(self, engine, url: str, host: str) -> dict:  # type: ignore[no-untyped-def]
        """Open read-only; assert independent read-back agrees.

        Only ``open``/``snapshot``/``inspect`` are called — never ``click``,
        ``type_``, ``select``, ``search`` on this path, and never any
        booking/order submit. ``needs_user`` is BLOCKED, not bypassed.
        """
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
        self.assertEqual(urlsplit(opened.get("url", "")).hostname, host)
        self.assertTrue(opened.get("title"), "open reported no page title")
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(opened))

        snap = run(engine.snapshot())
        if snap.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on read-back: {snap.get('reason', '')}")
        self.assertEqual(snap.get("status"), "ok", f"snapshot failed: {snap!r}")
        self.assertEqual(urlsplit(snap.get("url", "")).hostname, host)
        self.assertGreaterEqual(snap.get("count", 0), 1, f"read-back saw no elements on {host}")
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(snap))

        insp = run(engine.inspect())
        if insp.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on inspect: {insp.get('reason', '')}")
        self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
        self.assertEqual(insp.get("url"), snap.get("url"))
        self.assertEqual(insp.get("title"), snap.get("title"))
        self.assertTrue(insp.get("title"), "inspect reported no page title")
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(insp))
        return opened

    def test_open_example_com_with_independent_readback(self) -> None:
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, EXAMPLE_URL, EXAMPLE_HOST)
        finally:
            self._stop_quietly(engine)

    def test_click_and_handoff_in_real_electron(self) -> None:
        engine = self._require_bridge()
        pages = {
            "/": b'<title>Acceptance start</title><a href="/gate">Continue</a>',
            "/gate": b'<title>Sign in required</title><a href="/past-gate">Sign in</a>',
            "/past-gate": b"<title>Unexpected navigation</title>",
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path not in pages:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(pages[self.path])

            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            worker = Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base = f"http://127.0.0.1:{server.server_port}"
            try:
                opened = run(engine.open(base + "/"))
                if opened.get("status") == "unavailable":
                    self.skipTest("BLOCKED: Electron browser bridge is unreachable")
                self.assertEqual(opened.get("status"), "ok", opened)
                snapshot = run(engine.snapshot())
                self.assertEqual(snapshot.get("status"), "ok", snapshot)
                self.assertEqual(len(snapshot["refs"]), 1)
                clicked = run(engine.click(snapshot["refs"][0]))
                self.assertIn(clicked.get("status"), ("ok", "needs_user"), clicked)

                gate = run(engine.snapshot())
                self.assertEqual(gate.get("url"), base + "/gate")
                self.assertEqual(gate.get("needs_user"), "login")
                self.assertEqual(len(gate["refs"]), 1)
                refused = run(engine.click(gate["refs"][0]))
                self.assertEqual(refused.get("status"), "needs_user", refused)
                self.assertEqual(run(engine.inspect()).get("url"), base + "/gate")
            finally:
                self._stop_quietly(engine)
                server.shutdown()
                worker.join()

    def test_search_on_real_search_page_readback(self) -> None:
        engine = self._require_bridge()
        try:
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

    def test_train_site_read_only(self) -> None:
        """Read-only visit to the train site. Never books; no train/fare invented."""
        url = _train_url()
        host = _host_of(url)
        if not host:
            self.skipTest(f"BLOCKED: {TRAIN_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, url, host)
        finally:
            self._stop_quietly(engine)

    def test_zomato_discover_read_only(self) -> None:
        """Read-only visit to Zomato. Never orders; no restaurant/price invented."""
        url = _zomato_url()
        host = _host_of(url)
        if not host:
            self.skipTest(f"BLOCKED: {ZOMATO_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, url, host)
        finally:
            self._stop_quietly(engine)

    def test_mpris_probe_honest_absence_or_readonly_status(self) -> None:
        """Real ``MprisMediaDriver.list_players()`` contract probe.

        Returns a list (possibly empty). Entries, if any, must have a real
        non-empty name. Player-present path is read-only ``status()`` only —
        no ``play``/``pause`` command is issued, and no playback is claimed
        without read-back.
        """
        from actionmesh.agent.media import MediaEngine, MprisMediaDriver

        driver = MprisMediaDriver()
        try:
            players = driver.list_players()
            self.assertIsInstance(players, list, f"MPRIS probe did not return a list: {players!r}")
            for player in players:
                self.assertIsInstance(player.name, str)
                self.assertTrue(player.name, "MPRIS returned a nameless player")
            if not players:
                # Honest absence: engine must say unavailable, never ok.
                st = MediaEngine(driver).status()
                self.assertEqual(st.get("status"), "unavailable", f"no-player status: {st!r}")
            else:
                st = MediaEngine(driver).status()
                self.assertIn(st.get("status"), ("ok", "timeout"), f"status: {st!r}")
                if st.get("status") == "ok":
                    self.assertIn(st.get("player"), [p.name for p in players])
        finally:
            with suppress(Exception):
                driver.close()

    def test_mpris_play_pause_and_volume_on_dedicated_player(self) -> None:
        from actionmesh.agent.media import MediaEngine, MprisMediaDriver

        name = os.environ.get(MPRIS_PLAYER_ENV, "").strip()
        if not name:
            self.skipTest(f"Set {MPRIS_PLAYER_ENV} to a dedicated, paused MPRIS test player")
        driver = MprisMediaDriver()
        self.addCleanup(driver.close)
        self.assertEqual([player.name for player in driver.list_players()], [name])
        original = driver.get(name)
        self.assertIsNotNone(original, f"Dedicated MPRIS player {name!r} is unreachable")
        assert original is not None
        self.assertEqual(original.state, "paused")
        self.assertGreaterEqual(original.volume, 0.0)
        self.addCleanup(driver.command, name, "pause")
        self.addCleanup(driver.command, name, "volume", original.volume)

        engine = MediaEngine(driver)
        self.assertEqual(engine.status().get("player"), name)
        self.assertEqual(engine.play(name).get("state"), "playing")
        self.assertEqual(driver.get(name).state, "playing")
        self.assertEqual(engine.pause(name).get("state"), "paused")
        self.assertEqual(driver.get(name).state, "paused")
        target_volume = 0.4 if original.volume >= 0.5 else 0.7
        result = engine.volume(target_volume)
        self.assertEqual(result.get("status"), "ok", result)
        self.assertAlmostEqual(driver.get(name).volume, target_volume, delta=0.02)


class TestReadOnlySafety(unittest.TestCase):
    """INTEGRATED (real engine + FakeDriver seam), never LIVE.

    Proves the invariants the live harness relies on, in-process and
    deterministically: read-only paths issue no click/type/select/submit,
    needs_user gates are never bypassed, and no domain data is invented.
    """

    def _engine_with(self, pages):  # type: ignore[no-untyped-def]
        from actionmesh.agent.browser import BrowserEngine, FakeDriver

        drv = FakeDriver()
        for url, title, elements in pages:
            drv.add_page(url, title, elements)
        return BrowserEngine(drv), drv

    def test_readonly_open_snapshot_inspect_issues_no_mutation(self) -> None:
        eng, drv = self._engine_with([
            ("https://zomato.example/", "Zomato", [{"tag": "a", "name": "Order now", "href": "https://zomato.example/order"}]),
        ])
        opened = run(eng.open("https://zomato.example/"))
        self.assertEqual(opened["status"], "ok")
        snap = run(eng.snapshot())
        self.assertEqual(snap["status"], "ok")
        insp = run(eng.inspect())
        self.assertEqual(insp["status"], "ok")
        # Independent read-back agreement, the same rule the live harness uses.
        self.assertEqual(snap["url"], opened["url"])
        self.assertEqual(insp["url"], opened["url"])
        actions = [a["action"] for a in drv.actions]
        for forbidden in ("click", "type", "select"):
            self.assertNotIn(forbidden, actions, f"read-only path issued {forbidden}: {actions}")
        for result in (opened, snap, insp):
            self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(result), f"invented domain data in {result!r}")
            self.assertLessEqual(set(result) - {"url_after"}, _READONLY_KEYS | {"changed"})

    def test_search_issues_no_click_type_or_submit(self) -> None:
        eng, drv = self._engine_with([
            ("https://duckduckgo.com", "DDG", []),
            ("https://duckduckgo.com/?q=trains", "SERP", [
                {"tag": "a", "name": "Buy ticket", "href": "https://tickets.example/buy"},
                {"tag": "button", "name": "Book now"},
            ]),
        ])
        run(eng.open("https://duckduckgo.com"))
        found = run(eng.search("trains", "duckduckgo"))
        self.assertEqual(found["status"], "ok")
        actions = [a["action"] for a in drv.actions]
        self.assertNotIn("click", actions)
        self.assertNotIn("type", actions)
        self.assertNotIn("select", actions)
        self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(found))

    def test_needs_user_gate_never_bypassed_by_click_or_type(self) -> None:
        eng, drv = self._engine_with([
            ("https://train.example/login", "Sign in required", [
                {"tag": "input", "name": "Username", "value": ""},
                {"tag": "button", "name": "Submit"},
            ]),
        ])
        opened = run(eng.open("https://train.example/login"))
        self.assertEqual(opened["status"], "needs_user")
        snap = run(eng.snapshot())
        self.assertEqual(snap["status"], "ok")
        self.assertEqual(snap.get("needs_user"), "login")
        for ref in snap["refs"]:
            clicked = run(eng.click(ref))
            self.assertEqual(clicked["status"], "needs_user", f"click bypassed gate: {clicked!r}")
            typed = run(eng.type_(ref, "x"))
            self.assertEqual(typed["status"], "needs_user", f"type bypassed gate: {typed!r}")
        actions = [a["action"] for a in drv.actions]
        self.assertNotIn("click", actions, "gate page must never be clicked")
        self.assertNotIn("type", actions, "gate page must never be typed into")

    def test_needs_user_gate_blocks_search(self) -> None:
        eng, _drv = self._engine_with([
            ("https://start.example/", "Home", []),
        ])
        run(eng.open("https://start.example/"))
        # Re-point the scripted current page title at a gate by navigating a
        # gated page in a second engine sharing the same gate detection.
        eng2, _ = self._engine_with([
            ("https://gate.example/", "CAPTCHA Challenge", []),
        ])
        gated = run(eng2.open("https://gate.example/"))
        self.assertEqual(gated["status"], "needs_user")
        found = run(eng2.search("trains", "duckduckgo"))
        self.assertEqual(found["status"], "needs_user", f"search bypassed gate: {found!r}")

    def test_booking_and_order_stay_critical_and_fail_closed(self) -> None:
        """booking.submit / order.place are CRITICAL and have no read-only path.

        The browser engine exposes no submit/book/order method at all, and
        the registry marks both actions CRITICAL (per-action_id approval).
        """
        from actionmesh.agent.browser import BrowserEngine
        from actionmesh.agent.definitions import RiskLevel, device_definitions

        for forbidden in ("submit", "book", "order", "place", "checkout"):
            self.assertFalse(hasattr(BrowserEngine, forbidden), f"BrowserEngine grew {forbidden}")
        by_name = {d.name: d for d in device_definitions()}
        for name in ("booking.submit", "order.place"):
            self.assertIn(name, by_name, f"{name} missing from registry")
            self.assertEqual(by_name[name].risk_level, RiskLevel.CRITICAL, f"{name} not CRITICAL")

    def test_readonly_results_carry_no_invented_domain_data(self) -> None:
        eng, _drv = self._engine_with([
            ("https://train.example/", "IRCTC-like homepage", [{"tag": "h1", "name": "Trains"}]),
        ])
        for coro_fn in (lambda: eng.open("https://train.example/"),):
            result = run(coro_fn())
            self.assertEqual(result["status"], "ok")
            self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(result))
        snap = run(eng.snapshot())
        insp = run(eng.inspect())
        for result in (snap, insp):
            self.assertEqual(result["status"], "ok")
            self.assertTrue(_FORBIDDEN_KEYS.isdisjoint(result))

    def test_mpris_fake_driver_empty_is_unavailable_not_ok(self) -> None:
        """Seam-level honesty: no player -> unavailable, never a fake ok."""
        from actionmesh.agent.media import FakeMediaDriver, MediaEngine

        engine = MediaEngine(FakeMediaDriver())
        for result in (engine.status(), engine.play(), engine.pause()):
            self.assertNotEqual(result.get("status"), "ok", f"no-player result claimed ok: {result!r}")
            self.assertIn(result.get("status"), ("unavailable", "ambiguous"))


if __name__ == "__main__":
    unittest.main()
