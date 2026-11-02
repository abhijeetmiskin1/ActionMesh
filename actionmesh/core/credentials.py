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

from actionmesh.core.identity import IdentityStore
from actionmesh.core.permissions.catalog import is_concrete, permission
from actionmesh.core.permissions.context import PermissionGrant
from actionmesh.core.permissions.risk import risk_rank
from actionmesh.protocol.models import RiskLevel

#: A credential that outlives the session it was minted for is a standing
#: backdoor, so credentials always expire. Thirty days is a phone you keep, not a
#: phone you lose; :meth:`CredentialStore.rotate` renews without re-pairing.
DEFAULT_CREDENTIAL_TTL_S = 30 * 24 * 3600.0


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


class CredentialStore:
    """Bearer credentials bound to C19 identities."""

    def __init__(
        self,
        identities: IdentityStore,
        *,
        key: bytes,
        consents: Callable[[str], Iterable[PermissionGrant]] | None = None,
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
        # Resolving through the identity store is the only admission test. A
        # revoked or unknown identity fails here, before any secret exists.
        identity = self.identities.resolve(identity_id)
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
        return record, token

    def rotate(
        self,
        credential_id: str,
        *,
        max_risk: RiskLevel | None = None,
        ttl_s: float | None = None,
        label: str | None = None,
        now: datetime | None = None,
    ) -> tuple[Credential, str]:
        """Revoke one credential and mint its replacement.

        Rotation is revoked-then-issued rather than issued-then-revoked: if the
        caller fails between the two, the worse outcome is a dead credential, not
        two live ones.
        """
        current = self.get(credential_id)
        if current is None:
            raise CredentialError(f"unknown credential {credential_id!r}")
        self.revoke(credential_id, now=now)
        return self.issue(
            identity_id=current.identity_id,
            actor_id=current.actor_id,
            max_risk=max_risk if max_risk is not None else current.max_risk,
            ttl_s=ttl_s if ttl_s is not None else DEFAULT_CREDENTIAL_TTL_S,
            label=label if label is not None else current.label,
            now=now,
        )

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
        return self.get(credential_id) or record

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

    def authenticate(
        self, token: str, *, now: datetime | None = None
    ) -> Credential:
        """Resolve a presented bearer token, or raise.

        The lookup is a digest comparison against a keyed hash of a 256-bit
        secret: a wrong token produces a different digest and therefore a miss,
        with no prefix in common to time. Failure counts and lockouts are
        pairing's concern (C19c); this method only ever says yes to a credential
        that is live *and* whose identity is live.
        """
        if not isinstance(token, str) or not token.strip():
            raise CredentialError("a bearer token is required")
        digest = hash_secret(token.strip(), key=self._key)
        key = self._by_hash.get(digest)
        record = self._credentials.get(key) if key is not None else None
        if record is None:
            raise CredentialError("unknown credential")
        moment = now or _utcnow()
        if record.revoked:
            raise CredentialError(f"credential {record.credential_id} is revoked")
        if record.is_expired(moment):
            raise CredentialError(f"credential {record.credential_id} has expired")
        # Revoking the *device* has to invalidate its credentials, or forgetting a
        # stolen phone only hides it from the dashboard.
        self.identities.resolve(record.identity_id)
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
        self.identities.resolve(live.identity_id)
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

    # -- storage hook ---------------------------------------------------
    def _write(self, record: Credential) -> None:
        previous = self._credentials.get(record.credential_id)
        if previous is not None:
            self._by_hash.pop(previous.secret_hash, None)
        self._credentials[record.credential_id] = record
        self._by_hash[record.secret_hash] = record.credential_id


__all__ = [
    "DEFAULT_CREDENTIAL_TTL_S",
    "Credential",
    "CredentialError",
    "CredentialStore",
    "hash_secret",
    "new_credential_id",
    "new_token",
]
