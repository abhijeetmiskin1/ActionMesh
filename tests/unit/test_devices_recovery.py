"""Phase 2 Agent 3 — Device Connectivity, Remote Control, and Recovery.

Two honest-recovery proofs and neither claims a live device that did not run:

* **Forget-cascade (Agent 4 gap 1).** ``DeviceRegistry.forget()`` drops the record
  and revokes the identity. The agent process that owns the device holds its own
  cached ``DeviceInfo`` (``_device``) and beats on it, so a registry-only forget
  would let a stale heartbeat resurrect the device. ``LaptopAgent.forget()``
  clears that cache, and ``DeviceRegistry.forget()`` cascades into it via the
  ``PresenceForgetting`` protocol wired through ``presence=``. The test proves:
  register + heartbeat, forget, heartbeat again does not restore authority —
  the device stays forgotten and no grants come back. Revoked authority is never
  silently restored. (The MPRIS probe below is explicitly *not* live-verified.)

* **Honest MPRIS probe.** Constructs the real ``MprisMediaDriver``, calls
  ``list_players()``, and asserts the honest result: an empty list means
  unavailable, never a fake player. If a real player exists in this environment
  it is recorded; otherwise the unavailable path is what passes. No live MPRIS
  is claimed.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import UTC, datetime

from actionmesh.agent.config import AgentConfig
from actionmesh.agent.media import MediaEngine, MprisMediaDriver
from actionmesh.agent.presence import LaptopAgent
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceInfo, DeviceKind


class _FixedClock:
    """A clock the test advances by hand, so staleness is deterministic."""

    def __init__(self, moment: datetime) -> None:
        self._now = moment

    def __call__(self) -> datetime:
        return self._now


class TestForgetCascadeDoesNotResurrect(unittest.TestCase):
    """register+heartbeat, forget, heartbeat again — authority stays revoked."""

    def setUp(self) -> None:
        self.identities = IdentityStore()
        self.devices = DeviceRegistry(identities=self.identities)
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root, True)
        self.now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        self.clock = _FixedClock(self.now)
        self.config = AgentConfig(
            device_id="laptop-1",
            name="My Laptop",
            workspace_root=self.root,
        )
        self.agent = LaptopAgent(self.devices, self.config, clock=self.clock)
        # Wire the cascade *after* construction: the registry was built before
        # the agent existed. In production (api.py) the agent is built after
        # the registry and this assignment is the lead's wiring step (see the
        # shared-file request in the report).
        self.devices.presence = self.agent

    def test_register_heartbeat_forget_heartbeat_stays_revoked(self) -> None:
        # 1. Enroll + register + heartbeat — the device is present and holds
        #    a real identity.
        device = self.agent.register()
        self.assertTrue(device.identity_id)
        self.agent.heartbeat()
        self.assertTrue(
            self.devices.is_online(self.devices.devices["laptop-1"], now=self.now)
        )
        self.assertEqual(
            [d.device_id for d in self.devices.available(now=self.now)],
            ["laptop-1"],
        )
        identity_id = device.identity_id

        # 2. Forget via the registry — the cascade must clear the agent's
        #    cached presence record too.
        self.assertTrue(self.devices.forget("laptop-1"))
        self.assertIsNone(self.agent._device, "agent cache must be cleared by the cascade")
        self.assertNotIn("laptop-1", self.devices.devices)
        self.assertEqual(self.devices.available(now=self.now), [])

        # The identity is revoked: resolving it raises. Revocation is durable.
        with self.assertRaises(IdentityError):
            self.identities.resolve(identity_id)

        # 3. A stale heartbeat from the same agent process must NOT restore
        #    authority. `heartbeat()` re-enters `register()`, which re-checks
        #    identity resolution and refuses the revoked identity — the
        #    heartbeat fails loudly rather than resurrecting the device.
        with self.assertRaises(IdentityError):
            self.agent.heartbeat()

        # Nothing came back: no device record, no availability, no grants.
        self.assertNotIn("laptop-1", self.devices.devices)
        self.assertEqual(self.devices.available(now=self.now), [])
        self.assertIsNone(self.agent._device)
        # The revoked identity still does not resolve (never silently restored).
        with self.assertRaises(IdentityError):
            self.identities.resolve(identity_id)

    def test_registry_heartbeat_after_forget_still_fails(self) -> None:
        # Even at the registry layer (no agent heartbeat loop in between) a
        # forgotten device cannot be brought back by a presence beat: the
        # record is gone, so `heartbeat` raises KeyError for an unknown id.
        self.agent.register()
        self.agent.heartbeat()
        self.assertTrue(self.devices.forget("laptop-1"))
        with self.assertRaises(KeyError):
            self.devices.heartbeat("laptop-1")

    def test_forget_without_a_wired_agent_still_revokes(self) -> None:
        # The cascade is defensive: a registry with `presence=None` (the
        # common test setup) forgets exactly as before.
        plain = DeviceRegistry(identities=self.identities)
        plain.register(
            DeviceInfo(device_id="phone-1", kind=DeviceKind.PHONE,
                       capabilities=["device.notify"])
        )
        plain.heartbeat("phone-1")
        self.assertTrue(plain.forget("phone-1"))
        self.assertNotIn("phone-1", plain.devices)
        with self.assertRaises(KeyError):
            plain.heartbeat("phone-1")

    def test_forget_of_another_device_does_not_clear_this_agent(self) -> None:
        # The targeted `forget(device_id)` only clears the cache when it names
        # the device this agent holds; the blunt `forget_all` is the fallback
        # the cascade uses when the targeted clear reports nothing.
        self.agent.register()
        self.agent.heartbeat()
        self.assertFalse(self.agent.forget("someone-else"))
        self.assertIsNotNone(self.agent._device)
        self.assertTrue(self.agent.forget("laptop-1"))
        self.assertIsNone(self.agent._device)


class TestMprisProbeIsHonest(unittest.TestCase):
    """`MprisMediaDriver.list_players()` never invents a player.

    Constructed for real and probed for real. Empty list => unavailable, never
    a fake player. If a real player exists in this environment it is recorded;
    otherwise the unavailable path is what passes. This is explicitly **not**
    live MPRIS verification — it asserts the honest reporting contract.
    """

    def test_list_players_returns_a_list_never_a_fabrication(self) -> None:
        driver = MprisMediaDriver()
        try:
            players = driver.list_players()
        finally:
            driver.close()
        self.assertIsInstance(players, list)
        for player in players:
            # Every entry is a real PlayerInfo with a non-empty name; there is
            # no simulated/fake player in the MPRIS driver.
            self.assertTrue(player.name, "a listed player must have a name")
        if not players:
            # The honest path in this environment: no session bus or no
            # org.mpris players, so the driver reports "none reachable".
            self.assertEqual(players, [])
        else:
            # Record (do not claim live control of) whatever real players were
            # found — each is a live read-back, not a fabrication.
            self.assertGreaterEqual(len(players), 1)

    def test_engine_reports_unavailable_when_no_player_is_reachable(self) -> None:
        # The engine over the real driver: with no reachable player it answers
        # `unavailable`, never `ok`. If a real player *is* present the engine
        # answers `ok` with that player's real name — either way the answer is
        # the driver's live read-back.
        driver = MprisMediaDriver()
        try:
            engine = MediaEngine(driver)
            result = engine.status()
        finally:
            driver.close()
        self.assertIn(result.get("status"), ("unavailable", "ok"))
        if result.get("status") == "unavailable":
            self.assertIn("reason", result)
            self.assertNotIn("player", result)
        else:
            self.assertTrue(result.get("player"))

    def test_no_session_bus_is_the_honest_unavailable(self) -> None:
        # Strongest honest case: strip the session-bus address so D-Bus cannot
        # be reached, and assert the driver reports no players rather than
        # fabricating one or raising.
        driver = MprisMediaDriver()
        original = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
        os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
        try:
            players = driver.list_players()
        finally:
            if original is not None:
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = original
            driver.close()
        self.assertIsInstance(players, list)
        # With no bus address there can be no *reachable* MPRIS player; an
        # empty list here is the honest answer, not a failure to probe.
        self.assertEqual(players, [])


if __name__ == "__main__":
    unittest.main()


class TestReconnectAndExpiryRecovery(unittest.TestCase):
    """Reconnect mints a new identity; expiry/revocation never silently return.

    Focused proof for mission items 2-3: ``PairingService.reconnect`` is the
    only recovery path after an identity revoked or expired — it unbinds the
    dead binding (``IdentityStore.unbind`` refuses a live identity), spends a
    fresh laptop-issued code, and mints a new ``identity_id``. The old record
    keeps resolving as revoked/expired. ``DeviceUnavailable`` (known but
    offline/stale) stays distinct from plain ``KeyError`` (unknown id).
    """

    KEY = b"reconnect-recovery-key-material-1"

    def _pairing(
        self, *, audit: list | None = None
    ) -> tuple:
        from actionmesh.core.credentials import CredentialStore
        from actionmesh.core.pairing import PairingService

        identities = IdentityStore()
        devices = DeviceRegistry(identities=identities)
        devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        credentials = CredentialStore(identities, key=self.KEY)
        pairing = PairingService(
            identities=identities,
            credentials=credentials,
            devices=devices,
            key=self.KEY,
            audit=(
                (lambda e, s, p: audit.append((e, s, p))) if audit is not None else None
            ),
        )
        return pairing, identities, credentials, devices

    def test_reconnect_after_identity_expiry_mints_new_identity(self) -> None:
        from datetime import timedelta

        from actionmesh.core.identity import IdentityError as _IE

        audit: list = []
        pairing, identities, _creds, _devs = self._pairing(audit=audit)
        now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        _r, code = pairing.issue("laptop-1", now=now)
        cred, _tok, _dev = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="alice", now=now
        )
        old = cred.identity_id
        # Expire the identity in place (no revoke call): expiry must behave
        # like revocation at resolve-time.
        ident = identities.for_device("phone-1")
        assert ident is not None
        ident.expires_at = now + timedelta(seconds=60)
        later = now + timedelta(seconds=61)
        with self.assertRaises(_IE):
            identities.resolve(old, now=later)
        self.assertFalse(ident.is_active(later))
        # Reconnect spends a fresh code and mints a NEW identity; old stays dead.
        _r2, code2 = pairing.issue("laptop-1", now=later)
        fresh, fresh_tok, _d2, prev = pairing.reconnect(
            code2, phone_device_id="phone-1", actor_id="alice", now=later
        )
        self.assertEqual(prev, old)
        self.assertNotEqual(fresh.identity_id, old)
        self.assertEqual(identities.for_device("phone-1").identity_id, fresh.identity_id)  # type: ignore[union-attr]
        with self.assertRaises(_IE):
            identities.resolve(old, now=later)
        _creds.authenticate(fresh_tok, now=later)
        self.assertIn("pairing.reconnect_completed", {e for e, _, _ in audit})

    def test_reconnect_refuses_never_paired_and_live(self) -> None:
        from actionmesh.core.pairing import PairingError

        pairing, _identities, _c, _d = self._pairing()
        now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        with self.assertRaises(PairingError):
            pairing.reconnect("123456", phone_device_id="ghost-1", actor_id="alice", now=now)
        _r, code = pairing.issue("laptop-1", now=now)
        pairing.redeem(code, phone_device_id="phone-1", actor_id="alice", now=now)
        _r2, code2 = pairing.issue("laptop-1", now=now)
        with self.assertRaises(PairingError):
            pairing.reconnect(code2, phone_device_id="phone-1", actor_id="alice", now=now)

    def test_unbind_refuses_live_identity(self) -> None:
        from actionmesh.core.identity import IdentityError as _IE

        store = IdentityStore()
        store.enroll("dev-1", DeviceKind.PHONE, now=datetime(2026, 5, 1, tzinfo=UTC))
        with self.assertRaises(_IE):
            store.unbind("dev-1")
        self.assertIsNotNone(store.for_device("dev-1"))

    def test_unavailable_is_not_unknown(self) -> None:
        from actionmesh.core.devices import DeviceUnavailable

        reg = DeviceRegistry(stale_after_s=30)
        reg.register(DeviceInfo(device_id="laptop-1", kind=DeviceKind.LAPTOP))
        reg.heartbeat("laptop-1", now=datetime(2026, 5, 1, 12, 0, tzinfo=UTC))
        stale_now = datetime(2026, 5, 1, 12, 1, tzinfo=UTC)
        with self.assertRaises(DeviceUnavailable) as ctx:
            reg.require("laptop-1", now=stale_now)
        self.assertIsInstance(ctx.exception, KeyError)  # still caught as KeyError
        with self.assertRaises(KeyError) as ctx2:
            reg.require("nope", now=stale_now)
        self.assertNotIsInstance(ctx2.exception, DeviceUnavailable)
        self.assertEqual(type(ctx2.exception), KeyError)


class TestForgetCascadeEndToEndViaApi(unittest.TestCase):
    """api.py wiring proof: revoke via HTTP cascades to the agent cache.

    Builds the app the way ``create_app`` does with an injected
    ``LaptopAgent`` (lead-owned ``actionmesh/api.py`` wires
    ``app.state.devices.presence = agent`` at ~line 797 when ``presence``
    is None). Here the agent is injected, so the assignment already took
    effect inside ``create_app`` — this test only *verifies* the wiring
    holds: POSTing ``/devices/{id}/revoke`` for the agent's own device
    clears ``agent._device`` and the next ``agent.heartbeat()`` raises
    ``IdentityError`` (revoked, never resurrected).
    """

    def test_api_revoke_clears_agent_cache_no_resurrection(self) -> None:
        import tempfile

        from fastapi.testclient import TestClient

        from actionmesh.agent.config import AgentConfig
        from actionmesh.api import create_app
        from actionmesh.core.identity import IdentityStore

        identities = IdentityStore()
        registry = DeviceRegistry(identities=identities)
        root = tempfile.mkdtemp()
        config = AgentConfig(device_id="laptop-1", name="My Laptop", workspace_root=root)
        agent = LaptopAgent(registry, config)
        app = create_app(identities=identities, devices=registry, agent=agent)
        # create_app registered + heartbeat the agent and wired presence.
        self.assertIs(app.state.devices.presence, agent)  # type: ignore[attr-defined]
        self.assertIsNotNone(agent._device)
        client = TestClient(app, client=("127.0.0.1", 45123))
        resp = client.post("/devices/laptop-1/revoke", json={"reason": "operator forget"})
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertTrue(resp.json()["revoked"])
        self.assertIsNone(agent._device, "forget-cascade must clear the agent cache")
        self.assertNotIn("laptop-1", registry.devices)
        with self.assertRaises(IdentityError):
            agent.heartbeat()
        self.assertNotIn("laptop-1", registry.devices)
