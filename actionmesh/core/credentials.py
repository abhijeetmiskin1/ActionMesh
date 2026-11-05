"""C19b credentials: the secret that proves a device holds an identity.

Pairing (C19c) decides *when* a credential is minted. This module owns what one
**is**, and the difference matters for one question in particular: what may a
paired phone ask for?

The answer is **nothing, on the strength of pairing alone**. A credential binds
three things together — an identity, an actor, and the device that actor controls
— and carries a risk ceiling. It deliberately carries **no list of permissions**.
The reason is the same invariant the whole project rests on: consent comes from the
`ConsentStore` and nowhere else. If a pairing response could hand back a set of
scopes, pairing would have become a second authorization system, and §17's warning
that the agent "must not become a second policy engine" would apply one layer too
far up the stack. A paired phone is an *identified* phone, not an *authorized* one.

A credential is therefore:

* **Opaque and wide.** 256 bits, generated, never derived from a device id or a
  timestamp, so two credentials for the same device cannot be predicted from each
  other.
* **Stored as a keyed digest.** The plaintext is returned exactly once by
  :meth:`CredentialStore.issue` and kept nowhere else — not in a record, not in a
  log, not in an audit payload.
* **Valid only while its identity is.** Authentication resolves through the
  identity store, so revoking a device (a lost phone, a decommissioned laptop)
  invalidates every credential minted for it without having to find them first.
  Forget-one-get-one-free is not a property you want in a revocation system.
* **Capped by its owner.** The ceiling is copied from the identity owner's consent
  ceiling when one exists, and :meth:`CredentialStore.grants_for` will not release
  a grant above what the actor actually consented to — a credential cannot
  authorize more than the person holding it agreed to.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from actionmesh.core.identity import IdentityError, IdentityStore
from actionmesh.core.permissions.catalog import is_concrete, permission
from actionmesh.core.permissions.context import PermissionGrant
from actionmesh.core.permissions.matching import scope_satisfied
from actionmesh.core.permissions.risk import max_risk, risk_rank
from actionmesh.protocol.models import ApprovalRequest, RiskLevel

#: A credential that outlives the session it was minted for is a standing
#: backdoor, so credentials always expire. Thirty days is a phone you keep, not a
#: phone you lose; :meth:`CredentialStore.rotate` renews without re-pairing.
DEFAULT_CREDENTIAL_TTL_S = 30 * 24 * 3600.0

#: Credentials expiring within this window are reported by ``expires_soon`` so an
#: operator can rotate before a phone with a credential is stranded mid-approval.
EXPIRY_WARNING_S = 24 * 3600.0

#: Payload keys that would name a secret rather than describe an event. Anything
#: reaching an audit sink is screened through this set (belt and braces next to
#: never putting a token in a payload in the first place).
_SECRET_PAYLOAD_KEYS = frozenset(
    {"code", "pair_code", "secret", "secret_hash", "token", "plaintext", "pin"}
)


def _looks_like_secret(value: object) -> bool:
    """True for long opaque strings that are shaped like bearer material.

    Keys are the first screen; this is the second. A bearer token from
    :func:`new_token` is ~43 url-safe characters with no whitespace, and a
    keyed digest is 64 hex characters. Anything at that length, with no
    whitespace and at least one digit and one letter, is indistinguishable
    from the material this function exists to keep out of audit — and human
    labels, ids and hostnames pass through untouched because they are shorter
    or contain spaces/punctuation that splits them into words.
    """
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 40 or any(ch.isspace() for ch in text):
        return False
    return any(ch.isdigit() for ch in text) and any(ch.isalpha() for ch in text)


def screen_credential_payload(payload: dict[str, object]) -> dict[str, object]:
    """Drop anything that could be a bearer token before it reaches an audit sink.

    Two screens: secret-shaped *keys* (the original, keyed on
    ``_SECRET_PAYLOAD_KEYS``) and, in defence in depth, secret-shaped
    *values* under any other key — a future field named ``session_note``
    should not be able to carry a bearer token into audit. Nested dicts are
    screened recursively; a dict that carries a secret anywhere inside is
    dropped wholesale rather than partially retained, so a structured payload
    cannot smuggle material past the top level.
    """
    screened: dict[str, object] = {}
    for key, value in payload.items():
        if key.lower() in _SECRET_PAYLOAD_KEYS:
            continue
        if _looks_like_secret(value):
            continue
        if isinstance(value, dict):
            nested = screen_credential_payload(value)
            if nested == {} and value:
                # Either the dict carried a secret (dropped above) or it was
                # empty to begin with; drop any non-empty dict whose entire
                # contents screened out.
                continue
            screened[key] = nested
            continue
        screened[key] = value
    return screened


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_token() -> str:
    """A fresh 256-bit bearer secret, returned to the caller once and never stored."""
    return secrets.token_urlsafe(32)


def hash_secret(secret: str, *, key: bytes) -> str:
    """Keyed digest of a secret, for storage and lookup.

    For a 256-bit token a plain digest would do: nothing can be brute-forced
    against it. The key earns its place on the *pairing codes*, which are short
    and typed by hand — without a key, anyone holding a copy of the store can
    compute the digest of every guess and read the live codes.
    """
    if not key:
        raise ValueError("a non-empty key is required to hash a secret")
    return hmac.new(key, secret.encode("utf-8"), hashlib.sha256).hexdigest()


class CredentialError(ValueError):
    """Raised for an invalid credential operation. Always fails closed."""


@dataclass
class Credential:
    """Proof of possession, bound to one identity and one actor.

    Not a ``MeshModel``: it carries the digest, and a class that knows how to
    serialize itself is one ``model_dump()`` call away from serializing that.
    Use :meth:`public` for anything a client may see.
    """

    credential_id: str
    identity_id: str
    device_id: str
    actor_id: str
    secret_hash: str
    max_risk: RiskLevel = RiskLevel.LOW
    issued_at: datetime = field(default_factory=_utcnow)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    label: str = ""

    def __post_init__(self) -> None:
        for required in ("credential_id", "identity_id", "device_id", "actor_id"):
            value = getattr(self, required)
            if not isinstance(value, str) or not value.strip():
                raise CredentialError(f"{required} is required")
        if not isinstance(self.secret_hash, str) or len(self.secret_hash) < 32:
            raise CredentialError("a credential must carry a digest, not a plaintext")
        if not isinstance(self.max_risk, RiskLevel):
            raise CredentialError(f"max_risk must be a RiskLevel, got {self.max_risk!r}")

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        return (now or _utcnow()) >= self.expires_at

    def is_active(self, now: datetime | None = None) -> bool:
        moment = now or _utcnow()
        return not self.revoked and not self.is_expired(moment)

    def time_to_expiry(self, now: datetime | None = None) -> float | None:
        """Seconds until expiry, or ``None`` when the credential never expires."""
        if self.expires_at is None:
            return None
        return (self.expires_at - (now or _utcnow())).total_seconds()

    def expires_soon(self, within_s: float = EXPIRY_WARNING_S, *, now: datetime | None = None) -> bool:
        remaining = self.time_to_expiry(now)
        return remaining is not None and 0 <= remaining <= within_s

    def is_stale(self, max_age_s: float, *, now: datetime | None = None) -> bool:
        """True when a live credential is older than the caller's freshness bound.

        A credential that has been in circulation for months is a token that has
        had months to leak; long-lived authentication should re-prove itself.
        """
        return ((now or _utcnow()) - self.issued_at).total_seconds() > max_age_s

    def to_grant(self) -> PermissionGrant:
        """This credential's own ceiling, as a grant-shaped object.

        Never used on its own: :meth:`CredentialStore.grants_for` intersects it
        with what the actor actually consented to.
        """
        return PermissionGrant(
            scope="",
            granted_by=self.actor_id,
            max_risk=self.max_risk,
            granted_at=self.issued_at,
            expires_at=self.expires_at,
        )

    def public(self) -> dict[str, Any]:
        """The safe view: what a credential is, never what it proves."""
        return {
            "credential_id": self.credential_id,
            "identity_id": self.identity_id,
            "device_id": self.device_id,
            "actor_id": self.actor_id,
            "max_risk": self.max_risk.value,
            "issued_at": self.issued_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked": self.revoked,
            "label": self.label,
        }


def new_credential_id() -> str:
    return f"cred_{secrets.token_hex(8)}"


@dataclass(frozen=True)
class CredentialAuthorization:
    """The verdict of :meth:`CredentialStore.authorize`. Never executes anything.

    ``allowed`` is True only when the credential is live, bound to the expected
    device/actor, holds an active grant for exactly ``scope`` at or above the
    required risk, and (for CRITICAL) a per-``action_id`` approval was presented.
    A grant alone never satisfies CRITICAL: ``requires_approval`` is True when
    the risk gate is met by consent but a fresh per-action approval is still
    needed, and ``allowed`` is False in that state.
    """

    allowed: bool
    requires_approval: bool
    reasons: tuple[str, ...]


class CredentialStore:
    """Bearer credentials bound to C19 identities."""

    def __init__(
        self,
        identities: IdentityStore,
        *,
        key: bytes,
        consents: Callable[[str], Iterable[PermissionGrant]] | None = None,
        audit: Callable[[str, str, dict[str, object]], None] | None = None,
    ) -> None:
        if not key:
            raise CredentialError(
                "a pairing/credential key is required; refusing to run with an "
                "empty secret because a store keyed by b'' is a store with no key"
            )
        self.identities = identities
        self._key = key
        self._credentials: dict[str, Credential] = {}
        self._by_hash: dict[str, str] = {}
        #: Hook to the consent store, so a credential can never outrank the
        #: consent its actor already gave. Injected to keep this module free of a
        #: second authorization path.
        self._consents = consents
        #: Called with (event, subject, screened_payload). Events describe the
        #: lifecycle (issued/revoked/rotated/auth_failed/...) and never carry the
        #: token, its digest, or any secret material.
        self._audit = audit

    def _record(self, event: str, subject: str, payload: dict[str, object]) -> None:
        if self._audit is None:
            return
        self._audit(event, subject, screen_credential_payload(payload))

    # -- issuing ----------------------------------------------------------
    def issue(
        self,
        *,
        identity_id: str,
        actor_id: str,
        max_risk: RiskLevel = RiskLevel.LOW,
        ttl_s: float = DEFAULT_CREDENTIAL_TTL_S,
        label: str = "",
        now: datetime | None = None,
    ) -> tuple[Credential, str]:
        """Mint a credential. Returns ``(record, plaintext_token)``.

        The token exists in this function's return value and nowhere else. Callers
        put it in one response body and then forget it.
        """
        moment = now or _utcnow()
        if not isinstance(max_risk, RiskLevel):
            raise CredentialError(f"max_risk must be a RiskLevel, got {max_risk!r}")
        if ttl_s <= 0:
            raise CredentialError("ttl_s must be positive; a never-expiring credential is a leak")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise CredentialError(
                "actor_id is required: a credential that names no principal cannot be audited"
            )
        # A credential can never be minted above the consent its actor actually
        # gave. Without this, issue() would be the privilege-escalation surface
        # grants_for() later has to defend against; fail here instead.
        ceiling = self._consent_ceiling(actor_id.strip(), moment=moment)
        if risk_rank(max_risk) > risk_rank(ceiling):
            raise CredentialError(
                f"refusing to mint a credential above the actor's consented "
                f"ceiling ({ceiling.value})"
            )
        # Resolving through the identity store is the only admission test. A
        # revoked or unknown identity fails here, before any secret exists.
        identity = self.identities.resolve(identity_id, now=moment)
        token = new_token()
        record = Credential(
            credential_id=new_credential_id(),
            identity_id=identity.identity_id,
            device_id=identity.device_id,
            actor_id=actor_id.strip(),
            secret_hash=hash_secret(token, key=self._key),
            max_risk=max_risk,
            issued_at=moment,
            expires_at=moment + timedelta(seconds=ttl_s),
            label=label.strip(),
        )
        self._write(record)
        self._record(
            "credential.issued",
            record.credential_id,
            {
                "credential_id": record.credential_id,
                "identity_id": record.identity_id,
                "device_id": record.device_id,
                "actor_id": record.actor_id,
                "max_risk": record.max_risk.value,
                "expires_at": record.expires_at.isoformat() if record.expires_at else None,
            },
        )
        return record, token

    def _consent_ceiling(self, actor_id: str, *, moment: datetime) -> RiskLevel:
        """The highest risk this actor has actually consented to (LOW if none).

        Used to cap what ``issue``/``rotate`` may mint. When no consent hook is
        configured there is nothing to cap against — but a paired credential is
        not an authorized one, so LOW is the safe floor, matching pairing's own
        default.
        """
        ceiling = RiskLevel.LOW
        if self._consents is not None:
            for grant in self._consents(actor_id):
                if not isinstance(grant, PermissionGrant):
                    continue
                if not isinstance(grant.scope, str) or not is_concrete(grant.scope):
                    continue
                if grant.is_expired(moment):
                    continue
                ceiling = max_risk(ceiling, grant.max_risk)
        return ceiling

    def rotate(
        self,
        credential_id: str,
        *,
        max_risk: RiskLevel | None = None,
        ttl_s: float | None = None,
        label: str | None = None,
        now: datetime | None = None,
        require_token: str | None = None,
    ) -> tuple[Credential, str]:
        """Revoke one credential and mint its replacement.

        Rotation is revoked-then-issued rather than issued-then-revoked: if the
        caller fails between the two, the worse outcome is a dead credential, not
        two live ones.

        A revoked or expired credential can never be rotated back to life:
        revocation is terminal for the token, and ``rotate`` on one raises rather
        than silently restoring revoked authority. Rotation also never raises the
        ceiling — ``max_risk`` above the current credential's ceiling is refused,
        and :meth:`issue` re-checks it against the actor's consent ceiling.

        ``require_token``, when given, must authenticate as this very credential:
        possession of the id alone is not proof the caller holds the token.
        """
        moment = now or _utcnow()
        current = self.get(credential_id)
        if current is None:
            raise CredentialError(f"unknown credential {credential_id!r}")
        if current.revoked:
            raise CredentialError(f"credential {credential_id} is revoked; re-pair to reconnect")
        if current.is_expired(moment):
            raise CredentialError(f"credential {credential_id} has expired; re-pair to reconnect")
        if require_token is not None:
            proven = self.authenticate(require_token, now=moment)
            if proven.credential_id != credential_id:
                raise CredentialError("token does not prove possession of this credential")
        requested = max_risk if max_risk is not None else current.max_risk
        if not isinstance(requested, RiskLevel):
            raise CredentialError(f"max_risk must be a RiskLevel, got {requested!r}")
        if risk_rank(requested) > risk_rank(current.max_risk):
            raise CredentialError(
                "rotation cannot raise a credential's risk ceiling; "
                "minting a higher-ceiling credential requires a fresh pairing"
            )
        self.revoke(credential_id, now=moment)
        replacement, token = self.issue(
            identity_id=current.identity_id,
            actor_id=current.actor_id,
            max_risk=requested,
            ttl_s=ttl_s if ttl_s is not None else DEFAULT_CREDENTIAL_TTL_S,
            label=label if label is not None else current.label,
            now=moment,
        )
        self._record(
            "credential.rotated",
            credential_id,
            {
                "credential_id": credential_id,
                "replacement_credential_id": replacement.credential_id,
                "identity_id": replacement.identity_id,
                "device_id": replacement.device_id,
                "actor_id": replacement.actor_id,
                "max_risk": replacement.max_risk.value,
            },
        )
        return replacement, token

    def revoke(self, credential_id: str, *, now: datetime | None = None) -> Credential:
        record = self.get(credential_id)
        if record is None:
            raise CredentialError(f"unknown credential {credential_id!r}")
        if record.revoked:
            return record
        self._write(
            Credential(
                credential_id=record.credential_id,
                identity_id=record.identity_id,
                device_id=record.device_id,
                actor_id=record.actor_id,
                secret_hash=record.secret_hash,
                max_risk=record.max_risk,
                issued_at=record.issued_at,
                expires_at=record.expires_at,
                revoked_at=now or _utcnow(),
                label=record.label,
            )
        )
        revoked = self.get(credential_id) or record
        self._record(
            "credential.revoked",
            credential_id,
            {
                "credential_id": credential_id,
                "identity_id": record.identity_id,
                "device_id": record.device_id,
                "actor_id": record.actor_id,
            },
        )
        return revoked

    def revoke_for_identity(self, identity_id: str, *, now: datetime | None = None) -> int:
        """Revoke every live credential bound to one identity. Returns the count.

        The complement of identity revocation: ``authenticate`` already fails
        closed through :meth:`IdentityStore.resolve`, but the credential records
        themselves stay ``is_active()`` until they are revoked here — so a
        forgotten device would otherwise still look live to any consumer that
        reads the store directly.
        """
        moment = now or _utcnow()
        count = 0
        for record in self.for_identity(identity_id):
            if not record.revoked:
                self.revoke(record.credential_id, now=moment)
                count += 1
        self._record(
            "credential.revoked_for_identity",
            identity_id,
            {"identity_id": identity_id, "revoked": count},
        )
        return count

    def revoke_for_actor(self, actor_id: str, *, now: datetime | None = None) -> int:
        """Revoke every live credential held by one actor. Returns the count."""
        moment = now or _utcnow()
        count = 0
        for record in sorted(self._credentials.values(), key=lambda c: c.credential_id):
            if record.actor_id == actor_id and not record.revoked:
                self.revoke(record.credential_id, now=moment)
                count += 1
        self._record(
            "credential.revoked_for_actor",
            actor_id,
            {"actor_id": actor_id, "revoked": count},
        )
        return count

    # -- lookup -----------------------------------------------------------
    def get(self, credential_id: str) -> Credential | None:
        return self._credentials.get(credential_id)

    def for_identity(self, identity_id: str) -> list[Credential]:
        return sorted(
            (c for c in self._credentials.values() if c.identity_id == identity_id),
            key=lambda c: c.credential_id,
        )

    def credentials(self, *, include_revoked: bool = True) -> list[Credential]:
        rows = sorted(self._credentials.values(), key=lambda c: c.credential_id)
        if include_revoked:
            return rows
        return [row for row in rows if row.is_active()]

    def expiring_soon(
        self, within_s: float = EXPIRY_WARNING_S, *, now: datetime | None = None
    ) -> list[Credential]:
        """Live credentials expiring within ``within_s`` seconds, soonest first.

        A query only — nothing is revoked or refreshed here. Rotation is the
        operator's explicit act, so an expiry warning can never silently
        extend a credential's life.
        """
        if within_s < 0:
            raise CredentialError("within_s must not be negative")
        moment = now or _utcnow()
        return sorted(
            (
                record
                for record in self._credentials.values()
                if record.is_active(moment)
                and record.expires_at is not None
                and record.expires_at - moment <= timedelta(seconds=within_s)
            ),
            key=lambda record: (record.expires_at or moment, record.credential_id),
        )

    def authenticate(
        self,
        token: str,
        *,
        now: datetime | None = None,
        expected_device_id: str | None = None,
        expected_actor_id: str | None = None,
        max_age_s: float | None = None,
    ) -> Credential:
        """Resolve a presented bearer token, or raise.

        The lookup is a digest comparison against a keyed hash of a 256-bit
        secret: a wrong token produces a different digest and therefore a miss,
        with no prefix in common to time. Failure counts and lockouts are
        pairing's concern (C19c); this method only ever says yes to a credential
        that is live *and* whose identity is live.

        ``expected_device_id``/``expected_actor_id`` pin the credential to the
        principal the caller is acting for: a token minted for one device or
        actor presented for another is a scope mismatch, not a success.
        ``max_age_s`` bounds how long a credential may authenticate without
        re-proof (staleness). Every failure is audited with a reason code and
        never with the token.
        """
        moment = now or _utcnow()
        if not isinstance(token, str) or not token.strip():
            self._record(
                "credential.auth_failed",
                "unknown",
                {"reason": "empty_token"},
            )
            raise CredentialError("a bearer token is required")
        digest = hash_secret(token.strip(), key=self._key)
        key = self._by_hash.get(digest)
        record = self._credentials.get(key) if key is not None else None
        if record is None:
            self._record(
                "credential.auth_failed", "unknown", {"reason": "unknown"}
            )
            raise CredentialError("unknown credential")
        if record.revoked:
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "revoked"},
            )
            raise CredentialError(f"credential {record.credential_id} is revoked")
        if record.is_expired(moment):
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "expired"},
            )
            raise CredentialError(f"credential {record.credential_id} has expired")
        # Revoking the *device* has to invalidate its credentials, or forgetting a
        # stolen phone only hides it from the dashboard.
        try:
            self.identities.resolve(record.identity_id, now=moment)
        except IdentityError:
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "identity_revoked"},
            )
            raise
        if expected_device_id is not None and record.device_id != expected_device_id:
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "device_mismatch"},
            )
            raise CredentialError("credential is not bound to this device")
        if expected_actor_id is not None and record.actor_id != expected_actor_id:
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "actor_mismatch"},
            )
            raise CredentialError("credential is not bound to this actor")
        if max_age_s is not None and max_age_s >= 0 and record.is_stale(max_age_s, now=moment):
            self._record(
                "credential.auth_failed",
                record.credential_id,
                {"credential_id": record.credential_id, "reason": "stale"},
            )
            raise CredentialError(f"credential {record.credential_id} requires re-authentication")
        return record

    def grants_for(
        self, credential: Credential, *, now: datetime | None = None
    ) -> list[PermissionGrant]:
        """Consents this credential may act on — never more than its actor gave.

        Intersecting with the actor's own consent ceiling is what keeps a credential
        from becoming a permission grant: pairing can only ever narrow what is
        already available to that actor, never widen it.

        The record is re-read from the store rather than trusted as handed over. A
        caller holding a ``Credential`` from before it was revoked or expired would
        otherwise keep using an object that says it is live; the object is a receipt,
        not the state.
        """
        moment = now or _utcnow()
        live = self._credentials.get(credential.credential_id)
        if live is None or not live.is_active(moment):
            return []
        try:
            self.identities.resolve(live.identity_id, now=moment)
        except IdentityError:
            return []
        if self._consents is None:
            return []
        out: list[PermissionGrant] = []
        for grant in self._consents(live.actor_id):
            if not isinstance(grant.scope, str) or not is_concrete(grant.scope):
                continue
            # Both ceilings apply: the credential's and the consent's.
            if risk_rank(live.max_risk) < risk_rank(grant.max_risk):
                capped = PermissionGrant(
                    scope=grant.scope,
                    granted_by=grant.granted_by,
                    max_risk=live.max_risk,
                    granted_at=grant.granted_at,
                    expires_at=grant.expires_at,
                )
                if capped.is_active(moment, permission(grant.scope).risk_level):
                    out.append(capped)
            elif grant.is_active(moment):
                out.append(grant)
        return sorted(out, key=lambda g: g.scope)

    # -- authorization ------------------------------------------------------
    def authorize(
        self,
        credential: Credential,
        scope: str,
        *,
        risk: RiskLevel | None = None,
        action_id: str = "",
        approvals: Iterable[ApprovalRequest] = (),
        now: datetime | None = None,
    ) -> CredentialAuthorization:
        """Decide whether this credential may act on ``scope`` at ``risk``.

        This is a *checker*, not a second policy engine: it re-reads the live
        credential (a handed-over object is a receipt, not state), intersects
        it with the actor's consents via :meth:`grants_for`, and requires an
        exact scope match at or above the required risk. The credential's own
        ceiling caps the consent — so a bearer token can never self-grant
        CRITICAL, and a grant is never an approval: CRITICAL (and any action
        whose effective risk is CRITICAL) additionally requires an approved
        :class:`ApprovalRequest` for this exact ``action_id``, which is not
        reusable across actions.
        """
        moment = now or _utcnow()
        live = self._credentials.get(credential.credential_id)
        if live is None:
            return CredentialAuthorization(False, False, ("unknown credential",))
        if live.revoked:
            return CredentialAuthorization(False, False, ("credential is revoked",))
        if live.is_expired(moment):
            return CredentialAuthorization(False, False, ("credential has expired",))
        try:
            self.identities.resolve(live.identity_id, now=moment)
        except IdentityError:
            return CredentialAuthorization(False, False, ("credential identity is revoked",))
        if not isinstance(scope, str) or not is_concrete(scope):
            return CredentialAuthorization(False, False, (f"unknown scope {scope!r}",))
        required_risk = risk if risk is not None else permission(scope).risk_level
        if not isinstance(required_risk, RiskLevel):
            return CredentialAuthorization(
                False, False, ("required risk must be a RiskLevel",)
            )
        # The credential's ceiling is a hard cap on what it may even ask for.
        if risk_rank(required_risk) > risk_rank(live.max_risk):
            return CredentialAuthorization(
                False,
                False,
                (
                    f"required risk {required_risk.value} exceeds credential "
                    f"ceiling {live.max_risk.value}",
                ),
            )
        granted = any(
            scope_satisfied(grant.scope, scope) and grant.is_active(moment, required_risk)
            for grant in self.grants_for(live, now=moment)
        )
        if not granted:
            return CredentialAuthorization(
                False,
                False,
                (f"no active consent for {scope} at {required_risk.value}",),
            )
        if required_risk is RiskLevel.CRITICAL:
            if not action_id:
                return CredentialAuthorization(
                    False,
                    True,
                    (
                        "CRITICAL risk always requires explicit per-action approval "
                        "tied to an action_id; a grant is not an approval",
                    ),
                )
            approved = any(
                approval.action_id == action_id
                and approval.status == "approved"
                and not approval.is_expired(moment)
                for approval in approvals
            )
            if not approved:
                return CredentialAuthorization(
                    False,
                    True,
                    (
                        f"no approved approval request for action {action_id!r}; "
                        "a grant is not an approval",
                    ),
                )
        return CredentialAuthorization(True, False, ("authorized",))

    # -- storage hook ---------------------------------------------------
    def _write(self, record: Credential) -> None:
        previous = self._credentials.get(record.credential_id)
        if previous is not None:
            self._by_hash.pop(previous.secret_hash, None)
        self._credentials[record.credential_id] = record
        self._by_hash[record.secret_hash] = record.credential_id


__all__ = [
    "DEFAULT_CREDENTIAL_TTL_S",
    "EXPIRY_WARNING_S",
    "Credential",
    "CredentialAuthorization",
    "CredentialError",
    "CredentialStore",
    "hash_secret",
    "new_credential_id",
    "new_token",
    "screen_credential_payload",
]
