"""Durable automations and their run history.

The fifth store family, and the same discipline as the other four: a versioned schema
that can tell a fresh database from a current one from one written by a newer program,
and an upgrade path that only ever adds.

Two tables, and one of them does a job the other cannot:

``automation_workflows``
    What the user saved. A plan, a trigger, and a switch. Nothing here is consulted
    when a decision about permission is made — the step list is a *proposal* that goes
    through the engine exactly as a sentence typed a moment ago.

``automation_runs``
    What actually happened, plus the ``(workflow_id, slot)`` uniqueness that makes a
    restart unable to run one occurrence twice. ``claim`` inserts first and asks
    afterwards: the row *is* the claim, so there is no window between "I checked" and
    "I started" in which a second process — or the same process after a crash — could
    slip through. A run row left at ``claimed`` by a process that stopped before it
    could start is reconciled to ``missed`` at startup, never silently deleted: a record
    of an occurrence that did not happen is the honest outcome, and a deleted one would
    let the next restart claim the same slot again.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from actionmesh.core.sqlite_guard import guarded, make_lock
from actionmesh.voiceos.workflows import AutomationRun, Workflow

#: Bump by one with each migration appended below.
SCHEMA_VERSION = 1

#: How many run rows to keep. A workflow on a five-minute interval produces 105,000 rows
#: a year; the last few thousand are the history and the rest is a liability.
MAX_RUNS = 2000


def _migrate_v0_to_v1(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS automation_workflows "
        "(workflow_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, "
        " enabled INTEGER NOT NULL, updated_at TEXT NOT NULL, "
        " next_run_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS automation_workflows_actor "
        "ON automation_workflows (actor_id, updated_at DESC)"
    )
    # A partial index over the schedules that are live is what the ticker scans. It is
    # partial because a disabled or manual workflow has no next_run_at worth indexing.
    connection.execute(
        "CREATE INDEX IF NOT EXISTS automation_workflows_next "
        "ON automation_workflows (next_run_at) WHERE next_run_at != ''"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS automation_runs "
        "(run_id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, actor_id TEXT NOT NULL, "
        " slot TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, "
        " payload TEXT NOT NULL, "
        # The whole duplicate-suppression story, in one constraint. A schedule's slot is
        # the due timestamp; an event's is the event id; a manual press gets a fresh id
        # and so is never suppressed.
        " UNIQUE (workflow_id, slot))"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS automation_runs_recent "
        "ON automation_runs (created_at DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS automation_runs_workflow "
        "ON automation_runs (workflow_id, created_at DESC)"
    )


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_migrate_v0_to_v1,)


class SchemaError(RuntimeError):
    """The stored schema is not one this program can safely open."""


class AutomationStore:
    """Workflow and run persistence, standard library only."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self._lock = make_lock()
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self._open_schema()

    def _open_schema(self) -> None:
        version = self._user_version()
        if version > len(MIGRATIONS):
            self.connection.close()
            raise SchemaError(
                f"automation store is schema {version}; "
                f"this program knows {len(MIGRATIONS)}"
            )
        for step in range(version, len(MIGRATIONS)):
            MIGRATIONS[step](self.connection)
            self._set_user_version(step + 1)
        self.connection.commit()

    def _user_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def _set_user_version(self, version: int) -> None:
        # PRAGMA cannot be parameterised; version is a program constant, never input.
        # The bounds check makes that structural: a value outside ``0 <= n <= 2147483647``
        # raises here rather than reaching the statement, so the interpolation below can
        # only ever receive decimal digits.
        if type(version) is not int or not 0 <= version <= 2147483647:
            raise ValueError(f"schema version must be a small non-negative int, got {version!r}")
        # nosemgrep: formatted-sql-query, sqlalchemy-execute-raw-query
        # Neither can be satisfied here: PRAGMA accepts no bound parameters, and the value
        # is already proven to be an int in sqlite's own range, so it renders as digits.
        self.connection.execute(f"PRAGMA user_version={version}")

    @guarded

    def schema_version(self) -> int:
        return self._user_version()

    # -- workflows ---------------------------------------------------------

    @guarded

    def save_workflow(self, workflow: Workflow) -> None:
        self.connection.execute(
            "INSERT INTO automation_workflows "
            "(workflow_id, actor_id, enabled, updated_at, next_run_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(workflow_id) DO UPDATE SET actor_id=excluded.actor_id, "
            "enabled=excluded.enabled, updated_at=excluded.updated_at, "
            "next_run_at=excluded.next_run_at, payload=excluded.payload",
            (
                workflow.workflow_id,
                workflow.actor_id,
                int(workflow.enabled),
                workflow.updated_at,
                workflow.next_run_at,
                json.dumps(workflow.to_dict()),
            ),
        )
        self.connection.commit()

    @guarded

    def get_workflow(self, workflow_id: str) -> Workflow | None:
        row = self.connection.execute(
            "SELECT payload FROM automation_workflows WHERE workflow_id = ?",
            (workflow_id,),
        ).fetchone()
        return Workflow.from_dict(json.loads(row[0])) if row else None

    @guarded

    def list_workflows(self, actor_id: str | None = None, limit: int = 100) -> list[Workflow]:
        """Newest first. ``actor_id=None`` is the operator's unscoped view."""
        if actor_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM automation_workflows "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM automation_workflows WHERE actor_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (actor_id, limit),
            ).fetchall()
        return [Workflow.from_dict(json.loads(row[0])) for row in rows]

    @guarded

    def delete_workflow(self, workflow_id: str) -> bool:
        """Remove a workflow. Its run history is kept: it is a record, not a child."""
        cursor = self.connection.execute(
            "DELETE FROM automation_workflows WHERE workflow_id = ?", (workflow_id,)
        )
        self.connection.commit()
        return cursor.rowcount > 0

    @guarded

    def due_workflows(self, now_iso: str, limit: int = 20) -> list[Workflow]:
        """Enabled workflows whose next run is at or before ``now_iso``.

        The comparison is a string compare on ISO-8601 UTC timestamps, which orders
        correctly only because every writer of ``next_run_at`` uses ``_now``-shaped UTC.
        :func:`actionmesh.voiceos.workflows.next_occurrence` is the only such writer.
        """
        rows = self.connection.execute(
            "SELECT payload FROM automation_workflows "
            "WHERE enabled = 1 AND next_run_at != '' AND next_run_at <= ? "
            "ORDER BY next_run_at ASC LIMIT ?",
            (now_iso, limit),
        ).fetchall()
        return [Workflow.from_dict(json.loads(row[0])) for row in rows]

    # -- runs --------------------------------------------------------------

    @guarded

    def claim(self, run: AutomationRun) -> bool:
        """Write the run row, or report that someone else already owns the slot.

        Returns True only for the caller that inserted. This is deliberately a database
        constraint rather than a lookup followed by an insert: between those two there
        is a window, and a restart that lands in it would run the occurrence twice.
        """
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO automation_runs "
            "(run_id, workflow_id, actor_id, slot, state, created_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run.run_id,
                run.workflow_id,
                run.actor_id,
                run.slot,
                run.state,
                run.created_at,
                json.dumps(run.to_dict()),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    @guarded

    def save_run(self, run: AutomationRun) -> None:
        self.connection.execute(
            "UPDATE automation_runs SET state = ?, payload = ? WHERE run_id = ?",
            (run.state, json.dumps(run.to_dict()), run.run_id),
        )
        self.connection.commit()
        self._trim_runs()

    @guarded

    def get_run(self, run_id: str) -> AutomationRun | None:
        row = self.connection.execute(
            "SELECT payload FROM automation_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        return AutomationRun.from_dict(json.loads(row[0])) if row else None

    @guarded

    def run_for_slot(self, workflow_id: str, slot: str) -> AutomationRun | None:
        row = self.connection.execute(
            "SELECT payload FROM automation_runs WHERE workflow_id = ? AND slot = ?",
            (workflow_id, slot),
        ).fetchone()
        return AutomationRun.from_dict(json.loads(row[0])) if row else None

    @guarded

    def list_runs(
        self,
        *,
        actor_id: str | None = None,
        workflow_id: str | None = None,
        limit: int = 50,
    ) -> list[AutomationRun]:
        """Most recent first — the automation history view reads exactly this."""
        clauses: list[str] = []
        params: list[object] = []
        if actor_id is not None:
            clauses.append("actor_id = ?")
            params.append(actor_id)
        if workflow_id is not None:
            clauses.append("workflow_id = ?")
            params.append(workflow_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        rows = self.connection.execute(
            f"SELECT payload FROM automation_runs {where}"
            "ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [AutomationRun.from_dict(json.loads(row[0])) for row in rows]

    @guarded

    def reconcile_claimed(self) -> list[AutomationRun]:
        """A run a previous process claimed and never started becomes ``missed``.

        Called once at startup, before the ticker runs. Doing nothing here would be a
        lie twice over: the history would show a run that never happened, and a slot
        claimed by a dead process would never be retried, so the user would be told a
        schedule is working when it silently stopped firing. Marking it ``missed`` says
        exactly what is true — the occurrence was due, this program was not there for it
        — and the next occurrence is scheduled normally.
        """
        rows = self.connection.execute(
            "SELECT payload FROM automation_runs WHERE state = 'claimed'"
        ).fetchall()
        missed: list[AutomationRun] = []
        for row in rows:
            run = AutomationRun.from_dict(json.loads(row[0]))
            run.state = "missed"
            run.error = (
                "the program stopped after claiming this occurrence and before "
                "starting it; it was not run"
            )
            run.finished_at = run.finished_at or run.created_at
            run.steps = [
                {**step, "state": "skipped", "error": "the run never started"}
                for step in run.steps
            ]
            self.save_run(run)
            missed.append(run)
        return missed

    def _trim_runs(self) -> None:
        """Keep the newest ``MAX_RUNS``; the tail is deleted in one statement."""
        self.connection.execute(
            "DELETE FROM automation_runs WHERE run_id NOT IN ("
            "SELECT run_id FROM automation_runs ORDER BY created_at DESC LIMIT ?)",
            (MAX_RUNS,),
        )
        self.connection.commit()

    @guarded

    def healthy(self) -> bool:
        try:
            self.connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    @guarded

    def close(self) -> None:
        self.connection.close()


__all__ = [
    "MAX_RUNS",
    "MIGRATIONS",
    "SCHEMA_VERSION",
    "AutomationStore",
    "SchemaError",
]