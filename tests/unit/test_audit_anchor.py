"""C16 — external chain anchor.

These tests exist because of a measured gap, not a theoretical worry. Without an
anchor, an attacker with write access to the audit database can delete the last
records, append a forged one, and leave a chain that verifies cleanly. The
mid-chain cases (edit, delete, reorder) are already covered elsewhere; the cases
here are the ones the hash chain alone provably cannot catch.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from actionmesh.core.audit import (
    AnchorMismatchError,
    AuditIntegrityError,
    AuditLog,
    FileAuditAnchor,
    SqliteAuditBackend,
)


def build(path: Path, anchor_path: Path, count: int = 5):
    """Write ``count`` records, seal the anchor, and return the head."""
    backend = SqliteAuditBackend(path)
    log = AuditLog.open(backend, anchor=FileAuditAnchor(anchor_path))
    for i in range(count):
        log.append(kind="event", actor="u1", subject=f"e{i}", result="started")
    head = log.seal()[1]
    backend.close()
    return head


def tamper(path: Path, sql: str) -> None:
    """Edit the store with a plain sqlite3 connection, no ActionMesh involved."""
    raw = sqlite3.connect(str(path))
    raw.execute(sql)
    raw.commit()
    raw.close()


class TestChainDetectsWhatItCan(unittest.TestCase):
    """Regression guard: the anchor must not weaken existing detection."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "audit.db"
        self.anchor = self.root / "anchor.json"
        build(self.path, self.anchor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_untouched_chain_opens(self) -> None:
        backend = SqliteAuditBackend(self.path)
        log = AuditLog.open(backend, anchor=FileAuditAnchor(self.anchor))
        self.assertTrue(log.verify().ok)
        self.assertEqual(len(log), 5)
        backend.close()


class TestAnchorDetectsTruncation(unittest.TestCase):
    """The gap: a valid prefix, with its tail removed, verifies on its own."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "audit.db"
        self.anchor = self.root / "anchor.json"
        build(self.path, self.anchor)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_truncation_alone_is_invisible_without_an_anchor(self) -> None:
        # This is the defect being fixed, pinned so it cannot regress.
        tamper(self.path, "DELETE FROM audit_records WHERE seq >= 3")
        backend = SqliteAuditBackend(self.path)
        try:
            log = AuditLog.open(backend)  # deliberately no anchor
            self.assertTrue(log.verify().ok)
            self.assertEqual(len(log), 3)
        finally:
            backend.close()

    def test_truncation_is_refused_when_an_anchor_exists(self) -> None:
        tamper(self.path, "DELETE FROM audit_records WHERE seq >= 3")
        backend = SqliteAuditBackend(self.path)
        try:
            with self.assertRaises(AnchorMismatchError) as ctx:
                AuditLog.open(backend, anchor=FileAuditAnchor(self.anchor))
        finally:
            backend.close()
        self.assertIn("truncated", str(ctx.exception))

    def test_emptying_the_chain_is_refused(self) -> None:
        tamper(self.path, "DELETE FROM audit_records")
        backend = SqliteAuditBackend(self.path)
        try:
            with self.assertRaises(AnchorMismatchError):
                AuditLog.open(backend, anchor=FileAuditAnchor(self.anchor))
        finally:
            backend.close()

    def test_a_longer_chain_than_the_anchor_is_fine(self) -> None:
        # The anchor is a periodic checkpoint, not a high-water mark: ordinary
        # appends after sealing must not look like tampering.
        backend = SqliteAuditBackend(self.path)
        log = AuditLog.open(backend, anchor=FileAuditAnchor(self.anchor))
        log.append(kind="event", actor="u1", subject="later", result="started")
        backend.close()

        backend2 = SqliteAuditBackend(self.path)
        reopened = AuditLog.open(backend2, anchor=FileAuditAnchor(self.anchor))
        self.assertEqual(len(reopened), 6)
        self.assertTrue(reopened.verify().ok)
        backend2.close()

    def test_a_missing_anchor_is_not_a_failure(self) -> None:
        # No anchor has ever been sealed: nothing to contradict.
        fresh = self.root / "other.db"
        other_anchor = self.root / "never-sealed.json"
        build(fresh, other_anchor, count=2)
        other_anchor.unlink()
        backend = SqliteAuditBackend(fresh)
        log = AuditLog.open(backend, anchor=FileAuditAnchor(other_anchor))
        self.assertEqual(len(log), 2)
        backend.close()


if __name__ == "__main__":
    unittest.main()


    def test_mid_chain_edit_is_still_refused(self) -> None:
        tamper(self.path, "UPDATE audit_records SET actor='mallory' WHERE seq=2")
        backend = SqliteAuditBackend(self.path)
        with self.assertRaises(AuditIntegrityError):
            AuditLog.open(backend, anchor=FileAuditAnchor(self.anchor))
        backend.close()
