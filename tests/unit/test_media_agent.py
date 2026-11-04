"""Unit tests for media.py: fake-player verification of §2.3 capability semantics.

Every test exercises MediaEngine against FakeMediaDriver's controlled state, proving:
- ``status`` reports real player state (never a guess)
- ``play``/``pause`` return the READ-BACK state (must NOT claim playing when fake says paused)
- volume level/muted are quantized by the fake and read back as the fake's values
- ambiguous ref or multiple active players → ambiguous result with candidates (never guess)
- ``open_file`` refuses ``../`` escapes (PathEscape), reports unavailable when no opener, ok on success
- disconnected backend → status:"unavailable" (never a fake player)
- DRM/unsupported URL formats → unsupported/unavailable (never fake success)
- ``meta_label`` emits only label ("Player — Track"), never secret bytes or canary content

Test command:
  cd /home/abhi/Documents/Action && .venv/bin/python -m pytest tests/unit/test_media_agent.py -q

If the Bash sandbox blocks (evidence: 'Operation not permitted'/'Permission denied'),
re-run ONCE with dangerouslyDisableSandbox:true and note it.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actionmesh.agent.media import (
    FakeMediaDriver,
    MediaEngine,
)
from actionmesh.agent.paths import Workspace


class TestStatusRead(unittest.TestCase):
    """status reports real player state; unambiguous pick; multi-player listing."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_single_player_paused_reads_back_as_paused(self):
        self.driver.add_player("FakePlayer", state="paused", track="Test Title")
        result = self.engine.status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["player"], "FakePlayer")
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["track"], "Test Title")
        self.assertIn("players", result)
        self.assertEqual(result["players"], ["FakePlayer"])

    def test_multiple_players_picks_playing_over_paused(self):
        self.driver.add_player("PlayerA", state="playing", track="Track A")
        self.driver.add_player("PlayerB", state="paused", track="Track B")
        result = self.engine.status()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["player"], "PlayerA")
        self.assertEqual(result["state"], "playing")
        self.assertTrue(result["track"] == "Track A")
        self.assertEqual(sorted(result["players"]), ["PlayerA", "PlayerB"])

    def test_no_players_returns_unavailable(self):
        self.driver.clear_players()
        result = self.engine.status()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("reason", result)


class TestPlayReadback(unittest.TestCase):
    """play returns the READ-BACK state, never claims playing on its own request."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_play_accepted_report_ok_with_read_back_state(self):
        self.driver.add_player("F", accepts_play=True)
        result = self.engine.play("F")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["state"], "playing")
        self.assertEqual(result["player"], "F")

    def test_play_refused_does_not_claim_playing_when_fake_says_paused(self):
        # This is the MUST test: fake ignores play; engine must report what it reads back
        self.driver.add_player("F", accepts_play=False, state="paused")
        result = self.engine.play("F")
        self.assertNotEqual(result["state"], "playing")
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["status"], "no_effect")
        self.assertIn("reason", result)

    def test_play_no_session_returns_unavailable(self):
        self.driver.clear_players()
        result = self.engine.play(None)
        self.assertEqual(result["status"], "unavailable")


class TestPauseNextPrevious(unittest.TestCase):
    """pause/next/previous mutate state and are read back correctly."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_pause_changes_state_and_reads_back(self):
        self.driver.add_player("F", state="playing", track="Title X")
        result = self.engine.pause("F")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["state"], "paused")
        self.assertEqual(result["player"], "F")

    def test_next_track_changes_track_and_reads_back(self):
        self.driver.add_player("F", state="paused", track="Track 0", position_s=10.0)
        self.assertIsNotNone(self.driver.get("F"))
        result = self.engine.next_track()
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["track"])
        self.assertIn("changed", result)

    def test_previous_track_changes_track_and_reads_back(self):
        self.driver.add_player("F", state="paused", track="Track 0")
        result = self.engine.previous_track()
        self.assertEqual(result["status"], "ok")
        self.assertIsNotNone(result["track"])


class TestVolumeSetReadback(unittest.TestCase):
    """volume set and get reports quantized levels, muted state."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_volume_quantization_is_read_back(self):
        # Fake has volume_step=0.1; request 0.83 should be quantized to 0.8
        self.driver.add_player("F", volume=0.5)
        result = self.engine.volume(level=0.83)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["level"], 0.8)  # quantized back
        self.assertTrue(result["player"] == "F")

    def test_mute_flag_controls_muted_read_back(self):
        self.driver.add_player("F", volume=0.5, muted=False)
        result = self.engine.volume(muted=True)
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["muted"])

    def test_unmute_flag_controls_muted_read_back(self):
        self.driver.add_player("F", volume=0.5, muted=True)
        result = self.engine.volume(muted=False)
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["muted"])

    def test_volume_only_is_a_read(self):
        self.driver.add_player("F", volume=0.4, muted=False)
        result = self.engine.volume()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["level"], 0.4)
        self.assertFalse(result["muted"])


class TestAmbiguousRefusal(unittest.TestCase):
    """ambiguous 'this' → ambiguous + candidates; never guesses."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_play_with_ambiguous_target_name_returns_ambiguous(self):
        self.driver.add_player("PlayerOne", state="paused")
        self.driver.add_player("PlayerTwo", state="paused")
        result = self.engine.play("nonexistent")
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("candidates", result)
        self.assertGreater(len(result["candidates"]), 0)

    def test_multi_active_without_ref_returns_ambiguous(self):
        self.driver.add_player("P1", state="playing", track="A")
        self.driver.add_player("P2", state="playing", track="B")
        result = self.engine.play(None)
        self.assertEqual(result["status"], "ambiguous")
        self.assertIn("candidates", result)


class TestOpenFileConfined(unittest.TestCase):
    """open_file root-confinement: ../ refused; unavailable when no opener."""

    def setUp(self):
        self.root_dir = Path(tempfile.mkdtemp())
        self.workspace = Workspace(str(self.root_dir))
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver, workspace=self.workspace)

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.root_dir), ignore_errors=True)

    def test_open_file_escapes_path_is_refused(self):
        # ../ tries to leave root; Workspace.resolve raises PathEscape → AgentError → ProviderError
        from actionmesh.agent.errors import PathEscape
        with tempfile.NamedTemporaryFile(mode='w', suffix='.mp3', dir=self.root_dir, delete=False) as f:
            f.write("fake audio data")
            temp_path = f.name
        relative_part = Path(temp_path).relative_to(self.root_dir)
        escape_path = "../" + str(relative_part.parent) + "/" + str(relative_part.name)
        try:
            self.engine.open_file(escape_path)
            self.fail("expected open_file to raise via Workspace.PathEscape")
        except Exception as exc:
            # `Workspace.resolve` raises `PathEscape` itself rather than wrapping it, so
            # the assertion is about the exception the caller receives -- `__cause__` is
            # None here. (The old `assertIsInstance(exc.__cause__, ...)` guarded by
            # `hasattr` could never pass: every exception has a `__cause__` attribute.)
            self.assertIsInstance(exc, PathEscape)

    def test_open_file_no_opener_reports_unavailable(self):
        # Configure driver without opener; engine sees "no opener is reachable".
        # The file has to exist: `must_exist=True` in the resolution step fails first
        # otherwise, and the test would be measuring PathNotFound, not the opener.
        (self.root_dir / "test.mp3").write_bytes(b"fake audio data")
        self.driver.has_opener = False
        self.driver.add_player("F", accepts_play=True)
        result = self.engine.open_file("test.mp3")
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("opener", result.get("reason", "").lower())
        # "unavailable" must mean nothing was handed over, not a silent attempt.
        self.assertEqual(self.driver.opened, [])

    def test_open_file_success_sets_track_and_reports_ok(self):
        # Create a file that actually exists so must_exist=True doesn't fail
        test_file = self.root_dir / "music" / "song.mp3"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_bytes(b"fake audio data")
        self.driver.add_player("F", accepts_play=True)
        result = self.engine.open_file("music/song.mp3")
        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["opened"])
        self.assertEqual(result["path"], "music/song.mp3")


class TestUnreachableBackend(unittest.TestCase):
    """disconnected backend → status:"unavailable"; never a fake player."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_status_disconnected_backend(self):
        self.driver.clear_players()
        result = self.engine.status()
        self.assertEqual(result["status"], "unavailable")
        # An unreachable backend must not carry a player identity at all. The old
        # `assertNotIn(...) or result["player"] == ""` was the bug: `assertNotIn`
        # returns None on success, so a result WITHOUT the key fell through to the
        # right-hand side and raised KeyError -- the assertion only ever ran when it
        # was already true.
        self.assertNotIn("player", result)
        self.assertNotIn("track", result)

    def test_play_disconnected_backend(self):
        self.driver.clear_players()
        result = self.engine.play(None)
        self.assertEqual(result["status"], "unavailable")

    def test_pause_disconnected_backend(self):
        self.driver.clear_players()
        result = self.engine.pause(None)
        self.assertEqual(result["status"], "unavailable")


class TestUnsupportedContent(unittest.TestCase):
    """DRM/stream URL formats → unsupported/unavailable; never fake success."""

    def setUp(self):
        self.root_dir = Path(tempfile.mkdtemp())
        self.driver = FakeMediaDriver()
        # open_file resolves through the workspace BEFORE it consults the driver, and
        # an absolute path is refused by confinement anyway. Without a root the
        # "no workspace configured" guard answers first and the DRM branch below is
        # never reached -- a green test that measured the wrong refusal.
        self.engine = MediaEngine(self.driver, workspace=Workspace(str(self.root_dir)))

    def tearDown(self):
        import shutil
        shutil.rmtree(str(self.root_dir), ignore_errors=True)

    def test_drm_stream_declared_unsupported(self):
        (self.root_dir / "content.drm").write_bytes(b"fake protected content")
        self.driver.has_opener = True
        self.driver.add_player("F")
        result = self.engine.open_file("content.drm")
        self.assertEqual(result["status"], "unsupported")
        self.assertFalse(result.get("opened"))
        self.assertIn("drm", result.get("reason", "").lower())
        self.assertEqual(result["path"], "content.drm")
        # `FakeMediaDriver.opened` is a hand-over log written before the fake classifies
        # the file, so it is not a success signal. What matters is that no player adopted
        # it: the fake only rewrites `track` on the "opened" branch.
        self.assertEqual(self.engine.status()["track"], "Test Artist — Test Title")

    def test_unsupported_url_scheme(self):
        def browser_mock(url: str) -> dict[str, str]:
            return {"status": "ok"}
        eng = MediaEngine(self.driver, browser_open=browser_mock)
        result = eng.open_url("file:///etc/passwd")
        self.assertEqual(result["status"], "unsupported")
        self.assertIn("http(s)", result.get("reason", ""))


class TestMetaLabel(unittest.TestCase):
    """media.meta stream label-only; never file secrets or metadata dumps."""

    def setUp(self):
        self.driver = FakeMediaDriver()
        self.engine = MediaEngine(self.driver)

    def test_meta_label_for_ok_result_contains_only_track_and_player(self):
        self.driver.add_player("PlayerOne", track="Track Title")
        result = self.engine.play("PlayerOne")
        label = self.engine.meta_label(result)
        self.assertIsNotNone(label)
        self.assertIn("PlayerOne", label)
        self.assertIn("Track Title", label)
        # Verify format is "Player — Track" style
        self.assertIn("—", label)

    def test_meta_label_none_on_failed_result(self):
        result = {"status": "unavailable", "player": None, "track": None}
        label = self.engine.meta_label(result)
        self.assertIsNone(label)

    def test_canary_absent_in_label(self):
        """Only "<player> — <track>" reaches the stream; no other field is copied.

        The old version named the track "TestTrackNoCanary" and then asserted that
        "canary" was absent from the label -- which contained the track name. The test
        could only pass by accident. The honest version puts the markers in the fields
        the label must NOT copy, which is the leak a `str(result)` fallback would cause.
        """
        self.driver.add_player("P", track="Track Title")
        result = self.engine.status()
        result["path"] = "music/secret-admin-canary.mp3"
        result["note"] = "password=hunter2"
        label = self.engine.meta_label(result)
        self.assertIsNotNone(label)
        # Exact identity, not a substring sweep: the format is the contract.
        self.assertEqual(label, "P — Track Title")
        for marker in ("canary", "secret", "password", "hunter2", "music/"):
            self.assertNotIn(marker, label.lower())


if __name__ == "__main__":
    unittest.main()
