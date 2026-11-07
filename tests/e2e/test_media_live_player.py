"""Opt-in acceptance against an owned VLC process decoding generated silent WAVs."""

import asyncio
import os
import subprocess
import time
import wave
from pathlib import Path

import pytest

from actionmesh.agent.definitions import register_device_actions
from actionmesh.agent.media import HostMprisMediaDriver
from actionmesh.agent.paths import Workspace
from actionmesh.agent.provider import LaptopProvider
from actionmesh.agent.verification import register_device_strategies
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import GraphBuilder
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import Action, Identity, RiskLevel


@pytest.mark.skipif(os.environ.get("ACTIONMESH_LIVE_ACCEPTANCE") != "1", reason="live media acceptance is opt-in")
def test_real_vlc_media_pipeline(tmp_path, monkeypatch) -> None:
    binary = os.environ.get("ACTIONMESH_LIVE_VLC", "")
    if not binary or not Path(binary).is_file():
        pytest.skip("set ACTIONMESH_LIVE_VLC to a real VLC executable")
    # A separate active player makes implicit volume/next targeting ambiguous.
    # Refuse that environment instead of touching a user's playback.
    if HostMprisMediaDriver().list_players():
        pytest.skip("another player is reachable; this test operates only its own VLC")
    files = []
    for name in ["first.wav", "second.wav"]:
        target = tmp_path / name
        with wave.open(str(target), "wb") as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(16000)
            audio.writeframes(b"\0\0" * 16000 * 120)
        files.append(target)
    env = {**os.environ, "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"), "XDG_DATA_HOME": str(tmp_path / "data")}
    for source, destination in [("ACTIONMESH_LIVE_VLC_LIBRARIES", "LD_LIBRARY_PATH"),
                                ("ACTIONMESH_LIVE_VLC_PLUGINS", "VLC_PLUGIN_PATH")]:
        if os.environ.get(source):
            env[destination] = os.environ[source]
    player = subprocess.Popen([binary, "--intf", "dummy", "--extraintf", "dbus", "--aout", "pulse",
        "--no-video", "--no-one-instance", "--ignore-config", "--loop", *map(str, files)],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setenv("ACTIONMESH_NATIVE_MEDIA", "1")
    monkeypatch.setenv("ACTIONMESH_NATIVE_DESKTOP", "0")
    try:
        async def journey() -> None:
            registry = ActionRegistry.with_seeds()
            register_device_actions(registry)
            provider = LaptopProvider(Workspace(str(tmp_path)))
            assert isinstance(provider.media.driver, HostMprisMediaDriver)
            engine = ExecutionEngine(registry, PolicyEngine(), [provider])
            register_device_strategies(engine.verifier)
            context = PolicyContext(actor=Identity(id="live-media-fixture"),
                policy=policy_from_rules([{"action": "media.*", "decision": "allow"}]))

            async def execute(kind: str, parameters: dict):
                graph = GraphBuilder(kind)
                graph.action(Action(type=kind, provider="laptop", parameters=parameters), node_id="media")
                graph.end(depends_on=["media"])
                return await engine.execute(graph.build(), context)

            denied = await execute("media.status", {})
            assert denied.state.value == "denied"
            scopes = {scope for verb in ["status", "play", "pause", "next", "previous", "volume", "open_file"]
                      for scope in registry.get(f"media.{verb}").permissions}
            context.grants = [PermissionGrant(scope=scope, max_risk=RiskLevel.HIGH) for scope in scopes]
            deadline = time.monotonic() + 10
            players = []
            while time.monotonic() < deadline:
                players = await asyncio.to_thread(provider.media.driver.list_players)
                if players and players[0].duration_s == 120:
                    break
                await asyncio.sleep(0.1)
            assert player.poll() is None, "VLC did not start"
            assert len(players) == 1, "one VLC must not be counted twice through its aliases"
            initial = players[0]
            await asyncio.sleep(0.4)
            later = await asyncio.to_thread(provider.media.driver.get, initial.name)
            assert later and later.position_s > initial.position_s, "the real playback clock must advance"
            for verb, parameters in [("status", {}), ("pause", {"ref": initial.name}),
                ("play", {"ref": initial.name}), ("volume", {"level": 0.31}),
                ("volume", {"muted": True}), ("volume", {"muted": False}),
                ("next", {}), ("previous", {})]:
                result = await execute(f"media.{verb}", parameters)
                node = result.nodes["media"]
                assert result.state.value == "success", (verb, result.state.value, node.result, node.error)
                assert node.verified, verb
            # OpenUri is actually dispatched, while a generic opener receipt stays
            # unverified until an independent loaded-file identity is available.
            opened = await execute("media.open_file", {"path": "second.wav"})
            assert opened.nodes["media"].result.get("opened") is True
            assert not opened.nodes["media"].verified

        asyncio.run(journey())
    finally:
        player.terminate()
        try:
            player.wait(timeout=5)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait()
