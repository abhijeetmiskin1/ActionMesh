"""C19c pairing: how a phone earns the right to be a principal.

The threat this exists to kill is the one every LAN control protocol ships with:
"the phone can reach port 8000, therefore the phone is trusted". Reachability is
not admission. A pairing code is the admission ceremony — **the laptop produces it,
the phone spends it**, and nobody who cannot see the laptop's screen gets in.

The code is 6 digits, which is barely 20 bits. That is the whole argument for the
rest of this module. Six typed digits are what a phone keyboard can actually enter,
so the design does not fix the weakness by making the code longer; it fixes it by
bounding how many guesses anyone gets:

* **Short-lived.** Five minutes by default; an expired code is not a code.
* **Single-use, spent before anything else happens.** A replay of a successful
  transcript gets nothing, and a crash mid-redemption cannot leave a live code next
  to a live credential.
* **One live code per laptop.** Requesting again replaces the outstanding code, so a
  mistyped ceremony does not accumulate invitations.
* **Globally rate-limited**, with the guessed-at code *not* being tracked per code —
  see the note below, because this is where the honest arithmetic lives.
* **Issued only locally.** No HTTP route mints a code. To want a new one, an
  attacker must already be standing at the laptop.

**Why there is no per-code attempt counter.** Codes are stored as keyed digests, so
a wrong guess cannot be attributed to the code the guesser was *aiming at* — there is
nothing to increment. A `max_attempts_per_code` setting would be a limit nobody could
enforce, which is worse than no limit because it reads like protection. The bound
that *is* enforceable is per-attempt across the service: with 20 guesses a minute and
a 5-minute code, an attacker spends at most ~100 of 10⁶ possibilities (**1 in 10,000**)
before the window trips, at which point every live code is revoked and pairing locks.

The code never appears in a log, an audit payload, an exception message, or a
``repr``. A code that leaks through a log file is a code that no longer needs
guessing.
"""

from __future__ import annotations

import secrets
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from actionmesh.core.credentials import (
    DEFAULT_CREDENTIAL_TTL_S,
    Credential,
    CredentialStore,
    hash_secret,
)
from actionmesh.core.devices import DeviceRegistry
from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.protocol.models import DeviceInfo, DeviceKind, RiskLevel

#: Six digits, typed on a phone keyboard. Security comes from the attempt budget,
#: not from the length.
CODE_DIGITS = 6
DEFAULT_CODE_TTL_S = 300.0
#: Redemption attempts across the whole service inside the window. Exceeding it
#: revokes every live code and locks pairing — a wrong guess costs the guesser more
#: than it costs anyone else.
MAX_ATTEMPTS_PER_WINDOW = 20
ATTEMPT_WINDOW_S = 60.0
LOCKOUT_S = 300.0

#: The ceiling a credential carries because its device paired — LOW, unless the
#: person at the laptop raises it while issuing the code.
#:
#: Pairing is not permission: it says *which device*, and consent still says *what it
#: may ask for*. What a fresh pairing gets on its own is the read-only end — project
#: lists, server status — because those are the actions whose risk is genuinely LOW.
#: Anything above that is chosen at the ceremony, deliberately, on the screen the
#: human is looking at, and never by the requester: a ceiling the phone could pick
#: would be a ceiling with no one enforcing it.
PAIRING_MAX_RISK = RiskLevel.LOW


class PairingError(ValueError):
    """Raised for any failed pairing step. Never carries a code."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def format_code(code: str) -> str:
    """``742381`` -> ``742-381``, for a human reading a laptop screen."""
    return f"{code[:3]}-{code[3:]}"


def normalize_code(raw: object) -> str:
    """Accept what a phone would send and fail closed on anything else.

    Spaces and separators are tolerated because the UI shows them. Letters, a wrong
    digit count, or an empty string are refusals rather than something to be clever
    about: a lenient parser turns a typo into a live attempt and a homoglyph into a
    bypass.
    """
    if not isinstance(raw, str):
        raise PairingError("a pairing code must be a string")
    digits = "".join(character for character in raw if character.isdigit())
    if len(digits) != CODE_DIGITS:
        raise PairingError(f"a pairing code is exactly {CODE_DIGITS} digits")
    return digits


@dataclass
class PairingCode:
    """One live invitation. Carries a digest, never the code.

    ``secret_hash`` is ``repr=False`` and absent from :meth:`public`, because a
    digest plus a guess list recovers a 20-bit code by brute force — so neither an
    unhandled exception trace nor a dashboard response may carry it.
    """

    code_id: str
    laptop_device_id: str
    secret_hash: str = field(repr=False)
    created_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime = field(
        default_factory=lambda: _utcnow() + timedelta(seconds=DEFAULT_CODE_TTL_S)
    )
    #: The risk ceiling this invitation buys. Chosen **at the laptop**, when a human
    #: is looking at the screen, and copied onto the credential at redemption. The
    #: phone never gets a vote: §14 says a client cannot set a risk level, and a
    #: ceiling the requester picks is not a ceiling.
    max_risk: RiskLevel = PAIRING_MAX_RISK
    spent_at: datetime | None = None
    revoked_at: datetime | None = None

    @property
    def spent(self) -> bool:
        return self.spent_at is not None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        return (now or _utcnow()) >= self.expires_at

    def public(self) -> dict[str, object]:
        """What the laptop may display: which device, and how long is left."""
        return {
            "code_id": self.code_id,
            "laptop_device_id": self.laptop_device_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_risk": self.max_risk.value,
            "spent": self.spent,
            "revoked": self.revoked,
        }


class PairingService:
    """Issue and redeem pairing codes; mint the credential a code earns."""

    def __init__(
        self,
        *,
        identities: IdentityStore,
        credentials: CredentialStore,
        devices: DeviceRegistry,
        key: bytes,
        code_ttl_s: float = DEFAULT_CODE_TTL_S,
        max_attempts_per_window: int = MAX_ATTEMPTS_PER_WINDOW,
        window_s: float = ATTEMPT_WINDOW_S,
        lockout_s: float = LOCKOUT_S,
        credential_ttl_s: float = DEFAULT_CREDENTIAL_TTL_S,
        audit: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        if not key:
            raise PairingError("pairing requires a non-empty key")
        if code_ttl_s <= 0:
            raise PairingError("code_ttl_s must be positive")
        if credential_ttl_s <= 0:
            raise PairingError("credential_ttl_s must be positive")
        if max_attempts_per_window < 1:
            raise PairingError("max_attempts_per_window must be at least 1")
        self.identities = identities
        self.credentials = credentials
        self.devices = devices
        self._key = key
        self._code_ttl_s = float(code_ttl_s)
        self._credential_ttl_s = float(credential_ttl_s)
        self._max_per_window = int(max_attempts_per_window)
        self._window_s = float(window_s)
        self._lockout_s = float(lockout_s)
        self._codes: dict[str, PairingCode] = {}
        self._by_hash: dict[str, str] = {}
        self._attempts: deque[float] = deque()
        self._locked_until: datetime | None = None
        #: Called with (event, subject, screened_payload).
        self._audit = audit

    @property
    def locked_until(self) -> datetime | None:
        return self._locked_until

    # -- issuance (laptop side) -------------------------------------------
    def issue(
        self,
        laptop_device_id: str,
        *,
        max_risk: RiskLevel = PAIRING_MAX_RISK,
        now: datetime | None = None,
    ) -> tuple[PairingCode, str]:
        """Mint a code for an enrolled laptop. Returns ``(record, plaintext_code)``.

        Local-only by construction: no route reaches this method. A device that is
        not enrolled cannot be paired *to*, and a phone cannot issue an invitation
        for itself.
        """
        moment = now or _utcnow()
        self._raise_if_locked(moment)
        device_id = (laptop_device_id or "").strip()
        device = self.devices.devices.get(device_id)
        if device is None:
            raise PairingError(f"unknown device {device_id!r}; enroll the laptop first")
        if device.kind not in (DeviceKind.LAPTOP, DeviceKind.SERVER):
            raise PairingError(
                f"{device_id!r} is a {device.kind.value}; a control device cannot "
                "issue a pairing code for itself"
            )
        # One live code per laptop, so a mistyped ceremony cannot leave several
        # invitations alive at the same time.
        for existing in list(self._codes.values()):
            if existing.laptop_device_id == device_id and not existing.revoked:
                existing.revoked_at = moment
        code = "".join(secrets.choice("0123456789") for _ in range(CODE_DIGITS))
        if not isinstance(max_risk, RiskLevel):
            raise PairingError(f"max_risk must be a RiskLevel, got {max_risk!r}")
        # C36: the code's ceiling becomes the *phone's* ceiling on redemption, and
        # `DeviceRegistry.set_risk_ceiling` is the only way to move a ceiling after
        # that — it refuses every increase. So the value chosen here, by the person
        # standing at the laptop, is the highest authority that phone will ever hold,
        # and pairing itself cannot raise it afterwards.
        #
        # It is deliberately not capped by the laptop's own ceiling. They measure
        # different things: the laptop's ceiling is what that machine may *execute*,
        # the phone's is what it may *ask for* anywhere in the mesh. A locked-down
        # workstation is a perfectly legitimate place to pair a controller for a
        # separate server, and capping here would make that impossible.
        record = PairingCode(
            code_id=f"pair_{secrets.token_hex(8)}",
            laptop_device_id=device_id,
            secret_hash=hash_secret(code, key=self._key),
            created_at=moment,
            expires_at=moment + timedelta(seconds=self._code_ttl_s),
            max_risk=max_risk,
        )
        self._codes[record.code_id] = record
        self._by_hash[record.secret_hash] = record.code_id
        self._record("pairing.code_issued", device_id, record.public())
        return record, code

    def active(self, *, now: datetime | None = None) -> list[PairingCode]:
        moment = now or _utcnow()
        return sorted(
            (
                record
                for record in self._codes.values()
                if not record.spent
                and not record.revoked
                and not record.is_expired(moment)
            ),
            key=lambda record: record.code_id,
        )

    def revoke_all(self, *, now: datetime | None = None) -> int:
        """Burn every live code. The answer to "I did not ask for this"."""
        moment = now or _utcnow()
        count = 0
        for record in list(self._codes.values()):
            if not record.spent and not record.revoked:
                record.revoked_at = moment
                count += 1
        self._record("pairing.codes_revoked", "all", {"revoked": count})
        return count

    # -- redemption (phone side) ------------------------------------------
    def redeem(
        self,
        raw_code: object,
        *,
        phone_device_id: str,
        actor_id: str,
        phone_name: str = "",
        now: datetime | None = None,
    ) -> tuple[Credential, str, DeviceInfo]:
        """Spend a code for a phone identity and credential.

        Returns ``(credential, plaintext_token, phone_device)``. The token is the
        only plaintext secret this call produces and belongs in exactly one response
        body. The credential's risk ceiling is the one that was chosen when the code
        was *issued*, at the laptop; there is deliberately no argument here for the
        caller to set it.

        The order of operations *is* the security argument:

        1. The attempt budget is consumed before the code is examined, so a garbage
           request is not free and the limit cannot be bypassed by sending noise.
        2. The code is marked spent before anything is minted, so a failure or a
           crash cannot leave a live code sitting next to a live credential.
        3. The phone is enrolled through the identity store rather than accepted as a
           caller-supplied id — a device that names itself is a device that renames
           itself whenever it likes.
        """
        moment = now or _utcnow()
        self._consume_attempt(moment)
        code = normalize_code(raw_code)

        digest = hash_secret(code, key=self._key)
        code_id = self._by_hash.get(digest)
        record = self._codes.get(code_id) if code_id is not None else None
        if record is None:
            raise PairingError("invalid pairing code")
        if record.revoked:
            raise PairingError("this pairing code was revoked")
        if record.spent:
            raise PairingError("this pairing code was already used")
        if record.is_expired(moment):
            raise PairingError("this pairing code has expired")

        phone = (phone_device_id or "").strip()
        if not phone:
            raise PairingError("phone_device_id is required")
        if phone == record.laptop_device_id:
            raise PairingError("a device cannot pair with itself")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise PairingError(
                "actor_id is required: a credential that names no principal cannot "
                "be audited or revoked by the person who owns the phone"
            )

        record.spent_at = moment
        try:
            identity = self.identities.enroll(
                phone, DeviceKind.PHONE, label=phone_name, now=moment
            )
        except IdentityError as exc:
            raise PairingError(
                f"{phone!r} is already enrolled ({exc}); revoke the device before "
                "pairing it again"
            ) from exc
        try:
            credential, token = self.credentials.issue(
                identity_id=identity.identity_id,
                actor_id=actor_id,
                max_risk=record.max_risk,
                ttl_s=self._credential_ttl_s,
                label=f"paired from {record.laptop_device_id}",
                now=moment,
            )
            device = self.devices.register(
                DeviceInfo(
                    device_id=phone,
                    name=phone_name.strip() or phone,
                    kind=DeviceKind.PHONE,
                    identity_id=identity.identity_id,
                    # A paired phone advertises no execution capabilities: it is a
                    # control surface, and what it may request is decided by consent
                    # and policy, not by what it claims to offer.
                    capabilities=[],
                    # The redemption itself is the proof of presence, so the phone
                    # shows online without needing a second round trip.
                    online=True,
                    last_seen=moment,
                    # C36: the code's ceiling is the owner's decision, made at the
                    # laptop, and it becomes the *device's* ceiling here. Pairing
                    # cannot raise one: `register` stores what it is given, and the
                    # only writer that can move a ceiling afterwards is
                    # `set_risk_ceiling`, which refuses increases.
                    risk_ceiling=record.max_risk,
                )
            )
        except Exception:
            # A half-finished pairing must not leave an identity that blocks the
            # next attempt and that nothing can authenticate as.
            self.identities.revoke(identity.identity_id, now=moment)
            raise
        self._record(
            "pairing.completed",
            phone,
            {
                "laptop_device_id": record.laptop_device_id,
                "identity_id": identity.identity_id,
                "credential_id": credential.credential_id,
                "actor_id": credential.actor_id,
                "max_risk": credential.max_risk.value,
                "code_id": record.code_id,
            },
        )
        return credential, token, device

    # -- attempt accounting ------------------------------------------------
    def _consume_attempt(self, now: datetime) -> None:
        self._raise_if_locked(now)
        self._attempts.append(now.timestamp())
        cutoff = now.timestamp() - self._window_s
        while self._attempts and self._attempts[0] < cutoff:
            self._attempts.popleft()
        if len(self._attempts) > self._max_per_window:
            self._locked_until = now + timedelta(seconds=self._lockout_s)
            burned = self.revoke_all(now=now)
            self._attempts.clear()
            self._record(
                "pairing.locked",
                "attempts",
                {
                    "attempts_in_window": self._max_per_window + 1,
                    "codes_revoked": burned,
                    "locked_until": self._locked_until.isoformat(),
                },
            )
            raise PairingError(
                "too many pairing attempts; every live code was revoked and pairing "
                f"is locked until {self._locked_until.isoformat()}"
            )

    def _raise_if_locked(self, now: datetime) -> None:
        if self._locked_until is not None and now < self._locked_until:
            raise PairingError(
                f"pairing is locked until {self._locked_until.isoformat()}"
            )

    # -- internals ---------------------------------------------------------
    def _record(self, event: str, subject: str, payload: dict[str, object]) -> None:
        if self._audit is None:
            return
        self._audit(event, subject, screen_payload(payload))


#: Payload keys that would name a secret rather than describe an event.
_SECRET_KEYS = frozenset({"code", "pair_code", "secret", "token", "plaintext", "pin"})


def screen_payload(payload: dict[str, object]) -> dict[str, object]:
    """Drop anything that could be a code before it reaches the audit trail.

    :meth:`PairingCode.public` already avoids the secret; this is the second gate,
    because a single ``"code"`` key added to a payload months from now should fail
    quietly rather than write a live invitation into a durable, exportable chain.
    """
    screened: dict[str, object] = {}
    for key, value in payload.items():
        if key.lower() in _SECRET_KEYS:
            continue
        if isinstance(value, str) and len(value) == CODE_DIGITS and value.isdigit():
            continue
        screened[key] = value
    return screened


__all__ = [
    "ATTEMPT_WINDOW_S",
    "CODE_DIGITS",
    "DEFAULT_CODE_TTL_S",
    "LOCKOUT_S",
    "MAX_ATTEMPTS_PER_WINDOW",
    "PAIRING_MAX_RISK",
    "PairingCode",
    "PairingError",
    "PairingService",
    "format_code",
    "normalize_code",
    "screen_payload",
]
