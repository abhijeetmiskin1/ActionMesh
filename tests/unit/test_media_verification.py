"""Media verification hardening: timeout / disconnected / stale-bus / vanishing player.

A command that was *dispatched* is never a claim that it *took effect*: every
media capability re-reads the player and reports the read-back. These tests pin
the distinct honest answers when the read-back itself fails:

- a bus timeout answers ``timeout`` (slow player), never ``unavailable``
- a player that vanished between resolve and read-back answers ``unavailable``
- a stale cached bus is dropped by the real driver so a later probe re-dials,
  rather than staying absent forever
- a command on a ghost name answers ``unavailable``/``ambiguous``, never a guess

These tests use fixtures. The opt-in mpv test lives in
`tests/e2e/test_phase4_live_acceptance.py`.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import Mock

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


def test_mpris_uses_the_reply_type_for_each_dbus_method(monkeypatch) -> None:
    glib = SimpleNamespace(
        Variant=lambda signature, arguments: (signature, arguments),
        VariantType=lambda signature: signature,
    )
    monkeypatch.setitem(sys.modules, "gi", SimpleNamespace(require_version=Mock()))
    monkeypatch.setitem(sys.modules, "gi.repository", SimpleNamespace(GLib=glib))
    bus = Mock()
    bus.call_sync.return_value.unpack.return_value = ({"PlaybackStatus": "Paused"},)
    driver = MprisMediaDriver()
    monkeypatch.setattr(driver, "_session_bus", lambda: bus)

    assert driver.get("mpv").state == "paused"
    get_all = bus.call_sync.call_args.args
    assert get_all[3:6] == ("GetAll", ("(s)", (driver._PLAYER_IFACE,)), "(a{sv})")

    assert driver.command("mpv", "play")
    play = bus.call_sync.call_args.args
    assert play[3:6] == ("Play", ("()", ()), "()")


def test_mpris_open_confirms_the_current_file_after_dispatch(monkeypatch) -> None:
    driver = MprisMediaDriver()
    monkeypatch.setattr(driver, "_player_names", lambda: ["mpv", "vlc"])
    dispatch = Mock()
    monkeypatch.setattr(driver, "_call", dispatch)
    monkeypatch.setattr("actionmesh.agent.media.time.sleep", lambda _: None)

    stale = {"Metadata": {"xesam:url": "file:///tmp/old.wav"}}
    get_all = Mock(return_value=stale)
    monkeypatch.setattr(driver, "_get_all", get_all)
    assert driver.open_file("/tmp/new track.wav") == "rejected"
    dispatch.assert_called_once()

    dispatch.reset_mock()
    get_all.side_effect = [
        stale, {"Metadata": {"xesam:url": "file:///tmp/new%20track.wav"}},
    ]
    assert driver.open_file("/tmp/new track.wav") == "opened"
    assert dispatch.call_args.args[3:] == ("OpenUri", "(s)", ("file:///tmp/new%20track.wav",))
