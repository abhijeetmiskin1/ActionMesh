"""Phase 3 live acceptance: honest end-to-end probes, opt-in only.

Extends :mod:`test_phase2_live_acceptance` with additional read-only live
surfaces (train search, Zomato discover), an honest MPRIS probe, and a
read-only packaged-deb contents check. This file never runs in the default
suite: every test skips unless ``ACTIONMESH_LIVE_ACCEPTANCE=1`` is set, and
even then skips (never passes, never fakes) when the real external
dependencies are absent:

* the Electron browser bridge must be configured
  (``ACTIONMESH_BROWSER_BRIDGE_SOCKET`` + ``ACTIONMESH_BROWSER_BRIDGE_TOKEN``)
  — without it the engine would fall back to ``FakeDriver`` and any
  "success" would be a simulation; there is no FakeDriver substitute here;
* for each live site, TCP reachability to that host is probed from this
  process first; failure skips with an explicit BLOCKED reason instead of
  fabricating results (no invented restaurants, prices, or trains);
* when neither ``DISPLAY`` nor ``WAYLAND_DISPLAY`` is set, tests skip with
  a BLOCKED "no display" reason — a headless CI box cannot answer a bridge
  that wants a window.

When a test does run, success is claimed only from independent read-back:
``snapshot()`` / ``inspect()`` re-read driver state and must agree with
``open()`` on url/title. Read-only means: open/inspect/snapshot only —
no clicks, no submits, no orders, no booking. Login / CAPTCHA / OTP / MFA
gates surface as ``needs_user`` and are reported as BLOCKED, never bypassed.

Acceptance labels (reported by the runner, asserted here only as honesty):
MOCKED = FakeDriver only; INTEGRATED = real engine + FakeDriver seam;
LIVE = real bridge + real network + read-back agreement; BLOCKED = skipped
with an explicit reason. This module can only produce LIVE or BLOCKED.
"""

from __future__ import annotations

import os
import socket
import tarfile
import unittest
from contextlib import suppress
from pathlib import Path

LIVE_ENV = "ACTIONMESH_LIVE_ACCEPTANCE"

EXAMPLE_URL = "https://example.com"
EXAMPLE_HOST = "example.com"

SEARCH_ENGINE = "duckduckgo"
SEARCH_HOST = "duckduckgo.com"
SEARCH_QUERY = "ActionMesh"

# Optional overrides so an operator can point at a site they can actually
# reach; defaults are the public homepages. Nothing is invented from them:
# only the read-back url/title/host are asserted.
TRAIN_URL_ENV = "ACTIONMESH_LIVE_TRAIN_URL"
TRAIN_URL = os.environ.get(TRAIN_URL_ENV, "https://www.irctc.co.in/").strip()
TRAIN_HOST = TRAIN_URL.split("://", 1)[-1].split("/", 1)[0] if "://" in TRAIN_URL else ""

ZOMATO_URL_ENV = "ACTIONMESH_LIVE_ZOMATO_URL"
ZOMATO_URL = os.environ.get(ZOMATO_URL_ENV, "https://www.zomato.com/").strip()
ZOMATO_HOST = ZOMATO_URL.split("://", 1)[-1].split("/", 1)[0] if "://" in ZOMATO_URL else ""

REPO_ROOT = Path(__file__).resolve().parents[2]
DEB_DIR = REPO_ROOT / "desktop" / "release"


def run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


def _tcp_reachable(host: str, port: int, timeout_s: float = 3.0) -> bool:
    """Best-effort probe; False means "do not attempt", not a verdict."""
    if not host:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True
    except OSError:
        return False


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _deb_contents(deb_path: Path) -> list[str]:
    """Read-only listing of the ``data.tar.*`` inside a ``.deb`` (ar archive).

    Uses only :mod:`tarfile` and local bytes — no dpkg dependency, no
    extraction to disk, no rebuild. Raises on a malformed archive.
    """
    raw = deb_path.read_bytes()
    if not raw.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive (expected .deb)")
    members: dict[str, bytes] = {}
    offset = 8
    while offset + 60 <= len(raw):
        header = raw[offset : offset + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            break
        name = header[0:16].decode("ascii", "replace").strip()
        try:
            size = int(header[48:58].decode("ascii", "replace").strip() or "0")
        except ValueError:
            break
        body_start = offset + 60
        body = raw[body_start : body_start + size]
        members[name] = body
        # 2-byte alignment padding in ar.
        offset = body_start + size + (size % 2)
    import io

    data_member = next(
        (v for k, v in members.items() if k.startswith("data.tar")),
        None,
    )
    if data_member is None:
        raise ValueError("no data.tar member in .deb")
    names: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data_member), mode="r:*") as tar:
        names = tar.getnames()
    return names


@unittest.skipUnless(
    os.environ.get(LIVE_ENV) == "1",
    f"live acceptance is opt-in: set {LIVE_ENV}=1 to run it",
)
class TestPhase3LiveAcceptance(unittest.TestCase):
    """Real-bridge, read-only live acceptance. Skips honestly when blocked."""

    # -- gating helpers ----------------------------------------------------

    def _require_display(self) -> None:
        if not _has_display():
            self.skipTest(
                "BLOCKED: no display (DISPLAY/WAYLAND_DISPLAY unset) — "
                "a headless environment cannot host the Electron browser window"
            )

    def _require_bridge(self) -> object:
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
                "ACTIONMESH_BROWSER_BRIDGE_TOKEN unset) — "
                "external dependency: running Electron desktop with a browser "
                "window; no FakeDriver substitute is accepted for live acceptance"
            )
        return BrowserEngine(BridgeDriver(make_request(config)))

    def _require_network(self, host: str) -> None:
        if not _tcp_reachable(host, 443):
            self.skipTest(
                f"BLOCKED: network unreachable from this sandbox "
                f"(tcp {host}:443 refused/timed out) — skipped, not faked"
            )

    def _stop_quietly(self, engine: object) -> None:
        from actionmesh.agent.browser import BrowserEngine

        with suppress(Exception):
            run(engine.stop()) if isinstance(engine, BrowserEngine) else None  # type: ignore[attr-defined]

    def _open_readonly(self, engine: object, url: str, host: str) -> dict:
        """Open ``url`` read-only and assert independent read-back agrees.

        Read-only: only ``open`` + ``snapshot`` + ``inspect`` are ever
        called — never ``click``, ``type_``, ``select``, or any submit.
        A ``needs_user`` gate (login/CAPTCHA/OTP/MFA) is BLOCKED, not bypassed.
        """
        from actionmesh.agent.browser import BrowserEngine

        assert isinstance(engine, BrowserEngine)
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
        self.assertEqual(
            opened.get("status"),
            "ok",
            f"open({url}) did not succeed: {opened!r}",
        )
        observed_url = opened.get("url", "")
        observed_title = opened.get("title", "")
        self.assertIn(host, observed_url, f"open reported unexpected url: {observed_url!r}")
        self.assertTrue(observed_title, "open reported no page title")

        snap = run(engine.snapshot())
        if snap.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on read-back: {snap.get('reason', '')}")
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
        self.assertGreaterEqual(snap.get("count", 0), 1, f"read-back saw no elements on {host}")

        insp = run(engine.inspect())
        if insp.get("status") == "unavailable":
            self.skipTest(f"BLOCKED: bridge unreachable on inspect: {insp.get('reason', '')}")
        self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
        self.assertEqual(insp.get("url"), observed_url)
        self.assertEqual(insp.get("title"), observed_title)
        self.assertTrue(insp.get("title"), "inspect reported no page title")
        return opened

    # -- live browser surfaces (each is LIVE or BLOCKED, never MOCKED) ----

    def test_open_example_com_with_independent_readback(self) -> None:
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, EXAMPLE_URL, EXAMPLE_HOST)
        finally:
            self._stop_quietly(engine)

    def test_search_on_real_search_page_readback(self) -> None:
        from actionmesh.agent.browser import BrowserEngine

        engine = self._require_bridge()
        assert isinstance(engine, BrowserEngine)
        try:
            self._require_display()
            self._require_network(SEARCH_HOST)
            opened = run(engine.open(EXAMPLE_URL))
            if opened.get("status") != "ok":
                if opened.get("status") == "unavailable":
                    self.skipTest(
                        f"BLOCKED: browser bridge unreachable: {opened.get('reason', '')}"
                    )
                self.skipTest(f"BLOCKED: could not open a starting page: {opened!r}")
            found = run(engine.search(SEARCH_QUERY, SEARCH_ENGINE))
            if found.get("status") == "unavailable":
                self.skipTest(
                    f"BLOCKED: browser bridge unreachable on search: {found.get('reason', '')}"
                )
            if found.get("status") == "needs_user":
                self.skipTest(
                    "BLOCKED: search page presented a login/CAPTCHA gate "
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

            insp = run(engine.inspect())
            if insp.get("status") == "unavailable":
                self.skipTest(
                    f"BLOCKED: browser bridge unreachable on read-back: {insp.get('reason', '')}"
                )
            self.assertEqual(insp.get("status"), "ok", f"inspect failed: {insp!r}")
            self.assertEqual(insp.get("url"), search_url)
            self.assertTrue(insp.get("title"), "search page reported no title")
        finally:
            self._stop_quietly(engine)

    def test_train_site_search_read_only(self) -> None:
        """Read-only visit to the configured train site. Never books.

        No train, fare, or schedule is asserted or invented — only the
        read-back url/title/host agreement. ``booking.submit``-style actions
        are never invoked; this test only opens and reads.
        """
        if not TRAIN_HOST:
            self.skipTest(f"BLOCKED: {TRAIN_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, TRAIN_URL, TRAIN_HOST)
        finally:
            self._stop_quietly(engine)

    def test_zomato_discover_read_only(self) -> None:
        """Read-only visit to the configured Zomato site. Never orders.

        No restaurant, dish, price, or rating is asserted or invented —
        only the read-back url/title/host agreement. ``order.place``-style
        actions are never invoked; this test only opens and reads.
        """
        if not ZOMATO_HOST:
            self.skipTest(f"BLOCKED: {ZOMATO_URL_ENV} did not yield a host")
        engine = self._require_bridge()
        try:
            self._open_readonly(engine, ZOMATO_URL, ZOMATO_HOST)
        finally:
            self._stop_quietly(engine)

    # -- MPRIS honesty probe (live, read-only, never fabricates) -----------

    def test_mpris_probe_returns_empty_when_no_player(self) -> None:
        """The real ``MprisMediaDriver.list_players()`` must return ``[]``
        when no player is reachable — never a fabricated player.

        This is the honest-absence probe ``actionmesh/agent/media.py:300``
        (marked "not live-verified") alludes to: we assert the contract
        (a list, possibly empty) rather than any specific player. If the
        session bus or ``gi`` is absent the driver must still return ``[]``
        (its ``_session_bus`` probe never raises) — that is itself the
        fail-closed behaviour under test, not a failure.
        """
        from actionmesh.agent.media import MprisMediaDriver

        driver = MprisMediaDriver()
        try:
            players = driver.list_players()
        finally:
            with suppress(Exception):
                driver.close()
        self.assertIsInstance(players, list, f"MPRIS probe did not return a list: {players!r}")
        # Every entry, if any player IS present, must carry a real name —
        # a nameless entry would be a fabricated player.
        for player in players:
            self.assertIsInstance(player.name, str)
            self.assertTrue(player.name, "MPRIS returned a nameless player")

    # -- packaged deb contents (read-only; Agent 6 owns packaging) --------

    def test_packaged_deb_contents_hygiene(self) -> None:
        """Read-only hygiene check over ``desktop/release/*.deb``.

        Asserts the packaged tree ships its desktop file and runtime, and
        does NOT ship ``settings.json`` / secret keys / ``.env`` files or
        PEM material. Skips with BLOCKED when no .deb is present (nothing
        to verify — never a pass from absence). Packaging itself is owned
        by Agent 6; this test only reads bytes already on disk.
        """
        debs = sorted(DEB_DIR.glob("*.deb")) if DEB_DIR.is_dir() else []
        if not debs:
            self.skipTest("BLOCKED: no packaged .deb in desktop/release/ — nothing to verify")
        failures: list[str] = []
        for deb in debs:
            try:
                names = _deb_contents(deb)
            except ValueError as exc:
                failures.append(f"{deb.name}: malformed .deb ({exc})")
                continue
            lowered = [n.lower() for n in names]
            if not any(n.endswith("actionmesh.desktop") for n in lowered):
                failures.append(f"{deb.name}: missing actionmesh.desktop entry")
            if not any("/runtime/" in n or n.startswith("./runtime/") for n in lowered):
                failures.append(f"{deb.name}: missing runtime/ payload")
            leaked = [
                n
                for n in lowered
                if n.endswith("settings.json")
                or n.endswith(".key")
                or n.endswith(".env")
                # A bare-bfilename .pem is secret material; a public CA
                # bundle shipped inside the vendored runtime (certifi's
                # cacert.pem) is not — allowlist it explicitly rather than
                # weakening the check for everything.
                or (n.endswith(".pem") and "cacert.pem" not in n)
                or n.endswith("settings.json.bak")
            ]
            if leaked:
                failures.append(f"{deb.name}: leaks secrets {leaked}")
            # The electron-builder "files" allowlist excludes app/*.test.js
            # and **/*.map — assert the shipped tree honours that, so a
            # future packaging regression that ships test harnesses is caught.
            shipped_tests = [n for n in lowered if n.endswith(".test.js")]
            if shipped_tests:
                failures.append(f"{deb.name}: ships test files {shipped_tests[:5]}")
        self.assertEqual(failures, [], "packaged .deb hygiene failures")


if __name__ == "__main__":
    unittest.main()
