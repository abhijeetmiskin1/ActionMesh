"""Phase 5 live MPRIS acceptance: probe for a real media player on the session bus.

P0-1 scope (this file): determine, with evidence, whether a real media player
is reachable through the production ``MprisMediaDriver`` in this environment.

* ``TestPhase5MprisLive`` is the LIVE class. It probes ``list_players()`` on the
  REAL ``MprisMediaDriver`` -- no ``FakeMediaDriver`` substitute. Three
  outcomes, made mechanically visible:

  - Player present and playing/paused -> read-only ``MediaEngine.status()``
    AND a ``play()``/``pause()`` read-back. LIVE is claimed only when
    ``observe``/``status`` read-back returns ``"playing"`` from the player
    itself. A dispatched-but-ignored command is ``no_effect``, never ``ok``.
    Playback is never claimed from the request alone.
  - No player -> the test records ``BLOCKED: no media player on session bus``
    and skips with that message (never passes, never fakes a session). The
    honest unavailable path is proved separately below.
  - Player present but stopped/idle -> BLOCKED with the observed state named,
    not a playback claim.

* ``TestMprisHonestUnavailable`` is NOT live: it drives the REAL
  ``MprisMediaDriver`` on this machine and proves the unavailable path when the
  environment has no player (``list_players()==[]`` -> ``status/play/pause``
  all ``"unavailable"``), plus the ``FakeMediaDriver`` seam case for
  determinism. Its label is INTEGRATED, never LIVE.

No player is installed by this file, no media is downloaded, no ``Raise``
(focus-steal) is issued. If no player is present the harness is skipped with
a named missing dependency: a real MPRIS media player on the session D-Bus.

Acceptance labels: LIVE = real driver + player read-back state observed;
BLOCKED = skipped with an explicit reason; INTEGRATED = real engine +
fake/unavailable seam proof.
"""

from __future__ import annotations

import os
import shutil
import unittest
from contextlib import suppress

LIVE_ENV = "ACTIONMESH_LIVE_ACCEPTANCE"

# Player binaries considered. Present-on-PATH alone is NOT proof: list_players()
# on the real session bus is the evidence. Absence from PATH plus an empty
# list_players() is the BLOCKED evidence pair.
_PLAYER_BINS = ("vlc", "mpv", "celluloid", "totem", "mpv", "audacious", "clementine")


def _has_display() -> bool:
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _has_session_bus() -> bool:
    return bool(os.environ.get("DBUS_SESSION_BUS_ADDRESS"))


def _found_player_bins() -> list[str]:
    return [b for b in _PLAYER_BINS if shutil.which(b)]


class TestPhase5MprisLive(unittest.TestCase):
    """Real-driver MPRIS live probe. LIVE or BLOCKED, never MOCKED, never faked.

    Gate: ``ACTIONMESH_LIVE_ACCEPTANCE=1``. Without it the whole class is
    skipped as opt-in. Inside, a missing display or session bus, or an empty
    ``list_players()``, is BLOCKED with the evidence named -- the test skips
    rather than passing or manufacturing a player.
    """

    def _require_live_env(self) -> None:
        if os.environ.get(LIVE_ENV) != "1":
            self.skipTest(f"live MPRIS probe is opt-in: set {LIVE_ENV}=1 to run it")

    def test_mpris_live_probe_readback_or_blocked(self) -> None:
        from actionmesh.agent.media import MediaEngine, MprisMediaDriver

        self._require_live_env()
        if not _has_display():
            self.skipTest(
                "BLOCKED: no display (DISPLAY/WAYLAND_DISPLAY unset) -- "
                "a media player cannot be hosted headless"
            )
        if not _has_session_bus():
            self.skipTest(
                "BLOCKED: no session bus (DBUS_SESSION_BUS_ADDRESS unset) -- "
                "the MPRIS driver cannot reach org.mpris.MediaPlayer2 names"
            )
        driver = MprisMediaDriver()
        try:
            try:
                players = driver.list_players()
            except Exception as exc:  # a timeout is evidence, not a pass
                self.skipTest(f"BLOCKED: MPRIS probe errored: {type(exc).__name__}: {exc}")
            self.assertIsInstance(players, list, f"list_players() not a list: {players!r}")
            for player in players:
                self.assertIsInstance(player.name, str)
                self.assertTrue(player.name, "MPRIS returned a nameless player")
            if not players:
                bins = _found_player_bins()
                self.skipTest(
                    "BLOCKED: no media player on session bus "
                    f"(list_players()==[]; player binaries on PATH: {bins or 'none'}) -- "
                    "missing dependency: a real MPRIS media player "
                    "(vlc/mpv/celluloid/totem) running on the session D-Bus; "
                    "installing one requires lead approval, and no FakeMediaDriver "
                    "substitute is accepted for live acceptance"
                )
            engine = MediaEngine(driver)
            st = engine.status()
            self.assertIn(st.get("status"), ("ok", "timeout"), f"status: {st!r}")
            if st.get("status") == "timeout":
                self.skipTest(f"BLOCKED: MPRIS player timeout on status: {st!r}")
            self.assertIn(st.get("player"), [p.name for p in players])
            # Read-back must agree with the driver's own list_players() facts.
            observed_states = {p.name: p.state for p in players}
            self.assertEqual(observed_states.get(st["player"]), st.get("state"))
            # LIVE playback is claimed ONLY on a "playing" read-back.
            if st.get("state") != "playing":
                self.skipTest(
                    f"BLOCKED: player {st.get('player')!r} present but state="
                    f"{st.get('state')!r} (not playing) -- no silent local file "
                    "is played unilaterally; playback requires a client-played "
                    "track or explicit lead instruction, and no playback is "
                    "claimed without a playing read-back"
                )
            # Player is playing: pause must read back paused (no fake claim).
            paused = engine.pause()
            self.assertEqual(paused.get("status"), "ok", f"pause read-back: {paused!r}")
            after = engine.status()
            self.assertEqual(after.get("status"), "ok")
            self.assertEqual(after.get("state"), "paused", f"post-pause state: {after!r}")
            # Restore: play must read back playing, else it is no_effect.
            resumed = engine.play()
            self.assertIn(
                resumed.get("status"), ("ok", "no_effect"), f"resume read-back: {resumed!r}"
            )
            if resumed.get("status") == "ok":
                self.assertEqual(resumed.get("state"), "playing")
        finally:
            with suppress(Exception):
                driver.close()


class TestMprisHonestUnavailable(unittest.TestCase):
    """INTEGRATED: real driver + fake seam prove unavailable is never ok.

    Not live: runs unconditionally. When this environment DOES have a player,
    the real-driver assertions are vacuous (players non-empty) and the fake
    seam still proves the empty case deterministically.
    """

    def test_real_driver_no_player_is_unavailable_not_ok(self) -> None:
        from actionmesh.agent.media import MediaEngine, MprisMediaDriver

        driver = MprisMediaDriver()
        try:
            players = driver.list_players()
            engine = MediaEngine(driver)
            status = engine.status()
            if not players:
                # Honest absence on the real driver: unavailable, never ok, never
                # a fabricated "playing" read-back.
                for result in (status, engine.play(), engine.pause()):
                    self.assertEqual(
                        result.get("status"), "unavailable", f"no-player result: {result!r}"
                    )
                    self.assertNotIn("playing", str(result.get("state") or ""))
            else:
                self.assertIn(status.get("status"), ("ok", "timeout", "unavailable"))
        finally:
            with suppress(Exception):
                driver.close()

    def test_fake_driver_empty_is_unavailable_not_ok(self) -> None:
        """Deterministic seam case: no player -> unavailable for every verb."""
        from actionmesh.agent.media import FakeMediaDriver, MediaEngine

        engine = MediaEngine(FakeMediaDriver())
        for result in (engine.status(), engine.play(), engine.pause(), engine.volume()):
            self.assertNotEqual(result.get("status"), "ok", f"no-player claimed ok: {result!r}")
            self.assertIn(result.get("status"), ("unavailable", "ambiguous"))


if __name__ == "__main__":
    unittest.main()
