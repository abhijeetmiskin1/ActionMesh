"""D-Bus wire regressions, distinct from real-player acceptance."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from actionmesh.agent import mpris, native_desktop
from actionmesh.agent.media import HostMprisMediaDriver, production_driver


def test_properties_and_void_methods_use_their_actual_dbus_reply_types() -> None:
    driver = mpris.MprisMediaDriver()
    bus = Mock()
    driver._bus = bus
    driver._bus_checked = True
    glib = SimpleNamespace(Variant=lambda signature, values: (signature, values), VariantType=lambda value: value)
    with patch.dict("sys.modules", {"gi": Mock(), "gi.repository": SimpleNamespace(GLib=glib)}):
        driver._call("org.mpris.MediaPlayer2.fixture", "/org/mpris/MediaPlayer2",
            driver._PROPS_IFACE, "GetAll", "(s)", (driver._PLAYER_IFACE,))
        assert bus.call_sync.call_args.args[5] == "(a{sv})"
        driver._call("org.mpris.MediaPlayer2.fixture", "/org/mpris/MediaPlayer2",
            driver._PLAYER_IFACE, "Pause")
        assert bus.call_sync.call_args.args[5] == "()"
        assert bus.call_sync.call_args.args[4] == ("()", ())


def test_file_open_uses_player_open_uri_without_broadcast_or_wrong_tracklist_method() -> None:
    driver = mpris.MprisMediaDriver()
    with patch.object(driver, "list_players", return_value=[mpris.PlayerInfo(name="fixture")]), \
         patch.object(driver, "_get_all", return_value={"CanPlay": True}), \
         patch.object(driver, "_call") as call:
        assert driver.open_file("/workspace/a b.wav") == "opened"
    assert call.call_count == 1
    assert call.call_args.args[2:5] == (driver._PLAYER_IFACE, "OpenUri", "(s)")
    assert call.call_args.args[5] == ("file:///workspace/a%20b.wav",)


def test_file_open_refuses_ambiguous_players() -> None:
    driver = mpris.MprisMediaDriver()
    with patch.object(driver, "list_players", return_value=[mpris.PlayerInfo(name="first"), mpris.PlayerInfo(name="second")]), \
         patch.object(driver, "_call") as call:
        assert driver.open_file("/workspace/test.wav") == "unavailable"
    call.assert_not_called()


def test_metadata_labels_are_bounded_redacted_and_never_contain_media_urls() -> None:
    driver = mpris.MprisMediaDriver()
    with patch.object(driver, "_get_all", return_value={"PlaybackStatus": "Playing", "Metadata": {
        "xesam:title": "secret=do-not-echo " + "A" * 500,
        "xesam:artist": "Test", "xesam:url": "file:///private/path.wav"}}):
        player = driver.get("fixture")
    assert player is not None
    assert len(player.track) <= 200
    assert "do-not-echo" not in player.track
    assert "/private/" not in repr(player)


@pytest.mark.parametrize("operation,parameters", [
    ("eval", {}), ("get", {"name": "name;code"}), ("command", {"name": "fixture", "operation": "Raise"}),
    ("command", {"name": "fixture", "operation": "volume", "argument": float("nan")}),
    ("command", {"name": "fixture", "operation": "volume", "argument": 2}),
    ("open_file", {"path": "relative.wav"}), ("list", {"host": "external"}),
])
def test_host_transport_refuses_non_schema_commands(operation, parameters) -> None:
    with patch.object(native_desktop, "host_call", AsyncMock()) as call:
        answer = HostMprisMediaDriver()._request(operation, parameters)
    assert answer["status"] == "unavailable"
    call.assert_not_called()


def test_media_native_transport_has_its_own_boundary_and_never_sends_credentials(monkeypatch) -> None:
    monkeypatch.setenv("ACTIONMESH_API_KEY", "private-control-plane-key")
    from actionmesh.agent.runner import CommandOutcome

    run = AsyncMock(return_value=CommandOutcome(exit_code=0, stdout='{"status":"ok","players":[]}'))
    with patch.object(native_desktop.CommandRunner, "run", run):
        answer = asyncio.run(native_desktop.host_call("media", {"operation": "list", "parameters": {}}))
    assert answer["players"] == []
    decision = run.call_args.args[0]
    assert decision.arguments[0] == "-I"
    assert decision.arguments[1].endswith("/mpris.py")
    assert "ACTIONMESH_API_KEY" not in decision.environment
    monkeypatch.setenv("ACTIONMESH_NATIVE_MEDIA", "1")
    monkeypatch.delenv("ACTIONMESH_NATIVE_DESKTOP", raising=False)
    assert isinstance(production_driver(), HostMprisMediaDriver)
