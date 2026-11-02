"""C11/C19 device presence: who is here, what they can do, and whether we can prove it.

The registry answers three questions and deliberately none of them is "is this
allowed":

* **Is it present?** ``online`` is a claim; presence is derived from the last
  heartbeat and expires. A device that stops reporting is offline, whether or not
  its record still says ``online = True``.
* **Can we attribute it?** A device bound to an identity is a principal; one
  without a binding is *visible but not authorizable*. Presence is not identity —
  a phone that reconnects from a new address is the same device precisely because
  its identity is not its address.
* **Does it offer the capability?** ``capabilities`` is what the device reports it
  can do, checked against the action registry so a device cannot advertise a
  capability ActionMesh has never heard of.

Authorization is C3's job. This module is a *precondition* for it: the executor
never asks the policy engine to run an action on a device it cannot name.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceInfo, DeviceKind

#: A device that has not been heard from for this long is treated as offline even
#: if its record still claims to be online. Loose enough for a laptop sleeping for
#: a minute, tight enough that a stale entry cannot hold a control session open.
DEFAULT_STALE_AFTER_S = 45.0

#: Roles that may *execute* on a device. A phone is a control surface: it approves,
#: it does not run. Letting a phone register as an execution device would invert the
#: control plane, so the role is checked rather than assumed.
EXECUTION_KINDS: frozenset[DeviceKind] = frozenset({DeviceKind.LAPTOP, DeviceKind.SERVER})


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeviceRegistry:
    """Device presence and capability gating; no network connection is implied."""

    def __init__(
        self,
        *,
        identities: IdentityStore | None = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        known_capability: Callable[[str], bool] | None = None,
    ) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.devices: dict[str, DeviceInfo] = {}
        self.identities = identities
        self.stale_after_s = float(stale_after_s)
        #: Usually ``ActionRegistry.has`` — see :meth:`register`.
        self.known_capability = known_capability

    # -- registration -----------------------------------------------------
    def register(self, device: DeviceInfo) -> DeviceInfo:
        """Record a device, validating what it claims before it is listed.

        Two claims are checked rather than trusted:

        * an ``identity_id`` must resolve through the configured identity store.
          A device that *supplies* a plausible-looking identity string it does not
          hold would otherwise self-assert a principal that audits will believe.
        * an execution role (laptop, server) must carry a bound identity, and a
          capability list may not contain a name the registry cannot resolve.
        """
        if not isinstance(device, DeviceInfo):
            raise TypeError("device must be a DeviceInfo")
        if not device.device_id.strip():
            raise ValueError("device_id is required")

        if self.identities is not None:
            if device.identity_id:
                self.identities.resolve(device.identity_id)
            elif device.kind in EXECUTION_KINDS:
                raise IdentityError(
                    f"device {device.device_id!r} claims role {device.kind.value} "
                    "but has no bound identity; enroll it first"
                )

        if self.known_capability is not None:
            unknown = [
                cap for cap in device.capabilities if not self.known_capability(cap)
            ]
            if unknown:
                raise ValueError(
                    f"device {device.device_id!r} advertises unregistered "
                    f"capacities: {unknown}; a capability must be a registered action"
                )

        # Presence is derived, never inherited from a stale record. A device
        # registered as ``online=True`` with no heartbeat has not been seen.
        if device.last_seen is None:
            device.online = False
        self.devices[device.device_id] = device
        return device

    def enroll(
        self,
        *,
        device_id: str,
        kind: DeviceKind,
        name: str = "",
        platform: str = "",
        os_version: str = "",
        agent_version: str = "",
        capabilities: Iterable[str] = (),
    ) -> DeviceInfo:
        """Mint an identity and register the device that holds it, in one step.

        This is the only way a device enters a registry configured with an
        identity store, and it is *local*: there is no HTTP route that reaches it.
        Remote admission goes through pairing, which has to prove the request came
        from someone standing at the laptop.
        """
        if self.identities is None:
            raise IdentityError(
                "this registry has no identity store; register() a device directly"
            )
        identity = self.identities.enroll(device_id, kind, label=name)
        return self.register(
            DeviceInfo(
                device_id=device_id.strip(),
                name=name.strip(),
                kind=kind,
                platform=platform,
                os_version=os_version,
                agent_version=agent_version,
                capabilities=sorted(set(capabilities)),
                identity_id=identity.identity_id,
            )
        )

    def forget(self, device_id: str, *, now: datetime | None = None) -> bool:
        """Drop a device and revoke the identity bound to it.

        Removing the presence record alone would leave the credential live, so a
        "forgotten" laptop could still show up later under the same identity. Both
        halves go together or neither does.
        """
        device = self.devices.pop(device_id, None)
        if device is None:
            return False
        if self.identities is not None and device.identity_id:
            self.identities.revoke(device.identity_id, now=now)
        return True

    # -- presence ---------------------------------------------------------
    def heartbeat(
        self, device_id: str, *, online: bool = True, now: datetime | None = None
    ) -> DeviceInfo:
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        device.online = online
        device.last_seen = now or _utcnow()
        return device

    def is_online(self, device: DeviceInfo, *, now: datetime | None = None) -> bool:
        """True only for a device seen recently. Everything else is offline."""
        if not device.online or device.last_seen is None:
            return False
        moment = now or _utcnow()
        age = (moment - device.last_seen).total_seconds()
        return age <= self.stale_after_s

    def available(
        self,
        capability: str | None = None,
        *,
        kind: DeviceKind | None = None,
        now: datetime | None = None,
    ) -> list[DeviceInfo]:
        devices = [
            device for device in self.devices.values() if self.is_online(device, now=now)
        ]
        if kind is not None:
            devices = [device for device in devices if device.kind is kind]
        if capability is not None:
            devices = [device for device in devices if capability in device.capabilities]
        return sorted(devices, key=lambda device: device.device_id)

    def stale(self, *, now: datetime | None = None) -> list[DeviceInfo]:
        """Devices claiming to be online but not heard from in time.

        Reported separately rather than silently hidden, so a UI can say "this
        laptop went away" instead of "this laptop is gone, reason unknown".
        """
        return sorted(
            (
                device
                for device in self.devices.values()
                if device.online and not self.is_online(device, now=now)
            ),
            key=lambda device: device.device_id,
        )

    def require(
        self,
        device_id: str,
        capability: str | None = None,
        *,
        kind: DeviceKind | None = None,
        now: datetime | None = None,
    ) -> DeviceInfo:
        """Resolve ``device_id`` as a usable target, or raise.

        Checked in order: present, attributed, fresh, correct role, capable. Each
        failure is its own exception type because each means something different to
        the caller — and to the audit record.
        """
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        if self.identities is not None:
            # Existence of the string is not possession of the identity.
            if not device.identity_id:
                raise IdentityError(
                    f"device {device_id!r} has no bound identity and cannot be targeted"
                )
            self.identities.resolve(device.identity_id)
        if not self.is_online(device, now=now):
            raise KeyError(f"device {device_id!r} is unavailable")
        if kind is not None and device.kind is not kind:
            raise PermissionError(
                f"device {device_id!r} is a {device.kind.value}, not a {kind.value}"
            )
        if capability is not None and capability not in device.capabilities:
            raise PermissionError(f"device {device_id!r} lacks {capability!r}")
        return device

    def all_devices(self) -> list[DeviceInfo]:
        """Every device on record, offline ones included.

        Named apart from the ``devices`` dict it reads: a listing method shadowed
        by an instance attribute is a method nobody can call.
        """
        return sorted(self.devices.values(), key=lambda device: device.device_id)


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "EXECUTION_KINDS",
    "DeviceRegistry",
]
