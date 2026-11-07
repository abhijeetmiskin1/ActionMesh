"""History queries keep their bounded access path when the database grows."""

import sqlite3

from actionmesh.core.storage import MIGRATIONS, SQLiteStore
from actionmesh.protocol.models import ActionState, ExecutionRecord


def test_v3_upgrade_adds_history_indexes_and_preserves_records(tmp_path) -> None:
    path = tmp_path / 'history.db'
    old = sqlite3.connect(path)
    for migration in MIGRATIONS[:3]:
        migration(old)
    record = ExecutionRecord(execution_id='kept', graph_id='graph', state=ActionState.SUCCESS)
    old.execute('INSERT INTO execution_records(execution_id,payload,state) VALUES (?,?,?)',
                ('kept', record.model_dump_json(), 'success'))
    old.execute('PRAGMA user_version=3')
    old.commit()
    old.close()
    store = SQLiteStore(path)
    try:
        assert store.get_record('kept').state is ActionState.SUCCESS
        plan = store.connection.execute('EXPLAIN QUERY PLAN SELECT payload FROM execution_records ORDER BY updated_at DESC LIMIT 25').fetchall()
        assert any('execution_records_recent' in row[3] for row in plan)
        assert not any('TEMP B-TREE' in row[3] for row in plan)
        events = store.connection.execute('EXPLAIN QUERY PLAN SELECT payload FROM mesh_events WHERE execution_id = ?', ('kept',)).fetchall()
        assert any('mesh_events_execution' in row[3] for row in events)
    finally:
        store.close()
    reopened = SQLiteStore(path)
    try:
        assert reopened.get_record('kept').state is ActionState.SUCCESS
        assert reopened.schema_version == 4
    finally:
        reopened.close()
