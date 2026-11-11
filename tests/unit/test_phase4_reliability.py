"""Phase 4 reliability: guard pins for the owned stores + AutomationStore stress
re-verification + packaging exclusion pin + multi-device readiness probe.

Scope (Agent 6):
- ``SQLiteStore`` (storage.py) and ``AutomationStore`` (automation_store.py) and
  ``VoiceOsStore`` (voiceos/store.py) public methods must carry ``@guarded``.
  ``SQLiteStore.schema_version`` is a property that takes the lock manually
  (documented in storage.py); it is pinned separately and is NOT expected to
  have ``__wrapped__``.
- AutomationStore concurrent-writer stress (scaled <30s): 6 writers / 4 readers,
  ~4s, zero sqlite errors expected. Mirrors test_reliability_phase2 but for
  the automation store, which the Phase 2 file deliberately did not stress.
- Packaging: ``desktop/package.json`` ``build.files`` must exclude secrets
  (``*.key``, ``settings.json``, ``.env``, ``*.test.js``) and must NOT exclude
  the bundled CA bundle allowlist (``runtime/**/cacert.pem`` ships inside the
  vendored certifi tree under ``runtime/python`` — it is a public trust
  bundle, not a secret; the exclusion is ``runtime/**/*.pem`` in ``files``
  but ``runtime`` is delivered via ``extraResources``). Pinned by reading
  package.json only — no rebuild required for this pin.
- Multi-device readiness (P3-3 prep, read-only): the device registry
  ``forget`` + presence cascade works. This is Agent 3's surface; this test
  only verifies the cascade contract end-to-end with fakes, without
  duplicating Agent 3's registry tests. If the cascade regresses, this
  fails loudly here too.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import tempfile
import threading
import time
import unittest

from actionmesh.core.sqlite_guard import assert_guarded
from actionmesh.core.storage import SCHEMA_VERSION, SQLiteStore
from actionmesh.voiceos.automation_store import AutomationStore
from actionmesh.voiceos.models import Conversation, TaskSession
from actionmesh.voiceos.store import VoiceOsStore
from actionmesh.voiceos.workflows import AutomationRun, Workflow


def _workflow(idx: int) -> Workflow:
    return Workflow(
        workflow_id=f"wf-{idx % 10}",
        actor_id="stress",
        name=f"wf {idx}",
    )


def _run(idx: int) -> AutomationRun:
    return AutomationRun(
        run_id=f"run-{idx}",
        workflow_id=f"wf-{idx % 10}",
        workflow_name="stress",
        actor_id="stress",
        slot=f"slot-{idx}",
        trigger="manual",
    )


class TestGuardPinsOwnedStores(unittest.TestCase):
    def test_sqlite_store_public_methods_guarded(self) -> None:
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

    def test_sqlite_store_schema_version_property_takes_lock(self) -> None:
        # schema_version is a @property that acquires self._lock manually
        # (storage.py). It must not be callable without the lock held by
        # another thread tearing the read — pin that it returns the
        # current version and is stable under the lock protocol.
        with tempfile.TemporaryDirectory() as directory:
            store = SQLiteStore(pathlib.Path(directory) / "v.db")
            self.addCleanup(store.close)
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            # Holding the lock externally must not deadlock a read (RLock).
            with store._lock:  # type: ignore[attr-defined]
                self.assertEqual(store.schema_version, SCHEMA_VERSION)

    def test_automation_store_public_methods_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AutomationStore(pathlib.Path(directory) / "a.db")
            self.addCleanup(store.close)
            assert_guarded(store, [
                "schema_version", "save_workflow", "get_workflow",
                "list_workflows", "delete_workflow", "due_workflows",
                "claim", "save_run", "get_run", "run_for_slot",
                "list_runs", "reconcile_claimed", "healthy", "close",
            ])

    def test_voiceos_store_public_methods_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = VoiceOsStore(pathlib.Path(directory) / "v.db")
            self.addCleanup(store.close)
            assert_guarded(store, [
                "schema_version", "save_session", "get_session",
                "list_sessions", "get_conversation", "save_conversation",
                "clear_conversation", "healthy", "close",
            ])


class TestAutomationStoreConcurrentStress(unittest.TestCase):
    """Scaled-down concurrent stress for AutomationStore (<30s).

    6 writer + 4 reader threads, ~4 seconds. Zero sqlite
    OperationalError/IndexError/InterfaceError expected. Data must land.
    """

    def test_concurrent_workflow_and_run_writes_zero_sqlite_errors(self) -> None:
        errors: list[BaseException] = []
        counter_lock = threading.Lock()
        written_workflows = 0
        written_runs = 0

        with tempfile.TemporaryDirectory() as directory:
            store = AutomationStore(pathlib.Path(directory) / "stress.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer(n: int) -> None:
                nonlocal written_workflows, written_runs
                i = 0
                while not stop.is_set():
                    try:
                        store.save_workflow(_workflow(n * 1000 + i))
                        store.claim(_run(n * 100000 + i))
                        with counter_lock:
                            written_workflows += 1
                            written_runs += 1
                    except (sqlite3.OperationalError, IndexError, sqlite3.InterfaceError) as exc:
                        errors.append(exc)
                    except BaseException as exc:  # pragma: no cover
                        errors.append(exc)
                    i += 1

            def reader() -> None:
                while not stop.is_set():
                    try:
                        _ = store.schema_version()
                        _ = store.list_workflows(limit=20)
                        _ = store.list_runs(limit=20)
                        _ = store.due_workflows("9999-12-31T23:59:59+00:00")
                        _ = store.healthy()
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
            self.assertGreater(written_workflows, 100)
            self.assertGreater(written_runs, 100)
            # Workflows collapse by workflow_id (INSERT OR REPLACE, 10 ids).
            self.assertEqual(len(store.list_workflows(limit=50)), 10)
            # Every claimed run landed (unique slots).
            self.assertEqual(len(store.list_runs(limit=100000)), written_runs)

    def test_concurrent_session_writes_zero_errors(self) -> None:
        errors: list[BaseException] = []
        with tempfile.TemporaryDirectory() as directory:
            store = VoiceOsStore(pathlib.Path(directory) / "vstress.db")
            self.addCleanup(store.close)
            stop = threading.Event()

            def writer(n: int) -> None:
                i = 0
                while not stop.is_set():
                    try:
                        store.save_session(TaskSession(
                            session_id=f"s-{n}-{i % 20}",
                            actor_id="stress",
                            request=f"req {i}",
                        ))
                        store.save_conversation(Conversation(actor_id=f"a-{n}"))
                        _ = store.get_session(f"s-{n}-{i % 20}")
                        _ = store.get_conversation(f"a-{n}")
                        _ = store.list_sessions("stress", limit=20)
                    except BaseException as exc:
                        errors.append(exc)
                    i += 1

            threads = [threading.Thread(target=writer, args=(n,), daemon=True) for n in range(4)]
            for t in threads:
                t.start()
            time.sleep(2)
            stop.set()
            for t in threads:
                t.join(timeout=10)
            self.assertEqual(errors, [])
            self.assertTrue(store.healthy())


class TestPackagingExclusions(unittest.TestCase):
    """Pin package.json files exclusions without rebuilding the deb."""

    def _build_files(self) -> list[str]:
        pkg = pathlib.Path("desktop/package.json")
        data = json.loads(pkg.read_text())
        files = data["build"]["files"]
        assert isinstance(files, list)
        return [str(f) for f in files]

    def test_secret_patterns_excluded(self) -> None:
        files = self._build_files()
        for pattern in ("!**/*.key", "!**/settings.json", "!**/.env"):
            self.assertIn(pattern, files, f"{pattern} must be in build.files")

    def test_test_files_excluded(self) -> None:
        files = self._build_files()
        self.assertIn("!app/*.test.js", files)

    def test_desktop_name_top_level(self) -> None:
        data = json.loads(pathlib.Path("desktop/package.json").read_text())
        self.assertEqual(data["desktopName"], "actionmesh.desktop")
        self.assertTrue(data["build"]["linux"]["syncDesktopName"])

    def test_runtime_delivered_via_extra_resources_not_files(self) -> None:
        # runtime/ (incl. vendored certifi cacert.pem) is delivered via
        # extraResources, not the files allowlist. The files exclusion
        # "!runtime/**/*.pem" therefore does not strip the CA bundle
        # from the shipped runtime — the bundle is public trust data,
        # not a secret, and ships inside runtime/python/.../cacert.pem.
        data = json.loads(pathlib.Path("desktop/package.json").read_text())
        extra = data["build"]["extraResources"]
        self.assertTrue(any(e.get("from") == "runtime" for e in extra))


class TestMultiDeviceReadinessProbe(unittest.TestCase):
    """Read-only readiness check: forget + presence cascade contract.

    Does not duplicate Agent 3's registry tests — verifies the cascade
    wiring end-to-end with a fake presence object satisfying the
    PresenceForgetting protocol.
    """

    def test_forget_cascades_to_presence(self) -> None:
        from actionmesh.core.devices import DeviceRegistry
        from actionmesh.protocol.models import DeviceInfo, DeviceKind

        class FakePresence:
            def __init__(self) -> None:
                self.forgotten: list[str] = []
                self.forget_all_calls = 0

            def forget(self, device_id: str) -> bool:
                self.forgotten.append(device_id)
                return True

            def forget_all(self) -> bool:
                self.forget_all_calls += 1
                return False

        presence = FakePresence()
        registry = DeviceRegistry(presence=presence)  # type: ignore[arg-type]
        device = DeviceInfo(device_id="dev-1", kind=DeviceKind.LAPTOP)
        registry.register(device)
        self.assertTrue(registry.forget("dev-1"))
        self.assertEqual(presence.forgotten, ["dev-1"])
        self.assertEqual(registry.all_devices(), [])

    def test_forget_unknown_device_does_not_cascade(self) -> None:
        from actionmesh.core.devices import DeviceRegistry

        class FakePresence:
            def __init__(self) -> None:
                self.calls = 0

            def forget(self, device_id: str) -> bool:
                self.calls += 1
                return True

            def forget_all(self) -> bool:
                return False

        presence = FakePresence()
        registry = DeviceRegistry(presence=presence)  # type: ignore[arg-type]
        self.assertFalse(registry.forget("nope"))
        self.assertEqual(presence.calls, 0)


if __name__ == "__main__":
    unittest.main()
