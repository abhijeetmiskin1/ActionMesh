"""C11 device registry and capability-aware mesh presence."""

from __future__ import annotations

from datetime import UTC, datetime

from actionmesh.protocol.models import DeviceInfo


class DeviceRegistry:
    """In-memory device presence; no network connection is implied."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceInfo] = {}

    def register(self, device: DeviceInfo) -> DeviceInfo:
        if not device.device_id.strip():
            raise ValueError("device_id is required")
        self.devices[device.device_id] = device
        return device

    def heartbeat(self, device_id: str, *, online: bool = True) -> DeviceInfo:
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        device.online = online
        device.last_seen = datetime.now(UTC)
        return device

    def available(self, capability: str | None = None) -> list[DeviceInfo]:
        devices = [device for device in self.devices.values() if device.online]
        if capability is not None:
            devices = [device for device in devices if capability in device.capabilities]
        return sorted(devices, key=lambda device: device.device_id)

    def require(self, device_id: str, capability: str | None = None) -> DeviceInfo:
        device = self.devices.get(device_id)
        if device is None or not device.online:
            raise KeyError(f"device {device_id!r} is unavailable")
        if capability is not None and capability not in device.capabilities:
            raise PermissionError(f"device {device_id!r} lacks {capability!r}")
        return device


__all__ = ["DeviceRegistry"]