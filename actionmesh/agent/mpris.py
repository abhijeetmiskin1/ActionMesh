"""Standalone, fixed-operation MPRIS transport for native Linux bindings.

This module deliberately imports no application package: a bundled CPython can
run this shipped file with the host interpreter that owns PyGObject's ABI.
It never accepts commands, URLs, selectors, or executable code from a model.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from typing import Any


class MediaTimeoutError(RuntimeError):
    """A player read did not answer within the bus deadline.

    Distinct from a player that is gone or a bus that does not exist: the
    player may still be there, merely slow. Fail-closed like everything else
    here, but reported with its own ``timeout`` status so slow is not
    conflated with absent. Fake drivers raise this to stand in for a slow
    real player.
    """


#: Substrings that identify a D-Bus/bus timeout in exception text. D-Bus
#: errors arrive as ``org.freedesktop.DBus.Error.Timeout: ...`` (GLib.Error),
#: or plain ``"Timeout was reached"`` depending on the binding. Anything else
#: is treated as "not reachable", which is the pre-existing behaviour.
_TIMEOUT_MARKERS = ("timeout", "timed out", "timedout")


def _is_timeout_error(exc: BaseException) -> bool:
    """True when a D-Bus/bus exception is a timeout, not a plain miss."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return "org.freedesktop.dbus.error.timeout" in text or any(
        marker in text for marker in _TIMEOUT_MARKERS
    )


@dataclass
class PlayerInfo:
    """What a driver says one reachable player's real state is, right now.

    ``track`` is a display label assembled by the driver ("Artist — Title", or the
    filename). Drivers must not put file paths, URIs or metadata blobs here: the
    label rides the transient channel.
    """

    name: str
    track: str | None = None
    state: str = "unknown"  # playing | paused | stopped | unknown
    position_s: float = -1.0  # negative when the player does not report it
    duration_s: float = -1.0
    volume: float = -1.0  # 0.0..1.0, negative when not reported
    muted: bool = False
    track_id: str = ""  # opaque digest, never a path or URI


class MediaDriver:
    """The player backend interface. One implementation per transport (MPRIS, fake).

    A driver reports facts about real players and issues fixed commands. It has no
    notion of capabilities, statuses, or JSON: that is the engine's layer.
    """

    name: str = "abstract"

    def list_players(self) -> list[PlayerInfo]:
        """Every currently reachable media player, with its live state.

        Empty list means no player is reachable — the honest case, not an error.
        """
        raise NotImplementedError

    def get(self, name: str) -> PlayerInfo | None:
        """Re-read ONE player by exact name. None if it is gone.

        This is what a command's read-back goes through — the engine never
        reconstructs state from what it asked for.
        """
        raise NotImplementedError

    def command(self, name: str, operation: str, argument: Any = None) -> bool:
        """Issue one fixed operation on one player.

        ``operation`` is a closed set: play | pause | stop | next | previous |
        volume | mute. Returns True when the command was *dispatched* — never a
        claim that it took effect. The engine re-reads with :meth:`get`.
        """
        raise NotImplementedError

    def open_file(self, absolute_path: str) -> str:
        """Hand an already-resolved local media file to the player or OS opener.

        Returns one of: "opened", "rejected" (not a format/service this backend
        handles), "drm" (protected stream — not a capability), "unavailable"
        (no opener mechanism exists here). Never downloads anything.
        """
        raise NotImplementedError

    def close(self) -> None:
        """Release any bus connections. Safe to call twice."""


class MprisMediaDriver(MediaDriver):
    """The real backend: org.mpris.MediaPlayer2 over the session D-Bus (Linux).

    Chosen because MPRIS is the standard Linux media-player control surface — every
    mainstream desktop player (mpris:MPRISVersion 1.0/2.0) exposes it, and it is the
    same bus family `device_screen` already reaches through PyGObject/Gio, so this
    adds no new dependency kind, only a new bus name filter. Readback is a live
    Properties read, so a command that did not take effect is read as not having
    taken effect.

    If PyGObject or the session bus is missing, the adapter reports no reachable
    players. Real-player acceptance is recorded separately from fixture tests.

    Privacy of the label: only ``xesam:title`` and ``xesam:artist`` become the track
    label. ``xesam:url`` (a filesystem path or stream URL) is deliberately dropped.
    """

    name = "mpris"
    _PREFIX = "org.mpris.MediaPlayer2."
    _PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
    _ROOT_IFACE = "org.mpris.MediaPlayer2"
    _PROPS_IFACE = "org.freedesktop.DBus.Properties"

    def __init__(self) -> None:
        self._bus: Any = None
        self._bus_checked = False

    # -- plumbing ------------------------------------------------------------
    def _session_bus(self) -> Any:
        """The session bus, or None. One probe per process; never raises."""
        if self._bus_checked and self._bus is not None:
            return self._bus
        self._bus_checked = True
        extra = os.environ.get("ACTIONMESH_SYSTEM_PACKAGES", "")
        if extra and os.path.isdir(extra) and extra not in sys.path:
            sys.path.append(extra)
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio

            self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        except Exception:
            self._bus = None
        return self._bus

    def _drop_cached_bus(self) -> None:
        """Forget a cached bus so the next probe re-dials.

        A session bus can exist when the first probe runs (e.g. a test harness
        connects late) or vanish after it. Caching the probe result forever
        would make the first failure permanent for the process lifetime.
        """
        self._bus = None
        self._bus_checked = False

    def _call(
        self, destination: str, path: str, interface: str, method: str,
        signature: str = "()", parameters: Any = None,
    ) -> Any:
        import gi

        gi.require_version("GLib", "2.0")
        from gi.repository import GLib

        bus = self._session_bus()
        if bus is None:
            raise RuntimeError("no session bus")
        return bus.call_sync(
            destination,
            path,
            interface,
            method,
            GLib.Variant(signature, parameters or ()),
            GLib.VariantType("(a{sv})" if interface == self._PROPS_IFACE and method == "GetAll" else "()"),
            0,
            2000,  # 2s ceiling: a player that cannot answer in 2s is not reachable
            None,
        )

    def _get_all(self, destination: str, path: str, interface: str) -> dict[str, Any]:
        reply = self._call(
            destination, path, self._PROPS_IFACE, "GetAll", "(s)", (interface,)
        )
        raw = reply.unpack()[0]
        return {str(key): _unwrap(value) for key, value in raw.items()}

    def _player_names(self) -> list[str]:
        try:
            bus = self._session_bus()
        except Exception as exc:
            if _is_timeout_error(exc):
                raise MediaTimeoutError(str(exc)) from exc
            self._drop_cached_bus()
            return []
        if bus is None:
            return []
        try:
            import gi

            gi.require_version("GLib", "2.0")
            from gi.repository import GLib

            reply = bus.call_sync(
                "org.freedesktop.DBus",
                "/org/freedesktop/DBus",
                "org.freedesktop.DBus",
                "ListNames",
                GLib.Variant("()", ()),
                GLib.VariantType("(as)"),
                0,
                2000,
                None,
            )
        except Exception as exc:
            if _is_timeout_error(exc):
                raise MediaTimeoutError(str(exc)) from exc
            self._drop_cached_bus()
            return []
        if reply is None:
            return []
        names = reply.unpack()[0]
        candidates = sorted(n for n in names if isinstance(n, str) and n.startswith(self._PREFIX))[:32]
        owners: set[str] = set()
        unique: list[str] = []
        for name in candidates:
            try:
                owner_reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                    "org.freedesktop.DBus", "GetNameOwner", GLib.Variant("(s)", (name,)),
                    GLib.VariantType("(s)"), 0, 500, None)
                owner = owner_reply.unpack()[0]
                # VLC publishes a stable name and an instance name from separate
                # connections in the same process. They address the same player.
                if re.fullmatch(r"org\.mpris\.MediaPlayer2\.vlc(?:\.instance[0-9]+)?", name):
                    pid_reply = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                        "org.freedesktop.DBus", "GetConnectionUnixProcessID", GLib.Variant("(s)", (name,)),
                        GLib.VariantType("(u)"), 0, 500, None)
                    owner = "vlc-process-" + str(pid_reply.unpack()[0])
            except Exception as exc:
                if _is_timeout_error(exc):
                    raise MediaTimeoutError("player identity probe timed out") from exc
                continue
            if owner not in owners:
                owners.add(owner)
                unique.append(name)
        return unique

    def _info(self, destination: str) -> PlayerInfo | None:
        """Read one player's live properties. None if it vanished mid-read.

        A bus timeout is re-raised as :class:`MediaTimeoutError` (a slow
        player is not an absent player); other errors keep meaning "gone".
        """
        try:
            props = self._get_all(destination, "/org/mpris/MediaPlayer2", self._PLAYER_IFACE)
        except MediaTimeoutError:
            raise
        except Exception as exc:
            if _is_timeout_error(exc):
                raise MediaTimeoutError(str(exc)) from exc
            return None
        meta = props.get("Metadata") or {}
        title = _first_str(meta.get("xesam:title"))
        artist = _first_str(meta.get("xesam:artist"))
        label = None
        if title and artist:
            label = _safe_label(f"{artist} — {title}")
        elif title:
            label = _safe_label(title)
        # NOTE: meta.get("xesam:url") is deliberately not used. It is a path.
        position_us = props.get("Position")
        length_us = meta.get("mpris:length")
        try:
            duration_s = float(length_us) / 1_000_000.0 if length_us is not None else -1.0
        except (TypeError, ValueError):
            duration_s = -1.0
        try:
            position_s = float(position_us) / 1_000_000.0 if position_us is not None else -1.0
        except (TypeError, ValueError):
            position_s = -1.0
        return PlayerInfo(
            name=destination.removeprefix(self._PREFIX),
            track=label,
            track_id=hashlib.sha256(str(meta["mpris:trackid"]).encode()).hexdigest() if meta.get("mpris:trackid") else "",
            state=_normalize_state(props.get("PlaybackStatus")),
            position_s=position_s,
            duration_s=duration_s,
            volume=_as_float(props.get("Volume"), -1.0),
            muted=bool(props.get("Volume") is not None and _as_float(props.get("Volume"), 1.0) <= 0.0),
        )

    # -- MediaDriver surface ---------------------------------------------------
    def list_players(self) -> list[PlayerInfo]:
        out = []
        for destination in self._player_names():
            info = self._info(destination)  # may raise MediaTimeoutError
            if info is not None:
                out.append(info)
        return out

    def get(self, name: str) -> PlayerInfo | None:
        return self._info(self._PREFIX + name) if name else None

    def command(self, name: str, operation: str, argument: Any = None) -> bool:
        """One MPRIS method call, dispatched only. Effect is what ``get`` reads back."""
        destination = self._PREFIX + name
        method = {
            "play": "Play",
            "pause": "Pause",
            "stop": "Stop",
            "next": "Next",
            "previous": "Previous",
        }.get(operation)
        try:
            if method is not None:
                # NOT Raise: a media command must not yank window focus away from an
                # active text input. Playback control is not window control.
                self._call(destination, "/org/mpris/MediaPlayer2", self._PLAYER_IFACE, method)
                return True
            if operation == "volume":
                import gi

                gi.require_version("GLib", "2.0")
                from gi.repository import GLib

                self._call(
                    destination,
                    "/org/mpris/MediaPlayer2",
                    self._PROPS_IFACE,
                    "Set",
                    "(ssv)",
                    (self._PLAYER_IFACE, "Volume", GLib.Variant("d", float(argument))),
                )
                return True
            if operation == "mute":
                # MPRIS has no mute property; volume-to-0 is the honest
                # equivalent, and the read-back below reports exactly what
                # the player did. The pre-mute level for unmute is only read
                # when a level is actually available; inventing 0.5 would
                # report hardware state nobody chose.
                if not argument:
                    raise ValueError("unmute needs the pre-mute level; none is known")
                return self.command(name, "volume", 0.0)
        except MediaTimeoutError:
            raise
        except Exception as exc:
            if _is_timeout_error(exc):
                raise MediaTimeoutError(str(exc)) from exc
            return False
        return False

    def open_file(self, absolute_path: str) -> str:
        """OpenUri a file:// URI in a running player; "unavailable" if none is.

        No downloader, no transcode, no DRM handling — a protected file is refused
        by the player, and the refusal is what gets reported. Any bus timeout
        propagates as :class:`MediaTimeoutError` (a slow player), never folded
        into "rejected".
        """
        import urllib.parse

        players = self.list_players()
        if not players:
            return "unavailable"
        if len(players) > 1:
            active = [player for player in players if player.state in {"playing", "paused"}]
            if len(active) != 1:
                return "unavailable"
            players = active
        destinations = [self._PREFIX + players[0].name]
        uri = "file://" + urllib.parse.quote(absolute_path)
        for destination in destinations:
            try:
                props = self._get_all(destination, "/org/mpris/MediaPlayer2", self._PLAYER_IFACE)
            except MediaTimeoutError:
                raise
            except Exception as exc:
                if _is_timeout_error(exc):
                    raise MediaTimeoutError(str(exc)) from exc
                continue
            if not props.get("CanPlay", True):
                continue
            try:
                self._call(
                    destination, "/org/mpris/MediaPlayer2", self._PLAYER_IFACE,
                    "OpenUri", "(s)", (uri,),
                )
            except MediaTimeoutError:
                raise
            except Exception as exc:
                if _is_timeout_error(exc):
                    raise MediaTimeoutError(str(exc)) from exc
                continue
            # One player took the track. Broadcasting to every player on the
            # bus would play the same file N times; the first confirmed
            # hand-over is the result.
            return "opened"
        return "rejected"

    def close(self) -> None:
        self._bus = None
        self._bus_checked = False


def _unwrap(value: Any) -> Any:
    """GLib.Variant -> python, one level. Metadata values arrive wrapped."""
    inner = getattr(value, "unpack", None)
    return inner() if callable(inner) else value


def _first_str(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _normalize_state(value: Any) -> str:
    mapping = {"Playing": "playing", "Paused": "paused", "Stopped": "stopped"}
    return mapping.get(str(value or ""), "unknown")


def _as_float(value: Any, default: float) -> float:
    try:
        result = float(value)  # type: ignore[arg-type]
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default




def _safe_label(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"(?i)\b(bearer\s+|(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*)\S+", r"\1<redacted>", value)
    value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "<redacted>", value)
    return value[:200]


def valid_request(operation: str, parameters: Any) -> bool:
    if not isinstance(parameters, dict):
        return False
    keys = {"list": set(), "get": {"name"}, "command": {"name", "operation", "argument"},
            "open_file": {"path"}}
    if operation not in keys or set(parameters) - keys[operation]:
        return False
    if operation in {"get", "command"}:
        name = parameters.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}", name):
            return False
    if operation == "command":
        command = parameters.get("operation")
        if command not in {"play", "pause", "stop", "next", "previous", "volume", "mute"}:
            return False
        value = parameters.get("argument")
        if command == "volume":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
                return False
        elif command == "mute":
            if not isinstance(value, bool):
                return False
        elif value is not None:
            return False
    if operation == "open_file":
        path = parameters.get("path")
        if not isinstance(path, str) or not os.path.isabs(path) or len(path) > 4096 or "\0" in path:
            return False
    return True


def _main() -> None:
    import json
    import sys

    if sys.argv[1:] == ["--native-probe"]:
        try:
            import gi

            gi.require_version("Gio", "2.0")
            from gi.repository import Gio  # noqa: F401

            print(json.dumps({"available": True}))
        except (ImportError, ValueError):
            print(json.dumps({"available": False}))
        return
    if sys.argv[1:] != ["--native-request"]:
        return
    try:
        raw = os.environ.pop("ACTIONMESH_NATIVE_REQUEST", "")
        if len(raw.encode("utf-8")) > 64 * 1024:
            raise ValueError("request limit")
        request = json.loads(raw)
        operation, parameters = request.get("operation"), request.get("parameters")
        if not isinstance(operation, str) or not valid_request(operation, parameters):
            raise ValueError("invalid request")
        driver = MprisMediaDriver()
        answer: dict[str, Any]
        if operation == "list":
            answer = {"status": "ok", "players": [asdict(player) for player in driver.list_players()]}
        elif operation == "get":
            player = driver.get(parameters["name"])
            answer = {"status": "ok", "player": asdict(player) if player else None}
        elif operation == "command":
            answer = {"status": "ok", "dispatched": driver.command(parameters["name"],
                parameters["operation"], parameters.get("argument"))}
        else:
            answer = {"status": "ok", "outcome": driver.open_file(parameters["path"])}
    except MediaTimeoutError:
        answer = {"status": "timeout"}
    except Exception:
        answer = {"status": "unavailable"}
    encoded = json.dumps(answer)
    if len(encoded.encode("utf-8")) > 256 * 1024:
        encoded = json.dumps({"status": "unavailable"})
    print(encoded)


if __name__ == "__main__":
    _main()
