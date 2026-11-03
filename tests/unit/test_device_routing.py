"""C36 — target-device routing, identity and authorization.

The work order's central rule is that an execution must never be performed on
the wrong computer. These tests pin down the executor's half of that rule:

* an empty target still means "this machine" (every pre-C36 caller);
* a named target is resolved through the device registry before anything is
  dispatched, and a refusal names a reason a person can act on;
* "offline" and "no such device" are different answers, and neither is a
  licence to try a different computer;
* the device's role and risk ceiling are enforced at the point of dispatch;
* the routing decision is attached to the result and the record, including
  across a durable store.

They deliberately do not exercise the API, VoiceOS or pairing: those layers
must *set* ``target_device_id``, but the authority that makes it real lives
here, and it has to hold when a client sets any string it likes.
"""

from __future__ import annotations

import asyncio
import unittest

from actionmesh.core.devices import DeviceRegistry, DeviceUnavailable
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import GraphBuilder
from actionmesh.core.identity import IdentityStore
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import (
    ActionGraph,
    ActionState,
    DeviceInfo,
    DeviceKind,
    Identity,
    RiskLevel,
)
from actionmesh.providers import MockProvider

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])


def context(scopes: list[str], *, risk: RiskLevel = RiskLevel.HIGH) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id="user_1"),
        policy=ALLOW_ALL,
        grants=[PermissionGrant(scope=s, max_risk=risk) for s in scopes],
    )


def single(action: dict[str, object], target: str = "") -> ActionGraph:
    """One action plus END, addressed to ``target``.

    ``GraphBuilder.build()`` carries neither an intent-supplied target nor a
    device id, so the target is assigned afterwards -- exactly as a planner's
    routing request would be before the executor sees it.
    """
    builder = GraphBuilder("device routing")
    builder.action(action, node_id="a1")
    builder.end(depends_on=["a1"])
    graph = builder.build()
    graph.target_device_id = target
    return graph


def send(text: str = "hi") -> dict[str, object]:
    # The action's own risk is part of ``effective_risk`` (the definition's is the
    # other half), and an omitted value defaults to MEDIUM -- deliberately, so a
    # planner cannot under-declare risk by leaving it out.
    return {"type": "message.send", "parameters": {"message": text}}


def remind(text: str = "call") -> dict[str, object]:
    return {
        "type": "reminder.create",
        "risk_level": "low",
        "parameters": {"text": text},
    }


def run(coro):
    return asyncio.run(coro)


class RoutingCase(unittest.TestCase):
    """A live engine, a registry with an identity store, and one mock provider."""

    def setUp(self) -> None:
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.provider = MockProvider()
        self.engine = ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [self.provider],
            devices=self.devices,
        )
        # A control process with no agent adapter has not declared which machine
        # it serves; tests opt in per case, because guessing is the failure mode.
        self.local_id = "dev_linux"
        self.devices.enroll(
            device_id=self.local_id,
            kind=DeviceKind.LAPTOP,
            name="Linux Workstation",
            platform="linux",
            arch="x86_64",
            capabilities=["message.send", "reminder.create"],
            risk_ceiling=RiskLevel.LOW,
        )
        self.devices.heartbeat(self.local_id)
        self.engine.local_device_id = self.local_id

    def other_laptop(self, *, online: bool = True, ceiling: RiskLevel = RiskLevel.MEDIUM) -> str:
        device_id = "dev_mac"
        self.devices.enroll(
            device_id=device_id,
            kind=DeviceKind.LAPTOP,
            name="MacBook",
            platform="macos",
            arch="arm64",
            capabilities=["message.send", "reminder.create"],
            risk_ceiling=ceiling,
        )
        if online:
            self.devices.heartbeat(device_id)
        return device_id

    def events(self, execution_id: str, event_type: str) -> list:
        return [
            event
            for event in self.engine.events.history(execution_id)
            if event.event_type == event_type
        ]

    def refusal(self, execution_id: str) -> dict:
        events = self.events(execution_id, "execution.device_refused")
        self.assertEqual(len(events), 1, "exactly one refusal is published")
        return dict(events[0].payload)


class TestLocalMachine(RoutingCase):
    """The empty target is not a wildcard: it is the machine this process serves."""

    def test_empty_target_runs_locally(self) -> None:
        result = run(self.engine.execute(single(send()), context(["message.send"])))

        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(result.device_id, "")
        self.assertEqual(self.engine.record(result.execution_id).device_id, "")
        self.assertEqual(len(self.provider.calls), 1)

    def test_declared_local_target_runs_and_is_named(self) -> None:
        result = run(
            self.engine.execute(single(remind(), self.local_id), context(["calendar.write"]))
        )

        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(result.device_id, self.local_id)
        self.assertEqual(result.to_dict()["device_id"], self.local_id)
        record = self.engine.record(result.execution_id)
        self.assertEqual(record.device_id, self.local_id)
        self.assertEqual(record.device_name, "Linux Workstation")


class TestUnknownTarget(RoutingCase):
    def test_unknown_device_is_refused_before_dispatch(self) -> None:
        result = run(
            self.engine.execute(single(send(), "dev_does_not_exist"), context(["message.send"]))
        )

        self.assertIs(result.state, ActionState.DENIED)
        self.assertIn("dev_does_not_exist", result.error)
        self.assertEqual(self.provider.calls, [], "nothing may run on a refusal")
        self.assertEqual(self.refusal(result.execution_id)["cause"], "unknown")
        self.assertEqual(self.engine.record(result.execution_id).state, ActionState.DENIED)

    def test_named_target_without_a_registry_is_never_run_locally(self) -> None:
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(), PolicyEngine(), [self.provider]
        )
        result = run(engine.execute(single(send(), self.local_id), context(["message.send"])))

        self.assertIs(result.state, ActionState.DENIED)
        self.assertIn("no device registry is attached", result.error)
        self.assertEqual(self.provider.calls, [])


class TestOfflineTarget(RoutingCase):
    def test_offline_target_is_refused_and_not_redirected(self) -> None:
        offline = self.other_laptop(online=False)
        # A second, reachable laptop exists; the run must still stay aimed at the
        # machine the user named.
        online = "dev_online"
        self.devices.enroll(
            device_id=online, kind=DeviceKind.LAPTOP, name="Spare", capabilities=["message.send"]
        )
        self.devices.heartbeat(online)

        result = run(self.engine.execute(single(send(), offline), context(["message.send"])))

        self.assertIs(result.state, ActionState.DENIED)
        self.assertIn("is offline", result.error)
        self.assertIn("not being sent to another computer", result.error)
        self.assertEqual(self.provider.calls, [], "an offline target is not routed elsewhere")
        self.assertEqual(self.refusal(result.execution_id)["cause"], "offline")

    def test_offline_and_unknown_are_different_answers(self) -> None:
        offline = self.other_laptop(online=False)
        offline_result = run(
            self.engine.execute(single(send(), offline), context(["message.send"]))
        )
        unknown_result = run(
            self.engine.execute(single(send(), "dev_nope"), context(["message.send"]))
        )

        self.assertIn("is offline", offline_result.error)
        self.assertNotIn("is offline", unknown_result.error)
        self.assertEqual(
            self.refusal(offline_result.execution_id)["cause"],
            "offline",
        )
        self.assertEqual(self.refusal(unknown_result.execution_id)["cause"], "unknown")

    def test_device_unavailable_is_a_keyerror_with_a_readable_sentence(self) -> None:
        # Existing callers catch "not usable" as KeyError; the message is for
        # humans and must not come back wrapped in repr() quotes.
        exc = DeviceUnavailable("dev_mac")
        self.assertIsInstance(exc, KeyError)
        self.assertTrue(str(exc).startswith("device 'dev_mac' is offline"))
        self.assertIn("not being sent to another computer", str(exc))


class TestAttribution(RoutingCase):
    def test_revoked_identity_is_unattributed_and_refused(self) -> None:
        device = self.other_laptop()
        identity = self.identities.for_device(device)
        self.identities.revoke(identity.identity_id)

        result = run(self.engine.execute(single(send(), device), context(["message.send"])))

        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.refusal(result.execution_id)["cause"], "unattributed")

    def test_device_with_no_identity_at_all_cannot_be_targeted(self) -> None:
        # A phone may be listed without a bound identity; presence is not identity.
        self.devices.register(
            DeviceInfo(device_id="dev_phone", kind=DeviceKind.PHONE, name="Pixel")
        )
        self.devices.heartbeat("dev_phone")

        result = run(
            self.engine.execute(single(send(), "dev_phone"), context(["message.send"]))
        )

        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [])


class TestRoleGating(RoutingCase):
    def test_phone_is_a_control_surface_not_an_execution_device(self) -> None:
        self.devices.enroll(
            device_id="dev_phone", kind=DeviceKind.PHONE, name="Pixel"
        )
        self.devices.heartbeat("dev_phone")

        result = run(
            self.engine.execute(single(send(), "dev_phone"), context(["message.send"]))
        )

        self.assertIs(result.state, ActionState.DENIED)
        self.assertIn("cannot execute actions", result.error)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(self.refusal(result.execution_id)["cause"], "not_permitted")


class TestWrongMachine(RoutingCase):
    def test_target_not_served_by_this_process_is_never_dispatched(self) -> None:
        result = run(
            self.engine.execute(single(send(), self.other_laptop()), context(["message.send"]))
        )

        self.assertIs(result.state, ActionState.FAILED)
        self.assertIn("not this machine", result.error)
        self.assertEqual(self.provider.calls, [], "the local agent must not run it")

    def test_local_serving_is_by_declared_id_not_resemblance(self) -> None:
        # A device that merely looks like this host (same kind, same platform) is
        # not proof it *is* this host.
        self.engine.local_device_id = ""
        twin = self.other_laptop()
        self.devices.devices[twin].platform = "linux"

        result = run(self.engine.execute(single(send(), twin), context(["message.send"])))

        self.assertIs(result.state, ActionState.FAILED)
        self.assertEqual(self.provider.calls, [])

    def test_provider_carrying_the_device_id_is_the_adapter_for_it(self) -> None:
        # What a future agent transport will do: an adapter that declares which
        # device it serves is allowed to serve it -- but only in an engine that
        # has not declared a different machine for itself.
        device = self.other_laptop()
        self.provider.device_id = device
        self.engine.local_device_id = ""

        result = run(self.engine.execute(single(send(), device), context(["message.send"])))

        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(result.device_id, device)
        self.assertEqual(len(self.provider.calls), 1)


class TestRiskCeiling(RoutingCase):
    def test_device_ceiling_refuses_a_node_above_it(self) -> None:
        # The Linux workstation was enrolled with a LOW ceiling: it may create a
        # reminder, but not send a message.
        result = run(
            self.engine.execute(single(send(), self.local_id), context(["message.send"]))
        )

        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [])
        decided = self.events(result.execution_id, "authorization.decided")
        self.assertEqual(len(decided), 1)
        self.assertEqual(decided[0].payload["decision"], "deny")
        self.assertEqual(decided[0].payload["device_id"], self.local_id)
        self.assertEqual(decided[0].payload["device_risk_ceiling"], "low")
        self.assertIn("risk ceiling", decided[0].payload["reasons"][0])

    def test_same_device_admits_a_node_within_its_ceiling(self) -> None:
        result = run(
            self.engine.execute(single(remind(), self.local_id), context(["calendar.write"]))
        )

        self.assertIs(result.state, ActionState.SUCCESS)
        self.assertEqual(len(self.provider.calls), 1)

    def test_device_ceiling_is_not_the_credential_ceiling(self) -> None:
        # HIGH ceiling on the device, but the actor's grant only covers LOW: the
        # policy engine refuses it, which proves the two limits are independent.
        device = self.other_laptop(ceiling=RiskLevel.HIGH)
        self.engine.local_device_id = device

        result = run(
            self.engine.execute(
                single(send(), device),
                context(["message.send"], risk=RiskLevel.LOW),
            )
        )

        self.assertIs(result.state, ActionState.DENIED)
        self.assertEqual(self.provider.calls, [])


class TestDurableAttribution(RoutingCase):
    def test_record_keeps_the_routed_device_across_the_store(self) -> None:
        store = SQLiteStore()
        self.addCleanup(store.close)
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [self.provider],
            devices=self.devices,
            store=store,
        )
        engine.local_device_id = self.local_id

        result = run(
            engine.execute(single(remind(), self.local_id), context(["calendar.write"]))
        )
        self.assertIs(result.state, ActionState.SUCCESS)

        stored = store.get_record(result.execution_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.device_id, self.local_id)
        self.assertEqual(stored.device_name, "Linux Workstation")

    def test_refusal_is_persisted_with_the_named_device(self) -> None:
        store = SQLiteStore()
        self.addCleanup(store.close)
        engine = ExecutionEngine(
            ActionRegistry.with_seeds(),
            PolicyEngine(),
            [self.provider],
            devices=self.devices,
            store=store,
        )

        result = run(
            engine.execute(single(send(), "dev_missing"), context(["message.send"]))
        )

        stored = store.get_record(result.execution_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.state, ActionState.DENIED)
        self.assertEqual(stored.device_id, "dev_missing")
        self.assertIn("dev_missing", stored.error)


class TestSerialisation(unittest.TestCase):
    def test_target_device_round_trips_through_the_wire_format(self) -> None:
        from actionmesh.core.graph.serialization import graph_from_dict, graph_to_dict

        graph = single(send(), "dev_linux")
        restored = graph_from_dict(graph_to_dict(graph))

        self.assertEqual(restored.target_device_id, "dev_linux")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()