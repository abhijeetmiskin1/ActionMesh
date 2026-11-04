"""C19 device identity tests — enrollment, revocation, durability.

Identity is the premise every later phase depends on: pairing binds a credential
to an identity, execution attributes to one, and audit names one. A device that
can choose its own identity, or keep one it lost, makes all three lies.
"""

from __future__ import annotations

import re
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.identity import (
    DeviceIdentity,
    IdentityError,
    IdentityStore,
    SQLiteIdentityStore,
    new_identity_id,
)
from actionmesh.protocol.models import DeviceKind


class TestIdentityIds(unittest.TestCase):
    def test_ids_are_wide_and_random(self) -> None:
        first, second = new_identity_id(), new_identity_id()
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^idn_[0-9a-f]{32}$")
        # 32 hex chars is 128 bits of displayed entropy at minimum; the short
        # 48-bit suffix `new_id` uses would be fine for a label and not for this.
        self.assertGreaterEqual(len(re.sub(r"^idn_", "", first)) * 4, 128)


class TestEnrollment(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IdentityStore()

    def test_enroll_binds_the_declared_role(self) -> None:
        identity = self.store.enroll("laptop-1", DeviceKind.LAPTOP, label="My Laptop")
        self.assertEqual(identity.device_id, "laptop-1")
        self.assertIs(identity.kind, DeviceKind.LAPTOP)
        self.assertEqual(identity.label, "My Laptop")
        self.assertFalse(identity.revoked)
        self.assertIs(identity, self.store.for_device("laptop-1"))

    def test_unknown_role_is_refused(self) -> None:
        # An unattributed principal cannot be audited, so it cannot be enrolled.
        with self.assertRaises(IdentityError):
            self.store.enroll("mystery-1", DeviceKind.UNKNOWN)

    def test_blank_device_id_is_refused(self) -> None:
        for bad in ("", "   ", None):
            with self.subTest(bad=bad), self.assertRaises(IdentityError):
                self.store.enroll(bad, DeviceKind.LAPTOP)  # type: ignore[arg-type]

    def test_reenrolling_a_device_id_is_refused(self) -> None:
        self.store.enroll("laptop-1", DeviceKind.LAPTOP)
        with self.assertRaises(IdentityError):
            self.store.enroll("laptop-1", DeviceKind.PHONE)
        self.assertEqual(len(self.store), 1)

    def test_revoked_identity_cannot_be_reclaimed(self) -> None:
        """Re-enrollment is two explicit steps, so a lost identity stays lost."""
        identity = self.store.enroll("phone-1", DeviceKind.PHONE)
        self.store.revoke(identity.identity_id)
        with self.assertRaises(IdentityError):
            self.store.enroll("phone-1", DeviceKind.PHONE)
        # The revoked record survives: audit entries written under it must resolve.
        self.assertIsNotNone(self.store.get(identity.identity_id))

    def test_device_id_is_trimmed(self) -> None:
        identity = self.store.enroll("  laptop-1  ", DeviceKind.LAPTOP)
        self.assertEqual(identity.device_id, "laptop-1")
        self.assertIsNotNone(self.store.for_device("laptop-1"))


class TestRevocation(unittest.TestCase):
    def setUp(self) -> None:
        self.store = IdentityStore()
        self.identity = self.store.enroll("laptop-1", DeviceKind.LAPTOP)

    def test_resolve_works_while_active(self) -> None:
        self.assertIs(self.store.resolve(self.identity.identity_id), self.identity)
        self.assertIs(self.store.active_for_device("laptop-1"), self.identity)

    def test_resolve_refuses_a_revoked_identity(self) -> None:
        self.store.revoke(self.identity.identity_id)
        # Existence is not validity: the string still resolves to a record, and
        # that record is exactly what must no longer be usable.
        self.assertIsNotNone(self.store.get(self.identity.identity_id))
        with self.assertRaises(IdentityError):
            self.store.resolve(self.identity.identity_id)
        with self.assertRaises(IdentityError):
            self.store.active_for_device("laptop-1")

    def test_revoke_is_idempotent_and_keeps_the_first_timestamp(self) -> None:
        moment = datetime(2026, 1, 1, tzinfo=UTC)
        first = self.store.revoke(self.identity.identity_id, now=moment)
        second = self.store.revoke(self.identity.identity_id, now=moment + timedelta(days=1))
        self.assertEqual(first.revoked_at, moment)
        self.assertEqual(second.revoked_at, moment)

    def test_revoking_an_unknown_identity_fails(self) -> None:
        with self.assertRaises(IdentityError):
            self.store.revoke("idn_nope")

    def test_active_for_device_requires_enrollment(self) -> None:
        with self.assertRaises(IdentityError):
            self.store.active_for_device("never-seen")

    def test_listing_can_hide_revoked(self) -> None:
        other = self.store.enroll("phone-1", DeviceKind.PHONE)
        self.store.revoke(other.identity_id)
        self.assertEqual(len(self.store.identities()), 2)
        live = self.store.identities(include_revoked=False)
        self.assertEqual([i.device_id for i in live], ["laptop-1"])


class TestIdentityModel(unittest.TestCase):
    def test_identity_carries_no_secret(self) -> None:
        identity = IdentityStore().enroll("laptop-1", DeviceKind.LAPTOP)
        dumped = identity.model_dump()
        for key in dumped:
            self.assertNotIn("token", key.lower())
            self.assertNotIn("secret", key.lower())
        # expires_at is an expiry fact (like revoked_at), it is not a secret.
        self.assertEqual(
            set(dumped),
            {"identity_id", "device_id", "kind", "label", "created_at", "revoked_at", "expires_at"},
        )

    def test_identity_id_is_required(self) -> None:
        with self.assertRaises(IdentityError):
            DeviceIdentity(identity_id="", device_id="laptop-1", kind=DeviceKind.LAPTOP)


class TestDurableIdentityStore(unittest.TestCase):
    """Persistence is the point of C19: an identity re-minted each restart is a habit."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = f"{self.tmp.name}/identities.db"
        self.addCleanup(self.tmp.cleanup)

    def _open(self) -> SQLiteIdentityStore:
        store = SQLiteIdentityStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_identities_survive_a_reopen(self) -> None:
        first = self._open()
        identity = first.enroll("laptop-1", DeviceKind.LAPTOP, label="Work")
        expected = identity.model_dump()
        del first

        reopened = self._open()
        restored = reopened.get(identity.identity_id)
        self.assertIsNotNone(restored)
        assert restored is not None
        # Byte-for-byte: a restart must not re-mint, rename, or re-role the device.
        self.assertEqual(restored.model_dump(), expected)
        self.assertEqual(restored.device_id, "laptop-1")
        self.assertIs(restored.kind, DeviceKind.LAPTOP)
        self.assertEqual(restored.label, "Work")
        self.assertIs(restored, reopened.for_device("laptop-1"))

    def test_revocation_survives_a_reopen(self) -> None:
        store = self._open()
        identity = store.enroll("phone-1", DeviceKind.PHONE)
        store.revoke(identity.identity_id)
        del store

        reopened = self._open()
        with self.assertRaises(IdentityError):
            reopened.resolve(identity.identity_id)
        # And it still cannot be reclaimed after a restart.
        with self.assertRaises(IdentityError):
            reopened.enroll("phone-1", DeviceKind.PHONE)

    def test_duplicate_device_id_is_rejected_across_restarts(self) -> None:
        self._open().enroll("laptop-1", DeviceKind.LAPTOP)
        with self.assertRaises(IdentityError):
            self._open().enroll("laptop-1", DeviceKind.LAPTOP)

    def test_in_memory_and_sqlite_agree_on_shape(self) -> None:
        """The durable store is a drop-in, not a different contract."""
        plain = IdentityStore()
        durable = self._open()
        moment = datetime(2026, 2, 2, tzinfo=UTC)
        stored = plain.enroll("laptop-1", DeviceKind.LAPTOP, label="Work", now=moment)
        restored = durable.enroll("laptop-1", DeviceKind.LAPTOP, label="Work", now=moment)
        self.assertEqual(
            set(stored.model_dump()),
            set(restored.model_dump()),
        )
        # The identifier itself is random per enrollment, so it is the one field two
        # stores cannot be expected to agree on. Everything that carries meaning for
        # authorization must.
        first, second = stored.model_dump(), restored.model_dump()
        first.pop("identity_id")
        second.pop("identity_id")
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
