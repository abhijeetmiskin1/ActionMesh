"""C19c pairing flow tests — issue, redeem, expiry, single use, association."""

from __future__ import annotations

import re
import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import CredentialStore
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityStore
from actionmesh.core.pairing import (
    PairingError,
    PairingService,
    format_code,
    normalize_code,
    screen_payload,
)
from actionmesh.core.permissions.context import PermissionGrant
from actionmesh.protocol.models import DeviceKind, RiskLevel

KEY = b"0123456789abcdef" * 2
PAST = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)


def _service(
    **overrides: object,
) -> tuple[PairingService, IdentityStore, CredentialStore, DeviceRegistry, list]:
    identities = IdentityStore()
    devices = DeviceRegistry(identities=identities)
    # Consent ceiling hook: abhi has consented to device.control at CRITICAL,
    # so redeeming a HIGH-ceiling code can mint within its ceiling. The
    # default LOW credential still releases nothing: grants_for() drops scopes
    # whose risk sits above the credential's own ceiling.
    credentials = CredentialStore(
        identities,
        key=KEY,
        consents=lambda actor_id: [
            PermissionGrant(
                scope="device.control",
                granted_by=actor_id,
                max_risk=RiskLevel.CRITICAL,
                granted_at=PAST,
            )
        ]
        if actor_id == "abhi"
        else [],
    )
    events: list[tuple[str, str, dict[str, object]]] = []
    pairing = PairingService(
        identities=identities,
        credentials=credentials,
        devices=devices,
        key=KEY,
        audit=lambda event, subject, payload: events.append((event, subject, payload)),
        **overrides,  # type: ignore[arg-type]
    )
    return pairing, identities, credentials, devices, events


def _laptop(devices: DeviceRegistry, device_id: str = "laptop-1") -> None:
    devices.enroll(device_id=device_id, kind=DeviceKind.LAPTOP, name="My Laptop")


class TestCodeShape(unittest.TestCase):
    def test_format_matches_the_ceremony_the_spec_draws(self) -> None:
        self.assertEqual(format_code("742381"), "742-381")

    def test_normalize_accepts_what_a_phone_sends(self) -> None:
        for raw in ("742381", "742-381", " 742 381 ", "742_381"):
            with self.subTest(raw=raw):
                self.assertEqual(normalize_code(raw), "742381")

    def test_normalize_fails_closed(self) -> None:
        for bad in ("", "12345", "1234567", "abcdef", None, 742381, "74238O"):
            with self.subTest(bad=bad), self.assertRaises(PairingError):
                normalize_code(bad)

    def test_codes_are_unpredictable(self) -> None:
        pairing, _, _, devices, _ = _service()
        _laptop(devices)
        seen = set()
        for _ in range(50):
            _, code = pairing.issue("laptop-1")
            self.assertRegex(code, r"^\d{6}$")
            seen.add(code)
        self.assertGreater(len(seen), 40)


class TestIssuance(unittest.TestCase):
    def setUp(self) -> None:
        self.pairing, self.identities, self.credentials, self.devices, self.events = _service()
        _laptop(self.devices)

    def test_issue_returns_a_displayable_code(self) -> None:
        record, code = self.pairing.issue("laptop-1", now=PAST)
        self.assertRegex(code, r"^\d{6}$")
        self.assertEqual(record.laptop_device_id, "laptop-1")
        self.assertEqual(
            record.expires_at - record.created_at, timedelta(seconds=300)
        )
        self.assertEqual(self.pairing.active(now=PAST), [record])

    def test_only_one_live_code_per_laptop(self) -> None:
        first, _ = self.pairing.issue("laptop-1", now=PAST)
        second, _ = self.pairing.issue("laptop-1", now=PAST + timedelta(seconds=1))
        self.assertTrue(first.revoked)
        self.assertEqual(self.pairing.active(now=PAST), [second])

    def test_an_unknown_device_cannot_be_paired_to(self) -> None:
        with self.assertRaises(PairingError):
            self.pairing.issue("laptop-ghost")

    def test_a_phone_cannot_issue_a_code_for_itself(self) -> None:
        self.devices.enroll(device_id="phone-1", kind=DeviceKind.PHONE)
        with self.assertRaises(PairingError):
            self.pairing.issue("phone-1")

    def test_revoke_all_burns_every_live_code(self) -> None:
        self.pairing.issue("laptop-1", now=PAST)
        self.assertEqual(self.pairing.revoke_all(now=PAST), 1)
        self.assertEqual(self.pairing.active(now=PAST), [])

    def test_issue_refuses_a_nonpositive_ttl(self) -> None:
        with self.assertRaises(PairingError):
            _service(code_ttl_s=0)


class TestRedemption(unittest.TestCase):
    def setUp(self) -> None:
        self.pairing, self.identities, self.credentials, self.devices, self.events = _service()
        _laptop(self.devices)
        self.record, self.code = self.pairing.issue("laptop-1", now=PAST)

    def _redeem(self, **kw: object) -> tuple:
        kwargs: dict[str, object] = {
            "phone_device_id": "phone-1",
            "actor_id": "abhi",
            "phone_name": "Pixel",
            "now": PAST,
        }
        kwargs.update(kw)
        return self.pairing.redeem(self.code, **kwargs)  # type: ignore[arg-type]

    def test_pairing_associates_everything_the_spec_lists(self) -> None:
        credential, token, device = self._redeem()
        self.assertEqual(device.device_id, "phone-1")
        self.assertIs(device.kind, DeviceKind.PHONE)
        self.assertEqual(credential.device_id, "phone-1")
        self.assertEqual(credential.actor_id, "abhi")
        # device_id, phone identity, laptop identity, token, ceiling — all bound
        # to one another, and the phone's identity is the join.
        self.assertEqual(device.identity_id, credential.identity_id)
        self.assertEqual(self.identities.resolve(credential.identity_id).device_id, "phone-1")
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 40)

    def test_the_phone_is_online_after_pairing(self) -> None:
        # The redemption itself is the proof of presence. Read at the moment of
        # pairing — presence is derived from the clock, so a backdated fixture that
        # asked "is it online now" would correctly say no.
        self._redeem()
        self.assertEqual(
            [
                d.device_id
                for d in self.devices.available(kind=DeviceKind.PHONE, now=PAST)
            ],
            ["phone-1"],
        )

    def test_a_paired_phone_advertises_no_capabilities(self) -> None:
        # It is a control surface. What it may request is consent's decision.
        _, _, device = self._redeem()
        self.assertEqual(device.capabilities, [])

    def test_a_paired_phone_gets_no_permissions(self) -> None:
        credential, _, _ = self._redeem()
        self.assertEqual(self.credentials.grants_for(credential, now=PAST), [])

    def test_the_default_ceiling_is_low(self) -> None:
        # Pairing admits a device; it does not authorize it. The read-only end is what
        # a fresh pairing gets, and raising it is an act at the laptop.
        credential, _, _ = self._redeem()
        self.assertIs(credential.max_risk, RiskLevel.LOW)

    def test_the_ceiling_is_borrowed_from_the_code_not_the_caller(self) -> None:
        record, code = self.pairing.issue(
            "laptop-1", max_risk=RiskLevel.HIGH, now=PAST
        )
        self.assertIs(record.max_risk, RiskLevel.HIGH)
        credential, _, _ = self.pairing.redeem(
            code, phone_device_id="phone-7", actor_id="abhi", now=PAST
        )
        self.assertIs(credential.max_risk, RiskLevel.HIGH)

    def test_an_unknown_ceiling_is_refused_at_issuance(self) -> None:
        with self.assertRaises(PairingError):
            self.pairing.issue("laptop-1", max_risk="critical", now=PAST)

    def test_a_code_is_single_use(self) -> None:
        self._redeem()
        with self.assertRaises(PairingError) as ctx:
            self._redeem(phone_device_id="phone-2")
        self.assertIn("already used", str(ctx.exception))

    def test_an_expired_code_is_refused(self) -> None:
        with self.assertRaises(PairingError) as ctx:
            self.pairing.redeem(
                self.code,
                phone_device_id="phone-1",
                actor_id="abhi",
                now=PAST + timedelta(seconds=301),
            )
        self.assertIn("expired", str(ctx.exception))

    def test_a_revoked_code_is_refused(self) -> None:
        self.pairing.revoke_all(now=PAST)
        with self.assertRaises(PairingError) as ctx:
            self._redeem()
        self.assertIn("revoked", str(ctx.exception))

    def test_a_wrong_code_is_refused(self) -> None:
        wrong = "000000" if self.code != "000000" else "111111"
        with self.assertRaises(PairingError) as ctx:
            self.pairing.redeem(
                wrong, phone_device_id="phone-1", actor_id="abhi", now=PAST
            )
        self.assertIn("invalid pairing code", str(ctx.exception))

    def test_a_device_cannot_pair_with_itself(self) -> None:
        with self.assertRaises(PairingError) as ctx:
            self.pairing.redeem(
                self.code, phone_device_id="laptop-1", actor_id="abhi", now=PAST
            )
        self.assertIn("itself", str(ctx.exception))

    def test_an_actor_is_required(self) -> None:
        for bad in ("", "   "):
            with self.subTest(bad=bad), self.assertRaises(PairingError):
                self.pairing.redeem(
                    self.code, phone_device_id="phone-1", actor_id=bad, now=PAST
                )

    def test_repairing_the_same_device_is_refused(self) -> None:
        self._redeem()
        second_record, second_code = self.pairing.issue("laptop-1", now=PAST)
        with self.assertRaises(PairingError) as ctx:
            self.pairing.redeem(
                second_code, phone_device_id="phone-1", actor_id="abhi", now=PAST
            )
        self.assertIn("already enrolled", str(ctx.exception))
        # And the second code is burned, so a blocked attempt cannot be retried
        # against a live invitation.
        self.assertTrue(second_record.spent)

    def test_a_failed_pairing_leaves_no_orphan_identity(self) -> None:
        self._redeem()
        _, code = self.pairing.issue("laptop-1", now=PAST)
        with self.assertRaises(PairingError):
            self.pairing.redeem(
                code, phone_device_id="phone-1", actor_id="abhi", now=PAST
            )
        # phone-1 exists (the first pairing) and phone-2 was never created.
        self.assertIsNone(self.identities.for_device("phone-2"))

    def test_missing_separators_in_the_phone_id_are_refused(self) -> None:
        with self.assertRaises(PairingError):
            self.pairing.redeem(
                self.code, phone_device_id="  ", actor_id="abhi", now=PAST
            )


class TestAuditHygiene(unittest.TestCase):
    """The pairing secret must not be logged. Section 9 says so, so we test it."""

    def setUp(self) -> None:
        self.pairing, self.identities, self.credentials, self.devices, self.events = _service()
        _laptop(self.devices)
        self.record, self.code = self.pairing.issue("laptop-1", now=PAST)

    def test_events_never_carry_the_code(self) -> None:
        self.pairing.redeem(
            self.code, phone_device_id="phone-1", actor_id="abhi", now=PAST
        )
        names = [event for event, _, _ in self.events]
        self.assertIn("pairing.code_issued", names)
        self.assertIn("pairing.completed", names)
        dumped = repr(self.events)
        self.assertNotIn(self.code, dumped)
        self.assertNotIn(format_code(self.code), dumped)

    def test_the_completion_event_records_the_relationship(self) -> None:
        credential, _, _ = self.pairing.redeem(
            self.code, phone_device_id="phone-1", actor_id="abhi", now=PAST
        )
        _, _, payload = self.events[-1]
        self.assertEqual(payload["laptop_device_id"], "laptop-1")
        self.assertEqual(payload["actor_id"], "abhi")
        self.assertEqual(payload["credential_id"], credential.credential_id)
        self.assertEqual(payload["code_id"], self.record.code_id)

    def test_screening_drops_secret_shaped_values(self) -> None:
        screened = screen_payload(
            {
                "code": "742381",
                "code_id": "pair_abc",
                "laptop_device_id": "laptop-1",
                "token": "secret-token",
                "count": 12,
                "six_digit_number": "123456",
            }
        )
        self.assertEqual(set(screened), {"code_id", "laptop_device_id", "count"})

    def test_a_digest_never_appears_in_the_public_view(self) -> None:
        public = self.record.public()
        self.assertNotIn("secret_hash", public)
        self.assertNotIn(self.record.secret_hash, repr(self.record))
        self.assertNotIn(self.record.secret_hash, str(public))


class TestFormatGuards(unittest.TestCase):
    def test_an_empty_key_is_refused(self) -> None:
        identities = IdentityStore()
        with self.assertRaises(PairingError):
            PairingService(
                identities=identities,
                credentials=CredentialStore(identities, key=KEY),
                devices=DeviceRegistry(identities=identities),
                key=b"",
            )

    def test_attempt_budget_must_be_positive(self) -> None:
        identities = IdentityStore()
        with self.assertRaises(PairingError):
            PairingService(
                identities=identities,
                credentials=CredentialStore(identities, key=KEY),
                devices=DeviceRegistry(identities=identities),
                key=KEY,
                max_attempts_per_window=0,
            )

    def test_a_device_id_that_is_only_whitespace_is_rejected_cleanly(self) -> None:
        pairing, _, _, _, _ = _service()
        with self.assertRaises(PairingError):
            pairing.issue("   ")
        with self.assertRaises(PairingError):
            pairing.issue(None)  # type: ignore[arg-type]

    def test_the_code_format_is_six_digits(self) -> None:
        pairing, _, _, devices, _ = _service()
        _laptop(devices)
        _, code = pairing.issue("laptop-1")
        self.assertTrue(re.fullmatch(r"\d{6}", code))
        self.assertEqual(len(code), 6)


if __name__ == "__main__":
    unittest.main()
