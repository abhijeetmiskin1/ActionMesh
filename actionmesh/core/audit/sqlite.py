"""Durable SQLite backend for the audit chain.

Standard library only, matching ``actionmesh.core.storage.SQLiteStore``. The
schema is plain relational SQL with ``seq`` as the primary key, so moving to
PostgreSQL later is a driver change rather than a redesign.

Append-only is enforced by the schema, not by convention: ``seq`` is a PRIMARY
KEY, so a second write at a position that already exists raises rather than
overwriting history.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from actionmesh.protocol.models import AuditRecord

_COLUMNS = (
    "seq, record_id, timestamp, kind, actor, subject, decision, result, "
    "reason, payload, prev_hash, record_hash"
)


class SqliteAuditBackend:
    """Append-only durable sink for :class:`AuditRecord`."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS audit_records ("
            "seq INTEGER PRIMARY KEY, "
            "record_id TEXT NOT NULL UNIQUE, "
            "timestamp TEXT NOT NULL, "
            "kind TEXT NOT NULL, "
            "actor TEXT NOT NULL, "
            "subject TEXT NOT NULL, "
            "decision TEXT NOT NULL, "
            "result TEXT NOT NULL, "
            "reason TEXT NOT NULL, "
            "payload TEXT NOT NULL, "
            "prev_hash TEXT NOT NULL, "
            "record_hash TEXT NOT NULL)"
        )
        self.connection.commit()

    def write(self, record: AuditRecord) -> None:
        """Append one record. Fails loudly if ``seq`` already exists."""
        dumped = record.model_dump()
        self.connection.execute(
            f"INSERT INTO audit_records ({_COLUMNS}) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.seq,
                record.record_id,
                dumped["timestamp"],
                record.kind,
                record.actor,
                record.subject,
                record.decision,
                record.result,
                record.reason,
                json.dumps(dumped["payload"], sort_keys=True),
                record.prev_hash,
                record.record_hash,
            ),
        )
        self.connection.commit()

    def read_all(self) -> list[AuditRecord]:
        """Every record, in chain order."""
        rows = self.connection.execute(
            f"SELECT {_COLUMNS} FROM audit_records ORDER BY seq"
        ).fetchall()
        return [self._to_record(row) for row in rows]

    def count(self) -> int:
        row = self.connection.execute(
            "SELECT COUNT(*) FROM audit_records"
        ).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _to_record(row: tuple) -> AuditRecord:
        return AuditRecord(
            seq=row[0],
            record_id=row[1],
            # The ISO string is exactly what compute_hash() hashed, so a
            # round-tripped record re-verifies to the same digest.
            timestamp=datetime.fromisoformat(row[2]),
            kind=row[3],
            actor=row[4],
            subject=row[5],
            decision=row[6],
            result=row[7],
            reason=row[8],
            payload=json.loads(row[9]),
            prev_hash=row[10],
            record_hash=row[11],
        )

    def close(self) -> None:
        self.connection.close()


__all__ = ["SqliteAuditBackend"]
