"""C15 SQLite persistence for execution records and mesh events.

The schema is versioned with SQLite's ``PRAGMA user_version`` so a restart against an
existing file can tell three things apart, which an un-versioned store cannot: a fresh
database (create it), a current one (use it), and one written by a *newer* program
(refuse it). Migration ordering is a fixed list applied one step at a time, never a
free-for-all of ``CREATE TABLE IF NOT EXISTS`` whose meaning depends on what already
ran. Nothing here deletes or rewrites existing rows: an upgrade only adds.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from actionmesh.protocol.models import ActionState, ExecutionRecord, MeshEvent

#: States a stored run may still be in. Kept here as the mirror of
#: ``executor.lifecycle.TERMINAL``: importing that module from storage would be a
#: circular dependency, and a registry that answers "which runs are live?" has to know.
_TERMINAL_STATES = frozenset(
    {
        "success",
        "failed",
        "cancelled",
        "denied",
        "timeout",
        "partially_completed",
        "interrupted",
    }
)

#: The schema this code writes. Bump by one with each migration appended below.
SCHEMA_VERSION = 3


def _migrate_v0_to_v1(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_records "
        "(execution_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS mesh_events "
        "(event_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, payload TEXT NOT NULL)"
    )


def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
    """C35 — the execution as a durable object, with an owner and a cancellation flag.

    These are columns rather than payload fields because the questions that matter are
    queries: "which runs are not finished?", "who owns this one?", "has anyone asked it
    to stop?" A scan of every JSON blob is not a registry. The payload still carries the
    whole record; the columns are the index that keeps the truth reachable.
    """
    connection.execute("ALTER TABLE execution_records ADD COLUMN state TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE execution_records ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''")
    connection.execute("ALTER TABLE execution_records ADD COLUMN owner_id TEXT NOT NULL DEFAULT ''")
    connection.execute(
        "ALTER TABLE execution_records ADD COLUMN lease_expires_at TEXT NOT NULL DEFAULT ''"
    )
    connection.execute(
        "ALTER TABLE execution_records ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS execution_records_state ON execution_records (state)"
    )
    # A run that is not finished is only answerable after a restart if the graph and
    # the context it started with are still somewhere. They live here, and nowhere
    # else: neither the approval flow nor reconciliation may re-accept them from a caller.
    connection.execute(
        "CREATE TABLE IF NOT EXISTS execution_body "
        "(execution_id TEXT PRIMARY KEY, graph TEXT NOT NULL, context TEXT NOT NULL)"
    )


def _migrate_v2_to_v3(connection: sqlite3.Connection) -> None:
    """C35 phase 6 — what this laptop agent actually dispatched, and for which execution.

    The execution record says a run was started. This says what real work came out of
    it: a child program, its process group, whether it is still there, and the small
    effect description the verifier reads. It belongs in the same file as the records
    for exactly the reason the records do: neither is answerable after a restart from
    process memory, and reconciliation has to ask both.

    ``observation`` is the effect *shape* (which command, which exit code, how many
    items) and never a program's output. Bytes some command chose to print are not
    evidence, and are not kept here.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS agent_dispatch ("
        "action_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, "
        "process_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', "
        "pid INTEGER NOT NULL DEFAULT 0, pgid INTEGER NOT NULL DEFAULT 0, "
        "state TEXT NOT NULL DEFAULT 'running', exit_code INTEGER, "
        "observation TEXT NOT NULL DEFAULT '', "
        "started_at TEXT NOT NULL DEFAULT '', ended_at TEXT NOT NULL DEFAULT '')"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS agent_dispatch_execution "
        "ON agent_dispatch (execution_id)"
    )
    # C35 was written in two steps, and a development database from the first of them
    # carries the earlier name. Only a file that actually has it is touched.
    legacy = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_parked'"
    ).fetchone()
    current = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='execution_body'"
    ).fetchone()
    if legacy and not current:
        connection.execute("ALTER TABLE execution_parked RENAME TO execution_body")


#: Each entry upgrades from its index to index+1. Appending a step is the only way the
#: version moves; there is no branching on "does this table exist".
MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (
    _migrate_v0_to_v1,
    _migrate_v1_to_v2,
    _migrate_v2_to_v3,
)


class SchemaError(RuntimeError):
    """The stored schema is not one this program can safely open."""


class SQLiteStore:
    """Small durable store using only the Python standard library."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self._open_schema()

    def _open_schema(self) -> None:
        version = self._user_version()
        if version > len(MIGRATIONS):
            # A file from a newer ActionMesh. Migrating "down" would mean guessing
            # which columns to drop and losing data, so refuse and say why -- but
            # close first, so a refused open does not leak the handle we just made.
            self.connection.close()
            raise SchemaError(
                f"database schema version {version} is newer than this program's "
                f"{len(MIGRATIONS)}; upgrade ActionMesh rather than opening it here"
            )
        for step in range(version, len(MIGRATIONS)):
            MIGRATIONS[step](self.connection)
            self._set_user_version(step + 1)
        self.connection.commit()

    def _user_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row and row[0] else 0

    def _set_user_version(self, version: int) -> None:
        # PRAGMA cannot be parameterised; version is a program constant, never input.
        self.connection.execute(f"PRAGMA user_version={int(version)}")

    @property
    def schema_version(self) -> int:
        return self._user_version()

    def save_record(self, record: ExecutionRecord) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO execution_records(execution_id, payload, state, updated_at, "
            "owner_id, lease_expires_at, cancel_requested) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                record.execution_id,
                record.model_dump_json(),
                record.state.value,
                record.updated_at.isoformat() if record.updated_at else "",
                record.owner_id,
                record.lease_expires_at.isoformat() if record.lease_expires_at else "",
                1 if record.cancel_requested else 0,
            ),
        )
        self.connection.commit()

    def get_record(self, execution_id: str) -> ExecutionRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM execution_records WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        return _record_from_payload(row[0]) if row else None

    def list_non_terminal(self) -> list[ExecutionRecord]:
        """Every run this store believes is still going, oldest first.

        The reconciliation input. It answers from the columns rather than the payloads
        so a fresh process can ask the question before it has any of its own state.
        """
        placeholders = ", ".join("?" for _ in _TERMINAL_STATES)
        rows = self.connection.execute(
            f"SELECT payload FROM execution_records WHERE state NOT IN ({placeholders}) "
            "ORDER BY updated_at ASC",
            tuple(sorted(_TERMINAL_STATES)),
        ).fetchall()
        return [_record_from_payload(row[0]) for row in rows]

    def mark_cancel_requested(self, execution_id: str) -> bool:
        """Record a cancellation against the execution itself. True if it was recorded.

        Refused once the run is terminal: a completed execution must never be turned
        into a cancelled one by a late press. The payload is rewritten rather than the
        column poked, because the payload is the record and the columns are its index —
        updating one and reading the other is how a flag becomes a rumour.
        """
        record = self.get_record(execution_id)
        if record is None or record.state.value in _TERMINAL_STATES or record.cancel_requested:
            return False
        record.cancel_requested = True
        record.updated_at = datetime.now(UTC)
        self.save_record(record)
        return True

    def list_recent(self, limit: int = 25) -> list[ExecutionRecord]:
        """The newest records this store holds, whichever process wrote them."""
        rows = self.connection.execute(
            "SELECT payload FROM execution_records ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [_record_from_payload(row[0]) for row in rows]

    def release_leases(self, owner_id: str) -> int:
        """Give up the runs a departing process was watching. Returns how many.

        Called on shutdown. Clearing the lease is what tells the next process that
        nobody is looking any more, so it reconciles immediately instead of waiting for
        a lease to expire — and it is honest only because the owner is naming its own
        runs, not whoever happens to be in the table.
        """
        rows = self.connection.execute(
            "SELECT payload FROM execution_records WHERE owner_id = ? AND state NOT IN ("
            + ", ".join("?" for _ in _TERMINAL_STATES)
            + ")",
            (owner_id, *sorted(_TERMINAL_STATES)),
        ).fetchall()
        for row in rows:
            record = _record_from_payload(row[0])
            record.lease_expires_at = None
            record.updated_at = datetime.now(UTC)
            self.save_record(record)
        self.connection.commit()
        return len(rows)

    def save_body(self, execution_id: str, graph_json: str, context_json: str) -> None:
        """Keep what a run is, so a later process can answer for it.

        Written the moment an execution starts, not only when it stops for a human:
        reconciliation of a half-finished run needs the same graph the first process had.
        """
        self.connection.execute(
            "INSERT OR REPLACE INTO execution_body(execution_id, graph, context) "
            "VALUES (?, ?, ?)",
            (execution_id, graph_json, context_json),
        )
        self.connection.commit()

    def load_body(self, execution_id: str) -> tuple[str, str] | None:
        """The stored graph and context JSON, or ``None`` if no run was recorded.

        Returned as text on purpose: a store that rehydrates domain objects is a store
        that has started making decisions. Rebuilding is the engine's job.
        """
        row = self.connection.execute(
            "SELECT graph, context FROM execution_body WHERE execution_id = ?",
            (execution_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def drop_body(self, execution_id: str) -> None:
        self.connection.execute("DELETE FROM execution_body WHERE execution_id = ?", (execution_id,))
        self.connection.commit()

    def set_owner(self, execution_id: str, owner_id: str, lease_expires_at: datetime) -> None:
        self.connection.execute(
            "UPDATE execution_records SET owner_id = ?, lease_expires_at = ?, updated_at = ? "
            "WHERE execution_id = ?",
            (owner_id, lease_expires_at.isoformat(), datetime.now(UTC).isoformat(), execution_id),
        )
        self.connection.commit()


    # -- dispatched work --------------------------------------------------
    def save_dispatch(self, row: dict[str, Any]) -> None:
        """Record (or update) one piece of real work this agent started.

        Takes a plain dict of primitives, not a domain object: the store's job is to
        remember what it is told, and a store that reconstructs agent types would be
        one more place that decides what a process's state means.
        """
        self.connection.execute(
            "INSERT OR REPLACE INTO agent_dispatch(action_id, execution_id, process_id, "
            "kind, pid, pgid, state, exit_code, observation, started_at, ended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                row.get("action_id", ""),
                row.get("execution_id", ""),
                row.get("process_id", ""),
                row.get("kind", ""),
                int(row.get("pid") or 0),
                int(row.get("pgid") or 0),
                row.get("state", "running"),
                row.get("exit_code"),
                row.get("observation", ""),
                row.get("started_at", ""),
                row.get("ended_at", ""),
            ),
        )
        self.connection.commit()

    def list_dispatch(self, execution_id: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT action_id, execution_id, process_id, kind, pid, pgid, state, "
            "exit_code, observation, started_at, ended_at FROM agent_dispatch "
            "WHERE execution_id = ? ORDER BY started_at",
            (execution_id,),
        ).fetchall()
        return [
            dict(zip(_DISPATCH_COLUMNS, row, strict=True)) for row in rows
        ]

    def get_dispatch(self, action_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT action_id, execution_id, process_id, kind, pid, pgid, state, "
            "exit_code, observation, started_at, ended_at FROM agent_dispatch "
            "WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        return dict(zip(_DISPATCH_COLUMNS, row, strict=True)) if row else None

    def trim_dispatch(self, *, keep: int = 500) -> int:
        """Forget the oldest dispatched work, so a long-lived agent does not accumulate.

        Bounded on purpose: a row is small, but "small" multiplied by weeks of `run my
        tests` is not, and nothing reads a month-old child record except a reconciliation
        that is looking for a run of its own.
        """
        cursor = self.connection.execute(
            "DELETE FROM agent_dispatch WHERE action_id NOT IN "
            "(SELECT action_id FROM agent_dispatch ORDER BY started_at DESC LIMIT ?)",
            (int(keep),),
        )
        self.connection.commit()
        return cursor.rowcount

    def save_event(self, event: MeshEvent) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO mesh_events(event_id, execution_id, payload) VALUES (?, ?, ?)",
            (event.event_id, event.execution_id, event.model_dump_json()),
        )
        self.connection.commit()

    def list_events(
        self, execution_id: str | None = None, *, limit: int = 500
    ) -> list[MeshEvent]:
        """The retained timeline, oldest first, as another process left it.

        Insertion order (rowid) rather than ``event_id``, because ids are unguessable
        and therefore meaningless to sort by. Bounded by ``limit`` from the tail: a
        reconnecting client wants the recent story, and an unbounded read here would let
        any watch cost the server whatever the history happens to have grown to.
        """
        if execution_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM mesh_events ORDER BY rowid DESC LIMIT ?", (int(limit),)
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM mesh_events WHERE execution_id = ? "
                "ORDER BY rowid DESC LIMIT ?",
                (execution_id, int(limit)),
            ).fetchall()
        return [_event_from_payload(row[0]) for row in reversed(rows)]

    def trim_events(self, *, keep: int = 5000) -> int:
        """Forget the oldest events, keeping the window a reconnect can actually use."""
        cursor = self.connection.execute(
            "DELETE FROM mesh_events WHERE rowid NOT IN "
            "(SELECT rowid FROM mesh_events ORDER BY rowid DESC LIMIT ?)",
            (int(keep),),
        )
        self.connection.commit()
        return cursor.rowcount

    def event_count(self, execution_id: str | None = None) -> int:
        if execution_id is None:
            row = self.connection.execute("SELECT COUNT(*) FROM mesh_events").fetchone()
        else:
            row = self.connection.execute(
                "SELECT COUNT(*) FROM mesh_events WHERE execution_id = ?", (execution_id,)
            ).fetchone()
        return int(row[0])

    def healthy(self) -> bool:
        """A cheap readiness probe: can we still read our own schema version."""
        try:
            self.connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    def close(self) -> None:
        self.connection.close()


_DISPATCH_COLUMNS = (
    "action_id",
    "execution_id",
    "process_id",
    "kind",
    "pid",
    "pgid",
    "state",
    "exit_code",
    "observation",
    "started_at",
    "ended_at",
)


def _event_from_payload(payload_text: str) -> MeshEvent:
    raw = json.loads(payload_text)
    return MeshEvent(
        event_id=str(raw.get("event_id", "")),
        event_type=str(raw.get("event_type", "")),
        timestamp=_parse(raw.get("timestamp")) or datetime.now(UTC),
        execution_id=str(raw.get("execution_id", "")),
        action_id=str(raw.get("action_id", "")),
        actor=str(raw.get("actor", "system")),
        payload=dict(raw.get("payload") or {}),
    )


def _record_from_payload(payload_text: str) -> ExecutionRecord:
    """Rebuild a record from its stored JSON.

    A record written before C35 simply has no lifecycle fields; reading it must not
    invent a lease or a cancellation it never had, so every new field defaults quietly.
    """
    payload = json.loads(payload_text)
    started = _parse(payload.get("started_at"))
    return ExecutionRecord(
        execution_id=payload["execution_id"],
        graph_id=payload["graph_id"],
        actor_id=payload["actor_id"],
        state=ActionState(payload["state"]),
        started_at=started or datetime.now(UTC),
        finished_at=_parse(payload.get("finished_at")),
        node_states=payload["node_states"],
        results=payload["results"],
        error=payload["error"],
        updated_at=_parse(payload.get("updated_at")),
        owner_id=payload.get("owner_id", ""),
        lease_expires_at=_parse(payload.get("lease_expires_at")),
        cancel_requested=bool(payload.get("cancel_requested", False)),
        device_id=payload.get("device_id", ""),
        device_name=payload.get("device_name", ""),
    )


def _parse(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SQLiteStore", "SchemaError"]
