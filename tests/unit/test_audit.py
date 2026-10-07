"""C15 audit trail — append-only, tamper-evident records.

The point of this suite is not that SHA-256 works. It is that **editing,
deleting or reordering history is detected**, including the case where an
attacker re-seals the record they edited.

Where a test reaches into ``log._records`` it is simulating an attacker with
write access; the public API exposes only an immutable tuple view. That is the
whole reason ``verify()`` recomputes hashes instead of trusting stored ones.
"""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from actionmesh.core.audit import (
    AuditError,
    AuditIntegrityError,
    AuditLog,
    SqliteAuditBackend,
    compute_hash,
)
from actionmesh.core.events import EventHub
from actionmesh.core.executor import ExecutionEngine
from actionmesh.core.graph import GraphBuilder
from actionmesh.core.permissions import (
    PermissionGrant,
    PolicyContext,
    PolicyEngine,
    build_context,
    policy_from_rules,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import (
    GENESIS_HASH,
    AuditRecord,
    Identity,
    MeshEvent,
    RiskLevel,
)
from actionmesh.providers import MockProvider


class _BrokenBackend:
    """A durable sink that fails, to prove nothing is half-recorded."""

    def write(self, record: AuditRecord) -> None:
        raise RuntimeError("disk is full")

    def read_all(self) -> list[AuditRecord]:
        return []

ALLOW_ALL = policy_from_rules([{"action": "*", "decision": "allow"}])


def context(scopes: list[str], risk: RiskLevel = RiskLevel.HIGH) -> PolicyContext:
    return PolicyContext(
        actor=Identity(id="user_1"),
        policy=ALLOW_ALL,
        grants=[PermissionGrant(scope=s, max_risk=risk) for s in scopes],
    )


def send(text: str = "hi") -> dict[str, object]:
    return {"type": "message.send", "parameters": {"message": text}}


def graph_of(*actions: dict[str, object]):
    builder = GraphBuilder("audit")
    previous = None
    for index, action in enumerate(actions, start=1):
        node_id = f"a{index}"
        builder.action(action, node_id=node_id, depends_on=[previous] if previous else [])
        previous = node_id
    builder.end(depends_on=[previous] if previous else [])
    return builder.build()


class TestChainBasics(unittest.TestCase):
    def test_empty_log_verifies(self) -> None:
        log = AuditLog()
        status = log.verify()
        self.assertTrue(status)
        self.assertEqual(status.records, 0)
        self.assertIsNone(status.broken_at)
        self.assertEqual(log.head, GENESIS_HASH)

    def test_append_links_records_in_order(self) -> None:
        log = AuditLog()
        first = log.append(kind="decision", subject="a")
        second = log.append(kind="event", subject="b")
        self.assertEqual(first.seq, 0)
        self.assertEqual(second.seq, 1)
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        self.assertEqual(second.prev_hash, first.record_hash)
        self.assertTrue(log.verify())

    def test_records_view_is_immutable(self) -> None:
        log = AuditLog()
        log.append(kind="event")
        self.assertIsInstance(log.records, tuple)
        with self.assertRaises(AttributeError):
            log.records.append(None)  # type: ignore[attr-defined]

    def test_kind_is_required(self) -> None:
        with self.assertRaises(ValueError):
            AuditLog().append(kind="")

    def test_max_records_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            AuditLog(max_records=0)

    def test_overflow_raises_instead_of_truncating(self) -> None:
        # Silently dropping the oldest audit entries would itself be tampering.
        log = AuditLog(max_records=2)
        log.append(kind="event")
        log.append(kind="event")
        with self.assertRaises(AuditError):
            log.append(kind="event")
        self.assertEqual(len(log), 2)
        self.assertTrue(log.verify())

    def test_hash_is_deterministic_and_content_sensitive(self) -> None:
        log = AuditLog()
        record = log.append(kind="event", subject="x")
        self.assertEqual(compute_hash(record), record.record_hash)
        self.assertEqual(compute_hash(record), compute_hash(record))
        record.subject = "tampered"
        self.assertNotEqual(compute_hash(record), record.record_hash)

    def test_reset_starts_a_new_chain(self) -> None:
        log = AuditLog()
        log.append(kind="event")
        log.reset()
        self.assertEqual(len(log), 0)
        first = log.append(kind="event")
        self.assertEqual(first.prev_hash, GENESIS_HASH)
        self.assertTrue(log.verify())


class TestTamperDetection(unittest.TestCase):
    def _log_with(self, count: int = 3) -> AuditLog:
        log = AuditLog()
        for index in range(count):
            log.append(kind="event", subject=f"s{index}", result=f"r{index}")
        self.assertTrue(log.verify())
        return log

    def test_modified_content_is_detected(self) -> None:
        log = self._log_with()
        log._records[1].result = "forged"  # attacker edits history
        status = log.verify()
        self.assertFalse(status)
        self.assertEqual(status.broken_at, 1)
        self.assertIn("does not match its hash", status.problem)

    def test_deleted_record_is_detected(self) -> None:
        log = self._log_with()
        log._records.pop(1)
        status = log.verify()
        self.assertFalse(status)
        self.assertIn("sequence mismatch", status.problem)

    def test_reordered_records_are_detected(self) -> None:
        log = self._log_with()
        log._records[0], log._records[1] = log._records[1], log._records[0]
        status = log.verify()
        self.assertFalse(status)
        self.assertIn("sequence mismatch", status.problem)

    def test_resealed_edit_still_breaks_the_next_link(self) -> None:
        # The important case: an attacker edits a record AND recomputes its own
        # hash. The record is now self-consistent, but the following record
        # still points at the old hash — so the forgery is still caught.
        log = self._log_with()
        record = log._records[1]
        record.result = "forged"
        record.record_hash = compute_hash(record)
        status = log.verify()
        self.assertFalse(status)
        self.assertEqual(status.broken_at, 2)
        self.assertIn("not linked", status.problem)

    def test_forged_genesis_link_is_detected(self) -> None:
        log = self._log_with()
        log._records[0].prev_hash = "f" * 64
        status = log.verify()
        self.assertFalse(status)
        self.assertEqual(status.broken_at, 0)


class TestIntegration(unittest.TestCase):
    def test_event_hub_seals_every_published_event(self) -> None:
        log = AuditLog()
        hub = EventHub(audit=log)
        self.assertIs(hub.audit, log)
        for index in range(3):
            hub.publish(MeshEvent(event_type=f"e{index}", execution_id="exec_1"))
        self.assertEqual(len(log), 3)
        self.assertTrue(log.verify())
        self.assertEqual(
            [r.result for r in log.records], ["e0", "e1", "e2"]
        )

    def test_event_hub_without_audit_is_unchanged(self) -> None:
        hub = EventHub()
        self.assertIsNone(hub.audit)
        hub.publish(MeshEvent(event_type="e0"))
        self.assertEqual(len(hub.history()), 1)

    def test_record_decision_from_a_real_policy_engine(self) -> None:
        log = AuditLog()
        engine = PolicyEngine()
        # A real resolved action, so the decision records a real subject.
        resolved = ActionRegistry.with_seeds().resolve(send("audit"))
        ctx = build_context(
            resolved.action,
            resolved.definition,
            Identity(id="user_1"),
            grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
            policy=ALLOW_ALL,
        )
        outcome = engine.evaluate(ctx)
        record = log.record_decision(engine.explain(ctx), actor="user_1")
        self.assertEqual(record.kind, "decision")
        self.assertEqual(record.decision, outcome.decision.value)
        self.assertEqual(record.payload["action"], "message.send")
        self.assertEqual(record.subject, "message.send")
        self.assertTrue(log.verify())

    def test_execution_produces_a_verifiable_chain(self) -> None:
        log = AuditLog()
        hub = EventHub(audit=log)
        registry = ActionRegistry.with_seeds()
        provider = MockProvider()
        engine = ExecutionEngine(registry, PolicyEngine(), [provider], events=hub)
        graph = graph_of(send("audit me"))
        result = asyncio.run(
            engine.execute(graph, context(["message.send"]))
        )
        self.assertTrue(result.state.value in {"success", "requires_user"})
        self.assertGreater(len(log), 0, "a real execution must produce audit records")
        status = log.verify()
        self.assertTrue(status, f"chain broken: {status.problem}")
        self.assertEqual(status.records, len(log))

    def test_tampering_with_a_real_execution_trail_is_detected(self) -> None:
        log = AuditLog()
        hub = EventHub(audit=log)
        registry = ActionRegistry.with_seeds()
        engine = ExecutionEngine(
            registry, PolicyEngine(), [MockProvider()], events=hub
        )
        asyncio.run(engine.execute(graph_of(send()), context(["message.send"])))
        self.assertTrue(log.verify())
        log._records[0].result = "action.succeeded"
        self.assertFalse(log.verify())


class TestPersistence(unittest.TestCase):
    """The chain must survive process death — and detect edits made on disk."""

    def test_empty_store_opens_cleanly(self) -> None:
        backend = SqliteAuditBackend()
        self.addCleanup(backend.close)
        log = AuditLog.open(backend)
        self.assertEqual(len(log), 0)
        self.assertTrue(log.verify())

    def test_records_survive_a_restart_and_the_chain_continues(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            backend = SqliteAuditBackend(path)
            first = AuditLog.open(backend)
            first.append(kind="event", subject="before-restart")
            # The caller owns the backend, so the caller closes it.
            backend.close()

            reopened_backend = SqliteAuditBackend(path)
            reopened = AuditLog.open(reopened_backend)
            self.assertEqual(len(reopened), 1)
            self.assertEqual(reopened.records[0].subject, "before-restart")
            self.assertTrue(reopened.verify())

            reopened.append(kind="event", subject="after-restart")
            self.assertTrue(reopened.verify())
            # The new record links to the head persisted in the *previous* run.
            self.assertEqual(
                reopened.records[1].prev_hash, reopened.records[0].record_hash
            )
            reopened_backend.close()

    def test_tampering_in_the_store_is_detected_on_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            backend = SqliteAuditBackend(path)
            log = AuditLog.open(backend)
            log.append(kind="event", subject="honest")
            backend.close()

            # An attacker edits the row directly, with a plain SQL client and
            # no knowledge of ActionMesh at all.
            raw = sqlite3.connect(str(path))
            raw.execute(
                "UPDATE audit_records SET result = 'forged' WHERE seq = 0"
            )
            raw.commit()
            raw.close()

            # The caller still owns the backend even when open() refuses to
            # start, so it has to be closed either way.
            tampered = SqliteAuditBackend(path)
            try:
                with self.assertRaises(AuditIntegrityError):
                    AuditLog.open(tampered)
            finally:
                tampered.close()

    def test_open_can_permit_a_broken_chain_for_inspection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            backend = SqliteAuditBackend(path)
            log = AuditLog.open(backend)
            log.append(kind="event")
            backend.close()

            raw = sqlite3.connect(str(path))
            raw.execute("UPDATE audit_records SET result = 'x' WHERE seq = 0")
            raw.commit()
            raw.close()

            forensic_backend = SqliteAuditBackend(path)
            try:
                forensic = AuditLog.open(forensic_backend, require_intact=False)
                self.assertFalse(forensic.verify())
                self.assertEqual(len(forensic), 1, "forensics still needs the evidence")
            finally:
                forensic_backend.close()

    def test_reset_is_refused_on_a_persisted_chain(self) -> None:
        backend = SqliteAuditBackend()
        self.addCleanup(backend.close)
        log = AuditLog.open(backend)
        log.append(kind="event")
        with self.assertRaises(AuditError):
            log.reset()
        self.assertEqual(len(log), 1)

    def test_backend_is_exposed(self) -> None:
        backend = SqliteAuditBackend()
        self.addCleanup(backend.close)
        self.assertIs(AuditLog.open(backend).backend, backend)
        self.assertIsNone(AuditLog().backend)

    def test_failed_durable_write_records_nothing(self) -> None:
        log = AuditLog(backend=_BrokenBackend())
        with self.assertRaises(RuntimeError):
            log.append(kind="event")
        self.assertEqual(len(log), 0, "a failed durable write must leave no trace")
        self.assertTrue(log.verify())

    def test_duplicate_seq_is_rejected_by_the_store(self) -> None:
        # Append-only is enforced by the schema, not by convention.
        backend = SqliteAuditBackend()
        self.addCleanup(backend.close)
        record = AuditRecord(seq=0, kind="event")
        backend.write(record)
        with self.assertRaises(sqlite3.IntegrityError):
            backend.write(record)

    def test_nested_payload_survives_the_round_trip(self) -> None:
        payload = {"a": {"b": [1, 2, {"c": True}]}, "unicode": "café ✓"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.db"
            backend = SqliteAuditBackend(path)
            log = AuditLog.open(backend)
            log.append(kind="event", payload=payload)
            backend.close()

            reopened_backend = SqliteAuditBackend(path)
            reopened = AuditLog.open(reopened_backend)
            self.assertEqual(reopened.records[0].payload, payload)
            self.assertTrue(reopened.verify())
            reopened_backend.close()
