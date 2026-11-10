"""C19 laptop agent tests — configuration-derived capabilities and presence.

The agent is the component that makes this machine known to ActionMesh. Two of its
jobs are easy to get wrong silently: advertising a capability the machine cannot
actually perform, and looking online after it has stopped reporting.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from actionmesh.agent.config import (
    ENV_ALLOW_SHELL,
    ENV_DEVICE_ID,
    ENV_ENABLED,
    ENV_HEARTBEAT_S,
    ENV_WORKSPACE_ROOT,
    AgentConfig,
    AgentConfigError,
)
from actionmesh.agent.connectors import CONNECTOR_ACTIONS
from actionmesh.agent.definitions import (
    CAPABILITIES,
    IMPLEMENTED,
    REQUIRING_BROWSER,
    REQUIRING_DESKTOP,
    REQUIRING_MEDIA,
    REQUIRING_PAYMENT,
    device_definitions,
    register_device_actions,
)
from actionmesh.agent.presence import LaptopAgent, resources
from actionmesh.api import create_app
from actionmesh.core.devices import DEFAULT_STALE_AFTER_S, DeviceRegistry
from actionmesh.core.events import EventHub
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.permissions.catalog import is_concrete
from actionmesh.core.permissions.risk import risk_rank
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import DeviceKind, RiskLevel


class TestConfig(unittest.TestCase):
    def test_nothing_is_enabled_by_absence(self) -> None:
        config = AgentConfig(device_id="laptop-1")
        self.assertEqual(config.advertise(), ())
        self.assertFalse(config.allow_shell)
        self.assertEqual(config.workspace_root, "")

    def test_capturing_is_off_until_a_log_dir_is_configured(self) -> None:
        # A detached child's output is discarded unless the operator opens somewhere for
        # it. Nothing enabled by absence, including this.
        self.assertFalse(AgentConfig(device_id="laptop-1").capture_logs)

    def test_a_log_dir_must_be_absolute(self) -> None:
        with self.assertRaises(AgentConfigError) as ctx:
            AgentConfig(device_id="laptop-1", log_dir="agent-logs")
        self.assertIn("absolute", str(ctx.exception))

    def test_a_log_dir_inside_the_workspace_is_refused(self) -> None:
        # Agent-owned logs are not source. Under the root they would be readable through
        # the confined file API, which turns "capture a server's output" into "hand any
        # phone with file.read whatever that server printed".
        with tempfile.TemporaryDirectory() as root:
            inside = os.path.join(root, "agent-logs")
            with self.assertRaises(AgentConfigError) as ctx:
                AgentConfig(device_id="laptop-1", workspace_root=root, log_dir=inside)
        self.assertIn("workspace", str(ctx.exception))

    def test_the_log_dir_comes_from_the_environment(self) -> None:
        with tempfile.TemporaryDirectory() as log_dir:
            config = AgentConfig.from_env({"ACTIONMESH_AGENT_LOG_DIR": log_dir})
        self.assertTrue(config.capture_logs)

    def test_a_workspace_root_opens_file_and_project_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig(device_id="laptop-1", workspace_root=root)
        advertised = set(config.advertise())
        self.assertIn("device.file.read", advertised)
        self.assertIn("device.project.list", advertised)
        # Everything advertised is gated by a boundary the operator opened; the
        # shell half stays closed without ACTIONMESH_ALLOW_SHELL, and nothing outside
        # IMPLEMENTED can appear however the configuration is set.
        self.assertNotIn("device.terminal.execute", advertised)
        self.assertNotIn("device.ide.open", advertised)
        self.assertTrue(advertised <= set(IMPLEMENTED))

    def test_the_shell_flag_opens_terminal_once_the_flag_is_set(self) -> None:
        config = AgentConfig(device_id="laptop-1", allow_shell=True)
        advertised = set(config.advertise())
        # No workspace root: the file capabilities stay closed even with the shell on.
        self.assertNotIn("device.file.read", advertised)
        self.assertIn("device.terminal.execute", advertised)
        self.assertIn("device.server.start", advertised)
        with tempfile.TemporaryDirectory() as root:
            rooted = AgentConfig(device_id="laptop-1", workspace_root=root, allow_shell=True)
        # Root + shell opens every capability gated on those two boundaries, but NOT
        # the payment-gated submits (own boundary, closed by default), NOT the
        # desktop ones (third boundary), and NOT the browser/media ones (own
        # boundaries, closed by default). All gated sets are excluded by name.
        self.assertEqual(
            set(rooted.advertise()),
            set(IMPLEMENTED)
            - set(REQUIRING_DESKTOP)
            - set(REQUIRING_PAYMENT)
            - set(REQUIRING_BROWSER)
            - set(REQUIRING_MEDIA) - {"device.app.launch_native", "device.app.status_native"} - CONNECTOR_ACTIONS,
        )
        self.assertIn("device.terminal.execute", rooted.advertise())
        self.assertIn("device.file.read", rooted.advertise())

    def test_every_advertised_capability_is_a_real_action(self) -> None:
        registry = ActionRegistry.with_seeds()
        register_device_actions(registry)
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig(device_id="laptop-1", workspace_root=root, allow_shell=True)
        advertised = set(config.advertise())
        # Every capability the brief names is defined; the desktop, payment-gated
        # and browser/media-gated ones are defined but withheld until their own
        # boundaries are opened.
        self.assertEqual(
            advertised,
            set(CAPABILITIES) - set(REQUIRING_DESKTOP) - set(REQUIRING_PAYMENT)
            - set(REQUIRING_BROWSER) - set(REQUIRING_MEDIA) - {"device.app.launch_native", "device.app.status_native"} - CONNECTOR_ACTIONS,
        )
        self.assertEqual(
            advertised,
            set(IMPLEMENTED) - set(REQUIRING_DESKTOP) - set(REQUIRING_PAYMENT)
            - set(REQUIRING_BROWSER) - set(REQUIRING_MEDIA) - {"device.app.launch_native", "device.app.status_native"} - CONNECTOR_ACTIONS,
        )
        for capability in advertised:
            self.assertTrue(registry.has(capability), capability)

    def test_an_unconfigured_boundary_registers_nothing_for_it(self) -> None:
        # `allow_shell` off must mean the action is *unknown*, not merely refused at
        # the provider. An unresolvable action never reaches a policy decision, an
        # approval prompt, or a device — which is where the strongest fail-closed
        # boundary sits.
        registry = ActionRegistry.with_seeds()
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig(device_id="laptop-1", workspace_root=root)
        register_device_actions(registry, config.advertise())
        self.assertFalse(registry.has("device.terminal.execute"))
        self.assertTrue(registry.has("device.file.read"))

    def test_a_capability_with_no_handler_cannot_be_registered(self) -> None:
        registry = ActionRegistry.with_seeds()
        added = register_device_actions(registry, ["device.file.read", "device.not.real"])
        self.assertEqual(added, 1)
        self.assertFalse(registry.has("device.not.real"))

    def test_a_relative_root_is_refused(self) -> None:
        # A relative root resolves against wherever the server happened to start,
        # which is a different confinement every time.
        with self.assertRaises(AgentConfigError):
            AgentConfig(device_id="laptop-1", workspace_root="projects")

    def test_a_missing_root_is_refused_rather_than_emptied(self) -> None:
        with self.assertRaises(AgentConfigError) as ctx:
            AgentConfig(device_id="laptop-1", workspace_root="/definitely/not/here")
        self.assertIn("not a directory", str(ctx.exception))

    def test_heartbeat_window_is_bounded(self) -> None:
        for bad in (0, -1, 10_000):
            with self.subTest(bad=bad), self.assertRaises(AgentConfigError):
                AgentConfig(device_id="laptop-1", heartbeat_s=bad)

    def test_device_id_is_required(self) -> None:
        with self.assertRaises(AgentConfigError):
            AgentConfig(device_id="  ")

    def test_from_env_reads_the_documented_variables(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig.from_env(
                {
                    ENV_DEVICE_ID: "thinkpad",
                    "ACTIONMESH_DEVICE_NAME": "Work Laptop",
                    ENV_WORKSPACE_ROOT: root,
                    ENV_ALLOW_SHELL: "true",
                    ENV_HEARTBEAT_S: "20",
                }
            )
        self.assertEqual(config.device_id, "thinkpad")
        self.assertEqual(config.name, "Work Laptop")
        self.assertTrue(config.allow_shell)
        self.assertEqual(config.heartbeat_s, 20.0)
        self.assertIn("device.file.read", config.advertise())

    def test_from_env_defaults_the_name_to_the_device_id(self) -> None:
        config = AgentConfig.from_env({ENV_DEVICE_ID: "thinkpad"})
        self.assertEqual(config.name, "thinkpad")

    def test_a_non_numeric_heartbeat_is_a_clear_refusal(self) -> None:
        with self.assertRaises(AgentConfigError) as ctx:
            AgentConfig.from_env({ENV_HEARTBEAT_S: "every-few-minutes"})
        self.assertIn("number of seconds", str(ctx.exception))

    def test_a_relative_env_root_is_made_absolute(self) -> None:
        previous = os.getcwd()
        with tempfile.TemporaryDirectory() as root:
            os.chdir(root)
            try:
                config = AgentConfig.from_env({ENV_WORKSPACE_ROOT: "."})
            finally:
                os.chdir(previous)
        self.assertTrue(os.path.isabs(config.workspace_root))

    def test_enabled_reads_the_flag(self) -> None:
        self.assertFalse(AgentConfig.enabled({}))
        self.assertTrue(AgentConfig.enabled({ENV_ENABLED: "1"}))
        self.assertTrue(AgentConfig.enabled({ENV_ENABLED: "yes"}))
        self.assertFalse(AgentConfig.enabled({ENV_ENABLED: "0"}))

    def test_public_view_leaks_nothing_beyond_the_root(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig(device_id="laptop-1", workspace_root=root)
        public = config.public()
        self.assertEqual(public["workspace_root"], root)
        self.assertNotIn("env", str(public).lower())
        self.assertNotIn("secret", str(public).lower())


class TestActionInstallation(unittest.TestCase):
    def test_install_is_idempotent(self) -> None:
        registry = ActionRegistry.with_seeds()
        first = register_device_actions(registry)
        second = register_device_actions(registry)
        self.assertEqual(first, len(IMPLEMENTED))
        self.assertEqual(second, 0)
        self.assertEqual(len(registry.discover(provider="mock")), 16)

    def test_risk_is_fixed_by_the_definition(self) -> None:
        by_name = {definition.name: definition for definition in device_definitions()}
        for name, expected in (
            ("device.terminal.execute", "critical"),
            ("device.file.read", "medium"),
            ("device.file.write", "high"),
            ("device.file.delete", "high"),
            ("device.server.start", "high"),
            ("device.server.status", "low"),
            ("device.project.list", "low"),
            ("device.ide.open", "medium"),
        ):
            with self.subTest(name=name):
                self.assertEqual(by_name[name].risk_level.value, expected)

    def test_terminal_execution_needs_the_shell_scope(self) -> None:
        by_name = {definition.name: definition for definition in device_definitions()}
        terminal = by_name["device.terminal.execute"]
        self.assertEqual(terminal.permissions, ["system.shell"])
        # A retry would re-run a command that may already have touched the machine.
        self.assertEqual(terminal.retry_policy.max_attempts, 1)

    def test_no_device_action_is_simulated(self) -> None:
        for definition in device_definitions():
            self.assertEqual(definition.provider, "laptop", definition.name)

    def test_every_device_action_requires_a_known_concrete_permission(self) -> None:
        for definition in device_definitions():
            self.assertTrue(definition.permissions, definition.name)
            for scope in definition.permissions:
                self.assertTrue(is_concrete(scope), f"{definition.name}: {scope}")

    def test_every_required_parameter_is_described(self) -> None:
        for definition in device_definitions():
            properties = definition.input_schema["properties"]
            for required in definition.input_schema["required"]:
                self.assertIn(required, properties, definition.name)


class TestPresence(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.hub = EventHub()
        self.now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        # mkdtemp, not a `with TemporaryDirectory()`: the config outlives the setup
        # method, and a root that has already been deleted is a refused config.
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.config = AgentConfig(
            device_id="laptop-1", name="My Laptop", workspace_root=self.root
        )

    def _agent(self, **kw: object) -> LaptopAgent:
        return LaptopAgent(
            self.devices, self.config, events=self.hub, clock=lambda: self.now, **kw  # type: ignore[arg-type]
        )

    def test_register_enrolls_a_laptop_with_a_real_capability_list(self) -> None:
        device = self._agent().register()
        self.assertIs(device.kind, DeviceKind.LAPTOP)
        self.assertTrue(device.identity_id)
        self.assertIn("device.file.read", device.capabilities)
        self.assertNotIn("device.terminal.execute", device.capabilities)
        self.assertFalse(device.online)

    def test_the_enrolment_ceiling_matches_what_the_agent_advertises(self) -> None:
        # A file root alone advertises writes and deletes too, which are HIGH. The
        # ceiling has to admit the highest thing the agent advertises, or that
        # capability is announced, approved, and then refused at dispatch -- the
        # outcome the capability gates exist to prevent.
        device = self._agent().register()
        self.assertIs(device.risk_ceiling, RiskLevel.HIGH)
        self.assertGreaterEqual(
            risk_rank(device.risk_ceiling),
            max(risk_rank(d.risk_level) for d in device_definitions()
                if d.name in device.capabilities),
        )

    def test_a_machine_with_no_boundary_stays_at_the_fail_closed_floor(self) -> None:
        self.config = AgentConfig(device_id="bare-1")
        device = self._agent().register()
        self.assertEqual(device.capabilities, [])
        self.assertIs(device.risk_ceiling, RiskLevel.LOW)

    def test_closing_a_boundary_lowers_the_ceiling_on_the_next_start(self) -> None:
        # The ceiling is re-derived from configuration at every enrolment, so a
        # boundary the operator closes takes effect on restart. Lowering needs no
        # ceremony; raising is what `set_risk_ceiling` refuses.
        self.config = AgentConfig(
            device_id="laptop-1", workspace_root=self.root, allow_shell=True
        )
        held = self._agent().register()
        self.assertIs(held.risk_ceiling, RiskLevel.CRITICAL)
        self.config = AgentConfig(device_id="laptop-1", workspace_root=self.root)
        self.assertIs(self._agent().register().risk_ceiling, RiskLevel.HIGH)

    def test_heartbeat_brings_the_device_into_presence(self) -> None:
        agent = self._agent()
        agent.register()
        self.assertEqual(self.devices.available(now=self.now), [])
        agent.heartbeat()
        self.assertEqual(
            [
                d.device_id
                for d in self.devices.available("device.file.read", now=self.now)
            ],
            ["laptop-1"],
        )

    def test_connected_is_published_once_not_every_beat(self) -> None:
        agent = self._agent()
        agent.register()
        agent.heartbeat()
        agent.heartbeat()
        self.now += timedelta(seconds=5)
        agent.heartbeat()
        kinds = [event.event_type for event in self.hub.history()]
        self.assertEqual(kinds, ["device.connected"])

    def test_reconnection_republishes_connected(self) -> None:
        agent = self._agent()
        agent.register()
        agent.heartbeat()
        self.now += timedelta(seconds=DEFAULT_STALE_AFTER_S + 1)
        agent.heartbeat()
        kinds = [event.event_type for event in self.hub.history()]
        self.assertEqual(kinds, ["device.connected", "device.connected"])

    def test_disconnect_is_announced(self) -> None:
        agent = self._agent()
        agent.register()
        agent.heartbeat()
        agent.disconnect()
        kinds = [event.event_type for event in self.hub.history()]
        self.assertEqual(kinds, ["device.connected", "device.disconnected"])
        self.assertEqual(self.devices.available(), [])

    def test_disconnect_without_registration_is_a_no_op(self) -> None:
        self._agent().disconnect()
        self.assertEqual(self.hub.history(), [])

    def test_a_restart_readopts_the_same_identity(self) -> None:
        """The whole point of C19: a reboot must not mint a new principal."""
        first = self._agent().register()
        agent_two = self._agent()
        agent_two._device = None
        second = agent_two.register()
        self.assertEqual(second.identity_id, first.identity_id)
        self.assertEqual(self.identities.__len__(), 1)

    def test_a_restart_after_the_registry_emptied_still_reuses_the_identity(self) -> None:
        first = self._agent().register()
        self.devices.devices.clear()
        rebound = self._agent().register()
        self.assertEqual(rebound.identity_id, first.identity_id)

    def test_a_revoked_identity_is_not_readopted(self) -> None:
        first = self._agent().register()
        self.identities.revoke(first.identity_id)
        self.devices.devices.clear()
        with self.assertRaises(IdentityError):
            self._agent().register()

    def test_status_reports_the_device_card(self) -> None:
        agent = self._agent()
        agent.register()
        agent.heartbeat()
        status = agent.status()
        self.assertEqual(status["device_id"], "laptop-1")
        self.assertEqual(status["name"], "My Laptop")
        self.assertEqual(status["kind"], "laptop")
        self.assertTrue(status["online"])
        self.assertTrue(status["enrolled"])
        self.assertEqual(status["platform"], agent.config.platform)
        self.assertIn("capabilities", status)
        self.assertIn("resources", status)

    def test_status_before_registration_says_so(self) -> None:
        status = self._agent().status()
        self.assertFalse(status["registered"])

    def test_an_agent_without_events_still_works(self) -> None:
        agent = LaptopAgent(self.devices, self.config, clock=lambda: self.now)
        agent.register()
        agent.heartbeat()
        self.assertTrue(self.devices.is_online(self.devices.devices["laptop-1"], now=self.now))

    def test_run_loops_until_stopped_then_disconnects(self) -> None:
        """A heartbeat loop, driven for real rather than asserted about.

        ``unittest`` does not await coroutine tests — it reports them as passing
        without running the body, which is the exact hole that once hid four cycle
        tests in this suite. So the loop is run under ``asyncio.run``.
        """
        hub = EventHub()
        with tempfile.TemporaryDirectory() as root:
            config = AgentConfig(
                device_id="laptop-1",
                name="My Laptop",
                workspace_root=root,
                heartbeat_s=0.01,
            )
        agent = LaptopAgent(self.devices, config, events=hub)

        async def drive() -> list[str]:
            stop = asyncio.Event()
            task = asyncio.create_task(agent.run(stop))
            await asyncio.sleep(0.05)
            online = bool(self.devices.available())
            stop.set()
            await asyncio.wait_for(task, timeout=2.0)
            del online
            return [event.event_type for event in hub.history()]

        kinds = asyncio.run(drive())
        self.assertEqual(kinds, ["device.connected", "device.disconnected"])
        self.assertEqual(self.devices.available(), [])


class TestResources(unittest.TestCase):
    def test_resources_report_plausible_machine_load(self) -> None:
        found = resources()
        self.assertGreaterEqual(found.get("cpu_count", 0), 1)
        if "memory_total_mb" in found:
            self.assertGreater(found["memory_total_mb"], 0)
        if "memory_available_mb" in found:
            self.assertLess(found["memory_available_mb"], found["memory_total_mb"])
        for key in ("load_1m", "load_5m", "load_15m"):
            if key in found:
                self.assertGreaterEqual(found[key], 0.0)


class TestAgentInTheService(unittest.TestCase):
    """The wiring that decides whether a laptop is reachable at all."""

    GRAPH = {
        "nodes": [
            {
                "node_id": "a1",
                "action": {"type": "device.file.read", "parameters": {"path": "notes.txt"}},
            },
            {"node_id": "end", "node_type": "end", "depends_on": ["a1"]},
        ]
    }

    def setUp(self) -> None:
        for name in (ENV_ENABLED, ENV_WORKSPACE_ROOT, ENV_ALLOW_SHELL, ENV_DEVICE_ID,
                     "ACTIONMESH_DEVICE_NAME", ENV_HEARTBEAT_S, "ACTIONMESH_API_KEY"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_disabled_by_default_a_device_action_is_not_even_a_known_action(self) -> None:
        # Fail closed at the registry: no definition, no authorization, no provider.
        client = TestClient(create_app())
        response = client.post(
            "/execute",
            json={
                "graph": self.GRAPH,
                "actor": {"id": "abhi"},
                "policy": {"rules": [{"action": "device.file.read", "decision": "allow"}]},
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["state"], "denied")
        self.assertEqual(client.get("/devices").json(), [])

    def test_enabled_by_configuration_the_laptop_is_present_and_online(self) -> None:
        os.environ[ENV_ENABLED] = "1"
        os.environ[ENV_WORKSPACE_ROOT] = self.tmp.name
        os.environ[ENV_DEVICE_ID] = "thinkpad"
        os.environ["ACTIONMESH_DEVICE_NAME"] = "Work Laptop"
        app = create_app()
        client = TestClient(app)
        self.assertIsNotNone(app.state.agent)
        listed = client.get("/devices").json()
        self.assertEqual([d["device_id"] for d in listed], ["thinkpad"])
        self.assertTrue(listed[0]["online"])
        self.assertEqual(listed[0]["kind"], "laptop")
        self.assertIn("device.file.read", listed[0]["capabilities"])
        self.assertNotIn("device.terminal.execute", listed[0]["capabilities"])
        self.assertNotIn("identity_id", listed[0])

    def test_enabling_the_agent_registers_its_capabilities(self) -> None:
        os.environ[ENV_ENABLED] = "1"
        os.environ[ENV_WORKSPACE_ROOT] = self.tmp.name
        client = TestClient(create_app())
        offered = client.get("/capabilities", params={"provider": "laptop"}).json()
        names = [item["action"] for item in offered]
        # File and project capabilities only: this app was configured with a
        # workspace root and no shell flag. The C42 file verbs (11) are part of
        # the workspace-root boundary too, so the count is 7 + 11 = 18; the
        # browser/media ones stay behind their own flags (none set here).
        self.assertEqual(len(offered), 18)
        self.assertIn("device.project.list", names)
        self.assertNotIn("device.terminal.execute", names)
        self.assertNotIn("device.ide.open", names)
        self.assertEqual(len(client.get("/capabilities", params={"provider": "mock"}).json()), 16)

    def test_a_device_action_now_resolves_but_still_needs_consent(self) -> None:
        os.environ[ENV_ENABLED] = "1"
        os.environ[ENV_WORKSPACE_ROOT] = self.tmp.name
        client = TestClient(create_app())
        response = client.post(
            "/execute",
            json={
                "graph": self.GRAPH,
                "actor": {"id": "abhi"},
                "policy": {"rules": [{"action": "device.file.read", "decision": "allow"}]},
            },
        )
        # Registered, and still denied: no consent was recorded for file.read.
        self.assertEqual(response.json()["state"], "denied")

    def test_an_invalid_root_stops_startup(self) -> None:
        os.environ[ENV_ENABLED] = "1"
        os.environ[ENV_WORKSPACE_ROOT] = "/definitely/not/here"
        with self.assertRaises(ValueError) as ctx:
            create_app()
        self.assertIn("agent configuration is invalid", str(ctx.exception))

    def test_an_injected_agent_is_used_as_is(self) -> None:
        identities = IdentityStore()
        devices = DeviceRegistry(identities=identities)
        hub = EventHub()
        agent = LaptopAgent(
            devices,
            AgentConfig(device_id="injected", workspace_root=self.tmp.name),
            events=hub,
        )
        app = create_app(identities=identities, devices=devices, agent=agent)
        self.assertIs(app.state.agent, agent)
        self.assertEqual(
            [d["device_id"] for d in TestClient(app).get("/devices").json()], ["injected"]
        )


if __name__ == "__main__":
    unittest.main()
