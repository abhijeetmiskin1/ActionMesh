"""C19 persistent device identity.

A control plane has to answer one question before it can answer any other:
*which machine* asked for this? Answering it with an IP address, a hostname or a
user-agent string is not an answer — those are attacker-selected values, they
change on every network, and two devices on the same Wi-Fi look identical.

So every device that ActionMesh intends to act on gets an **opaque, randomly
generated identifier** with no relationship to anything the network can observe.
The identifier is the join between a phone, a laptop and an audit record.

Two properties are load-bearing here:

* **Enrollment is not self-service.** :meth:`IdentityStore.enroll` refuses a
  ``device_id`` that already exists. Re-claiming someone else's identifier would
  otherwise be the cheapest way to inherit their history, so rotation is an
  explicit ``revoke`` followed by an enroll — two steps, both auditable.
* **Revocation is durable and checked at use.** A revoked identity is not
  deleted; it keeps its record so an audit trail written under it remains
  attributable, and :meth:`IdentityStore.active_for_device` refuses to hand it
  out again. The store is the only place identity is resolved — a device record
  carrying an ``identity_id`` that does not resolve is treated as unauthenticated
  even though the string is right there.

This module mints identities. It does not issue the secret that *proves* one
(pairing, C19b) and it makes no authorization decision (C3).
"""

from __future__ import annotations

import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from actionmesh.core.sqlite_guard import guarded, make_lock
from actionmesh.protocol.models import DeviceKind, MeshModel


def _utcnow() -> datetime:
    return datetime.now(UTC)


def new_identity_id() -> str:
    """A fresh 256-bit opaque identifier.

    Full-width, not the 48-bit suffix ``new_id`` uses: this value is the only
    thing standing between a presented credential and the identity it claims, so
    a collision is an impersonation rather than a cosmetic clash.
    """
    return f"idn_{secrets.token_hex(16)}"


class IdentityError(ValueError):
    """Raised for an invalid identity operation. Always fails closed."""


@dataclass
class DeviceIdentity(MeshModel):
    """The durable record of *what* a device is.

    Safe to serialize: it carries no secret material. The credential that proves a
    device holds this identity is stored separately and is never dumped.

    Like every AMP model this is a plain (mutable) dataclass — ``MeshModel`` is not
    frozen, and a frozen child of it is a ``TypeError``. Revocation therefore builds
    a **new** record rather than editing this one, which is the behaviour an audit
    trail needs anyway: an identity that could be un-revoked in place would leave no
    trace that it ever was.
    """

    identity_id: str = field(default_factory=new_identity_id)
    device_id: str = ""
    kind: DeviceKind = DeviceKind.UNKNOWN
    label: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    revoked_at: datetime | None = None
    #: Optional expiry. When set, the identity stops resolving at this moment —
    #: fail closed like a revocation, but without needing a revoke call. An
    #: identity with no expiry keeps today's behaviour (revocation only).
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id.strip():
            raise IdentityError("device_id is required")
        if not isinstance(self.kind, DeviceKind):
            raise IdentityError(f"kind must be a DeviceKind, got {self.kind!r}")
        if not isinstance(self.identity_id, str) or not self.identity_id.strip():
            raise IdentityError("identity_id must be a non-empty string")

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


class IdentityStore:
    """In-memory identity ledger. ``SQLiteIdentityStore`` is the durable form."""

    def __init__(self) -> None:
        self._by_identity: dict[str, DeviceIdentity] = {}
        self._by_device: dict[str, str] = {}

    # -- enrollment -------------------------------------------------------
    @guarded
    def enroll(
        self,
        device_id: str,
        kind: DeviceKind,
        *,
        label: str = "",
        now: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> DeviceIdentity:
        """Mint the identity for ``device_id``.

        Raises when that id is already enrolled, including when it was enrolled
        and then revoked — a revoked identity keeps its record precisely so a
        second enrollment cannot quietly inherit the first one's history.
        """
        if not isinstance(device_id, str) or not device_id.strip():
            raise IdentityError("device_id must be a non-empty string")
        if not isinstance(kind, DeviceKind):
            raise IdentityError(f"kind must be a DeviceKind, got {kind!r}")
        if kind is DeviceKind.UNKNOWN:
            raise IdentityError(
                "refusing to enroll a device that has not declared its kind; "
                "an unknown-role device cannot be attributed in an audit"
            )
        cleaned = device_id.strip()
        if cleaned in self._by_device:
            existing = self._by_identity[self._by_device[cleaned]]
            raise IdentityError(
                f"device {cleaned!r} is already enrolled as {existing.identity_id}"
                + (" (revoked)" if existing.revoked else "")
                + "; revoke-then-enroll is deliberately two steps"
            )
        identity = DeviceIdentity(
            device_id=cleaned,
            kind=kind,
            label=label.strip(),
            created_at=now or _utcnow(),
            expires_at=expires_at,
        )
        self._write(identity)
        return identity

    @guarded
    def revoke(
        self, identity_id: str, *, now: datetime | None = None
    ) -> DeviceIdentity:
        """Mark an identity revoked. Idempotent; the record is kept, not deleted."""
        identity = self._by_identity.get(identity_id)
        if identity is None:
            raise IdentityError(f"unknown identity {identity_id!r}")
        if identity.revoked:
            return identity
        renewed = DeviceIdentity(
            identity_id=identity.identity_id,
            device_id=identity.device_id,
            kind=identity.kind,
            label=identity.label,
            created_at=identity.created_at,
            revoked_at=now or _utcnow(),
            expires_at=identity.expires_at,
        )
        self._write(renewed)
        return renewed

    def unbind(self, device_id: str) -> bool:
        """Release ``device_id`` so a revoked/expired identity can be replaced.

        This is the second half of the "revoke-then-enroll is deliberately two
        steps" rule: an identity record is never deleted (the audit trail
        written under it must stay attributable), but the device_id -> identity_id
        binding is what blocks re-enrollment, and dropping that binding after a
        revocation lets a *new* identity — with a new audit trail of its own —
        take the device_id. The old identity keeps its record and still fails
        :meth:`resolve`.

        Returns False when the device had no binding. Raises rather than
        unbinding a live identity: unbinding an active identity would leave a
        device whose credentials still authenticate against an identity no
        binding points at.
        """
        identity = self.for_device(device_id)
        if identity is None:
            return False
        if identity.is_active():
            raise IdentityError(
                f"refusing to unbind live identity {identity.identity_id!r}; "
                "revoke the identity before replacing it"
            )
        self._by_device.pop(device_id, None)
        return True

    # -- resolution -------------------------------------------------------
    def get(self, identity_id: str) -> DeviceIdentity | None:
        return self._by_identity.get(identity_id)

    def for_device(self, device_id: str) -> DeviceIdentity | None:
        key = self._by_device.get(device_id)
        return self._by_identity[key] if key is not None else None

    def resolve(
        self, identity_id: str, *, now: datetime | None = None
    ) -> DeviceIdentity:
        """Resolve an identity, refusing anything not active.

        This is the only entry point a security decision should use. A record
        that exists but is revoked is *not* a match: ``identity_id`` strings are
        attacker-visible once they appear in an audit export, so existence is
        never treated as validity.
        """
        identity = self._by_identity.get(identity_id)
        if identity is None:
            raise IdentityError(f"unknown identity {identity_id!r}")
        if identity.revoked:
            raise IdentityError(f"identity {identity_id!r} is revoked")
        if identity.is_expired(now):
            raise IdentityError(f"identity {identity_id!r} has expired")
        return identity

    def active_for_device(self, device_id: str) -> DeviceIdentity:
        identity = self.for_device(device_id)
        if identity is None:
            raise IdentityError(f"device {device_id!r} is not enrolled")
        return self.resolve(identity.identity_id)

    # -- enumeration ------------------------------------------------------
    def identities(self, *, include_revoked: bool = True) -> list[DeviceIdentity]:
        rows = sorted(self._by_identity.values(), key=lambda i: i.identity_id)
        if include_revoked:
            return rows
        return [row for row in rows if not row.revoked]

    def __len__(self) -> int:
        return len(self._by_identity)

    # -- storage hook ---------------------------------------------------
    def _write(self, identity: DeviceIdentity) -> None:
        self._by_identity[identity.identity_id] = identity
        self._by_device[identity.device_id] = identity.identity_id


def _decode(payload: str) -> DeviceIdentity:
    raw: dict[str, object] = json.loads(payload)
    return DeviceIdentity(
        identity_id=str(raw["identity_id"]),
        device_id=str(raw["device_id"]),
        kind=DeviceKind(str(raw["kind"])),
        label=str(raw.get("label", "")),
        created_at=datetime.fromisoformat(str(raw["created_at"])),
        revoked_at=(
            datetime.fromisoformat(str(raw["revoked_at"]))
            if raw.get("revoked_at")
            else None
        ),
        expires_at=(
            datetime.fromisoformat(str(raw["expires_at"]))
            if raw.get("expires_at")
            else None
        ),
    )


class SQLiteIdentityStore(IdentityStore):
    """Durable identity ledger with the same validation and API.

    Identities must outlive the process: a laptop that re-mints an identity on
    every restart has no identity, it has a habit. Revoked rows are kept on
    purpose — the audit entries written under them still need to resolve.
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        super().__init__()
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = make_lock()
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS device_identities "
            "(identity_id TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE, "
            "payload TEXT NOT NULL)"
        )
        self.connection.commit()
        #: Identity ids whose bindings were durably released via :meth:`unbind`.
        #: Kept so a later :meth:`_write` (e.g. a revoke of an already-dead
        #: identity) cannot resurrect the binding from its ``device_id`` field.
        self._unbound: set[str] = set()
        for row in self.connection.execute(
            "SELECT payload FROM device_identities"
        ).fetchall():
            identity = _decode(row[0])
            if json.loads(row[0]).get("unbound"):
                # Record only — no device_id -> identity_id binding restored.
                self._by_identity[identity.identity_id] = identity
                self._unbound.add(identity.identity_id)
            else:
                super()._write(identity)

    def _write(self, identity: DeviceIdentity) -> None:
        payload = json.loads(identity.model_dump_json())
        if identity.identity_id in self._unbound:
            payload["unbound"] = True
        self.connection.execute(
            "INSERT OR REPLACE INTO device_identities"
            "(identity_id, device_id, payload) VALUES (?, ?, ?)",
            (
                identity.identity_id,
                (
                    f"__unbound:{identity.identity_id}"
                    if identity.identity_id in self._unbound
                    else identity.device_id
                ),
                json.dumps(payload),
            ),
        )
        self.connection.commit()
        self._by_identity[identity.identity_id] = identity
        if identity.identity_id not in self._unbound:
            self._by_device[identity.device_id] = identity.identity_id

    @guarded
    def close(self) -> None:
        self.connection.close()

    @guarded
    def unbind(self, device_id: str) -> bool:
        """Durable form of the device -> identity binding drop.

        The in-memory base only pops ``self._by_device``; the row in
        ``device_identities`` keeps the (still-bound) payload so a restart
        would replay it and resurrect the very binding :meth:`unbind` dropped.
        That resurrection is the Phase-4 gap this override closes: after a
        restart the device would again read as "already enrolled as <revoked
        identity>" and the operator's re-enrollment after revoke+unbind would
        be refused forever.

        The binding is released durably: the row's payload gains
        ``unbound: true`` (the replay records the identity for audit
        attribution but does not restore ``_by_device``), and the SQL
        ``device_id`` column is renamed to a dead-namespace sentinel so a
        later write of the dead identity — a revoke, say — cannot collide
        with the replacement's row under the table's ``UNIQUE(device_id)``. The record itself is preserved and
        :meth:`resolve` still refuses it.
        """
        identity = self.for_device(device_id)
        if identity is None:
            return False
        if identity.is_active():
            raise IdentityError(
                f"refusing to unbind live identity {identity.identity_id!r}; "
                "revoke the identity before replacing it"
            )
        stored = self.connection.execute(
            "SELECT payload FROM device_identities WHERE identity_id = ?",
            (identity.identity_id,),
        ).fetchone()
        if stored is None or not json.loads(stored[0]).get("unbound"):
            payload = json.loads(stored[0]) if stored else identity.model_dump()
        payload["unbound"] = True
        self.connection.execute(
            "UPDATE device_identities SET payload = ?, device_id = ? "
            "WHERE identity_id = ?",
            (json.dumps(payload), f"__unbound:{identity.identity_id}", identity.identity_id),
        )
        self.connection.commit()
        self._by_device.pop(device_id, None)
        self._unbound.add(identity.identity_id)
        return True


__all__ = [
    "DeviceIdentity",
    "IdentityError",
    "IdentityStore",
    "SQLiteIdentityStore",
    "new_identity_id",
]
