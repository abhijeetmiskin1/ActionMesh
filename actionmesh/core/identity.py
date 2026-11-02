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


class IdentityStore:
    """In-memory identity ledger. ``SQLiteIdentityStore`` is the durable form."""

    def __init__(self) -> None:
        self._by_identity: dict[str, DeviceIdentity] = {}
        self._by_device: dict[str, str] = {}

    # -- enrollment -------------------------------------------------------
    def enroll(
        self,
        device_id: str,
        kind: DeviceKind,
        *,
        label: str = "",
        now: datetime | None = None,
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
        )
        self._write(identity)
        return identity

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
        )
        self._write(renewed)
        return renewed

    # -- resolution -------------------------------------------------------
    def get(self, identity_id: str) -> DeviceIdentity | None:
        return self._by_identity.get(identity_id)

    def for_device(self, device_id: str) -> DeviceIdentity | None:
        key = self._by_device.get(device_id)
        return self._by_identity[key] if key is not None else None

    def resolve(self, identity_id: str) -> DeviceIdentity:
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
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS device_identities "
            "(identity_id TEXT PRIMARY KEY, device_id TEXT NOT NULL UNIQUE, "
            "payload TEXT NOT NULL)"
        )
        self.connection.commit()
        for row in self.connection.execute(
            "SELECT payload FROM device_identities"
        ).fetchall():
            identity = _decode(row[0])
            super()._write(identity)

    def _write(self, identity: DeviceIdentity) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO device_identities"
            "(identity_id, device_id, payload) VALUES (?, ?, ?)",
            (identity.identity_id, identity.device_id, identity.model_dump_json()),
        )
        self.connection.commit()
        super()._write(identity)

    def close(self) -> None:
        self.connection.close()


__all__ = [
    "DeviceIdentity",
    "IdentityError",
    "IdentityStore",
    "SQLiteIdentityStore",
    "new_identity_id",
]
