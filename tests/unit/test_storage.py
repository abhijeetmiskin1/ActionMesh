"""C15 SQLite persistence tests."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path

from actionmesh.core.storage import SCHEMA_VERSION, SchemaError, SQLiteStore
from actionmesh.protocol.models import ActionState, ExecutionRecord, MeshEvent


class TestSQLiteStore(unittest.TestCase):
    def test_execution_record_survives_store_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actionmesh.db"
            first = SQLiteStore(path)
            first.save_record(
                ExecutionRecord(execution_id="e1", graph_id="g1", state=ActionState.SUCCESS)
            )
            first.close()
            second = SQLiteStore(path)
            record = second.get_record("e1")
            self.assertIsNotNone(record)
            self.assertEqual(record.state, ActionState.SUCCESS)
            second.close()


class TestSchemaVersioning(unittest.TestCase):
    def _path(self, directory: str) -> Path:
        return Path(directory) / "mesh.db"

    def test_a_fresh_database_is_stamped_with_the_current_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(self._path(directory))
            self.addCleanup(store.close)
            self.assertEqual(store.schema_version, SCHEMA_VERSION)

    def test_reopening_an_existing_database_does_not_re_migrate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            first = SQLiteStore(path)
            first.save_record(ExecutionRecord(execution_id="keep", graph_id="g"))
            first.close()
            second = SQLiteStore(path)
            self.addCleanup(second.close)
            # The row survived, which it would not have if migration recreated tables.
            self.assertIsNotNone(second.get_record("keep"))
            self.assertEqual(second.schema_version, SCHEMA_VERSION)

    def test_a_database_from_a_newer_program_is_refused_not_downgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            SQLiteStore(path).close()
            # Simulate a file written by a future ActionMesh (one migration ahead).
            raw = sqlite3.connect(str(path))
            raw.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            raw.commit()
            raw.close()
            with self.assertRaises(SchemaError):
                SQLiteStore(path)

    def test_an_unversioned_legacy_database_is_migrated_in_place(self) -> None:
        # A database created before versioning (tables present, user_version 0) must
        # open cleanly: the migration is CREATE TABLE IF NOT EXISTS, so it is a no-op
        # on the schema and only stamps the version.
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(directory)
            raw = sqlite3.connect(str(path))
            raw.execute(
                "CREATE TABLE execution_records (execution_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            raw.execute(
                "INSERT INTO execution_records VALUES ('legacy', '{\"execution_id\":\"legacy\",\"graph_id\":\"g\",\"actor_id\":\"\",\"state\":\"success\",\"node_states\":{},\"results\":{},\"error\":\"\"}')"
            )
            raw.commit()
            raw.close()
            store = SQLiteStore(path)
            self.addCleanup(store.close)
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertIsNotNone(store.get_record("legacy"))


class TestRecordRoundTrip(unittest.TestCase):
    def test_timestamps_and_results_survive_the_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(self._path(directory))
            self.addCleanup(store.close)
            started = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
            finished = datetime(2026, 10, 9, 12, 0, 3, tzinfo=UTC)
            store.save_record(
                ExecutionRecord(
                    execution_id="e1",
                    graph_id="g1",
                    actor_id="abhi",
                    state=ActionState.SUCCESS,
                    started_at=started,
                    finished_at=finished,
                    results={"n1": {"state": "success", "duration_ms": 2007}},
                )
            )
            record = store.get_record("e1")
            assert record is not None
            self.assertEqual(record.actor_id, "abhi")
            self.assertEqual(record.started_at, started)
            self.assertEqual(record.finished_at, finished)
            self.assertEqual(record.results["n1"]["duration_ms"], 2007)

    def _path(self, directory: str) -> Path:
        return Path(directory) / "mesh.db"


class TestReadiness(unittest.TestCase):
    def _path(self, directory: str) -> Path:
        return Path(directory) / "mesh.db"

    def test_healthy_store_reports_ready_and_counts_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(self._path(directory))
            self.addCleanup(store.close)
            self.assertTrue(store.healthy())
            store.save_event(MeshEvent(event_type="x", execution_id="e1"))
            store.save_event(MeshEvent(event_type="y", execution_id="e2"))
            self.assertEqual(store.event_count(), 2)
            self.assertEqual(store.event_count("e1"), 1)

    def test_a_closed_store_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(self._path(directory))
            store.close()
            self.assertFalse(store.healthy())



class TestConcurrentUse(unittest.TestCase):
    """One store is shared by FastAPI's threadpool: reads and writes must not race.

    Regression for the live ``IndexError: tuple index out of range`` in
    ``list_recent`` (and ``list_sessions``) when a write interleaved with a
    read on the single shared connection: without serialisation the reader
    saw ``()`` rows, torn payloads and ``InterfaceError``. With the store
    lock, this hammering runs clean.
    """

    def test_concurrent_save_and_list_recent_never_raises(self) -> None:
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(Path(directory) / "race.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer() -> None:
                i = 0
                while not stop.is_set():
                    try:
                        store.save_record(
                            ExecutionRecord(
                                execution_id=f"e{i % 10}",
                                graph_id="g",
                                state=ActionState.SUCCESS,
                            )
                        )
                    except BaseException as exc:
                        errors.append(exc)
                    i += 1

            def reader() -> None:
                while not stop.is_set():
                    try:
                        for record in store.list_recent(limit=50):
                            self.assertIn(record.execution_id, {f"e{i}" for i in range(10)})
                    except BaseException as exc:
                        errors.append(exc)

            threads = [threading.Thread(target=writer, daemon=True) for _ in range(2)]
            threads += [threading.Thread(target=reader, daemon=True) for _ in range(4)]
            for thread in threads:
                thread.start()
            time.sleep(2)
            stop.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()


    unittest.main()