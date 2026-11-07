"""C42 media control (Agent C): local playback through one driver interface.

Every capability in contract §2.3 is one method on :class:`MediaEngine`. The engine
never talks to D-Bus directly: it drives a :class:`MediaDriver`, exactly the way
``device_screen`` separates the capability surface from the AT-SPI/Gio adapter.
Unit tests inject :class:`FakeMediaDriver` — a controlled fake whose state the test
sets and can refuse to change. Production wires :class:`MprisMediaDriver`, the
org.mpris.MediaPlayer2 adapter over the session bus, at startup.

The honesty rules this module exists to enforce:

* **A playback command is a request, not a report.** ``play``/``pause``/``volume``
  issue the command and then *re-read the player*, and the result describes the
  read-back. If the player stayed paused, the result says so
  (``status:"no_effect"``) — the engine cannot report ``"playing"`` while the
  read-back says paused, and a test asserts exactly that.
* **No player is never a pretend player.** When the driver reports no reachable
  players, every capability answers ``status:"unavailable"`` with a reason — the
  ``device_screen`` pattern. There is no fallback state, no default track, no
  simulated session.
* **Ambiguity is returned, not guessed.** Several players with no single unambiguously
  active one, or a `ref` that names nothing reachable, answers
  ``status:"ambiguous"`` with the candidate list. Guessing which player a user means
  is worse than asking.
* **No secret downloads, no capability claims.** A DRM-protected stream is not a
  capability: it answers ``unsupported`` with the reason. ``open_url`` delegates to
  Agent A's browser capability — the engine never fetches, stores, or plays a URL
  itself.
* **User control is preserved.** ``open_file``/``open_url`` hand the file or URL to
  the player/browser; neither raises or grabs keyboard focus (MPRIS ``Raise`` is
  deliberately not called, so text in an active input is never lost), and neither
  moves the user's cursor. This is a behaviour the result depends on, not a side
  effect to be won back later.
* **The transient label is a label.** ``media.meta`` carries ``"<player> — <track>"``
  — the current track and player identity — and nothing else: no file contents, no
  ``xesam:url`` (which on MPRIS is a filesystem path), no metadata dump.

``open_file`` confinement is the provider's configured :class:`Workspace`: the path is
resolved with ``workspace.resolve(..., must_exist=True)`` and a refusal
(:class:`PathEscape`) propagates as an ``AgentError`` so the provider dispatch wraps
it as ``FailureKind.AUTHORIZATION``, exactly like every other confined capability.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from actionmesh.agent.mpris import (
    MediaDriver,
    MediaTimeoutError,
    MprisMediaDriver,
    PlayerInfo,
    _first_str,
)
from actionmesh.agent.paths import Workspace

#: The one transient stream this module emits (contract §6): track/player identity,
#: as a label. Deliberately not the metadata dict, not the file URI, not file bytes.
MEDIA_META_STREAM = "media.meta"

#: States a media player is *doing something* in. Anything else ("stopped",
#: "idle", unknown) is not an active session.
_ACTIVE_STATES = frozenset({"playing", "paused"})


class FakeMediaDriver(MediaDriver):
    """A controlled fake player for unit tests. NOT a simulator shipped as a backend.

    The test sets the state; commands mutate only what the fake was configured to
    allow. A player can be told to *ignore* the play command (``accepts_play=False``)
    so the test can prove the engine reports the read-back state and never a claim
    built from the request.
    """

    name = "fake"

    def __init__(self) -> None:
        self._players: dict[str, _FakePlayer] = {}
        #: When False, ``open_file`` says no opener exists (honest "unavailable").
        self.has_opener: bool = True
        #: Volume quantization the fake hardware imposes (e.g. 0.1 steps).
        self.volume_step: float = 0.1
        #: File-open commands are recorded here so refusal tests can assert nothing opened.
        self.opened: list[str] = []
        #: Formats this fake player declines.
        self.rejected_suffixes: tuple[str, ...] = ()

    # -- test-side configuration ------------------------------------------
    def add_player(
        self,
        name: str,
        *,
        state: str = "paused",
        track: str | None = "Test Artist — Test Title",
        position_s: float = 12.0,
        duration_s: float = 200.0,
        volume: float = 0.5,
        muted: bool = False,
        accepts_play: bool = True,
        rejects_volume: bool = False,
    ) -> _FakePlayer:
        """Register one reachable fake player and return it for direct tweaking."""
        player = _FakePlayer(
            name,
            state=state,
            track=track,
            position_s=position_s,
            duration_s=duration_s,
            volume=volume,
            muted=muted,
            accepts_play=accepts_play,
            rejects_volume=rejects_volume,
            volume_step=self.volume_step,
        )
        self._players[name] = player
        return player

    def clear_players(self) -> None:
        """Unplug every player: the no-backend case."""
        self._players.clear()

    # -- MediaDriver surface ------------------------------------------------
    def list_players(self) -> list[PlayerInfo]:
        return [player.info() for player in self._players.values()]

    def get(self, name: str) -> PlayerInfo | None:
        player = self._players.get(name)
        return player.info() if player is not None else None

    def command(self, name: str, operation: str, argument: Any = None) -> bool:
        player = self._players.get(name)
        if player is None:
            return False
        return player.run(operation, argument)

    def open_file(self, absolute_path: str) -> str:
        if not self.has_opener:
            return "unavailable"
        self.opened.append(absolute_path)
        lowered = absolute_path.lower()
        if any(lowered.endswith(suffix) for suffix in self.rejected_suffixes):
            return "rejected"
        if ".drm" in lowered:
            return "drm"
        name = absolute_path.rsplit("/", 1)[-1]
        for player in self._players.values():
            player.track = name
        return "opened"

    def close(self) -> None:
        self._players.clear()


class _FakePlayer:
    """One fake player's innards: state that commands may or may not change."""

    def __init__(
        self,
        name: str,
        *,
        state: str,
        track: str | None,
        position_s: float,
        duration_s: float,
        volume: float,
        muted: bool,
        accepts_play: bool,
        rejects_volume: bool,
        volume_step: float,
    ) -> None:
        self.name = name
        self.state = state
        self.track = track
        self.position_s = position_s
        self.duration_s = duration_s
        self.volume = volume
        self.muted = muted
        self.accepts_play = accepts_play
        self.rejects_volume = rejects_volume
        self.volume_step = volume_step
        self._queue_index = 0

    def info(self) -> PlayerInfo:
        return PlayerInfo(
            name=self.name,
            track=self.track,
            state=self.state,
            position_s=self.position_s,
            duration_s=self.duration_s,
            volume=self.volume,
            muted=self.muted,
        )

    def run(self, operation: str, argument: Any) -> bool:
        if operation == "play":
            # A player that refuses to start keeps its state. The engine must report
            # what it reads back, not what was asked.
            if self.accepts_play:
                self.state = "playing"
            return True
        if operation == "pause":
            self.state = "paused"
            return True
        if operation == "stop":
            self.state = "stopped"
            self.position_s = 0.0
            return True
        if operation in ("next", "previous"):
            self._queue_index += 1 if operation == "next" else -1
            self.track = f"Track {self._queue_index}"
            self.position_s = 0.0
            return True
        if operation == "volume":
            if self.rejects_volume:
                return True  # dispatched, but the level does not move
            level = float(argument)
            # Quantize like hardware: the read-back is the *player's* level, not the
            # requested one.
            self.volume = max(0.0, min(1.0, round(level / self.volume_step) * self.volume_step))
            self.muted = self.volume <= 0.0
            return True
        if operation == "mute":
            self.muted = bool(argument)
            return True
        return False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<_FakePlayer {self.name} {self.state}>"


class HostMprisMediaDriver(MediaDriver):
    """The same MPRIS adapter under the host interpreter's PyGObject ABI.

    Used only when the trusted desktop launcher enabled the media binding probe.
    Every command and re-read still goes through MediaEngine and LaptopProvider.
    """

    name = "mpris"

    def _request(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        from actionmesh.agent.mpris import valid_request
        from actionmesh.agent.native_desktop import host_call

        if not valid_request(operation, parameters):
            return {"status": "unavailable"}
        answer = asyncio.run(host_call("media", {"operation": operation, "parameters": parameters}))
        if answer.get("status") == "timeout":
            raise MediaTimeoutError("the MPRIS backend timed out")
        return answer

    def list_players(self) -> list[PlayerInfo]:
        reply = self._request("list", {})
        return [PlayerInfo(**row) for row in reply.get("players", []) if isinstance(row, dict)]

    def get(self, name: str) -> PlayerInfo | None:
        row = self._request("get", {"name": name}).get("player")
        return PlayerInfo(**row) if isinstance(row, dict) else None

    def command(self, name: str, operation: str, argument: Any = None) -> bool:
        answer = self._request("command", {"name": name, "operation": operation, "argument": argument})
        return answer.get("dispatched") is True

    def open_file(self, absolute_path: str) -> str:
        answer = self._request("open_file", {"path": absolute_path})
        outcome = answer.get("outcome")
        return outcome if outcome in {"opened", "rejected", "drm", "unavailable"} else "unavailable"


def production_driver() -> MediaDriver:
    if os.environ.get("ACTIONMESH_NATIVE_MEDIA") == "1":
        return HostMprisMediaDriver()
    return MprisMediaDriver()


class MediaEngine:
    """The §2.3 surface: one method per media capability, in plain dicts.

    Constructor arguments are wired by the provider (report §provider.py): a driver
    for the player bus, the configured :class:`Workspace` for ``open_file``
    confinement, and Agent A's browser delegate for ``open_url`` — the engine opens
    URLs only *through* the browser capability, never on its own.
    """

    def __init__(
        self,
        driver: MediaDriver,
        *,
        workspace: Workspace | None = None,
        browser_open: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.driver = driver
        self.workspace = workspace
        self.browser_open = browser_open
        self._unmuted_levels: dict[str, float] = {}

    # -- target resolution ----------------------------------------------------
    def _timeout(self, exc: MediaTimeoutError) -> dict[str, Any]:
        return {
            "status": "timeout",
            "reason": f"the {self.driver.name} backend did not answer in time: {exc}",
        }

    def _resolve_target(
        self, ref: str | None
    ) -> tuple[PlayerInfo | None, list[PlayerInfo], str | None]:
        """Which player does this action address? Returns (player, candidates, failure).

        ``failure`` is "unavailable" or "ambiguous" — never a default. A `ref` that
        names no reachable player is ambiguity (the user may mean a name we do have;
        guessing silently between them is the lie), and several active players with
        no ref is ambiguity too. Exactly one active player among stopped ones is NOT
        a guess: it is the session the reference resolved against, per §8.

        A driver :class:`MediaTimeoutError` propagates to the caller and is
        answered as ``timeout`` by the capability methods, never as unavailable.
        """
        players = self.driver.list_players()
        if ref:
            wanted = ref.strip().lower()
            for player in players:
                if player.name.lower() == wanted:
                    return player, players, None
            return None, players, "ambiguous"
        if not players:
            return None, [], "unavailable"
        if len(players) == 1:
            return players[0], players, None
        active = [p for p in players if p.state in _ACTIVE_STATES]
        if len(active) == 1:
            return active[0], players, None
        return None, players, "ambiguous"

    def _unavailable(self, why: str) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": f"no media player is reachable through the {self.driver.name} backend: {why}",
        }

    def _ambiguous(self, candidates: list[PlayerInfo], why: str) -> dict[str, Any]:
        names = sorted(player.name for player in candidates)
        return {"status": "ambiguous", "candidates": names, "reason": why}

    # -- capabilities -----------------------------------------------------------
    def status(self) -> dict[str, Any]:
        """media.status — what is playing right now, as the player itself reports.

        With several players, the *active* session is described (playing, else
        paused, else first) and every candidate is listed, so the answer is complete
        without pretending the user meant one of them.
        """
        try:
            players = self.driver.list_players()
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if not players:
            return self._unavailable("the probe found no org.mpris-style players")
        focused = _pick_focused(players)
        return {
            "status": "ok",
            "player": focused.name,
            "track": focused.track,
            "state": focused.state,
            "position_s": focused.position_s,
            "duration_s": focused.duration_s,
            "players": sorted(p.name for p in players),
        }

    def play(self, ref: str | None = None) -> dict[str, Any]:
        """media.play — issue Play, then report the READ-BACK state.

        ``ok`` requires the player to actually say "playing". A dispatched-but-
        ignored command is ``no_effect`` with the true state beside it — this is the
        capability that must never claim playback started on its own request alone.
        """
        try:
            player, candidates, failure = self._resolve_target(ref)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if failure == "unavailable":
            return self._unavailable("there is no session to start")
        if failure == "ambiguous":
            return self._ambiguous(
                candidates,
                "more than one media player could answer to this; name the one you mean"
                if candidates
                else "no player matches that reference",
            )
        assert player is not None
        try:
            dispatched = self.driver.command(player.name, "play")
            # Re-read. Not `player` from before the command — the driver's fresh answer.
            after = self.driver.get(player.name)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if after is None:
            return self._unavailable("the player disappeared while the command was in flight")
        if after.state == "playing":
            return {
                "status": "ok",
                "player": after.name,
                "track": after.track,
                "state": after.state,
            }
        return {
            "status": "no_effect",
            "player": after.name,
            "track": after.track,
            "state": after.state,
            "reason": (dispatched
            and f"the command was dispatched but {after.name!r} still reports {after.state!r}")
            or f"{after.name!r} refused the command",
        }

    def pause(self, ref: str | None = None) -> dict[str, Any]:
        """media.pause — issue Pause, then report the READ-BACK state."""
        try:
            player, candidates, failure = self._resolve_target(ref)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if failure == "unavailable":
            return self._unavailable("there is no session to pause")
        if failure == "ambiguous":
            return self._ambiguous(
                candidates,
                "more than one media player could answer to this; name the one you mean"
                if candidates
                else "no player matches that reference",
            )
        assert player is not None
        try:
            dispatched = self.driver.command(player.name, "pause")
            after = self.driver.get(player.name)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if after is None:
            return self._unavailable("the player disappeared while the command was in flight")
        if after.state == "paused":
            return {"status": "ok", "state": after.state, "player": after.name}
        return {
            "status": "no_effect",
            "state": after.state,
            "player": after.name,
            "reason": (dispatched
            and f"the command was dispatched but {after.name!r} still reports {after.state!r}")
            or f"{after.name!r} refused the command",
        }

    def next_track(self) -> dict[str, Any]:
        """media.next — issue Next, then report the track the player actually shows.

        A same-track read-back (single-item queue) is reported with ``changed``
        false, not narrated as a skip that happened.
        """
        return self._navigate("next")

    def previous_track(self) -> dict[str, Any]:
        """media.previous — issue Previous, then report the track read back."""
        return self._navigate("previous")

    def _navigate(self, operation: str) -> dict[str, Any]:
        try:
            player, candidates, failure = self._resolve_target(None)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if failure == "unavailable":
            return self._unavailable(f"there is no session to {operation}")
        if failure == "ambiguous":
            return self._ambiguous(
                candidates, f"more than one media player is active; {operation} needs one target"
            )
        assert player is not None
        before = player.track_id or player.track
        try:
            self.driver.command(player.name, operation)
            after = self.driver.get(player.name)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if after is None:
            return self._unavailable("the player disappeared while the command was in flight")
        return {
            "status": "ok" if (after.track_id or after.track) != before else "no_effect",
            "track": after.track,
            "player": after.name,
            "changed": (after.track_id or after.track) != before,
            "track_fingerprint": after.track_id,
        }

    def volume(
        self, level: float | None = None, muted: bool | None = None
    ) -> dict[str, Any]:
        """media.volume — set level and/or mute; the result carries the READ-BACK values.

        Hardware quantizes. If the player stores 0.8 for a request of 0.83, the
        answer says 0.8 — the claim is the player's, not the request's.
        """
        if level is None and muted is None:
            return self._read_volume_only()
        if level is not None:
            if isinstance(level, bool) or not isinstance(level, (int, float)):
                return {"status": "invalid_input", "reason": "level must be a number"}
            if not 0.0 <= float(level) <= 1.0:
                return {
                    "status": "invalid_input",
                    "reason": "level must be between 0.0 and 1.0",
                }
        if muted is not None and not isinstance(muted, bool):
            return {"status": "invalid_input", "reason": "muted must be a boolean"}
        try:
            player, candidates, failure = self._resolve_target(None)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if failure == "unavailable":
            return self._unavailable("there is no session to set volume on")
        if failure == "ambiguous":
            return self._ambiguous(
                candidates, "more than one media player is active; volume needs one target"
            )
        assert player is not None
        restore = level if level is not None and level > 0 else self._unmuted_levels.get(player.name)
        if restore is None and player.volume > 0:
            restore = player.volume
        if muted is False and restore is None:
            return {"status": "no_effect", "player": player.name, "level": player.volume,
                    "muted": player.muted, "reason": "No pre-mute level is known; set an explicit positive volume."}
        try:
            if muted is True:
                if restore is not None:
                    self._unmuted_levels[player.name] = float(restore)
                self.driver.command(player.name, "volume", 0.0)
            elif muted is False:
                assert restore is not None
                self.driver.command(player.name, "volume", float(restore))
            elif level is not None:
                self.driver.command(player.name, "volume", float(level))
            after = self.driver.get(player.name)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if after is None:
            return self._unavailable("the player disappeared while the command was in flight")
        result: dict[str, Any] = {
            "status": "ok",
            "level": after.volume,
            "muted": after.muted,
            "player": after.name,
        }
        if muted is not None and after.muted is not muted:
            result["status"] = "no_effect"
            result["reason"] = "The player did not reach the requested mute state."
        if muted is not True and level is not None and abs(after.volume - float(level)) > 1e-9:
            # The player moved to a different level than requested: keep it true.
            result["requested_level"] = float(level)
            result["status"] = "no_effect" if abs(after.volume - player.volume) < 1e-9 else "ok"
            result["reason"] = (
                f"{after.name!r} reports {after.volume!r} after a request for {float(level)!r}"
            )
        return result

    def _read_volume_only(self) -> dict[str, Any]:
        """media.volume with no arguments is a read of the current setting."""
        try:
            player, candidates, failure = self._resolve_target(None)
        except MediaTimeoutError as exc:
            return self._timeout(exc)
        if failure:
            return (
                self._unavailable("there is no session to read volume from")
                if failure == "unavailable"
                else self._ambiguous(candidates, "more than one media player is active")
            )
        assert player is not None
        return {"status": "ok", "level": player.volume, "muted": player.muted, "player": player.name}

    def open_file(self, path: str) -> dict[str, Any]:
        """media.open_file — hand a root-confined media file to the player/opener.
        ``path`` is resolved through the configured Workspace BEFORE anything is
        opened: absolute paths, ``..`` escapes and secret names raise
        :class:`AgentError` and are carried out as authorizations denials by the
        provider dispatch — nothing is opened, nothing is read. The file's *bytes*
        never pass through here at all; only its confined relative path is reported.

        Focus note: this hands the file to the player process; it does not raise or
        focus any window (no MPRIS ``Raise``), so text in an active input stays the
        user's.
        """
        if not isinstance(path, str) or not path.strip():
            return {"status": "invalid_input", "reason": "path must be a non-empty string"}
        if self.workspace is None:
            # Same posture as the provider's file capabilities: no root, no reach.
            return {
                "status": "unavailable",
                "reason": "no workspace root is configured, so media.open_file has nothing it may open",
            }
        resolved = self.workspace.resolve(path, must_exist=True)  # raises PathEscape/PathNotFound
        try:
            outcome = self.driver.open_file(resolved.absolute)
        except MediaTimeoutError as exc:
            return {
                **self._timeout(exc),
                "path": resolved.relative,
                "opened": False,
            }
        if outcome == "opened":
            return {"status": "ok", "path": resolved.relative, "opened": True}
        if outcome == "drm":
            return {
                "status": "unsupported",
                "path": resolved.relative,
                "opened": False,
                "reason": "protected (DRM) stream — the agent neither plays nor downloads protected content",
            }
        if outcome == "unavailable":
            return {
                "status": "unavailable",
                "path": resolved.relative,
                "opened": False,
                "reason": "no media opener is reachable from this session",
            }
        return {
            "status": "unsupported",
            "path": resolved.relative,
            "opened": False,
            "reason": "the player/opener declined this format or service",
        }

    def open_url(self, url: str) -> dict[str, Any]:
        """media.open_url — open a media site THROUGH the browser capability.

        The engine never fetches the URL itself: opening a page is exactly what
        Agent A's ``browser.open`` is for, and that capability carries its own
        scope, policy and untrusted-content rules. If the browser is not wired, the
        answer is "unavailable" — not a hidden urllib call that would sidestep the
        whole contract. Non-http(s) schemes are refused before any delegation: no
        file:, no javascript:, no secret-URI shapes.

        Focus note: like open_file, the delegation opens a session window without
        stealing keyboard focus from an active text input.
        """
        if not isinstance(url, str) or not url.strip():
            return {"status": "invalid_input", "reason": "url must be a non-empty string"}
        candidate = url.strip()
        if not candidate.lower().startswith(("http://", "https://")):
            return {
                "status": "unsupported",
                "url": candidate,
                "reason": "only http(s) media sites can be opened; anything else is not a capability here",
            }
        if self.browser_open is None:
            return {
                "status": "unavailable",
                "url": candidate,
                "reason": "the browser capability is not wired into this agent, so no site can be opened",
            }
        try:
            opened = self.browser_open(candidate)
        except MediaTimeoutError as exc:
            return {**self._timeout(exc), "url": candidate}
        inner = str(opened.get("status") or "unknown")
        if inner == "ok":
            return {"status": "ok", "url": candidate, "player": opened.get("player", "")}
        if inner in ("unsupported", "needs_user"):
            # The browser named the limit (e.g. a DRM service needing the user); pass it
            # through rather than flattening the reason into a generic failure.
            return {
                "status": inner,
                "url": candidate,
                "reason": str(opened.get("reason") or "the browser declined this service"),
            }
        if inner in ("unavailable", "not_started", "no_effect"):
            return {
                "status": "unavailable",
                "url": candidate,
                "reason": f"the browser capability reported {inner!r} for this request",
            }
        return {
            "status": "unavailable",
            "url": candidate,
            "reason": f"the browser capability reported an unknown status {inner!r}",
        }

    # -- transient label ----------------------------------------------------------
    def meta_label(self, result: dict[str, Any]) -> str | None:
        """The `media.meta` stream payload for one capability result, or None.

        Identity only: "<player> — <track>". Never emitted for a failed action,
        never extended with file contents or metadata.
        """
        if result.get("status") != "ok":
            return None
        player = _first_str(result.get("player"))
        track = _first_str(result.get("track"))
        if not player or not track:
            return None
        return f"{player} — {track}"

    def close(self) -> None:
        self.driver.close()


def _pick_focused(players: list[PlayerInfo]) -> PlayerInfo:
    """The player whose session is live: playing, else paused, else first.

    Deterministic and disclosed in the result (``players`` lists every candidate),
    so a multi-player status is a complete answer, not a guess at intent.
    """
    for wanted in ("playing", "paused"):
        for player in players:
            if player.state == wanted:
                return player
    return players[0]


__all__ = [
    "MEDIA_META_STREAM",
    "FakeMediaDriver",
    "HostMprisMediaDriver",
    "MediaDriver",
    "MediaEngine",
    "MediaTimeoutError",
    "MprisMediaDriver",
    "PlayerInfo",
    "production_driver",
]
