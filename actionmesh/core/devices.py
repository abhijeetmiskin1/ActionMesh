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
from typing import Protocol, runtime_checkable

from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.permissions.risk import risk_rank
from actionmesh.core.sqlite_guard import guarded, make_lock
from actionmesh.protocol.models import DeviceInfo, DeviceKind, RiskLevel

#: A device that has not been heard from for this long is treated as offline even
#: if its record still claims to be online. Loose enough for a laptop sleeping for
#: a minute, tight enough that a stale entry cannot hold a control session open.
DEFAULT_STALE_AFTER_S = 45.0

#: Roles that may *execute* on a device. A phone is a control surface: it approves,
#: it does not run. Letting a phone register as an execution device would invert the
#: control plane, so the role is checked rather than assumed.
EXECUTION_KINDS: frozenset[DeviceKind] = frozenset({DeviceKind.LAPTOP, DeviceKind.SERVER})


@runtime_checkable
class PresenceForgetting(Protocol):
    """The presence-side half of the forget-cascade (Agent 4 gap 1).

    ``DeviceRegistry.forget`` drops the record and revokes the identity. That is
    the registry's half. The agent process that owns the device holds its own
    cached ``DeviceInfo`` (``_device``) and beats on it; without clearing that
    cache the forgotten device could "resurrect" via a stale heartbeat — the
    next ``heartbeat()`` would re-register it. This protocol is the narrow
    surface the cascade calls, declared here so ``presence.py`` can satisfy it
    without an import cycle (``presence`` imports ``devices``, not the reverse).

    Duck-typed at runtime via ``isinstance`` and called defensively: a registry
    with no agent wired to it (the common case in tests) simply has nothing to
    cascade to.
    """

    def forget(self, device_id: str) -> bool:
        """Clear any cached presence record for ``device_id``. True if one existed."""
        ...

    def forget_all(self) -> bool:
        """Clear any cached presence record. True if one existed."""
        ...


@runtime_checkable
class CredentialRevokeCascade(Protocol):
    """The credential-side half of the forget-cascade (Phase 4 Agent 3 gap 1).

    ``DeviceRegistry.forget`` drops the record and revokes the identity, but the
    credential records bound to that identity stay ``is_active()`` until they
    are revoked too — any consumer reading the store directly would still see
    the forgotten device as live. ``CredentialStore.revoke_for_identity`` is
    the narrow surface this cascade calls, declared here so ``devices`` can
    reach the store without importing it: ``credentials`` already imports the
    identity store, and a hard import of the credential store from devices
    would risk a cycle (and tie a presence registry to the whole auth stack).

    Optional and defensive, exactly like :class:`PresenceForgetting`: a
    registry with no store wired forgets as it always has. This is *not* a
    second credential store and *not* a second authority surface — it is a
    revocation hook over the single canonical store.
    """

    def revoke_for_identity(self, identity_id: str, *, now: object = None) -> int:
        """Revoke every live credential bound to one identity; return the count."""
        ...


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
        presence: PresenceForgetting | None = None,
        credentials: CredentialRevokeCascade | None = None,
    ) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be positive")
        self.devices: dict[str, DeviceInfo] = {}
        self.identities = identities
        self.stale_after_s = float(stale_after_s)
        #: Usually ``ActionRegistry.has`` — see :meth:`register`.
        self.known_capability = known_capability
        #: The agent-side presence cache, if one is wired. ``forget`` cascades
        #: into it so a forgotten device cannot resurrect via a stale heartbeat
        #: from the agent process that still holds its ``DeviceInfo``.
        self.presence = presence
        #: The canonical credential store, if one is wired. ``forget`` cascades
        #: into it so the live credentials bound to the forgotten device's
        #: identity are revoked outright, not merely unreachable through
        #: :meth:`IdentityStore.resolve`. Optional and defensive (see
        #: :class:`CredentialRevokeCascade`); never a second store.
        self.credentials = credentials

    # -- registration -----------------------------------------------------
    @guarded
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

    @guarded
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

    @guarded
    def forget(self, device_id: str, *, now: datetime | None = None) -> bool:
        """Drop a device and revoke the identity bound to it.

        Removing the presence record alone would leave the credential live, so a
        "forgotten" laptop could still show up later under the same identity. Both
        halves go together or neither does.

        When a credential store is wired, the cascade also revokes every live
        credential bound to the identity (Phase 4 Agent 3 gap 1), so the
        records themselves are dead — not only unreachable via
        :meth:`IdentityStore.resolve`. The cascades are defensive
        (``isinstance`` + suppression): a registry with no agent or no store
        wired is not an error.

        The cascade (Agent 4 gap 1) also clears the agent-side presence cache:
        the owning ``LaptopAgent`` holds its own ``DeviceInfo`` and beats on it, so
        a registry-only forget would let the stale heartbeat in that process
        resurrect the device. Clearing ``_device`` forces the next heartbeat
        through ``register()``, which re-checks identity resolution and refuses
        the revoked identity — fail closed, never silently restoring revoked
        authority. The cascade is defensive (``isinstance`` + ``suppress``): a
        registry with no agent wired, or an agent that does not hold this device,
        is not an error.
        """
        device = self.devices.pop(device_id, None)
        if device is None:
            return False
        if self.identities is not None and device.identity_id:
            self.identities.revoke(device.identity_id, now=now)
        if device.identity_id:
            self._cascade_credential_revoke(device.identity_id, now=now)
        self._drop(device_id)
        self._cascade_presence_forget(device_id)
        return True

    def _cascade_credential_revoke(
        self, identity_id: str, *, now: datetime | None = None
    ) -> int:
        """Revoke the wired credential store's live credentials for one identity.

        Returns the number revoked. Never raises: like the presence cascade,
        this is best-effort hygiene on top of an already-successful forget, and
        a misbehaving store must not turn a successful forget into a failure
        that leaves the identity live. The store is duck-typed through
        :class:`CredentialRevokeCascade` so the registry never imports the
        credential stack.
        """
        store = self.credentials
        if store is None or not isinstance(store, CredentialRevokeCascade):
            return 0
        try:
            # ``revoke_for_identity`` accepts ``now`` in the canonical store;
            # a narrower cascade target may not, so try the time-aware call
            # first and fall back to the Protocol signature.
            try:
                return store.revoke_for_identity(identity_id, now=now)
            except TypeError:
                return store.revoke_for_identity(identity_id)
        except Exception:  # cascade must not fail a forget
            return 0

    def _cascade_presence_forget(self, device_id: str) -> bool:
        """Clear the wired agent's cached presence for ``device_id``.

        Returns True when a cached record was cleared. Never raises: the cascade
        is best-effort presence hygiene, and a misbehaving agent must not turn a
        successful forget into a failure that leaves the identity live.
        """
        presence = self.presence
        if presence is None or not isinstance(presence, PresenceForgetting):
            return False
        try:
            # Prefer the targeted clear; fall back to the blunt one only when
            # the targeted clear reports it held nothing (e.g. a test double
            # that only implements the blunt surface).
            cleared = presence.forget(device_id)
            if cleared:
                return True
            return presence.forget_all()
        except Exception:  # cascade must not fail a forget
            return False

    # -- presence ---------------------------------------------------------
    @guarded
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

    @guarded
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

    @guarded
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

    @guarded
    def ceiling_for(self, device_id: str) -> RiskLevel:
        device = self.devices.get(device_id)
        if device is None:
            raise KeyError(f"unknown device {device_id!r}")
        return device.risk_ceiling

    @guarded
    def is_online(self, device: DeviceInfo, *, now: datetime | None = None) -> bool:
        """True only for a device seen recently. Everything else is offline."""
        if not device.online or device.last_seen is None:
            return False
        moment = now or _utcnow()
        age = (moment - device.last_seen).total_seconds()
        return age <= self.stale_after_s

    @guarded
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

    @guarded
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

    @guarded
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

    @guarded
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
        presence: PresenceForgetting | None = None,
        credentials: CredentialRevokeCascade | None = None,
    ) -> None:
        # A restored record may legitimately name a capability the current process
        # has not registered yet (an agent upgrade that adds one is not a reason to
        # refuse to start); validation still runs, so unregistered *new* claims are
        # refused at registration time.
        super().__init__(
            identities=identities,
            stale_after_s=stale_after_s,
            known_capability=known_capability,
            presence=presence,
            credentials=credentials,
        )
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = make_lock()
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

    @guarded
    def close(self) -> None:
        self.connection.close()


__all__ = [
    "DEFAULT_STALE_AFTER_S",
    "EXECUTION_KINDS",
    "DeviceRegistry",
    "DeviceUnavailable",
    "PresenceForgetting",
    "SQLiteDeviceRegistry",
]
