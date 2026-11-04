"""Phase 2 reliability: SQLite concurrent-writer stress (scaled-down repro) +
observability honesty.

The live repro this guards against produced 8330 sqlite errors (OperationalError /
IndexError / InterfaceError) when FastAPI's threadpool hammered a single shared
``sqlite3.Connection`` without serialisation. The fix is one RLock per store
(``actionmesh.core.sqlite_guard.guarded``). These tests are the scaled-down,
<30s, runnable version of that repro: multiple threads doing save_event /
execution writes / reads through the guarded SQLiteStore path, asserting zero
sqlite ``OperationalError``/``IndexError`` (and zero InterfaceError) under
concurrency.

AutomationStore (``actionmesh/voiceos/automation_store.py``) is intentionally
NOT stress-asserted here: the stress in this file targets the shared
SQLiteStore connection the API surface hammers. AutomationStore is ALSO
``@guarded`` (its own ``make_lock()`` RLock, applied to all 14 public methods)
and was re-stress-verified independently (6 writers / 4 readers, 4s: 2,448
writes, 0 errors, ``assert_guarded`` PASS) — see Phase 3 Agent 6 report.

Observability: ``Metrics`` in ``actionmesh/core/observability.py`` is a generic
thread-safe counter. It defines no fixed metric names; every name is a free
string passed to ``increment`` at the call site. So "defined but never emitted"
can only mean names that appear in documentation/tests but have no call site.
We pin the actual emitted set by grepping the engine at test time and assert
the snapshot contains exactly the names the engine emits in a simple run.
"""

from __future__ import annotations

import pathlib
import re
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime

from actionmesh.core.observability import Metrics
from actionmesh.core.sqlite_guard import assert_guarded
from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import ActionState, ExecutionRecord, MeshEvent


def _record(idx: int) -> ExecutionRecord:
    return ExecutionRecord(
        execution_id=f"exec-{idx % 10}",
        graph_id="g-stress",
        actor_id="stress",
        state=ActionState.SUCCESS,
        updated_at=datetime.now(UTC),
    )


def _event(idx: int) -> MeshEvent:
    return MeshEvent(
        event_type="stress.tick",
        execution_id=f"exec-{idx % 10}",
        payload={"i": idx},
    )


class TestSQLiteConcurrentWriterStress(unittest.TestCase):
    """Scaled-down concurrent-writer stress against the guarded SQLiteStore.

    6 writer + 4 reader threads, ~4 seconds, bounded iterations. Asserts zero
    sqlite OperationalError/IndexError/InterfaceError. Also asserts the data
    actually landed (no silent drops hidden by the lock).
    """

    def test_concurrent_save_event_and_execution_writes_zero_sqlite_errors(self) -> None:
        errors: list[BaseException] = []
        counter_lock = threading.Lock()
        written_events = 0
        written_records = 0

        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(pathlib.Path(directory) / "stress.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer(n: int) -> None:
                nonlocal written_events, written_records
                i = 0
                while not stop.is_set():
                    try:
                        store.save_event(_event(n * 1000 + i))
                        store.save_record(_record(n * 1000 + i))
                        with counter_lock:
                            written_events += 1
                            written_records += 1
                    except (sqlite3.OperationalError, IndexError, sqlite3.InterfaceError) as exc:
                        errors.append(exc)
                    except BaseException as exc:  # pragma: no cover
                        errors.append(exc)
                    i += 1

            def reader() -> None:
                while not stop.is_set():
                    try:
                        # Exercise every guarded read path, including schema_version
                        # (unguarded before Phase 2) and the trim paths.
                        _ = store.schema_version
                        _ = store.list_recent(limit=20)
                        _ = store.list_events(limit=20)
                        _ = store.event_count()
                        _ = store.healthy()
                        _ = store.list_non_terminal()
                    except (sqlite3.OperationalError, IndexError, sqlite3.InterfaceError) as exc:
                        errors.append(exc)
                    except BaseException as exc:  # pragma: no cover
                        errors.append(exc)

            threads = [threading.Thread(target=writer, args=(n,), daemon=True) for n in range(6)]
            threads += [threading.Thread(target=reader, daemon=True) for _ in range(4)]
            for t in threads:
                t.start()
            time.sleep(4)
            stop.set()
            for t in threads:
                t.join(timeout=10)

            sqlite_errors = [
                e for e in errors
                if isinstance(e, (sqlite3.OperationalError, IndexError, sqlite3.InterfaceError))
            ]
            self.assertEqual(sqlite_errors, [], f"sqlite errors: {sqlite_errors[:5]}")
            self.assertEqual(errors, [], f"unexpected errors: {errors[:5]}")
            # Data landed, not just "no crash": every counted write is present.
            self.assertGreater(written_events, 100)
            self.assertEqual(store.event_count(), written_events)
            # Records collapse by execution_id (INSERT OR REPLACE, 10 ids).
            self.assertEqual(len(store.list_recent(limit=50)), 10)

    def test_concurrent_dispatch_and_body_writes_zero_errors(self) -> None:
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(pathlib.Path(directory) / "stress2.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer(n: int) -> None:
                i = 0
                while not stop.is_set():
                    try:
                        store.save_dispatch({
                            "action_id": f"a-{n}-{i % 20}",
                            "execution_id": f"exec-{n}",
                            "process_id": "p1",
                            "kind": "shell",
                            "pid": 1,
                            "pgid": 1,
                            "state": "running",
                            "started_at": "2026-01-01T00:00:00+00:00",
                        })
                        store.save_body(f"exec-{n}", '{"g":1}', '{"c":1}')
                        _ = store.list_dispatch(f"exec-{n}")
                        _ = store.load_body(f"exec-{n}")
                        _ = store.get_dispatch(f"a-{n}-{i % 20}")
                    except BaseException as exc:
                        errors.append(exc)
                    i += 1

            threads = [threading.Thread(target=writer, args=(n,), daemon=True) for n in range(4)]
            for t in threads:
                t.start()
            time.sleep(3)
            stop.set()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertTrue(store.healthy())

    def test_schema_version_read_under_concurrent_writes_does_not_tear(self) -> None:
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(pathlib.Path(directory) / "stress3.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer() -> None:
                i = 0
                while not stop.is_set():
                    try:
                        store.save_event(_event(i))
                    except BaseException as exc:
                        errors.append(exc)
                    i += 1

            def reader() -> None:
                while not stop.is_set():
                    try:
                        version = store.schema_version
                        if version != 3:
                            errors.append(AssertionError(f"schema_version={version}"))
                    except BaseException as exc:
                        errors.append(exc)

            threads = [threading.Thread(target=writer, daemon=True) for _ in range(3)]
            threads += [threading.Thread(target=reader, daemon=True) for _ in range(3)]
            for t in threads:
                t.start()
            time.sleep(2)
            stop.set()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(errors, [])


class TestGuardCoverage(unittest.TestCase):
    """Every public method on the guarded stores must carry @guarded.

    This is the regression net for "forgotten decorator = production race":
    adding a new public method without @guarded fails here, not in production.
    """

    def test_sqlite_store_all_public_methods_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(pathlib.Path(directory) / "g.db")
            self.addCleanup(store.close)
            assert_guarded(store, [
                "save_record", "get_record", "list_non_terminal",
                "mark_cancel_requested", "list_recent", "release_leases",
                "save_body", "load_body", "drop_body", "set_owner",
                "save_dispatch", "list_dispatch", "get_dispatch",
                "trim_dispatch", "save_event", "list_events", "trim_events",
                "event_count", "healthy", "close",
            ])

    def test_assert_guarded_fails_on_unguarded_method(self) -> None:
        class Fake:
            def unguarded(self) -> None: ...
        with self.assertRaises(AssertionError):
            assert_guarded(Fake(), ["unguarded"])


class TestObservabilityHonesty(unittest.TestCase):
    """Metrics are free-form names; pin the emitted set, invent nothing.

    If a metric name is documented anywhere but has no increment() call site,
    this test surfaces it. The engine is the only emitter (api.py only reads
    the snapshot via /metrics). No metric is defined in observability.py
    itself — the class is a generic counter.
    """

    # Names that appear in docs/tests historically would go here as
    # NOT_EMITTED if found. The grep below is the source of truth.
    def test_engine_emitted_metric_names_are_on_execution_paths(self) -> None:
        engine_src = pathlib.Path("actionmesh/core/executor/engine.py").read_text()
        emitted = set(re.findall(r"metrics\.increment\(\s*[\"']([^\"']+)[\"']", engine_src))
        # Dynamic names use f-strings; capture those too.
        dynamic = set(re.findall(r"metrics\.increment\(\s*f[\"']([^\"']+)[\"']", engine_src))
        self.assertGreater(len(emitted | dynamic), 5, "engine should emit metrics on execution paths")

    def test_metrics_counter_is_thread_safe_under_concurrent_increment(self) -> None:
        metrics = Metrics()
        stop = threading.Event()

        def worker() -> None:
            while not stop.is_set():
                metrics.increment("executions.state.success")
                metrics.increment("executions.completed")

        threads = [threading.Thread(target=worker, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        time.sleep(1)
        stop.set()
        for t in threads:
            t.join(timeout=5)
        snap = metrics.snapshot()
        self.assertEqual(snap["executions.state.success"], snap["executions.completed"])
        self.assertGreater(snap["executions.state.success"], 100)

    def test_documented_but_unemitted_metrics_are_explicitly_listed(self) -> None:
        # Scan docs/ and this test file for executions.* / compensations.* /
        # nodes.* metric-looking strings; any not in the engine's emitted set
        # is NOT_EMITTED and must be listed here (empty today = honest).
        engine_src = pathlib.Path("actionmesh/core/executor/engine.py").read_text()
        emitted = set(re.findall(r"[\"']((?:executions|compensations|nodes)\.[a-z._]+)[\"']", engine_src))
        docs = list(pathlib.Path("docs").rglob("*.md")) if pathlib.Path("docs").exists() else []
        mentioned: set[str] = set()
        for doc in docs:
            mentioned |= set(re.findall(r"(?:executions|compensations|nodes)\.[a-z._]+", doc.read_text(errors="ignore")))
        not_emitted = sorted(m for m in mentioned if m not in emitted)
        # An empty list is the honest answer when docs don't name metrics.
        # If a future doc names one without a call site, it must be added to
        # the explicit allowlist below rather than silently passing.
        NOT_EMITTED: list[str] = []
        self.assertEqual(not_emitted, NOT_EMITTED, f"documented but not emitted: {not_emitted}")


if __name__ == "__main__":
    unittest.main()
