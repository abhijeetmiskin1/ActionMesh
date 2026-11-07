"""Opt-in Electron acceptance with a local page and an isolated, disposable profile.

Run with ACTIONMESH_LIVE_ACCEPTANCE=1. No user cookies, vendor credentials,
external sites, booking, or payment are involved. All actions use ExecutionEngine.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import select
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from actionmesh.agent.browser_bridge import BrowserBridgeConfig
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
from actionmesh.protocol.models import Action, Identity, RiskLevel

ROOT = Path(__file__).resolve().parents[2]
ELECTRON = ROOT / "desktop/node_modules/electron/dist/electron"


@unittest.skipUnless(os.environ.get("ACTIONMESH_LIVE_ACCEPTANCE") == "1", "live Electron acceptance is opt-in")
class TestBrowserLiveControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not ELECTRON.exists() or not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            raise unittest.SkipTest("live Electron needs its installed binary and a desktop display")
        cls.directory = tempfile.TemporaryDirectory(prefix="actionmesh-live-browser-")
        cls.addClassCleanup(cls.directory.cleanup)
        root = Path(cls.directory.name)
        cls.token = secrets.token_hex(32)
        cls.socket = str(root / "browser.sock")
        helper = root / "electron.cjs"
        helper.write_text("""
const {app,BrowserWindow}=require('electron');
const http=require('http');
app.setPath('userData',process.env.SMOKE_PROFILE);
app.whenReady().then(async()=>{
 const drive=require(process.env.SMOKE_REPO+'/desktop/app/browser_window.js');
 const {startBridge}=require(process.env.SMOKE_REPO+'/desktop/app/browser_bridge.js');
 const server=http.createServer((req,res)=>{
  if(req.url==='/redirect'){res.writeHead(302,{Location:'/canonical'});res.end();return;}
  res.setHeader('Content-Type','text/html');
  res.end('<title>ActionMesh Acceptance</title><h1>Local test</h1><input aria-label="Search"><button onclick="this.textContent=String.fromCharCode(65,99,116,105,118,97,116,101,100)">Activate</button><select aria-label="Choice"><option value="a">A</option><option value="b">B</option></select><a href="/second">Next</a>');
 });
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const bridge=await startBridge({socketPath:process.env.SMOKE_SOCKET,token:process.env.SMOKE_TOKEN,drive});
 process.stdout.write('READY '+JSON.stringify({port:server.address().port})+'\\n');
 process.on('SIGTERM',async()=>{
  await bridge.close();server.close();
  for(const w of BrowserWindow.getAllWindows())w.destroy();app.quit();
 });
});
""")
        env = {**os.environ, "SMOKE_REPO": str(ROOT), "SMOKE_PROFILE": str(root / "profile"),
               "SMOKE_SOCKET": cls.socket, "SMOKE_TOKEN": cls.token}
        env.pop("ELECTRON_RUN_AS_NODE", None)
        cls.process = subprocess.Popen([str(ELECTRON), str(helper)], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        cls.addClassCleanup(cls.stop_process)
        assert cls.process.stdout is not None
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and cls.process.poll() is None:
            if select.select([cls.process.stdout], [], [], 1)[0]:
                line = cls.process.stdout.readline()
                if line.startswith("READY "):
                    cls.url = f"http://127.0.0.1:{json.loads(line[6:])['port']}/"
                    return
        raise RuntimeError("isolated Electron browser failed to start")

    @classmethod
    def stop_process(cls) -> None:
        cls.process.terminate()
        try:
            cls.process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            cls.process.kill()
            cls.process.wait()
        if cls.process.stdout:
            cls.process.stdout.close()

    def test_real_navigation_and_permission_refusal(self) -> None:
        async def journey() -> None:
            registry = ActionRegistry.with_seeds()
            register_device_actions(registry)
            provider = LaptopProvider(Workspace(self.directory.name),
                browser_bridge=BrowserBridgeConfig(self.socket, self.token))
            engine = ExecutionEngine(registry, PolicyEngine(), [provider])
            register_device_strategies(engine.verifier)
            context = PolicyContext(actor=Identity(id="live-acceptance"),
                policy=policy_from_rules([{"action": "browser.*", "decision": "allow"}]))

            async def execute(kind: str, params: dict):
                builder = GraphBuilder(kind)
                builder.action(Action(type=kind, provider="laptop", parameters=params), node_id="browser")
                builder.end(depends_on=["browser"])
                return await engine.execute(builder.build(), context)

            denied = await execute("browser.open", {"url": self.url})
            self.assertEqual(denied.state.value, "denied")
            context.grants = [PermissionGrant(scope="browser.control", max_risk=RiskLevel.HIGH)]
            for kind, params in [
                ("browser.open", {"url": self.url + "redirect"}),
                ("browser.open", {"url": self.url}), ("browser.snapshot", {}),
                ("browser.inspect", {}),
                ("browser.navigate", {"url": self.url + "redirect"}),
                ("browser.navigate", {"url": self.url + "second"}),
                ("browser.back", {}), ("browser.forward", {}), ("browser.refresh", {}),
            ]:
                result = await execute(kind, params)
                self.assertEqual(result.state.value, "success", (kind, result.error))
                self.assertTrue(result.nodes["browser"].verified, kind)

            async def snapshot() -> list[dict]:
                result = await execute("browser.snapshot", {})
                self.assertTrue(result.nodes["browser"].verified)
                events = engine.events.transient(result.execution_id)
                documents = [json.loads(event.payload["chunk"]) for event in events
                    if event.payload.get("stream") == "browser.elements" and event.payload.get("chunk")]
                self.assertTrue(documents, "the real UI needs a transient element document")
                return documents[-1]["elements"]

            elements = await snapshot()
            button = next(item for item in elements if item["tag"] == "button")
            click = await execute("browser.click", {"ref": str(button["ref"])})
            # Dispatch is real, but a generic click has no domain postcondition that
            # can independently certify its effect. Keep its verdict unverified.
            self.assertFalse(click.nodes["browser"].verified)
            elements = await snapshot()
            self.assertTrue(any(item["name"] == "Activated" for item in elements))
            field = next(item for item in elements if item["tag"] == "input")
            typed = await execute("browser.type", {"ref": str(field["ref"]), "text": "test text"})
            self.assertFalse(typed.nodes["browser"].verified)
            elements = await snapshot()
            self.assertEqual(next(item for item in elements if item["tag"] == "input")["value_len"], 9)
            choice = next(item for item in elements if item["tag"] == "select")
            selected = await execute("browser.select", {"ref": str(choice["ref"]), "value": "b"})
            self.assertFalse(selected.nodes["browser"].verified)
            self.assertEqual(selected.nodes["browser"].result.get("status"), "ok")

        asyncio.run(journey())
