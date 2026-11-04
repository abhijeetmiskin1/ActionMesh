"""Agent 5 — history invariants over the durable store, event hub and audit chain.

Covers, against the real SQLiteStore / EventHub / AuditLog (no mocks of the
ledger itself):
  * persisted history survives a restart (store reopened from the same file)
  * SSE/history replay dedup by stable event_id (no duplicate timeline entries)
  * transient output never reaches audit, the store, or history()
  * waiting-approval / unverified / interrupted are never terminal-success
  * event and execution ordering is insertion order, oldest first
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from actionmesh.core.audit import AuditLog, SqliteAuditBackend
from actionmesh.core.events import EventHub
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import ActionState, ExecutionRecord, MeshEvent


def _record(state: ActionState, execution_id: str = "exec_1") -> ExecutionRecord:
    return ExecutionRecord(execution_id=execution_id, state=state)


class TestPersistedHistorySurvivesReload(unittest.TestCase):
    def test_records_and_events_reopen_from_same_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "history.db")
            store = SQLiteStore(path)
            store.save_record(_record(ActionState.SUCCESS, "exec_a"))
            store.save_record(_record(ActionState.WAITING_PERMISSION, "exec_b"))
            store.save_event(MeshEvent(event_id="evt_1", event_type="execution.completed",
                                       execution_id="exec_a"))
            store.close()

            reopened = SQLiteStore(path)
            try:
                recent = {r.execution_id: r.state for r in reopened.list_recent(limit=10)}
                self.assertEqual(recent["exec_a"], ActionState.SUCCESS)
                # Waiting-for-approval survives as itself — never rewritten to success.
                self.assertEqual(recent["exec_b"], ActionState.WAITING_PERMISSION)
                events = reopened.list_events("exec_a")
                self.assertEqual([e.event_id for e in events], ["evt_1"])
                self.assertEqual(reopened.event_count("exec_a"), 1)
            finally:
                reopened.close()

    def test_non_terminal_survives_reopen_for_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "history.db")
            store = SQLiteStore(path)
            store.save_record(_record(ActionState.EXECUTING, "exec_live"))
            store.save_record(_record(ActionState.SUCCESS, "exec_done"))
            store.close()

            reopened = SQLiteStore(path)
            try:
                live = [r.execution_id for r in reopened.list_non_terminal()]
                self.assertEqual(live, ["exec_live"])
            finally:
                reopened.close()


class TestReplayDedup(unittest.TestCase):
    def test_restart_replay_has_no_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.db")
            store = SQLiteStore(path)
            first_hub = EventHub(audit=AuditLog(), store=store)
            first_hub.publish(MeshEvent(event_id="evt_1", event_type="execution.started",
                                        execution_id="exec_a"))
            stored_before = store.event_count("exec_a")

            # A new process: fresh hub, same store. Its in-memory history is
            # empty; the store contributes the pre-restart events exactly once.
            second_hub = EventHub(audit=AuditLog(), store=store)
            second_hub.publish(MeshEvent(event_id="evt_2", event_type="execution.completed",
                                         execution_id="exec_a"))
            ids = [e.event_id for e in second_hub.history("exec_a")]
            self.assertEqual(ids, ["evt_1", "evt_2"])
            self.assertEqual(len(ids), len(set(ids)))
            self.assertEqual(store.event_count("exec_a"), stored_before + 1)
            store.close()

    def test_since_cursor_skips_seen_events(self) -> None:
        async def scenario() -> tuple[str, str]:
            hub = EventHub()
            hub.publish(MeshEvent(event_id="evt_1", event_type="a", execution_id="exec_a"))
            hub.publish(MeshEvent(event_id="evt_2", event_type="b", execution_id="exec_a"))
            stream = hub.subscribe("exec_a", since_event_id="evt_1")
            first = await asyncio.wait_for(stream.__anext__(), timeout=1)
            await stream.aclose()
            return first.event_id, first.event_type

        event_id, event_type = asyncio.run(scenario())
        self.assertEqual((event_id, event_type), ("evt_2", "b"))


class TestTransientNeverEntersHistory(unittest.TestCase):
    def test_transient_is_not_audited_not_stored_not_replayed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "both.db")
            store = SQLiteStore(path)
            audit = AuditLog(backend=SqliteAuditBackend(path))
            hub = EventHub(audit=audit, store=store)
            hub.publish(
                MeshEvent(event_id="evt_out", event_type="execution.output",
                          execution_id="exec_a", payload={"chunk": "secret stdout"}),
                transient=True,
            )
            self.assertEqual(len(audit), 0)
            self.assertEqual(store.event_count(), 0)
            self.assertEqual(hub.history("exec_a"), [])
            # ...but the caller's own in-memory capture still holds it.
            self.assertEqual(len(hub.transient("exec_a")), 1)
            store.close()


class TestNoFakeSuccess(unittest.TestCase):
    def test_unverified_states_are_not_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "states.db")
            store = SQLiteStore(path)
            for state, eid in [
                (ActionState.WAITING_PERMISSION, "exec_wait"),
                (ActionState.REQUIRES_USER, "exec_unverified"),
                (ActionState.INTERRUPTED, "exec_interrupted"),
                (ActionState.PARTIALLY_COMPLETED, "exec_partial"),
                (ActionState.SUCCESS, "exec_ok"),
            ]:
                store.save_record(_record(state, eid))
            live = {r.execution_id for r in store.list_non_terminal()}
            self.assertIn("exec_wait", live)
            self.assertIn("exec_unverified", live)
            # Interrupted/partial are terminal in storage but are not success:
            # they must not be re-dispatched or reported as completed work.
            self.assertNotIn("exec_interrupted", live)
            self.assertNotIn("exec_partial", live)
            self.assertNotIn("exec_ok", live)
            store.close()

    def test_audit_chain_persists_and_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "audit.db")
            backend = SqliteAuditBackend(path)
            log = AuditLog(backend=backend)
            log.append(kind="event", actor="tester", subject="exec_a",
                       result="execution.completed")
            self.assertTrue(log.verify())
            backend.close()

            reopened_backend = SqliteAuditBackend(path)
            reopened = AuditLog.open(reopened_backend)
            self.assertTrue(reopened.verify())
            self.assertEqual(len(reopened), 1)
            reopened_backend.close()


if __name__ == "__main__":
    unittest.main()
