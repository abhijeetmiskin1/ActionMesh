"""C11 device registry tests."""

from __future__ import annotations

import unittest

from actionmesh.core.devices import DeviceRegistry
from actionmesh.protocol.models import DeviceInfo


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


if __name__ == "__main__":
    unittest.main()