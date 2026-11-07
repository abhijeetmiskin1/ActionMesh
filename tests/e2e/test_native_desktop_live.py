"""Opt-in host-PyGObject acceptance, restricted to a disposable GTK fixture."""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from actionmesh.agent import device_screen
from actionmesh.agent.definitions import register_device_actions
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
from actionmesh.protocol.models import Action, ApprovalRequest, Identity, RiskLevel


@pytest.mark.skipif(os.environ.get("ACTIONMESH_LIVE_ACCEPTANCE") != "1", reason="live desktop acceptance is opt-in")
def test_host_bindings_inspect_click_and_type_on_an_owned_fixture(tmp_path, monkeypatch) -> None:
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        pytest.skip("a graphical session is required")
    python = Path("/usr/bin/python3")
    if not python.exists():
        pytest.skip("host Python is unavailable")
    probe = subprocess.run([str(python), "-I", device_screen.__file__, "--native-probe"],
        capture_output=True, text=True, timeout=5)
    if probe.returncode or not json.loads(probe.stdout).get("available"):
        pytest.skip("host PyGObject/AT-SPI is unavailable")
    app_name = f"actionmesh-native-acceptance-{os.getpid()}"
    helper = tmp_path / "native_target.py"
    helper.write_text('''
import sys
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, GLib
GLib.set_prgname(sys.argv[1])
GLib.set_application_name(sys.argv[1])
window=Gtk.Window(title=sys.argv[1])
box=Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
label=Gtk.Label(label='Fixture before')
button=Gtk.Button(label='Activate fixture')
button.connect('clicked', lambda _: label.set_text('Fixture activated'))
entry=Gtk.Entry()
entry.get_accessible().set_name('Fixture entry')
for item in [label,button,entry]: box.pack_start(item,True,True,0)
window.add(box)
window.connect('destroy', Gtk.main_quit)
window.show_all()
print('READY',flush=True)
Gtk.main()
''')
    target = subprocess.Popen([str(python), "-I", str(helper), app_name],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    monkeypatch.setenv("ACTIONMESH_NATIVE_DESKTOP", "1")
    try:
        async def journey() -> None:
            registry = ActionRegistry.with_seeds()
            register_device_actions(registry)
            provider = LaptopProvider(Workspace(str(tmp_path)))
            engine = ExecutionEngine(registry, PolicyEngine(), [provider])
            register_device_strategies(engine.verifier)
            context = PolicyContext(actor=Identity(id="native-fixture"),
                policy=policy_from_rules([{"action": "device.*", "decision": "allow"}]))

            async def execute(kind: str, params: dict):
                action = Action(type=kind, provider="laptop", parameters=params)
                builder = GraphBuilder(kind)
                builder.action(action, node_id="operation")
                builder.end(depends_on=["operation"])
                graph = builder.build()
                result = await engine.execute(graph, context)
                if result.state.value == "waiting_permission":
                    engine.submit_approval(ApprovalRequest(action_id=action.action_id, status="approved"))
                    result = await engine.resume(graph.graph_id)
                return result

            denied = await execute("device.screen.inspect", {"window": app_name})
            assert denied.state.value == "denied"
            context.grants = [PermissionGrant(scope=scope, max_risk=RiskLevel.HIGH)
                for scope in ["device.screen.read", "device.screen.control"]]
            deadline = time.monotonic() + 10
            elements = []
            while time.monotonic() < deadline:
                result = await execute("device.screen.inspect", {"window": app_name})
                elements = result.nodes["operation"].result.get("elements", [])
                if any(item["name"] == "Activate fixture" for item in elements):
                    break
                await asyncio.sleep(0.2)
            assert target.poll() is None, "GTK fixture did not start"
            assert any(item["name"] == "Activate fixture" for item in elements), "owned fixture is not accessible"
            button = next(item for item in elements if item["name"] == "Activate fixture")
            clicked = await execute("device.gui.click", {"window": app_name,
                "role": button["role"], "name": button["name"]})
            assert clicked.nodes["operation"].result.get("dispatched") is True
            inspected = await execute("device.screen.inspect", {"window": app_name})
            assert any(item["name"] == "Fixture activated" for item in inspected.nodes["operation"].result["elements"])
            field = next(item for item in elements if item["name"] == "Fixture entry")
            typed = await execute("device.gui.type", {"window": app_name,
                "role": field["role"], "name": field["name"], "text": "test text"})
            assert typed.nodes["operation"].result.get("typed") is True
            assert typed.nodes["operation"].result.get("length", {}).get("inserted") == 9
            missing = await execute("device.screen.inspect", {"window": app_name + "-missing"})
            assert missing.nodes["operation"].result.get("status") == "window_not_found"
            assert missing.nodes["operation"].result.get("elements") == []

        asyncio.run(journey())
    finally:
        target.terminate()
        try:
            target.wait(timeout=5)
        except subprocess.TimeoutExpired:
            target.kill()
            target.wait()
