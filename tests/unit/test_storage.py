"""C15 SQLite persistence tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from actionmesh.core.storage import SQLiteStore
from actionmesh.protocol.models import ActionState, ExecutionRecord


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


if __name__ == "__main__":
    unittest.main()