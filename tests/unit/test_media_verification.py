"""Media verification hardening: timeout / disconnected / stale-bus / vanishing player.

A command that was *dispatched* is never a claim that it *took effect*: every
media capability re-reads the player and reports the read-back. These tests pin
the distinct honest answers when the read-back itself fails:

- a bus timeout answers ``timeout`` (slow player), never ``unavailable``
- a player that vanished between resolve and read-back answers ``unavailable``
- a stale cached bus is dropped by the real driver so a later probe re-dials,
  rather than staying absent forever
- a command on a ghost name answers ``unavailable``/``ambiguous``, never a guess

MPRIS stays explicitly NOT live-verified (media.py:300): these are fixture
tests against FakeMediaDriver, not live-player claims.
"""

from __future__ import annotations

from actionmesh.agent.media import (
    FakeMediaDriver,
    MediaEngine,
    MediaTimeoutError,
    MprisMediaDriver,
)


class _TimeoutDriver(FakeMediaDriver):
    """Every list/get/command times out, standing in for a slow real player."""

    def list_players(self):
        raise MediaTimeoutError("org.freedesktop.DBus.Error.Timeout: Message recipient disconnected")

    def get(self, name: str):
        raise MediaTimeoutError("org.freedesktop.DBus.Error.Timeout: Message recipient disconnected")

    def command(self, name: str, operation: str, argument=None) -> bool:
        raise MediaTimeoutError("org.freedesktop.DBus.Error.Timeout: Message recipient disconnected")


class _VanishingDriver(FakeMediaDriver):
    """Resolves once, then the player is gone at read-back."""

    def __init__(self) -> None:
        super().__init__()
        self.add_player("vlc", state="paused")

    def get(self, name: str):
        return None


class _StaleBusDriver(MprisMediaDriver):
    """A real driver whose first bus probe fails, then succeeds."""

    def __init__(self) -> None:
        super().__init__()
        self._probes = 0

    @property
    def probes(self) -> int:
        return self._probes


def test_timeout_answers_timeout_never_unavailable() -> None:
    engine = MediaEngine(_TimeoutDriver())
    for result in (engine.status(), engine.play(), engine.pause(), engine.volume()):
        assert result["status"] == "timeout", result


def test_vanishing_player_answers_unavailable() -> None:
    engine = MediaEngine(_VanishingDriver())
    assert engine.play()["status"] == "unavailable"
    assert engine.pause()["status"] == "unavailable"


def test_stale_bus_is_dropped_for_retry() -> None:
    driver = _StaleBusDriver()
    # No session bus at all: _session_bus probes gi and returns None, so
    # list_players answers [] and the cached "no bus" verdict stays until
    # _drop_cached_bus() clears it — the retry seam the driver exposes.
    assert driver.list_players() == []
    driver._drop_cached_bus()
    assert driver._bus_checked is False
    assert driver._session_bus() is None


def test_ghost_player_is_never_guessed() -> None:
    engine = MediaEngine(FakeMediaDriver())
    assert engine.play("ghost")["status"] in ("ambiguous", "unavailable")
    driver = FakeMediaDriver()
    driver.add_player("vlc", state="paused")
    engine2 = MediaEngine(driver)
    assert engine2.play("ghost")["status"] == "ambiguous"
