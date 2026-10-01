"""C15 SQLite persistence for execution records and mesh events."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from actionmesh.protocol.models import ActionState, ExecutionRecord, MeshEvent


class SQLiteStore:
    """Small durable store using only the Python standard library."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.connection = sqlite3.connect(str(path), check_same_thread=False)
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS execution_records "
            "(execution_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS mesh_events "
            "(event_id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.connection.commit()

    def save_record(self, record: ExecutionRecord) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO execution_records(execution_id, payload) VALUES (?, ?)",
            (record.execution_id, record.model_dump_json()),
        )
        self.connection.commit()

    def get_record(self, execution_id: str) -> ExecutionRecord | None:
        row = self.connection.execute(
            "SELECT payload FROM execution_records WHERE execution_id = ?", (execution_id,)
        ).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        return ExecutionRecord(
            execution_id=payload["execution_id"],
            graph_id=payload["graph_id"],
            actor_id=payload["actor_id"],
            state=ActionState(payload["state"]),
            node_states=payload["node_states"],
            results=payload["results"],
            error=payload["error"],
        )

    def save_event(self, event: MeshEvent) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO mesh_events(event_id, execution_id, payload) VALUES (?, ?, ?)",
            (event.event_id, event.execution_id, event.model_dump_json()),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()


__all__ = ["SQLiteStore"]