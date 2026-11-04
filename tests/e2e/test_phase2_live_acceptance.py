"""Phase 2 live acceptance: real production browser path, opt-in only.

This file never runs in the default suite. It is skipped cleanly unless
``ACTIONMESH_LIVE_ACCEPTANCE=1`` is set in the environment, and even then
it skips (never passes, never fakes) when the real external dependencies
are absent:

* the Electron browser bridge must be configured
  (``ACTIONMESH_BROWSER_BRIDGE_SOCKET`` + ``ACTIONMESH_BROWSER_BRIDGE_TOKEN``,
  written by Electron main at spawn) — without it the engine would fall
  back to ``FakeDriver`` and any "success" would be a simulation;
* for the search case only, TCP reachability to the search host is probed
  from this process first; if that probe fails the search case skips with
  a reason instead of failing or fabricating results.

When it does run, it drives the REAL production path —
``BrowserEngine(BridgeDriver(make_request(config)))``, the same objects
``LaptopProvider`` builds when a bridge config is present — against
``https://example.com`` and a real search page. Success is never claimed
from a launched window or an HTTP 200: the observed url/title are recorded
by an independent read-back (``snapshot()`` / ``inspect()``, each of which
re-reads driver state) and the read-back must agree with ``open()``.
"""

from __future__ import annotations

import os
import socket
import unittest
from contextlib import suppress

LIVE_ENV = "ACTIONMESH_LIVE_ACCEPTANCE"
EXAMPLE_URL = "https://example.com"
SEARCH_ENGINE = "duckduckgo"
SEARCH_HOST = "duckduckgo.com"
SEARCH_QUERY = "ActionMesh"


def run(coro):
    import asyncio

    return asyncio.run(coro)


def _tcp_reachable(host: str, port: int, timeout_s: float = 3.0) -> bool:
    """Best-effort reachability probe; False means "do not attempt", not a verdict."""
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


@unittest.skipUnless(
    os.environ.get(LIVE_ENV) == "1",
    f"live acceptance is opt-in: set {LIVE_ENV}=1 to run it",
)
class TestPhase2LiveAcceptance(unittest.TestCase):
    """Real-bridge browser acceptance. Skips honestly when the bridge is absent."""

    def _real_engine(self):
        """Build the production engine, or SKip when no real bridge exists.

        Deliberately no FakeDriver fallback here: a pass must mean the
        Electron window actually answered.
        """
        from actionmesh.agent.browser import BridgeDriver, BrowserEngine
        from actionmesh.agent.browser_bridge import (
            browser_bridge_config_from_env,
            make_request,
        )

        config = browser_bridge_config_from_env()
        if not config.configured:
            self.skipTest(
                "browser bridge is not configured "
                "(ACTIONMESH_BROWSER_BRIDGE_SOCKET / "
                "ACTIONMESH_BROWSER_BRIDGE_TOKEN unset) — "
                "external dependency: running Electron desktop with a browser "
                "window; no FakeDriver substitute is accepted for live acceptance"
            )
        return BrowserEngine(BridgeDriver(make_request(config)))

    def _stop_quietly(self, engine) -> None:
        # Teardown must not mask the assertion above it.
        with suppress(Exception):
            run(engine.stop())

    def test_open_example_com_with_independent_readback(self) -> None:
        engine = self._real_engine()
        try:
            opened = run(engine.open(EXAMPLE_URL))
            if opened.get("status") == "unavailable":
                self.skipTest(
                    f"browser bridge unreachable: {opened.get('reason', 'unavailable')}"
                )
            if opened.get("status") == "needs_user":
                self.skipTest(
                    f"example.com presented a user gate: {opened.get('reason', '')}"
                )
            self.assertEqual(
                opened.get("status"),
                "ok",
                f"open(https://example.com) did not succeed: {opened!r}",
            )
            observed_url = opened.get("url", "")
            observed_title = opened.get("title", "")
            self.assertTrue(
                observed_url.startswith("https://example.com"),
                f"open reported an unexpected url: {observed_url!r}",
            )
            self.assertTrue(observed_title, "open reported no page title")

            # Independent read-back #1: snapshot re-reads driver state.
            snap = run(engine.snapshot())
            if snap.get("status") == "unavailable":
                self.skipTest(
                    f"browser bridge unreachable on read-back: {snap.get('reason', '')}"
                )
            self.assertEqual(snap.get("status"), "ok", f"snapshot failed: {snap!r}")
            self.assertEqual(
                snap.get("url"),
                observed_url,
                "read-back url disagrees with open url — success not claimed",
            )
            self.assertEqual(
                snap.get("title"),
                observed_title,
                "read-back title disagrees with open title — success not claimed",
            )
            self.assertGreaterEqual(
                snap.get("count", 0), 1, "read-back saw no elements on example.com"
            )

            # Independent read-back #2: inspect re-reads driver state again.
            insp = run(engine.inspect())
            if insp.get("status") == "unavailable":
                self.skipTest(
                    f"browser bridge unreachable on inspect: {insp.get('reason', '')}"
                )
            self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
            self.assertEqual(insp.get("url"), observed_url)
            self.assertEqual(insp.get("title"), observed_title)
            self.assertTrue(insp.get("title"), "inspect reported no page title")
        finally:
            self._stop_quietly(engine)

    def test_search_on_real_search_page_readback(self) -> None:
        if not _tcp_reachable(SEARCH_HOST, 443):
            self.skipTest(
                f"network unreachable from this sandbox (tcp {SEARCH_HOST}:443 "
                "refused/timed out) — search case skipped, not faked"
            )
        engine = self._real_engine()
        try:
            opened = run(engine.open(EXAMPLE_URL))
            if opened.get("status") != "ok":
                if opened.get("status") == "unavailable":
                    self.skipTest(
                        f"browser bridge unreachable: {opened.get('reason', '')}"
                    )
                self.skipTest(f"could not open a starting page: {opened!r}")

            found = run(engine.search(SEARCH_QUERY, SEARCH_ENGINE))
            if found.get("status") == "unavailable":
                self.skipTest(
                    f"browser bridge unreachable on search: {found.get('reason', '')}"
                )
            if found.get("status") == "needs_user":
                self.skipTest(
                    "search page presented a login/CAPTCHA gate "
                    f"({found.get('reason', '')}) — not bypassed"
                )
            self.assertEqual(
                found.get("status"),
                "ok",
                f"search on a real {SEARCH_ENGINE} page failed: {found!r}",
            )
            search_url = found.get("url", "")
            self.assertIn(SEARCH_HOST, search_url)
            self.assertIsInstance(found.get("results"), list)

            # Read-back: the page we are actually on must still be the SERP.
            insp = run(engine.inspect())
            if insp.get("status") == "unavailable":
                self.skipTest(
                    f"browser bridge unreachable on read-back: {insp.get('reason', '')}"
                )
            self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
            self.assertEqual(insp.get("url"), search_url)
            self.assertTrue(insp.get("title"), "search page reported no title")
        finally:
            self._stop_quietly(engine)


if __name__ == "__main__":
    unittest.main()
