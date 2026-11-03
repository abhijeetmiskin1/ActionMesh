"""Durable task sessions and conversation context.

Same discipline as the execution store: a versioned schema that can tell a fresh
database from a current one from one written by a newer program, and an upgrade path
that only ever adds. A restart must not silently forget a task the user was mid-way
through — that is the whole point of persisting this at all.

The tables hold *interaction* state only. Nothing in here is consulted when a decision
about permission is made, and a session row that named a grant would be read by nobody
and trusted by worse.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from pathlib import Path

from actionmesh.voiceos.models import Conversation, TaskSession

#: Bump by one with each migration appended below.
SCHEMA_VERSION = 1


def _migrate_v0_to_v1(connection: sqlite3.Connection) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS voiceos_sessions "
        "(session_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, "
        " updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS voiceos_conversation "
        "(actor_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS voiceos_sessions_actor "
        "ON voiceos_sessions (actor_id, updated_at DESC)"
    )


MIGRATIONS: tuple[Callable[[sqlite3.Connection], None], ...] = (_migrate_v0_to_v1,)


class SchemaError(RuntimeError):
    """The stored schema is not one this program can safely open."""


class VoiceOsStore:
    """Session and conversation persistence, standard library only."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, check_same_thread=False)
        self._open_schema()

    def _open_schema(self) -> None:
        version = self._user_version()
        if version > len(MIGRATIONS):
            self.connection.close()
            raise SchemaError(
                f"VoiceOS store is schema {version}; this program knows {len(MIGRATIONS)}"
            )
        for step in range(version, len(MIGRATIONS)):
            MIGRATIONS[step](self.connection)
            self._set_user_version(step + 1)
        self.connection.commit()

    def _user_version(self) -> int:
        row = self.connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row else 0

    def _set_user_version(self, version: int) -> None:
        self.connection.execute(f"PRAGMA user_version={int(version)}")

    def schema_version(self) -> int:
        return self._user_version()

    # -- sessions ----------------------------------------------------------

    def save_session(self, session: TaskSession) -> None:
        self.connection.execute(
            "INSERT INTO voiceos_sessions (session_id, actor_id, updated_at, payload) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET updated_at=excluded.updated_at, "
            "payload=excluded.payload",
            # Storage, not the client view: a session reloaded without its `bind` and
            # `analysis` fields is a different plan -- the bound log read loses its
            # process id, and the explanation placeholder becomes a dispatchable step
            # with no action type, which fails the graph validator.
            (session.session_id, session.actor_id, session.updated_at, json.dumps(session.to_storage())),
        )
        self.connection.commit()

    def get_session(self, session_id: str) -> TaskSession | None:
        row = self.connection.execute(
            "SELECT payload FROM voiceos_sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        return TaskSession.from_dict(json.loads(row[0])) if row else None

    def list_sessions(self, actor_id: str | None, limit: int = 20) -> list[TaskSession]:
        """Most recent sessions. ``actor_id=None`` is the operator's unscoped view."""
        if actor_id is None:
            rows = self.connection.execute(
                "SELECT payload FROM voiceos_sessions ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT payload FROM voiceos_sessions WHERE actor_id = ? "
                "ORDER BY updated_at DESC LIMIT ?",
                (actor_id, limit),
            ).fetchall()
        return [TaskSession.from_dict(json.loads(row[0])) for row in rows]

    # -- conversation ------------------------------------------------------

    def get_conversation(self, actor_id: str) -> Conversation:
        row = self.connection.execute(
            "SELECT payload FROM voiceos_conversation WHERE actor_id = ?", (actor_id,)
        ).fetchone()
        if row is None:
            return Conversation(actor_id=actor_id)
        return Conversation.from_dict(json.loads(row[0]))

    def save_conversation(self, conversation: Conversation) -> None:
        conversation.updated_at = conversation.updated_at
        self.connection.execute(
            "INSERT INTO voiceos_conversation (actor_id, updated_at, payload) VALUES (?, ?, ?) "
            "ON CONFLICT(actor_id) DO UPDATE SET updated_at=excluded.updated_at, "
            "payload=excluded.payload",
            (
                conversation.actor_id,
                conversation.updated_at,
                json.dumps(conversation.to_dict()),
            ),
        )
        self.connection.commit()

    def clear_conversation(self, actor_id: str) -> None:
        self.connection.execute(
            "DELETE FROM voiceos_conversation WHERE actor_id = ?", (actor_id,)
        )
        self.connection.commit()

    def healthy(self) -> bool:
        try:
            self.connection.execute("SELECT 1").fetchone()
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        self.connection.close()


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "SchemaError", "VoiceOsStore"]
