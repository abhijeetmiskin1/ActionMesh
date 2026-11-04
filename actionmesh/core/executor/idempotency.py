"""C5 — idempotency ledger.

Prevents a retried request from producing a second side effect (a duplicate
message, a double booking, a double payment).

The key point: the ledger is consulted **inside** the executor, immediately
before dispatch, and the entry is recorded **before** the provider is called.
Recording afterwards would leave a window where a crash mid-dispatch causes the
retry to run the action twice — which is the exact failure this exists to stop.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from actionmesh.core.sqlite_guard import guarded, make_lock


@dataclass
class _Entry:
    state: str  # "running" | "done" | "failed"
    result: Any = None
    started_at: float = field(default_factory=time.monotonic)


class IdempotencyLedger:
    """In-memory record of executed idempotency keys. C15 moves this to SQLite."""

    def __init__(self) -> None:
        self._entries: dict[str, _Entry] = {}

    def begin(self, key: str) -> tuple[bool, Any]:
        """Claim ``key``.

        Returns ``(is_duplicate, previous_result)``. A key already marked
        ``done`` is a duplicate; a key still ``running`` means a concurrent
        attempt owns it, which is also treated as a duplicate so the work is
        not performed twice.
        """
        entry = self._entries.get(key)
        if entry is None:
            self._entries[key] = _Entry(state="running")
            return False, None
        if entry.state == "failed":
            self._entries[key] = _Entry(state="running")
            return False, None
        return True, entry.result

    def complete(self, key: str, result: Any) -> None:
        self._entries[key] = _Entry(state="done", result=result)

    def fail(self, key: str) -> None:
        """Mark a key as failed, releasing the claim so a retry may proceed."""
        self._entries.pop(key, None)

    def release(self, key: str) -> None:
        """Drop a claim entirely (used on cancellation)."""
        self._entries.pop(key, None)

    def state(self, key: str) -> str | None:
        entry = self._entries.get(key)
        return entry.state if entry else None

    def __len__(self) -> int:
        return len(self._entries)


class SQLiteIdempotencyLedger:
    """Durable drop-in ledger for restart-safe duplicate suppression."""

    def __init__(self, path: str = ":memory:") -> None:
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self._lock = make_lock()
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS idempotency_entries "
            "(key TEXT PRIMARY KEY, state TEXT NOT NULL, result TEXT)"
        )
        self.connection.commit()

    @guarded
    def begin(self, key: str) -> tuple[bool, Any]:
        try:
            self.connection.execute(
                "INSERT INTO idempotency_entries(key, state) VALUES (?, 'running')", (key,)
            )
            self.connection.commit()
            return False, None
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT result FROM idempotency_entries WHERE key = ?", (key,)
            ).fetchone()
            return True, json.loads(row[0]) if row and row[0] else None

    @guarded
    def complete(self, key: str, result: Any) -> None:
        self.connection.execute(
            "UPDATE idempotency_entries SET state = 'done', result = ? WHERE key = ?",
            (json.dumps(result), key),
        )
        self.connection.commit()

    @guarded
    def fail(self, key: str) -> None:
        self.release(key)

    @guarded
    def release(self, key: str) -> None:
        self.connection.execute("DELETE FROM idempotency_entries WHERE key = ?", (key,))
        self.connection.commit()

    @guarded
    def state(self, key: str) -> str | None:
        row = self.connection.execute(
            "SELECT state FROM idempotency_entries WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    @guarded
    def close(self) -> None:
        self.connection.close()


__all__ = ["IdempotencyLedger", "SQLiteIdempotencyLedger"]
