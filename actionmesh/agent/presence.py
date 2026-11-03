"""C19 the laptop agent: presence, capabilities, and nothing else.

The agent is the component the brief calls ``LaptopAgent`` — the thing that makes
this machine known to ActionMesh and keeps saying "still here". It is deliberately
small, and its smallness is the architecture: **the agent executes authorized
actions; it does not decide which ones are authorized.** Everything that decides —
registry, policy, approval, graph, verification, audit — lives above it in C2-C6.

Two properties are worth stating because they are easy to lose later:

* **The agent registers itself, locally.** There is no route that reaches
  :meth:`register`. Admission of a *remote* controller is pairing's job, and a
  component that can both enroll devices and execute on them has stopped being an
  adapter.
* **Presence is a claim that expires.** ``device.connected`` is published on the
  transition into being seen, not on every heartbeat — otherwise the SSE stream a
  phone subscribes to becomes a 15-second metronome, and a real status change hides
  inside the noise. ``DeviceRegistry`` decides liveness from the timestamp, so a
  silent agent is an offline one without anyone having to notice.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from datetime import UTC, datetime

from actionmesh.agent.config import AgentConfig
from actionmesh.agent.definitions import advertised_ceiling, register_device_actions
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.events import EventHub
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceInfo, DeviceKind, MeshEvent


def _utcnow() -> datetime:
    return datetime.now(UTC)


class LaptopAgent:
    """Owns this machine's presence record and keeps it fresh."""

    def __init__(
        self,
        devices: DeviceRegistry,
        config: AgentConfig,
        *,
        events: EventHub | None = None,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.devices = devices
        self.config = config
        self.events = events
        self._clock = clock
        self._device: DeviceInfo | None = None

    # -- action registry --------------------------------------------------
    @staticmethod
    def install_actions(registry: object) -> int:
        """Register the device capabilities into an action registry.

        Returns how many definitions were new. Called by whoever assembles the
        engine, because the agent must not hold a private copy of the registry — a
        second registry would be a second answer to "what action is this".
        """
        return register_device_actions(registry)

    # -- presence ---------------------------------------------------------
    def register(self) -> DeviceInfo:
        """Enroll this machine, or re-adopt the identity it already held.

        A restart must not mint a fresh principal every time: the audit trail, and
        every credential that names this device, point at the identity that already
        exists. So the identity is looked up first and reused when it is there, and
        minted only when it is genuinely absent.
        """
        now = self._clock()
        device_id = self.config.device_id
        # Advertise once and use the same list for both the record and the ceiling
        # derived from it: a ceiling computed from a *different* advertisement than
        # the one published would describe a machine that does not exist.
        device_capabilities = list(self.config.advertise())
        identities: IdentityStore | None = self.devices.identities
        existing = self.devices.devices.get(device_id)
        # A restored identity record cannot be resolved while it is revoked: that is
        # exactly what revoking means. So catch "not active" here and fall through to
        # enrollment, which refuses a duplicate device id with a clear error, rather
        # than letting a startup `resolve` raise an exception nobody maps to an action.
        if existing is not None and existing.identity_id:
            identity_id = existing.identity_id
        elif identities is not None:
            held = identities.for_device(device_id)
            identity_id = ""
            if held is not None:
                try:
                    identity_id = identities.resolve(held.identity_id).identity_id
                except IdentityError:
                    # Revoked while this machine was down. Re-minting here would undo
                    # the revocation; `enroll` below fails loudly instead, and the
                    # operator un-revokes deliberately.
                    identity_id = ""
        else:
            identity_id = ""

        device = DeviceInfo(
            device_id=device_id,
            name=self.config.name or device_id,
            kind=DeviceKind.LAPTOP,
            platform=self.config.platform,
            os_version=self.config.os_version,
            agent_version=self.config.agent_version,
            capabilities=device_capabilities,
            identity_id=identity_id,
            arch=self.config.arch,
            app_version=self.config.app_version,
            # The ceiling this machine enrols with, derived from the boundaries the
            # operator configured for *this* process — never wider than what it
            # advertises. `DeviceInfo` would otherwise take the fail-closed ``LOW``
            # default, and a laptop honestly advertising a MEDIUM-risk capability
            # would then refuse its own actions after every approval had been given.
            # Lowering is free; raising is something only the device's own owner can
            # do, which is exactly what re-enrolling from configuration is.
            risk_ceiling=advertised_ceiling(device_capabilities),
        )
        if not identity_id and identities is not None:
            # Fresh machine: mint the identity through the store rather than
            # assembling one here, so a revoked identity cannot be re-adopted by
            # restarting.
            self.devices.enroll(
                device_id=device_id,
                kind=DeviceKind.LAPTOP,
                name=device.name,
                platform=device.platform,
                os_version=device.os_version,
                agent_version=device.agent_version,
                capabilities=device.capabilities,
                risk_ceiling=device.risk_ceiling,
            )
            self._device = self.devices.devices[device_id]
            return self._device
        self._device = self.devices.register(device)
        self._device.last_seen = now
        # Rebuilding this machine's own record at startup *is* a connection: the
        # agent that owns the device is demonstrably alive. Saying so is what keeps
        # "last connected" meaningful across a restart instead of silently reverting
        # to the last heartbeat before it.
        self.devices.mark_connected(device_id, now=now)
        return self._device

    def heartbeat(self) -> DeviceInfo:
        """Report this machine present. Publishes ``device.connected`` on recovery.

        The transition matters more than the beat. A phone watching the event stream
        needs to learn that the laptop came back; it does not need the same fact
        every fifteen seconds, and a stream full of metronomes is how a real change
        gets missed.
        """
        now = self._clock()
        device = self._device if self._device is not None else self.register()
        was_online = self.devices.is_online(device, now=now)
        self.devices.heartbeat(device.device_id, now=now)
        if not was_online:
            self._publish("device.connected", device)
        return device

    def disconnect(self) -> None:
        """Announce the agent is going away, then go offline.

        Explicit, because the alternative is a phone watching a card that says
        "online" for forty-five seconds after the process exited, and then silently
        losing it.
        """
        device = self._device
        if device is None:
            return
        self.devices.heartbeat(device.device_id, online=False, now=self._clock())
        self._publish("device.disconnected", device)

    def status(self) -> dict[str, object]:
        """The device card the dashboard draws: §1 metadata plus live presence."""
        device = self._device
        if device is None:
            return {"registered": False, **self.config.public()}
        online = self.devices.is_online(device, now=self._clock())
        return {
            "registered": True,
            "device_id": device.device_id,
            "name": device.name,
            "kind": device.kind.value,
            "platform": device.platform,
            "os_version": device.os_version,
            "agent_version": device.agent_version,
            "capabilities": list(device.capabilities),
            "online": online,
            "last_seen": device.last_seen.isoformat() if device.last_seen else None,
            "enrolled": bool(device.identity_id),
            "resources": resources(),
        }

    # -- heartbeat loop ---------------------------------------------------
    async def run(self, stop: asyncio.Event | None = None) -> None:
        """Heartbeat until told to stop, then announce the departure.

        ``finally`` matters: a task cancelled mid-flight that never publishes
        ``device.disconnected`` leaves a dashboard showing a laptop that is not there.
        """
        self.register()
        try:
            while stop is None or not stop.is_set():
                self.heartbeat()
                delay = self.config.heartbeat_s
                if stop is None:
                    await asyncio.sleep(delay)
                else:
                    try:
                        async with asyncio.timeout(delay):
                            await stop.wait()
                    except TimeoutError:
                        continue
                    return
        finally:
            self.disconnect()

    # -- internals --------------------------------------------------------
    def _publish(self, event_type: str, device: DeviceInfo) -> None:
        if self.events is None:
            return
        self.events.publish(
            MeshEvent(
                event_type=event_type,
                actor=device.device_id,
                payload={
                    "device_id": device.device_id,
                    "kind": device.kind.value,
                    "name": device.name,
                    "capabilities": list(device.capabilities),
                    "online": self.devices.is_online(device, now=self._clock()),
                },
            )
        )


def resources() -> dict[str, float]:
    """Coarse machine load for the device card.

    Deliberately modest, and deliberately not a capability: this is the laptop
    describing *itself* to the dashboard, which is why it reads ``/proc/meminfo``
    directly instead of accepting any path. A client-supplied path never reaches
    here. Anything unavailable on the platform is omitted rather than guessed —
    reporting a plausible number is worse than reporting none.
    """
    out: dict[str, float] = {}
    cpus = os.cpu_count()
    if cpus:
        out["cpu_count"] = float(cpus)
    try:
        one, five, fifteen = os.getloadavg()
        out.update({"load_1m": one, "load_5m": five, "load_15m": fifteen})
    except (OSError, AttributeError):
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    out["memory_total_mb"] = float(line.split()[1]) / 1024.0
                elif line.startswith("MemAvailable:"):
                    out["memory_available_mb"] = float(line.split()[1]) / 1024.0
                    break
    except (OSError, ValueError, IndexError):
        pass
    return out


__all__ = ["LaptopAgent", "resources"]
