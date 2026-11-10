"""Append-only, tamper-evident audit log.

Closes the C14 "audit tampering" threat: every entry carries a hash of its own
content and a hash of its predecessor, so editing, deleting or reordering an
entry invalidates the chain from that point on.

Honest limitation, stated up front: a chain stored beside the data it protects
detects accidental or partial tampering, not an attacker who can rewrite the
whole store — that requires anchoring outside the store (an append-only medium,
a signed checkpoint, or write-once storage). The residual risk is recorded as
such in ``docs/security.md`` rather than papered over.

Nothing here logs secrets: ``payload`` is caller-supplied and the log performs
no redaction, so callers must not put credentials in it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any, Protocol

from actionmesh.core.audit.anchor import AuditAnchor, verify_anchor
from actionmesh.protocol.models import GENESIS_HASH, AuditRecord, MeshEvent


class AuditError(RuntimeError):
    """Raised instead of silently truncating or corrupting the audit trail."""


class AuditIntegrityError(AuditError):
    """Raised when persisted history does not verify.

    Separate from :class:`AuditError` so a caller can refuse to start on
    tampered history without conflating it with a capacity problem.
    """


class AuditBackend(Protocol):
    """Durable sink for audit records.

    Deliberately tiny — two methods — so a PostgreSQL or remote-sink backend can
    satisfy it later without touching the chain logic. Implementations must
    *append*: a second write at an existing ``seq`` has to fail loudly rather
    than overwrite history.
    """

    def write(self, record: AuditRecord) -> None: ...

    def read_all(self) -> list[AuditRecord]: ...


#: Every field covered by ``record_hash``. ``record_hash`` itself is excluded so
#: the digest can be recomputed, and nothing else is.
HASHED_FIELDS = (
    "record_id",
    "seq",
    "timestamp",
    "kind",
    "actor",
    "subject",
    "decision",
    "result",
    "reason",
    "payload",
    "prev_hash",
)


def compute_hash(record: AuditRecord) -> str:
    """Deterministic SHA-256 over the record's content.

    Uses ``model_dump()`` so enums and datetimes are already JSON-safe, then
    sorts keys and drops whitespace so the encoding is stable across runs and
    platforms.
    """
    dumped = record.model_dump()
    material = {name: dumped[name] for name in HASHED_FIELDS}
    blob = json.dumps(
        material, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return sha256(blob.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainStatus:
    """Result of ``AuditLog.verify()``. Truthy when the chain is intact."""

    ok: bool
    records: int
    broken_at: int | None = None
    problem: str = ""

    def __bool__(self) -> bool:
        return self.ok


class AuditLog:
    """Append-only hash-chained store of audit records.

    Records are append-only in practice: the public surface exposes a tuple
    view, and ``verify()`` recomputes every hash rather than trusting stored
    values. Reaching ``_records`` from outside is always detected.
    """

    def __init__(
        self,
        max_records: int | None = None,
        backend: AuditBackend | None = None,
    ) -> None:
        if max_records is not None and max_records < 1:
            raise ValueError("max_records must be positive")
        self._max = max_records
        self._backend = backend
        self._records: list[AuditRecord] = []
        #: Optional external checkpoint, used by ``open()`` to detect truncation.
        self._anchor: AuditAnchor | None = None

    @property
    def backend(self) -> AuditBackend | None:
        """The durable sink, when this log is persisted."""
        return self._backend

    @classmethod
    def open(
        cls,
        backend: AuditBackend,
        *,
        require_intact: bool = True,
        anchor: AuditAnchor | None = None,
    ) -> AuditLog:
        """Reopen a persisted chain, refusing to build on tampered history.

        Restoring a chain that does not verify would let an attacker who edited
        the store be silently trusted from the very next append onwards.

        ``anchor`` is an external checkpoint (see :mod:`.anchor`). Without one,
        **tail truncation is undetectable**: dropping the last records leaves a
        valid prefix, and a forged record can be re-anchored onto it. When an
        anchor is supplied and the chain is shorter than (or contradicts) it, an
        :class:`AnchorMismatchError` is raised rather than returning quietly.

        The caller keeps ownership of ``backend`` — including when this raises —
        and is responsible for closing it.
        """
        log = cls(backend=backend)
        log._records = list(backend.read_all())
        status = log.verify()
        if log._records and require_intact and not status:
            raise AuditIntegrityError(
                f"persisted audit chain is broken at seq {status.broken_at}: "
                f"{status.problem} — refusing to append to tampered history"
            )
        # A chain can verify perfectly and still be missing its tail, so the
        # anchor is consulted separately from the hash chain.
        verify_anchor(anchor, len(log._records), log.head)
        log._anchor = anchor
        return log

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        """Immutable view — callers cannot edit history through it."""
        return tuple(self._records)

    @property
    def head(self) -> str:
        """Hash of the newest record, or the genesis value when empty."""
        return self._records[-1].record_hash if self._records else GENESIS_HASH

    def __len__(self) -> int:
        return len(self._records)

    def seal(self) -> tuple[int, str]:
        """Write the current ``(records, head)`` to the external anchor.

        Called on a deliberate cadence (end of a run, a cron job, on shutdown).
        Anchoring is a *decision*, not something to do on every append — an
        anchor rewritten as often as the log sits next to it protects nothing.
        """
        snapshot = (len(self._records), self.head)
        if self._anchor is not None:
            self._anchor.seal(*snapshot)
        return snapshot

    def append(
        self,
        *,
        kind: str,
        actor: str = "system",
        subject: str = "",
        decision: str = "",
        result: str = "",
        reason: str = "",
        payload: dict[str, Any] | None = None,
    ) -> AuditRecord:
        """Append one entry and seal it with the chain hash."""
        if self._max is not None and len(self._records) >= self._max:
            raise AuditError(
                f"audit log is full ({self._max} records); refusing to "
                "truncate an audit trail — raise max_records or rotate "
                "deliberately"
            )
        if not kind:
            raise ValueError("kind is required for an audit record")
        record = AuditRecord(
            seq=len(self._records),
            kind=kind,
            actor=actor,
            subject=subject,
            decision=decision,
            result=result,
            reason=reason,
            payload=dict(payload or {}),
            prev_hash=self.head,
        )
        record.record_hash = compute_hash(record)
        if self._backend is not None:
            # Persist first: if the durable write fails, nothing is recorded
            # anywhere, rather than memory and disk quietly disagreeing.
            self._backend.write(record)
        self._records.append(record)
        return record

    def record_event(self, event: MeshEvent) -> AuditRecord:
        """Chain a published event. Used by ``EventHub(audit=...)``."""
        return self.append(
            kind="event",
            actor=event.actor or "system",
            subject=event.execution_id or event.action_id,
            result=event.event_type,
            payload={
                "event_id": event.event_id,
                "event_type": event.event_type,
                "execution_id": event.execution_id,
                "action_id": event.action_id,
                "data": event.payload,
            },
        )

    def record_decision(
        self, explain: dict[str, Any], actor: str = "system"
    ) -> AuditRecord:
        """Chain the output of ``PolicyEngine.explain()``."""
        reasons = explain.get("reasons") or []
        authorized = bool(explain.get("authorizes_execution"))
        return self.append(
            kind="decision",
            actor=actor,
            subject=str(explain.get("action", "")),
            decision=str(explain.get("decision", "")),
            result="authorized" if authorized else "blocked",
            reason="; ".join(str(item) for item in reasons),
            payload=dict(explain),
        )

    def verify(self) -> ChainStatus:
        """Recompute the whole chain. Any edit, deletion or reorder is found.

        Checks run per record in order: sequence, predecessor link, then
        content. Editing an entry and re-sealing its own hash still fails,
        because the *next* record still points at the old hash.
        """
        records = self._records
        total = len(records)
        for index, record in enumerate(records):
            if record.seq != index:
                return ChainStatus(
                    False, total, index,
                    f"sequence mismatch at {index}: found {record.seq}",
                )
            expected_prev = (
                GENESIS_HASH if index == 0 else records[index - 1].record_hash
            )
            if record.prev_hash != expected_prev:
                return ChainStatus(
                    False, total, index,
                    f"record {index} is not linked to its predecessor",
                )
            if record.record_hash != compute_hash(record):
                return ChainStatus(
                    False, total, index,
                    f"record {index} content does not match its hash",
                )
        return ChainStatus(True, total)

    def reset(self) -> None:
        """Start a new chain. Deliberately explicit — never automatic.

        Refused outright when the log is persisted: erasing a durable chain in
        place destroys the very history it exists to preserve. Open a new store
        instead so the old chain stays auditable.
        """
        if self._backend is not None:
            raise AuditError(
                "refusing to reset a persisted audit chain in place; open a new "
                "store so the existing chain remains auditable"
            )
        self._records.clear()


__all__ = ["HASHED_FIELDS", "AuditError", "AuditLog", "ChainStatus", "compute_hash"]
