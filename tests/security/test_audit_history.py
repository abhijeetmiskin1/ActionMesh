"""Agent 5 — security invariants for history/audit.

Transient output must never be sealed into the audit chain or persisted, and a
tampered durable chain must be refused rather than rendered as clean history.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actionmesh.core.audit import AuditIntegrityError, AuditLog, SqliteAuditBackend
from actionmesh.core.events import EventHub
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import MeshEvent


class TestTransientSecretNeverAudited(unittest.TestCase):
    def test_secret_chunk_not_in_audit_chain_or_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "leak.db")
            store = SQLiteStore(path)
            backend = SqliteAuditBackend(path + ".audit")
            audit = AuditLog(backend=backend)
            hub = EventHub(audit=audit, store=store)
            secret = "AKIA-SECRET-chunk-must-not-persist"
            hub.publish(
                MeshEvent(
                    event_id="evt_out", event_type="execution.output",
                    execution_id="exec_a", payload={"chunk": secret},
                ),
                transient=True,
            )
            self.assertEqual(len(audit), 0)
            self.assertEqual(store.event_count(), 0)
            # Defence in depth: the raw rows carry no trace of the chunk.
            raw_audit = Path(path + ".audit").read_bytes()
            self.assertNotIn(secret.encode(), raw_audit)
            backend.close()
            store.close()


class TestTamperedChainRefused(unittest.TestCase):
    def test_edited_row_fails_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "chain.db")
            backend = SqliteAuditBackend(path)
            log = AuditLog(backend=backend)
            log.append(kind="event", actor="tester", subject="exec_a",
                       result="execution.completed")
            self.assertTrue(log.verify())
            backend.connection.execute(
                "UPDATE audit_records SET result='tampered' WHERE seq=0"
            )
            backend.connection.commit()
            with self.assertRaises(AuditIntegrityError):
                AuditLog.open(backend, require_intact=True)
            backend.close()


if __name__ == "__main__":
    unittest.main()
