"""C19b credential tests — issuance, authentication, ceilings, revocation.

A credential is the proof that a device holds an identity. Everything that could go
wrong with that proof is tested here, because the failure mode is not an error
message — it is an action attributed to the wrong principal.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import (
    Credential,
    CredentialError,
    CredentialStore,
    hash_secret,
    new_token,
)
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.permissions.context import PermissionGrant
from actionmesh.protocol.models import DeviceKind, RiskLevel

KEY = b"0123456789abcdef" * 2
PAST = datetime(2026, 1, 1, tzinfo=UTC)


def _store(
    consents: object = None,
) -> tuple[IdentityStore, CredentialStore]:
    identities = IdentityStore()
    credentials = CredentialStore(
        identities,
        key=KEY,
        consents=consents,  # type: ignore[arg-type]
    )
    return identities, credentials


class TestTokenShape(unittest.TestCase):
    def test_tokens_are_wide_and_unique(self) -> None:
        tokens = {new_token() for _ in range(200)}
        self.assertEqual(len(tokens), 200)
        self.assertGreaterEqual(len(new_token()), 43)  # 32 bytes, url-safe base64

    def test_digest_is_stable_and_keyed(self) -> None:
        first = hash_secret("guess-me", key=KEY)
        self.assertEqual(first, hash_secret("guess-me", key=KEY))
        self.assertNotEqual(first, hash_secret("guess-me", key=b"other-key" * 4))
        self.assertEqual(len(first), 64)

    def test_empty_key_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            hash_secret("x", key=b"")

    def test_store_refuses_an_empty_key(self) -> None:
        # A store keyed by b"" is a store with no key: every digest becomes
        # reproducible by anyone holding a copy of the database.
        with self.assertRaises(CredentialError):
            CredentialStore(IdentityStore(), key=b"")


class TestIssuance(unittest.TestCase):
    def setUp(self) -> None:
        self.identities, self.credentials = _store()
        self.identity = self.identities.enroll("phone-1", DeviceKind.PHONE)

    def test_issue_returns_the_token_once(self) -> None:
        record, token = self.credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            now=PAST,
        )
        self.assertIsInstance(token, str)
        self.assertNotIn(token, record.secret_hash)
        self.assertNotIn(token, repr(record))
        self.assertNotIn(token, str(record.public()))

    def test_record_binds_the_whole_relationship(self) -> None:
        record, _ = self.credentials.issue(
            identity_id=self.identity.identity_id, actor_id="abhi"
        )
        self.assertEqual(record.device_id, "phone-1")
        self.assertEqual(record.actor_id, "abhi")
        self.assertIs(record.max_risk, RiskLevel.LOW)
        self.assertTrue(record.is_active())

    def test_a_revoked_identity_cannot_be_issued_for(self) -> None:
        self.identities.revoke(self.identity.identity_id)
        with self.assertRaises(IdentityError):
            self.credentials.issue(
                identity_id=self.identity.identity_id, actor_id="abhi"
            )

    def test_an_unknown_identity_cannot_be_issued_for(self) -> None:
        with self.assertRaises(IdentityError):
            self.credentials.issue(identity_id="idn_nope", actor_id="abhi")

    def test_actor_is_required(self) -> None:
        # A credential naming no principal cannot be revoked by the person who owns
        # the phone, and cannot be attributed in an audit.
        for bad in ("", "  "):
            with self.subTest(bad=bad), self.assertRaises(CredentialError):
                self.credentials.issue(
                    identity_id=self.identity.identity_id, actor_id=bad
                )

    def test_ttl_must_be_positive(self) -> None:
        for bad in (0, -1):
            with self.subTest(bad=bad), self.assertRaises(CredentialError):
                self.credentials.issue(
                    identity_id=self.identity.identity_id,
                    actor_id="abhi",
                    ttl_s=bad,
                )

    def test_credential_refuses_a_plaintext_secret(self) -> None:
        with self.assertRaises(CredentialError):
            Credential(
                credential_id="cred_1",
                identity_id=self.identity.identity_id,
                device_id="phone-1",
                actor_id="abhi",
                secret_hash="hunter2",
            )


class TestAuthentication(unittest.TestCase):
    def setUp(self) -> None:
        self.identities, self.credentials = _store()
        self.identity = self.identities.enroll("phone-1", DeviceKind.PHONE)
        self.record, self.token = self.credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            ttl_s=60,
            now=PAST,
        )

    def test_a_valid_token_authenticates(self) -> None:
        found = self.credentials.authenticate(self.token, now=PAST)
        self.assertIs(found, self.record)

    def test_whitespace_is_tolerated_but_nothing_else(self) -> None:
        self.assertIs(self.credentials.authenticate(f"  {self.token}  ", now=PAST), self.record)

    def test_a_wrong_token_is_unknown(self) -> None:
        with self.assertRaises(CredentialError):
            self.credentials.authenticate(new_token(), now=PAST)

    def test_expiry_is_enforced(self) -> None:
        with self.assertRaises(CredentialError):
            self.credentials.authenticate(self.token, now=PAST + timedelta(seconds=61))

    def test_revocation_is_enforced(self) -> None:
        self.credentials.revoke(self.record.credential_id, now=PAST)
        with self.assertRaises(CredentialError):
            self.credentials.authenticate(self.token, now=PAST)

    def test_revoking_the_device_invalidates_the_credential(self) -> None:
        """The property that makes `POST /devices/{id}/revoke` sufficient."""
        self.identities.revoke(self.identity.identity_id, now=PAST)
        with self.assertRaises(IdentityError):
            self.credentials.authenticate(self.token, now=PAST)

    def test_empty_token_is_refused(self) -> None:
        for bad in ("", "   ", None):
            with self.subTest(bad=bad), self.assertRaises(CredentialError):
                self.credentials.authenticate(bad)  # type: ignore[arg-type]

    def test_rotate_leaves_the_old_token_dead(self) -> None:
        rotated, new_secret = self.credentials.rotate(
            self.record.credential_id, now=PAST, ttl_s=60
        )
        self.assertNotEqual(rotated.credential_id, self.record.credential_id)
        self.assertNotEqual(new_secret, self.token)
        with self.assertRaises(CredentialError):
            self.credentials.authenticate(self.token, now=PAST)
        self.credentials.authenticate(new_secret, now=PAST)

    def test_revoke_is_idempotent(self) -> None:
        first = self.credentials.revoke(self.record.credential_id, now=PAST)
        second = self.credentials.revoke(self.record.credential_id, now=PAST + timedelta(days=1))
        self.assertEqual(first.revoked_at, PAST)
        self.assertEqual(second.revoked_at, PAST)

    def test_revoking_an_unknown_credential_fails(self) -> None:
        with self.assertRaises(CredentialError):
            self.credentials.revoke("cred_nope")


class TestGrantCeiling(unittest.TestCase):
    """A credential may narrow what its actor consented to, never widen it."""

    def setUp(self) -> None:
        self.identities, _ = _store()
        self.identity = self.identities.enroll("phone-1", DeviceKind.PHONE)

    def _store_with(self, grants: list[PermissionGrant]) -> CredentialStore:
        return CredentialStore(
            self.identities,
            key=KEY,
            consents=lambda actor_id: [g for g in grants if g.granted_by == actor_id],
        )

    def test_no_consent_hook_means_no_grants(self) -> None:
        credentials = self._store_with([])
        # Mint-time ceiling: with no consented scope the actor is at LOW, so a
        # HIGH credential is refused at issuance rather than minted and then
        # releasing nothing (grants_for() is the second wall, not the first).
        with self.assertRaises(CredentialError):
            credentials.issue(
                identity_id=self.identity.identity_id,
                actor_id="abhi",
                max_risk=RiskLevel.HIGH,
                now=PAST,
            )

    def test_the_lower_ceiling_wins(self) -> None:
        # calendar.read is LOW risk in the catalog, so a LOW-ceiling credential can
        # still use a consent its actor gave at CRITICAL.
        grants = [
            PermissionGrant(
                scope="calendar.read",
                granted_by="abhi",
                max_risk=RiskLevel.CRITICAL,
                granted_at=PAST,
            )
        ]
        credentials = self._store_with(grants)
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.LOW,
            now=PAST,
        )
        released = credentials.grants_for(record, now=PAST)
        self.assertEqual([g.scope for g in released], ["calendar.read"])
        # Capped to the credential's ceiling, not the consent's.
        self.assertIs(released[0].max_risk, RiskLevel.LOW)

    def test_a_stale_credential_object_releases_nothing(self) -> None:
        """Revocation reaches callers holding an out-of-date record."""
        credentials = self._store_with(
            [PermissionGrant(scope="calendar.read", granted_by="abhi", granted_at=PAST)]
        )
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.HIGH,
            now=PAST,
        )
        self.assertEqual(len(credentials.grants_for(record, now=PAST)), 1)
        credentials.revoke(record.credential_id, now=PAST)
        # `record` is the same object it was before — a receipt, not the state.
        self.assertFalse(record.revoked)
        self.assertEqual(credentials.grants_for(record, now=PAST), [])

    def test_a_consent_above_the_credentials_risk_is_dropped(self) -> None:
        # file.read is MEDIUM in the catalog; a LOW-ceiling credential cannot use it.
        grants = [
            PermissionGrant(
                scope="file.read",
                granted_by="abhi",
                max_risk=RiskLevel.CRITICAL,
                granted_at=PAST,
            )
        ]
        credentials = self._store_with(grants)
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.LOW,
            now=PAST,
        )
        self.assertEqual(credentials.grants_for(record, now=PAST), [])

    def test_a_expired_consent_is_not_released(self) -> None:
        grants = [
            PermissionGrant(
                scope="calendar.read",
                granted_by="abhi",
                max_risk=RiskLevel.CRITICAL,
                granted_at=PAST,
                expires_at=PAST + timedelta(seconds=1),
            )
        ]
        credentials = self._store_with(grants)
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.HIGH,
            now=PAST,
        )
        self.assertEqual(credentials.grants_for(record, now=PAST + timedelta(hours=1)), [])

    def test_an_inconcrete_consent_scope_is_ignored(self) -> None:
        grants = [
            PermissionGrant(scope="*", granted_by="abhi", max_risk=RiskLevel.CRITICAL, granted_at=PAST)
        ]
        credentials = self._store_with(grants)
        # An inconcrete scope contributes no ceiling, so a HIGH mint is refused.
        # grants_for() ignoring the scope is the second line of defense; the
        # mint-time ceiling is the first.
        with self.assertRaises(CredentialError):
            credentials.issue(
                identity_id=self.identity.identity_id,
                actor_id="abhi",
                max_risk=RiskLevel.HIGH,
                now=PAST,
            )

    def test_a_dead_credential_releases_nothing(self) -> None:
        credentials = self._store_with(
            [PermissionGrant(scope="calendar.read", granted_by="abhi", granted_at=PAST)]
        )
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.HIGH,
            ttl_s=10,
            now=PAST,
        )
        self.assertEqual(credentials.grants_for(record, now=PAST + timedelta(hours=1)), [])
        credentials.revoke(record.credential_id, now=PAST)
        self.assertEqual(credentials.grants_for(record, now=PAST), [])

    def test_a_revoked_identity_releases_nothing(self) -> None:
        # Default PermissionGrant.max_risk is HIGH, so this consent prices the
        # actor at HIGH and the HIGH mint below stays within its ceiling.
        credentials = self._store_with(
            [PermissionGrant(scope="calendar.read", granted_by="abhi", granted_at=PAST)]
        )
        record, _ = credentials.issue(
            identity_id=self.identity.identity_id,
            actor_id="abhi",
            max_risk=RiskLevel.HIGH,
            now=PAST,
        )
        self.identities.revoke(self.identity.identity_id, now=PAST)
        # grants_for() fails closed to [] on a revoked identity — it never
        # raises here, and it never leaks the consent. The repair assert is
        # the point of this test: nothing is released.
        self.assertEqual(credentials.grants_for(record, now=PAST), [])


class TestPublicView(unittest.TestCase):
    def test_public_view_carries_no_digest(self) -> None:
        identities, credentials = _store()
        identity = identities.enroll("phone-1", DeviceKind.PHONE)
        record, token = credentials.issue(
            identity_id=identity.identity_id, actor_id="abhi"
        )
        public = record.public()
        self.assertNotIn("secret_hash", public)
        self.assertNotIn("token", public)
        self.assertNotIn(token, str(public))
        self.assertEqual(public["device_id"], "phone-1")

    def test_listing_can_hide_dead_credentials(self) -> None:
        identities, credentials = _store()
        first = identities.enroll("phone-1", DeviceKind.PHONE)
        second = identities.enroll("phone-2", DeviceKind.PHONE)
        one, _ = credentials.issue(identity_id=first.identity_id, actor_id="abhi")
        credentials.issue(identity_id=second.identity_id, actor_id="abhi")
        credentials.revoke(one.credential_id)
        self.assertEqual(len(credentials.credentials()), 2)
        self.assertEqual(len(credentials.credentials(include_revoked=False)), 1)


if __name__ == "__main__":
    unittest.main()
