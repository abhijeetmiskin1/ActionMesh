"""C11/C19 device registry tests — presence, capability gating, identity binding."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import DeviceInfo, DeviceKind


class TestDeviceRegistry(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = DeviceRegistry()
        self.registry.register(
            DeviceInfo(
                device_id="phone-1",
                name="Phone",
                capabilities=["device.notify", "device.send_file"],
            )
        )

    def test_offline_devices_are_not_available(self) -> None:
        self.assertEqual(self.registry.available(), [])

    def test_heartbeat_makes_device_available(self) -> None:
        device = self.registry.heartbeat("phone-1")
        self.assertTrue(device.online)
        self.assertEqual(self.registry.available("device.notify")[0].device_id, "phone-1")

    def test_capability_is_required(self) -> None:
        self.registry.heartbeat("phone-1")
        with self.assertRaises(PermissionError):
            self.registry.require("phone-1", "calendar.write")

    def test_unknown_heartbeat_fails(self) -> None:
        with self.assertRaises(KeyError):
            self.registry.heartbeat("missing")


class TestPresenceIsDerived(unittest.TestCase):
    """A device that stops reporting is offline, whatever its record claims."""

    def setUp(self) -> None:
        self.registry = DeviceRegistry(stale_after_s=30)
        self.registry.register(DeviceInfo(device_id="laptop-1", kind=DeviceKind.LAPTOP))

    def test_registered_online_without_a_heartbeat_is_offline(self) -> None:
        # Fail closed: `online=True` is a claim, `last_seen` is the evidence.
        self.registry.register(
            DeviceInfo(device_id="ghost", name="Ghost", online=True)
        )
        self.assertFalse(self.registry.is_online(self.registry.devices["ghost"]))
        self.assertEqual(self.registry.available("anything"), [])

    def test_a_fresh_heartbeat_is_online(self) -> None:
        self.registry.heartbeat("laptop-1")
        self.assertTrue(self.registry.is_online(self.registry.devices["laptop-1"]))

    def test_a_stale_heartbeat_is_offline(self) -> None:
        self.registry.heartbeat("laptop-1", now=datetime.now(UTC) - timedelta(seconds=31))
        self.assertFalse(self.registry.is_online(self.registry.devices["laptop-1"]))
        self.assertEqual(self.registry.available(), [])

    def test_stale_devices_are_reported_not_hidden(self) -> None:
        self.registry.heartbeat("laptop-1", now=datetime.now(UTC) - timedelta(minutes=5))
        stale = self.registry.stale()
        self.assertEqual([d.device_id for d in stale], ["laptop-1"])
        # The record still says online; the registry does not echo the claim back.
        self.assertTrue(stale[0].online)

    def test_explicit_offline_beats_a_fresh_timestamp(self) -> None:
        self.registry.heartbeat("laptop-1")
        self.registry.heartbeat("laptop-1", online=False)
        self.assertFalse(self.registry.is_online(self.registry.devices["laptop-1"]))

    def test_stale_after_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            DeviceRegistry(stale_after_s=0)


class TestCapabilityRegistry(unittest.TestCase):
    """Capabilities are ordinary ActionMesh actions, so the registry checks them."""

    def setUp(self) -> None:
        self.actions = ActionRegistry.with_seeds()
        self.registry = DeviceRegistry(known_capability=self.actions.has)

    def test_registered_capability_is_accepted(self) -> None:
        device = self.registry.register(
            DeviceInfo(device_id="laptop-1", capabilities=["message.send"])
        )
        self.assertEqual(device.capabilities, ["message.send"])

    def test_invented_capability_is_refused(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self.registry.register(
                DeviceInfo(device_id="laptop-1", capabilities=["device.delete_world"])
            )
        self.assertIn("unregistered", str(ctx.exception))
        self.assertEqual(self.registry.all_devices(), [])


class TestIdentityBinding(unittest.TestCase):
    def setUp(self) -> None:
        self.identities = IdentityStore()
        self.registry = DeviceRegistry(identities=self.identities)

    def test_enroll_mints_and_binds_an_identity(self) -> None:
        device = self.registry.enroll(
            device_id="laptop-1",
            kind=DeviceKind.LAPTOP,
            name="My Laptop",
            platform="linux",
            os_version="Ubuntu 24.04",
            agent_version="0.1.0",
            capabilities=["message.send"],
        )
        self.assertTrue(device.identity_id)
        self.assertEqual(
            self.identities.resolve(device.identity_id).device_id, "laptop-1"
        )
        self.assertFalse(device.online)  # enrolled, not yet seen

    def test_execution_role_without_an_identity_is_refused(self) -> None:
        with self.assertRaises(IdentityError):
            self.registry.register(
                DeviceInfo(device_id="laptop-9", kind=DeviceKind.LAPTOP)
            )

    def test_a_supplied_identity_must_resolve(self) -> None:
        # The string is attacker-visible once it appears in an export; possession
        # of the identity is what the store has to confirm.
        with self.assertRaises(IdentityError):
            self.registry.register(
                DeviceInfo(device_id="phone-9", identity_id="idn_forged_value")
            )

    def test_a_revoked_identity_cannot_be_used_to_register(self) -> None:
        identity = self.identities.enroll("laptop-1", DeviceKind.LAPTOP)
        self.identities.revoke(identity.identity_id)
        with self.assertRaises(IdentityError):
            self.registry.register(
                DeviceInfo(device_id="laptop-1", identity_id=identity.identity_id)
            )

    def test_require_refuses_an_unattributed_device(self) -> None:
        # Registered before identity was wired: present, but not a principal.
        plain = DeviceRegistry()
        plain.register(DeviceInfo(device_id="laptop-1", kind=DeviceKind.LAPTOP))
        plain.heartbeat("laptop-1")
        self.assertTrue(plain.is_online(plain.devices["laptop-1"]))

        guarded = DeviceRegistry(identities=self.identities)
        guarded.devices["laptop-1"] = plain.devices["laptop-1"]
        with self.assertRaises(IdentityError):
            guarded.require("laptop-1", "message.send")

    def test_forget_revokes_the_bound_identity(self) -> None:
        device = self.registry.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        self.assertTrue(self.registry.forget("laptop-1"))
        self.assertIsNone(self.registry.devices.get("laptop-1"))
        with self.assertRaises(IdentityError):
            self.identities.resolve(device.identity_id)

    def test_forget_of_an_unknown_device_is_false(self) -> None:
        self.assertFalse(self.registry.forget("nope"))

    def test_enroll_without_a_store_is_refused(self) -> None:
        with self.assertRaises(IdentityError):
            DeviceRegistry().enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)


class TestRoleGating(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = DeviceRegistry()
        self.registry.register(DeviceInfo(device_id="laptop-1", kind=DeviceKind.LAPTOP))
        self.registry.register(DeviceInfo(device_id="phone-1", kind=DeviceKind.PHONE))
        for device_id in ("laptop-1", "phone-1"):
            self.registry.heartbeat(device_id)

    def test_kind_is_checked(self) -> None:
        # A phone is a control surface. Targeting one as an execution device is a
        # category error, not a permission to grant.
        with self.assertRaises(PermissionError):
            self.registry.require("phone-1", kind=DeviceKind.LAPTOP)
        self.registry.require("laptop-1", kind=DeviceKind.LAPTOP)

    def test_available_filters_by_kind(self) -> None:
        self.assertEqual(
            [d.device_id for d in self.registry.available(kind=DeviceKind.PHONE)],
            ["phone-1"],
        )

    def test_all_devices_lists_offline_ones(self) -> None:
        self.registry.heartbeat("phone-1", online=False)
        listed = self.registry.all_devices()
        self.assertEqual([d.device_id for d in listed], ["laptop-1", "phone-1"])
        self.assertEqual(
            [d.device_id for d in self.registry.available()], ["laptop-1"]
        )


if __name__ == "__main__":
    unittest.main()
