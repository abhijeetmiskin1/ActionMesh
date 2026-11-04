"""Credential lifecycle: expiry, revocation, scope, escalation, staleness, reconnect.

The invariants under test (ActionMesh security contract):

* Fail closed — expired/revoked/wrong-scope/stale credentials deny, never widen
  privilege, and a revoked identity never comes back.
* A bearer credential cannot self-grant CRITICAL, and a grant is not a CRITICAL
  approval: CRITICAL needs a per-``action_id`` approved ApprovalRequest.
* No secret ever reaches the audit sink or an exception message.

Everything runs against fakes (in-memory stores, fixed clock), so expiry and
revocation are testable without any live provider credential.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import (
    CredentialError,
    CredentialStore,
)
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.pairing import PairingError, PairingService
from actionmesh.core.permissions.context import Consent
from actionmesh.core.permissions.store import ConsentStore
from actionmesh.protocol.models import (
    ApprovalRequest,
    DeviceKind,
    RiskLevel,
)

KEY = b"credential-lifecycle-key-material-01"
NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)
ACTOR = "alice"


def _consents() -> ConsentStore:
    """One actor, consented up to HIGH for file.write and CRITICAL for shell."""
    store = ConsentStore()
    store.grant(
        Consent(
            scope="file.write",
            granted_by=ACTOR,
            reason="test",
            max_risk=RiskLevel.HIGH,
            granted_at=NOW,
        )
    )
    store.grant(
        Consent(
            scope="system.shell",
            granted_by=ACTOR,
            reason="test",
            max_risk=RiskLevel.CRITICAL,
            granted_at=NOW,
        )
    )
    return store


def _store(
    *,
    audit: list[tuple[str, str, dict[str, object]]] | None = None,
    consents: ConsentStore | None = None,
) -> tuple[CredentialStore, IdentityStore, object]:
    identities = IdentityStore()
    identities.enroll("phone-1", DeviceKind.PHONE, label="Phone", now=NOW)
    sink = audit
    store = CredentialStore(
        identities,
        key=KEY,
        consents=(consents or _consents()).grants_for,
        audit=(
            (lambda event, subject, payload: sink.append((event, subject, payload)))
            if sink is not None
            else None
        ),
    )
    return store, identities, sink


def _issue(
    store: CredentialStore,
    identities: IdentityStore,
    *,
    actor: str = ACTOR,
    max_risk: RiskLevel = RiskLevel.HIGH,
    ttl_s: float = 3600.0,
    now: datetime = NOW,
):
    identity = identities.for_device("phone-1")
    assert identity is not None
    return store.issue(
        identity_id=identity.identity_id,
        actor_id=actor,
        max_risk=max_risk,
        ttl_s=ttl_s,
        label="test",
        now=now,
    )


class TestExpiryAndRevocation(unittest.TestCase):
    def test_expired_credential_is_denied(self) -> None:
        store, identities, _ = _store()
        record, token = _issue(store, identities, ttl_s=60.0)
        # Still live inside the TTL.
        self.assertEqual(
            store.authenticate(token, now=NOW + timedelta(seconds=30)).credential_id,
            record.credential_id,
        )
        with self.assertRaises(CredentialError):
            store.authenticate(token, now=NOW + timedelta(seconds=61))
        self.assertEqual(
            store.authorize(record, "file.write", now=NOW + timedelta(seconds=61)).allowed,
            False,
        )

    def test_revoked_credential_dead_and_rotate_refuses_resurrection(self) -> None:
        store, identities, _ = _store()
        record, token = _issue(store, identities)
        store.revoke(record.credential_id, now=NOW)
        with self.assertRaises(CredentialError):
            store.authenticate(token, now=NOW)
        # Rotation must never silently restore revoked authority.
        with self.assertRaises(CredentialError):
            store.rotate(record.credential_id, now=NOW)

    def test_expired_credential_rotate_refuses_resurrection(self) -> None:
        store, identities, _ = _store()
        record, _token = _issue(store, identities, ttl_s=60.0)
        with self.assertRaises(CredentialError):
            store.rotate(record.credential_id, now=NOW + timedelta(seconds=61))

    def test_rotation_succeeds_and_old_token_dies(self) -> None:
        store, identities, _ = _store()
        record, token = _issue(store, identities)
        replacement, replacement_token = store.rotate(
            record.credential_id, now=NOW + timedelta(seconds=10)
        )
        self.assertNotEqual(replacement.credential_id, record.credential_id)
        with self.assertRaises(CredentialError):
            store.authenticate(token, now=NOW + timedelta(seconds=20))
        self.assertEqual(
            store.authenticate(replacement_token, now=NOW + timedelta(seconds=20)).credential_id,
            replacement.credential_id,
        )

    def test_revocation_propagates_through_identity_revoke(self) -> None:
        """authenticate() already fails via resolve(); the record must also die."""
        store, identities, _ = _store()
        record, token = _issue(store, identities)
        identity = identities.for_device("phone-1")
        assert identity is not None
        identities.revoke(identity.identity_id, now=NOW)
        with self.assertRaises(IdentityError):
            store.authenticate(token, now=NOW)
        self.assertEqual(store.revoke_for_identity(identity.identity_id, now=NOW), 1)
        revoked = store.get(record.credential_id)
        assert revoked is not None
        self.assertTrue(revoked.revoked)
        self.assertFalse(revoked.is_active(NOW))
        self.assertEqual(store.grants_for(record, now=NOW), [])


class TestScopeAndCeiling(unittest.TestCase):
    def test_wrong_scope_denied(self) -> None:
        store, identities, _ = _store()
        record, _token = _issue(store, identities)
        # file.write is consented; calendar.create is a different scope.
        self.assertTrue(store.authorize(record, "file.write", now=NOW).allowed)
        denied = store.authorize(record, "calendar.create", now=NOW)
        self.assertFalse(denied.allowed)
        self.assertFalse(denied.requires_approval)

    def test_credential_ceiling_caps_consent(self) -> None:
        store, identities, _ = _store()
        record, _token = _issue(store, identities, max_risk=RiskLevel.LOW)
        # Consent allows HIGH, but this credential's ceiling is LOW.
        denied = store.authorize(record, "file.write", now=NOW)
        self.assertFalse(denied.allowed)

    def test_issue_above_consent_ceiling_refused(self) -> None:
        identities = IdentityStore()
        identities.enroll("phone-1", DeviceKind.PHONE, now=NOW)
        consents = ConsentStore()
        consents.grant(
            Consent(
                scope="file.write",
                granted_by=ACTOR,
                reason="test",
                max_risk=RiskLevel.LOW,
                granted_at=NOW,
            )
        )
        store = CredentialStore(identities, key=KEY, consents=consents.grants_for)
        identity = identities.for_device("phone-1")
        assert identity is not None
        with self.assertRaises(CredentialError):
            store.issue(
                identity_id=identity.identity_id,
                actor_id=ACTOR,
                max_risk=RiskLevel.HIGH,
                ttl_s=3600.0,
                now=NOW,
            )

    def test_rotate_cannot_raise_ceiling(self) -> None:
        store, identities, _ = _store()
        record, _token = _issue(store, identities, max_risk=RiskLevel.LOW)
        with self.assertRaises(CredentialError):
            store.rotate(
                record.credential_id, max_risk=RiskLevel.CRITICAL, now=NOW
            )

    def test_rotate_requires_proof_of_possession_when_token_given(self) -> None:
        store, identities, _ = _store()
        record, token = _issue(store, identities)
        other, _other_token = _issue(store, identities)
        with self.assertRaises(CredentialError):
            store.rotate(record.credential_id, require_token=_other_token, now=NOW)
        replacement, _ = store.rotate(
            record.credential_id, require_token=token, now=NOW
        )
        self.assertNotEqual(replacement.credential_id, record.credential_id)
        self.assertNotEqual(other.credential_id, replacement.credential_id)

    def test_device_and_actor_binding_enforced(self) -> None:
        store, identities, _ = _store()
        _record, token = _issue(store, identities)
        with self.assertRaises(CredentialError):
            store.authenticate(token, expected_device_id="phone-2", now=NOW)
        with self.assertRaises(CredentialError):
            store.authenticate(token, expected_actor_id="bob", now=NOW)
        self.assertEqual(
            store.authenticate(
                token, expected_device_id="phone-1", expected_actor_id=ACTOR, now=NOW
            ).actor_id,
            ACTOR,
        )

    def test_stale_credential_denied(self) -> None:
        store, identities, _ = _store()
        _record, token = _issue(store, identities, ttl_s=30 * 24 * 3600.0)
        old = NOW + timedelta(days=10)
        with self.assertRaises(CredentialError):
            store.authenticate(token, max_age_s=7 * 24 * 3600.0, now=old)
        # Freshness proves itself when fresh.
        self.assertEqual(
            store.authenticate(token, max_age_s=7 * 24 * 3600.0, now=NOW).actor_id, ACTOR
        )


class TestGrantIsNotApproval(unittest.TestCase):
    def _critical_credential(self) -> tuple[CredentialStore, object]:
        store, identities, _ = _store()
        record, _token = _issue(store, identities, max_risk=RiskLevel.CRITICAL)
        return store, record

    def test_critical_grant_alone_denied(self) -> None:
        store, record = self._critical_credential()
        verdict = store.authorize(record, "system.shell", now=NOW)
        self.assertFalse(verdict.allowed)
        self.assertTrue(verdict.requires_approval)

    def test_critical_needs_per_action_approved_request(self) -> None:
        store, record = self._critical_credential()
        approval = ApprovalRequest(
            action_id="act-1",
            action_type="system.shell",
            summary="test",
            risk_level=RiskLevel.CRITICAL,
            status="approved",
            created_at=NOW,
        )
        ok = store.authorize(
            record, "system.shell", action_id="act-1", approvals=[approval], now=NOW
        )
        self.assertTrue(ok.allowed)
        # An approval for a different action_id is not this action's approval.
        wrong = store.authorize(
            record, "system.shell", action_id="act-2", approvals=[approval], now=NOW
        )
        self.assertFalse(wrong.allowed)
        self.assertTrue(wrong.requires_approval)
        # A pending ApprovalRequest is not an approval.
        pending = ApprovalRequest(
            action_id="act-1",
            action_type="system.shell",
            summary="test",
            risk_level=RiskLevel.CRITICAL,
            status="pending",
            created_at=NOW,
        )
        denied = store.authorize(
            record, "system.shell", action_id="act-1", approvals=[pending], now=NOW
        )
        self.assertFalse(denied.allowed)

    def test_bearer_cannot_self_grant_critical(self) -> None:
        """No credential, however minted, authorizes CRITICAL on its own."""
        store, identities, _ = _store()
        record, _token = _issue(store, identities, max_risk=RiskLevel.HIGH)
        self.assertFalse(store.authorize(record, "system.shell", now=NOW).allowed)


class TestAuditSafety(unittest.TestCase):
    def test_audit_payloads_never_carry_tokens(self) -> None:
        audit: list[tuple[str, str, dict[str, object]]] = []
        store, identities, _ = _store(audit=audit)
        record, token = _issue(store, identities)
        store.rotate(record.credential_id, now=NOW + timedelta(seconds=10))
        store.revoke(record.credential_id, now=NOW)
        with self.assertRaises(CredentialError):
            store.authenticate("bogus-token", now=NOW)
        self.assertTrue(audit)
        for event, subject, payload in audit:
            self.assertNotIn(token, repr(payload))
            self.assertNotIn(token, subject)
            for key in payload:
                self.assertNotIn(key.lower(), {"token", "plaintext", "secret", "secret_hash"})
            self.assertNotIn("token", event.lower())
        events = {event for event, _, _ in audit}
        self.assertIn("credential.issued", events)
        self.assertIn("credential.rotated", events)
        self.assertIn("credential.auth_failed", events)

    def test_expiring_soon_is_query_only(self) -> None:
        store, identities, _ = _store()
        record, _token = _issue(store, identities, ttl_s=3600.0)
        soon = store.expiring_soon(7200.0, now=NOW)
        self.assertEqual([r.credential_id for r in soon], [record.credential_id])
        # Nothing was revoked or extended by asking.
        live = store.get(record.credential_id)
        assert live is not None
        self.assertTrue(live.is_active(NOW))
        self.assertEqual(store.expiring_soon(100.0, now=NOW), [])

    def test_identity_expiry_fails_closed(self) -> None:
        identities = IdentityStore()
        identity = identities.enroll(
            "phone-1", DeviceKind.PHONE, now=NOW,
            expires_at=NOW + timedelta(hours=1),
        )
        store = CredentialStore(identities, key=KEY)
        record, token = store.issue(
            identity_id=identity.identity_id,
            actor_id=ACTOR,
            max_risk=RiskLevel.LOW,
            ttl_s=7200.0,
            now=NOW,
        )
        # Both the credential and the identity expire within hours; at `later`
        # the credential is expired, and the identity would also fail resolve
        # on its own. (Order matters: authenticate hits the credential first.)
        later = NOW + timedelta(hours=2)
        self.assertTrue(record.is_expired(later))
        self.assertTrue(identity.is_expired(later))
        with self.assertRaises(CredentialError):
            store.authenticate(token, now=later)
        self.assertEqual(store.grants_for(record, now=later), [])
        with self.assertRaises(IdentityError):
            identities.resolve(identity.identity_id, now=later)


class TestReconnect(unittest.TestCase):
    def _pairing(self, audit: list | None = None) -> tuple[PairingService, IdentityStore]:
        identities = IdentityStore()
        devices = DeviceRegistry(identities=identities)
        devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
        credentials = CredentialStore(identities, key=KEY)
        pairing = PairingService(
            identities=identities,
            credentials=credentials,
            devices=devices,
            key=KEY,
            audit=(
                (lambda event, subject, payload: audit.append((event, subject, payload)))
                if audit is not None
                else None
            ),
        )
        return pairing, identities

    def test_reconnect_after_revocation_mints_new_identity(self) -> None:
        audit: list = []
        pairing, identities = self._pairing(audit)
        _record, code = pairing.issue("laptop-1", now=NOW)
        credential, _token, _device = pairing.redeem(
            code, phone_device_id="phone-1", actor_id=ACTOR, now=NOW
        )
        old_identity = credential.identity_id
        identities.revoke(old_identity, now=NOW)
        _record2, code2 = pairing.issue("laptop-1", now=NOW + timedelta(seconds=1))
        fresh, fresh_token, _device2, previous = pairing.reconnect(
            code2,
            phone_device_id="phone-1",
            actor_id=ACTOR,
            now=NOW + timedelta(seconds=1),
        )
        self.assertEqual(previous, old_identity)
        self.assertNotEqual(fresh.identity_id, old_identity)
        self.assertTrue(fresh.is_active(NOW))
        # The reconnected credential authenticates; the revoked identity stays dead.
        self.assertEqual(
            pairing.credentials.authenticate(fresh_token, now=NOW).credential_id,
            fresh.credential_id,
        )
        with self.assertRaises(IdentityError):
            identities.resolve(old_identity)
        events = {event for event, _, _ in audit}
        self.assertIn("pairing.reconnect_completed", events)

    def test_reconnect_refused_for_live_identity(self) -> None:
        pairing, _identities = self._pairing()
        _record, code = pairing.issue("laptop-1", now=NOW)
        pairing.redeem(code, phone_device_id="phone-1", actor_id=ACTOR, now=NOW)
        _record2, code2 = pairing.issue("laptop-1", now=NOW + timedelta(seconds=1))
        with self.assertRaises(PairingError):
            pairing.reconnect(
                code2,
                phone_device_id="phone-1",
                actor_id=ACTOR,
                now=NOW + timedelta(seconds=1),
            )


if __name__ == "__main__":
    unittest.main()
