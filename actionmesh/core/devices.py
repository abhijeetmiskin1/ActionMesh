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

import json
import sqlite3
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path

from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.permissions.risk import risk_rank
from actionmesh.protocol.models import DeviceInfo, DeviceKind, RiskLevel

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


class DeviceUnavailable(KeyError):
    """A device that exists but cannot be reached right now.

    Separate from a plain ``KeyError`` for an unknown id because the two reach the
    user as different sentences, and the difference is the point of C36: "there is no
    such computer" and "that computer is offline" are not the same answer, and only
    the second invites a user to go and switch it on. Both are still ``KeyError`` so
    that every existing ``require`` caller keeps catching "not usable" in one place.
    """

    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        super().__init__(
            f"device {device_id!r} is offline: it cannot be reached, and the request "
            "is not being sent to another computer"
        )

    def __str__(self) -> str:
        # KeyError wraps its argument in repr() when stringified; the sentence above
        # is already one, and a UI should show it verbatim.
        return str(self.args[0]) if self.args else ""


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
        # C36: registration and first connection are facts about *when the record
        # was created*, so they are stamped here rather than by every caller. An
        # explicit value survives, which is what re-registering a restored record
        # needs; a caller that invents one is choosing to lie about the age of a
        # device, and that choice stays visible in the record.
        if device.registered_at is None:
            device.registered_at = _utcnow()
        if device.last_connected_at is None and device.last_seen is not None:
            device.last_connected_at = device.last_seen
        self.devices[device.device_id] = device
        self._write(device)
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
        arch: str = "",
        app_version: str = "",
        risk_ceiling: RiskLevel = RiskLevel.LOW,
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
                arch=arch,
                app_version=app_version,
                risk_ceiling=risk_ceiling,
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
        self._drop(device_id)
        return True

    # -- presence ---------------------------------------------------------
    def heartbeat(
        self, device_id: str, *, online: bool = True, now: datetime | None = None
    ) -> DeviceInfo:
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        moment = now or _utcnow()
        # A heartbeat that re-establishes contact is a *connection*, and recording
        # it here means "last connected" needs no second, easily-forgotten call. A
        # heartbeat that merely repeats the current state does not move it: the
        # field is meant to answer "when did it last come back", and a value that
        # advances every 15 seconds answers nothing.
        if online and not self.is_online(device, now=moment):
            device.last_connected_at = moment
        device.online = online
        device.last_seen = moment
        self._write(device)
        return device

    def mark_connected(
        self, device_id: str, *, now: datetime | None = None
    ) -> DeviceInfo:
        """Record that this device has just established contact.

        Separate from :meth:`heartbeat` because they answer different questions, and
        a caller that owns a device (the agent rebuilding its own record at startup)
        knows it is connected without having heard a heartbeat from anyone. It is the
        only writer of ``last_connected_at`` for a device this process did not pair.
        """
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        device.last_connected_at = now or _utcnow()
        self._write(device)
        return device

    def set_risk_ceiling(
        self, device_id: str, ceiling: RiskLevel, *, by: str = ""
    ) -> DeviceInfo:
        """Lower a device's risk ceiling. Never raises one.

        The ceiling is a security boundary, so this method enforces the direction
        rather than trusting its caller: an increase has to go through enrollment by
        the device's owner. Pairing calls this on redeem, where the "owner" is the
        person at the laptop who chose the code's ceiling, and the check is what stops
        a pairing flow from ever becoming a way to grant more authority than the
        device already had.
        """
        if not isinstance(ceiling, RiskLevel):
            raise ValueError(f"risk ceiling must be a RiskLevel, got {ceiling!r}")
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        if risk_rank(ceiling) > risk_rank(device.risk_ceiling):
            raise PermissionError(
                f"device {device_id!r} has ceiling {device.risk_ceiling.value}; "
                f"refusing to raise it to {ceiling.value}"
                + (f" (requested by {by})" if by else "")
            )
        device.risk_ceiling = ceiling
        self._write(device)
        return device

    def ceiling_for(self, device_id: str) -> RiskLevel:
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        return device.risk_ceiling

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
        risk: RiskLevel | None = None,
        now: datetime | None = None,
    ) -> DeviceInfo:
        """Resolve ``device_id`` as a usable target, or raise.

        Checked in order: present, attributed, fresh, correct role, capable, within
        its risk ceiling. Each failure is its own exception type because each means
        something different to the caller — and to the audit record.

        ``risk`` is the risk the *action* would carry. A device's ceiling is chosen by
        its owner and can only be lowered, so this is where "this machine was never
        trusted to run that" stops a graph before a provider is ever consulted.
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
            raise DeviceUnavailable(device_id)
        if kind is not None and device.kind is not kind:
            raise PermissionError(
                f"device {device_id!r} is a {device.kind.value}, not a {kind.value}"
            )
        if capability is not None and capability not in device.capabilities:
            raise PermissionError(f"device {device_id!r} lacks {capability!r}")
        if risk is not None and risk_rank(risk) > risk_rank(device.risk_ceiling):
            raise PermissionError(
                f"device {device_id!r} has risk ceiling {device.risk_ceiling.value}; "
                f"refusing a {risk.value}-risk action on it"
            )
        return device

    def all_devices(self) -> list[DeviceInfo]:
        """Every device on record, offline ones included.

        Named apart from the ``devices`` dict it reads: a listing method shadowed
        by an instance attribute is a method nobody can call.
        """
        return sorted(self.devices.values(), key=lambda device: device.device_id)

    # -- storage hooks ----------------------------------------------------
    def _write(self, device: DeviceInfo) -> None:
        """Persist one device record. A no-op here; durable subclasses override it.

        The in-memory registry is the default and the right default: a development
        server should not open a file to answer a question about its own machine. A
        deployment that must remember its computers asks for :class:`SQLiteDeviceRegistry`
        explicitly, and every mutation routes through these two hooks so it cannot be
        forgotten at one call site and remembered at another.
        """
        return None

    def _drop(self, device_id: str) -> None:
        """Forget one device record. A no-op here; see :meth:`_write`."""
        return None


def _decode(payload: str) -> DeviceInfo:
    """Rebuild a :class:`DeviceInfo` from its stored JSON.

    Written out field by field rather than with ``DeviceInfo(**json.loads(...))`` so
    a stored record with an unexpected extra key cannot become a constructor
    argument, and a missing optional key gets its default instead of assuming the
    schema never changes. Unknown capability strings are kept: this is a record of
    what the device claimed, and quietly deleting claims would make the audit trail
    disagree with the registry.
    """
    raw: dict[str, object] = json.loads(payload)

    def _moment(key: str) -> datetime | None:
        value = raw.get(key)
        return datetime.fromisoformat(str(value)) if value else None

    claimed = raw.get("capabilities")
    capabilities = (
        [str(cap) for cap in claimed] if isinstance(claimed, list) else []
    )

    return DeviceInfo(
        device_id=str(raw["device_id"]),
        name=str(raw.get("name", "")),
        kind=DeviceKind(str(raw.get("kind", DeviceKind.UNKNOWN.value))),
        platform=str(raw.get("platform", "")),
        os_version=str(raw.get("os_version", "")),
        agent_version=str(raw.get("agent_version", "")),
        capabilities=capabilities,
        online=bool(raw.get("online", False)),
        last_seen=_moment("last_seen"),
        identity_id=str(raw.get("identity_id", "")),
        arch=str(raw.get("arch", "")),
        app_version=str(raw.get("app_version", "")),
        risk_ceiling=RiskLevel(str(raw.get("risk_ceiling", RiskLevel.LOW.value))),
        registered_at=_moment("registered_at"),
        last_connected_at=_moment("last_connected_at"),
    )


class SQLiteDeviceRegistry(DeviceRegistry):
    """A device registry that survives the process that wrote it.

    Its own database and its own table, following :class:`SQLiteIdentityStore` rather
    than the execution ``MIGRATIONS`` chain: presence is a different lifecycle from
    execution records, and forcing it through a schema version bump would make every
    execution-store assertion depend on a device table it has no use for.

    Rows are replayed on construction, and the replay deliberately does *not* re-run
    the admission checks in :meth:`DeviceRegistry.register`: those validate what a
    device is *claiming now*, and a restored row is a record of what was already
    admitted. Re-validating it would refuse to start on a device whose capabilities
    predate an action-registry change — a stale listing is a reason to show the device
    as needing attention, not a reason for the control service to fail closed on every
    other computer. Authority is not restored by this: :meth:`require` resolves the
    identity and re-derives presence on every use, so a device revoked while the
    process was down comes back visible and unusable.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        identities: IdentityStore | None = None,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        known_capability: Callable[[str], bool] | None = None,
    ) -> None:
        # A restored record may legitimately name a capability the current process
        # has not registered yet (an agent upgrade that adds one is not a reason to
        # refuse to start); validation still runs, so unregistered *new* claims are
        # refused at registration time.
        super().__init__(
            identities=identities,
            stale_after_s=stale_after_s,
            known_capability=known_capability,
        )
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        # The registry is shared by the agent's heartbeat thread and request handlers,
        # so it gets the same journal discipline as the execution store rather than
        # whatever the box happens to default to.
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS device_presence "
            "(device_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()
        for row in self.connection.execute(
            "SELECT payload FROM device_presence"
        ).fetchall():
            restored = _decode(row[0])
            # Presence never survives a restart as a *claim*: the process that heard
            # the heartbeat is gone, so the honest state is "record known, not
            # currently heard from". `last_seen` is kept so a human can see how long
            # it has been, and `is_online` turns it into False on its own.
            restored.online = False
            self.devices[restored.device_id] = restored

    def _write(self, device: DeviceInfo) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO device_presence (device_id, payload) VALUES (?, ?)",
            (device.device_id, device.model_dump_json()),
        )
        self.connection.commit()

    def _drop(self, device_id: str) -> None:
        self.connection.execute(
            "DELETE FROM device_presence WHERE device_id = ?", (device_id,)
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "EXECUTION_KINDS",
    "DeviceRegistry",
    "DeviceUnavailable",
    "SQLiteDeviceRegistry",
]
