"""Phase 4 Agent 3 — Credential expiry/revocation/recovery + pairing hardening.

Proves the "never silently restore revoked authority" invariant end to end across
the credential/identity/pairing/device/presence stack. Each class pins one gap:

* expired identity -> resolve raises, credential authenticate raises, reconnect
  mints a *new* identity_id, old record stays revoked/expired.
* forgetting a device revokes credentials for that identity (not just hides the
  device) and the stale-heartbeat cascade never resurrects it.
* SQLiteIdentityStore.unbind persists across a reopen (else a restart silently
  re-binds a device to its dead identity and refuses the reporter's
  re-enrollment with "already enrolled" forever).
* screen_credential_payload strips token-shaped values, not just token-shaped
  keys, so a future payload field cannot leak a bearer token into audit.
* revoked/expired credentials cannot rotate, authorize, or release grants.
* the consent ceiling defends in depth: issue() caps at consent, grants_for()
  intersects, authorize() caps again; a handed-over stale object is ignored.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import (
    CredentialError,
    CredentialStore,
    screen_credential_payload,
)
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore, SQLiteIdentityStore
from actionmesh.core.pairing import PairingError, PairingService
from actionmesh.core.permissions.context import PermissionGrant
from actionmesh.protocol.models import DeviceKind, RiskLevel

KEY = b"phase4-recovery-key-material-01"
PAST = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def _stack() -> tuple[IdentityStore, CredentialStore, DeviceRegistry]:
    identities = IdentityStore()
    credentials = CredentialStore(identities, key=KEY)
    devices = DeviceRegistry(identities=identities, credentials=credentials)
    devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
    return identities, credentials, devices


def _pairing(
    identities: IdentityStore,
    credentials: CredentialStore,
    devices: DeviceRegistry,
    audit: list | None = None,
) -> PairingService:
    return PairingService(
        identities=identities,
        credentials=credentials,
        devices=devices,
        key=KEY,
        audit=((lambda e, s, p: audit.append((e, s, p))) if audit is not None else None),
    )


class TestExpiryRequiresNewIdentity(unittest.TestCase):
    def test_expired_identity_credentials_and_grants_all_fail_closed(self) -> None:
        identities, credentials, _ = _stack()
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, token = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", ttl_s=3600, now=PAST
        )
        live = identities.for_device("phone-1")
        assert live is not None
        live.expires_at = PAST + timedelta(seconds=60)
        later = PAST + timedelta(seconds=61)
        with self.assertRaises(IdentityError):
            identities.resolve(identity.identity_id, now=later)
        with self.assertRaises(IdentityError):
            credentials.authenticate(token, now=later)
        self.assertEqual(credentials.grants_for(record, now=later), [])
        auth = credentials.authorize(record, "calendar.read", now=later)
        self.assertFalse(auth.allowed)

    def test_reconnect_after_expiry_mints_new_identity_and_old_stays_dead(self) -> None:
        identities, credentials, devices = _stack()
        pairing = _pairing(identities, credentials, devices)
        _, code = pairing.issue("laptop-1", now=PAST)
        first, _, _ = pairing.redeem(code, phone_device_id="phone-1", actor_id="alice", now=PAST)
        old = first.identity_id
        ident = identities.for_device("phone-1")
        assert ident is not None
        ident.expires_at = PAST + timedelta(seconds=30)
        later = PAST + timedelta(seconds=31)
        _, code2 = pairing.issue("laptop-1", now=later)
        fresh, fresh_token, _, previous = pairing.reconnect(
            code2, phone_device_id="phone-1", actor_id="alice", now=later
        )
        self.assertEqual(previous, old)
        self.assertNotEqual(fresh.identity_id, old)
        with self.assertRaises(IdentityError):
            identities.resolve(old, now=later)
        bound = identities.for_device("phone-1")
        assert bound is not None
        self.assertEqual(bound.identity_id, fresh.identity_id)
        auth = credentials.authenticate(fresh_token, now=later)
        self.assertEqual(auth.identity_id, fresh.identity_id)

    def test_reconnect_after_revocation_mints_new_identity(self) -> None:
        identities, credentials, devices = _stack()
        pairing = _pairing(identities, credentials, devices)
        _, code = pairing.issue("laptop-1", now=PAST)
        first, _, _ = pairing.redeem(code, phone_device_id="phone-1", actor_id="alice", now=PAST)
        identities.revoke(first.identity_id, now=PAST)
        _, code2 = pairing.issue("laptop-1", now=PAST + timedelta(hours=1))
        later = PAST + timedelta(hours=1)
        fresh, _, _, previous = pairing.reconnect(
            code2, phone_device_id="phone-1", actor_id="alice", now=later
        )
        self.assertEqual(previous, first.identity_id)
        self.assertNotEqual(fresh.identity_id, first.identity_id)

    def test_reconnect_refuses_a_live_identity_and_never_paired(self) -> None:
        identities, credentials, devices = _stack()
        pairing = _pairing(identities, credentials, devices)
        with self.assertRaises(PairingError):
            pairing.reconnect("123456", phone_device_id="ghost", actor_id="alice", now=PAST)
        _, code = pairing.issue("laptop-1", now=PAST)
        pairing.redeem(code, phone_device_id="phone-1", actor_id="alice", now=PAST)
        _, code2 = pairing.issue("laptop-1", now=PAST)
        with self.assertRaises(PairingError):
            pairing.reconnect(code2, phone_device_id="phone-1", actor_id="alice", now=PAST)

    def test_expired_credential_cannot_rotate_or_authorize(self) -> None:
        identities, credentials, _ = _stack()
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, _ = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", ttl_s=60, now=PAST
        )
        later = PAST + timedelta(seconds=61)
        with self.assertRaises(CredentialError):
            credentials.rotate(record.credential_id, now=later)
        auth = credentials.authorize(record, "calendar.read", now=later)
        self.assertFalse(auth.allowed)

    def test_revoked_credential_cannot_rotate(self) -> None:
        identities, credentials, _ = _stack()
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, _ = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", now=PAST
        )
        credentials.revoke(record.credential_id, now=PAST)
        with self.assertRaises(CredentialError):
            credentials.rotate(record.credential_id, now=PAST)

    def test_forget_revokes_credentials_for_the_identity(self) -> None:
        """DeviceRegistry.forget must nullify live credentials, not just presence."""
        identities, credentials, devices = _stack()
        identity = identities.enroll("laptop-9", DeviceKind.LAPTOP)
        record, token = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", now=PAST
        )
        credentials.authenticate(token, now=PAST)
        devices.register(
            __import__("actionmesh.protocol.models", fromlist=["DeviceInfo"]).DeviceInfo(
                device_id="laptop-9",
                kind=DeviceKind.LAPTOP,
                identity_id=identity.identity_id,
                capabilities=["device.status"],
                online=True,
                last_seen=PAST,
            )
        )
        devices.heartbeat("laptop-9", now=PAST)
        self.assertTrue(devices.forget("laptop-9"))
        with self.assertRaises((IdentityError, CredentialError)):
            credentials.authenticate(token, now=PAST)
        # The credential record itself is revoked too, so any consumer reading the
        # store directly (not via authenticate) sees it as dead.
        stored = credentials.get(record.credential_id)
        assert stored is not None
        self.assertTrue(stored.revoked)


class TestAuditScreening(unittest.TestCase):
    def test_secret_shaped_values_are_dropped(self) -> None:
        screened = screen_credential_payload(
            {
                "credential_id": "cred_1",
                "token": "a-real-bearer-token-value",
                "session_note": "eyJhbGciOiJIUzI1NiJ9" + "x" * 40,
                "nested": {"token": "inner-secret"},
                "count": 3,
            }
        )
        self.assertEqual(screened["credential_id"], "cred_1")
        self.assertNotIn("token", screened)
        # Long opaque values that look like bearer tokens/keys are dropped even
        # under innocent keys.
        self.assertNotIn("session_note", screened)
        self.assertNotIn("nested", screened)
        self.assertEqual(screened["count"], 3)

    def test_audit_payload_after_revoke_carries_no_secret(self) -> None:
        identities = IdentityStore()
        events: list[tuple[str, str, dict[str, object]]] = []
        credentials = CredentialStore(
            identities, key=KEY, audit=lambda e, s, p: events.append((e, s, p))
        )
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, token = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", now=PAST
        )
        credentials.revoke(record.credential_id, now=PAST)
        dumped = repr(events)
        self.assertNotIn(token, dumped)
        self.assertNotIn(record.secret_hash, dumped)
        names = {e for e, _, _ in events}
        self.assertIn("credential.issued", names)
        self.assertIn("credential.revoked", names)


class TestSQLiteUnbindPersists(unittest.TestCase):
    def test_unbind_survives_reopen_and_reenrollment_works(self) -> None:
        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, "identities.db")
        first = SQLiteIdentityStore(path)
        identity = first.enroll("phone-1", DeviceKind.PHONE, now=PAST)
        first.revoke(identity.identity_id, now=PAST + timedelta(hours=1))
        self.assertTrue(first.unbind("phone-1"))
        self.assertIsNone(first.for_device("phone-1"))
        first.close()

        reopened = SQLiteIdentityStore(path)
        self.addCleanup(reopened.close)
        # The revoked record is still there and still refuses resolve...
        with self.assertRaises(IdentityError):
            reopened.resolve(identity.identity_id)
        # ...but the device binding did not come back with it: re-enrollment
        # under the same device_id mints a fresh identity.
        replacement = reopened.enroll("phone-1", DeviceKind.PHONE, now=PAST + timedelta(hours=2))
        self.assertNotEqual(replacement.identity_id, identity.identity_id)
        self.assertIs(reopened.for_device("phone-1"), replacement)
        self.assertIs(reopened.resolve(replacement.identity_id), replacement)


class TestConsentCeilingIsDefenceInDepth(unittest.TestCase):
    def test_issue_above_consent_fails_and_authorize_stays_capped(self) -> None:
        identities = IdentityStore()
        grants = [
            PermissionGrant(scope="calendar.read", granted_by="alice", max_risk=RiskLevel.LOW, granted_at=PAST),
        ]
        credentials = CredentialStore(
            identities,
            key=KEY,
            consents=lambda actor: [g for g in grants if g.granted_by == actor],
        )
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        with self.assertRaises(CredentialError):
            credentials.issue(
                identity_id=identity.identity_id, actor_id="alice", max_risk=RiskLevel.HIGH, now=PAST
            )
        low, _ = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", max_risk=RiskLevel.LOW, now=PAST
        )
        # calendar.read is LOW -> allowed within the capped ceiling.
        self.assertTrue(credentials.authorize(low, "calendar.read", now=PAST).allowed)
        # file.read is MEDIUM -> above the credential ceiling, refused even
        # though the consent grant itself is not scope-capped.
        denied = credentials.authorize(low, "file.read", now=PAST)
        self.assertFalse(denied.allowed)

    def test_stale_object_after_revoke_releases_no_grants(self) -> None:
        identities = IdentityStore()
        credentials = CredentialStore(
            identities,
            key=KEY,
            consents=lambda actor: [
                PermissionGrant(scope="calendar.read", granted_by=actor, max_risk=RiskLevel.HIGH, granted_at=PAST)
            ],
        )
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, _ = credentials.issue(
            identity_id=identity.identity_id, actor_id="alice", max_risk=RiskLevel.HIGH, now=PAST
        )
        credentials.revoke(record.credential_id, now=PAST)
        self.assertFalse(record.revoked)  # caller-held receipt is stale
        self.assertEqual(credentials.grants_for(record, now=PAST), [])
        self.assertFalse(credentials.authorize(record, "calendar.read", now=PAST).allowed)


if __name__ == "__main__":
    unittest.main()
