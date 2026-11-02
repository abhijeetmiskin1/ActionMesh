"""C19 adversarial pairing tests — guessing, replay, theft, and the limits that hold.

These are the threats the mobile control plane actually adds. Each test states the
attack as the attacker would, because a pairing ceremony that only passes tests
written from the defender's point of view has not been tested.
"""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import (
    CredentialError,
    CredentialStore,
    hash_secret,
    new_token,
)
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.pairing import (
    CODE_DIGITS,
    LOCKOUT_S,
    MAX_ATTEMPTS_PER_WINDOW,
    PairingError,
    PairingService,
)
from actionmesh.protocol.models import DeviceKind, RiskLevel

KEY = b"adversarial-key-material-0123456"
NOW = datetime(2026, 4, 1, 9, 0, tzinfo=UTC)


def token_digest(token: str) -> str:
    return hash_secret(token, key=KEY)


def _build(**overrides: object) -> tuple[PairingService, DeviceRegistry, CredentialStore, IdentityStore]:
    identities = IdentityStore()
    devices = DeviceRegistry(identities=identities)
    devices.enroll(device_id="laptop-1", kind=DeviceKind.LAPTOP)
    credentials = CredentialStore(identities, key=KEY)
    pairing = PairingService(
        identities=identities,
        credentials=credentials,
        devices=devices,
        key=KEY,
        **overrides,  # type: ignore[arg-type]
    )
    return pairing, devices, credentials, identities


class TestGuessing(unittest.TestCase):
    """The code is 10^6 wide. What matters is how many guesses anyone gets."""

    def guess(self, pairing: PairingService, count: int, now: datetime) -> str | None:
        """Send ``count`` wrong codes; return the lockout message if one fired.

        Each wrong code raises on its own, so a loop wrapped in ``assertRaises``
        proves nothing but that ``"000000"`` is wrong — the first guess leaves the
        loop and the budget is never walked. The budget has to be spent attempt by
        attempt, exactly as an attacker would.
        """
        for index in range(count):
            try:
                pairing.redeem(
                    "000000", phone_device_id=f"phone-{index}", actor_id="a", now=now
                )
            except PairingError as exc:
                if "too many pairing attempts" in str(exc):
                    return str(exc)
        return None

    def test_the_window_caps_every_attempt_including_garbage(self) -> None:
        pairing, _, _, _ = _build()
        pairing.issue("laptop-1", now=NOW)
        # The budget counts attempts, not *valid* attempts: a guesser who could send
        # malformed codes for free has an unlimited budget.
        self.assertIsNone(
            self.guess(pairing, MAX_ATTEMPTS_PER_WINDOW, now=NOW),
            "the budget must not trip on its own count",
        )
        self.assertIsNotNone(
            self.guess(pairing, 1, now=NOW), "one past the budget must trip it"
        )

    def test_tripping_the_limit_burns_every_live_code(self) -> None:
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        self.guess(pairing, MAX_ATTEMPTS_PER_WINDOW + 1, now=NOW)
        self.assertEqual(pairing.active(now=NOW), [])
        with self.assertRaises(PairingError) as ctx:
            pairing.redeem(code, phone_device_id="victim", actor_id="a", now=NOW)
        # Locked first, so the message is the lockout rather than "revoked" — either
        # way the burned code cannot be spent, which is the property that matters.
        self.assertIn("locked until", str(ctx.exception))

    def test_lockout_blocks_minting_until_it_expires(self) -> None:
        # Deliberate: a locked-out honest user waits out a five-minute lock and mints
        # a new code. An unlocked guessing machine does not stop, so the lock has to
        # cover the door as well as the lock.
        pairing, _, _, _ = _build()
        self.guess(pairing, MAX_ATTEMPTS_PER_WINDOW + 1, now=NOW)
        with self.assertRaises(PairingError) as ctx:
            pairing.issue("laptop-1", now=NOW)
        self.assertIn("locked until", str(ctx.exception))
        _, fresh = pairing.issue("laptop-1", now=NOW + timedelta(seconds=LOCKOUT_S + 1))
        credential, _, _ = pairing.redeem(
            fresh, phone_device_id="phone-9", actor_id="abhi",
            now=NOW + timedelta(seconds=LOCKOUT_S + 1),
        )
        self.assertEqual(credential.device_id, "phone-9")

    def test_lockout_expires_and_pairing_recovers(self) -> None:
        pairing, _, _, _ = _build(lockout_s=60)
        self.guess(pairing, MAX_ATTEMPTS_PER_WINDOW + 1, now=NOW)
        later = NOW + timedelta(seconds=61)
        _, code = pairing.issue("laptop-1", now=later)
        credential, _, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=later
        )
        self.assertIs(credential.max_risk, RiskLevel.LOW)

    def test_the_guess_space_is_not_reduced_by_a_matched_prefix(self) -> None:
        """No failure path says how close a guess was."""
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        near = code[:5] + ("0" if code[5] != "0" else "1")
        messages = set()
        for guess in (near, "000000" if code != "000000" else "111111"):
            with self.assertRaises(PairingError) as ctx:
                pairing.redeem(
                    guess, phone_device_id="p", actor_id="a", now=NOW + timedelta(seconds=1)
                )
            messages.add(str(ctx.exception))
        self.assertEqual(messages, {"invalid pairing code"})

    def test_the_window_slides_so_a_slow_guesser_is_not_free(self) -> None:
        pairing, _, _, _ = _build(max_attempts_per_window=3, window_s=60)
        self.guess(pairing, 3, now=NOW)
        # The fourth inside the window trips it, even 59 seconds after the first.
        self.assertIsNotNone(
            self.guess(pairing, 1, now=NOW + timedelta(seconds=59))
        )
        # Attempts older than the window are forgotten, so a guesser paced outside it
        # is never locked — which is what makes this a limit on guessing rather than
        # a limit on people.
        slow, _, _, _ = _build(max_attempts_per_window=3, window_s=60)
        for index in range(6):
            self.guess(slow, 1, now=NOW + timedelta(seconds=61 * index))
        self.assertIsNone(slow.locked_until)


class TestReplay(unittest.TestCase):
    def test_a_spent_code_buys_nothing_twice(self) -> None:
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        _, token, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        with self.assertRaises(PairingError):
            pairing.redeem(
                code, phone_device_id="phone-2", actor_id="mallory", now=NOW
            )
        # The first pairing is not disturbed by the failed replay.
        pairing.credentials.authenticate(token, now=NOW)

    def test_an_expired_code_is_not_a_code(self) -> None:
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        with self.assertRaises(PairingError) as ctx:
            pairing.redeem(
                code, phone_device_id="phone-1", actor_id="abhi", now=NOW + timedelta(seconds=301)
            )
        self.assertIn("expired", str(ctx.exception))

    def test_a_credential_expires_too(self) -> None:
        pairing, _, _, _ = _build(credential_ttl_s=60)
        _, code = pairing.issue("laptop-1", now=NOW)
        _, token, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        pairing.credentials.authenticate(token, now=NOW + timedelta(seconds=59))
        with self.assertRaises(CredentialError):
            pairing.credentials.authenticate(token, now=NOW + timedelta(seconds=61))


class TestTheft(unittest.TestCase):
    def test_a_stolen_token_dies_when_the_device_is_forgotten(self) -> None:
        pairing, devices, credentials, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        _, token, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        credentials.authenticate(token, now=NOW)
        devices.forget("phone-1", now=NOW)
        with self.assertRaises(IdentityError):
            credentials.authenticate(token, now=NOW)

    def test_a_stolen_token_store_does_not_contain_the_tokens(self) -> None:
        pairing, _, credentials, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        _, token, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        dumped = repr(credentials.credentials())
        self.assertNotIn(token, dumped)
        self.assertNotIn(token, str(credentials.credentials()[0].public()))
        # What a copy of the store yields is a keyed digest. Without the key the
        # only route left is guessing, and the token is 256 bits.
        self.assertEqual(credentials.credentials()[0].secret_hash, token_digest(token))

    def test_a_stolen_code_store_never_held_the_codes(self) -> None:
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        self.assertNotIn(code, repr(pairing._codes))
        self.assertNotIn(code, str(pairing.active(now=NOW)[0].public()))
        self.assertEqual(len(code), CODE_DIGITS)

    def test_a_leaked_credential_id_is_not_a_handle_on_the_token(self) -> None:
        pairing, _, credentials, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        credential, token, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        # Nothing resolves a credential by id and hands back a secret; `get` gives
        # the record, and the record cannot speak its own token.
        looked_up = credentials.get(credential.credential_id)
        assert looked_up is not None
        self.assertNotEqual(credential.credential_id, token)
        self.assertNotIn(token, repr(looked_up))
        self.assertNotIn(token, str(looked_up.public()))
        with self.assertRaises(CredentialError):
            credentials.authenticate(credential.credential_id, now=NOW)
        # And the token space is not walkable either: a fresh random token misses.
        with self.assertRaises(CredentialError):
            credentials.authenticate(new_token(), now=NOW)


class TestUnauthorizedRegistration(unittest.TestCase):
    def test_a_remote_caller_cannot_invent_a_laptop(self) -> None:
        pairing, devices, _, _ = _build()
        with self.assertRaises(PairingError):
            pairing.issue("attacker-machine", now=NOW)
        self.assertIsNone(devices.devices.get("attacker-machine"))

    def test_pairing_grants_nothing_by_itself(self) -> None:
        pairing, _, credentials, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        credential, _, _ = pairing.redeem(
            code, phone_device_id="phone-1", actor_id="abhi", now=NOW
        )
        self.assertEqual(credentials.grants_for(credential, now=NOW), [])

    def test_a_paired_phone_is_not_an_execution_device(self) -> None:
        pairing, devices, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        pairing.redeem(code, phone_device_id="phone-1", actor_id="abhi", now=NOW)
        with self.assertRaises(PermissionError):
            devices.require("phone-1", kind=DeviceKind.LAPTOP, now=NOW)
        # And it cannot mint an invitation for itself.
        with self.assertRaises(PairingError):
            pairing.issue("phone-1", now=NOW)

    def test_two_phones_cannot_claim_one_device_id(self) -> None:
        pairing, _, _, _ = _build()
        _, code = pairing.issue("laptop-1", now=NOW)
        pairing.redeem(code, phone_device_id="phone-1", actor_id="abhi", now=NOW)
        _, second = pairing.issue("laptop-1", now=NOW)
        with self.assertRaises(PairingError):
            pairing.redeem(
                second, phone_device_id="phone-1", actor_id="mallory", now=NOW
            )


if __name__ == "__main__":
    unittest.main()
