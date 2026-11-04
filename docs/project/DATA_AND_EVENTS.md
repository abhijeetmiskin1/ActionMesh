# Data and Events

## Database schema (`actionmesh/core/storage.py`, `SQLiteStore`)

| Table | Contents | Notes |
|---|---|---|
| `execution_records` | One row per execution (v0→v1 migration) | Record payload serialized; `_record_from_payload` |
| `mesh_events` | Persisted non-transient events | `save_event` / `list_events` / `trim_events(keep=5000)` |
| `execution_body` | Graph + context JSON per execution (v1→v2) | `save_body` / `load_body` / `drop_body`; enables resume after restart |
| `agent_dispatch` | Per-action dispatch log (v2→v3) | `save_dispatch` / `list_dispatch` / `get_dispatch(action_id)` / `trim_dispatch(keep=500)` |
| `audit_records` | Hash-chained audit (`core/audit/sqlite.py::SqliteAuditBackend`) | `compute_hash` per record; `AuditLog.verify()` walks the chain |
| `user_version` | Schema version (`PRAGMA user_version`) | `_user_version` / `_set_user_version` / `schema_version` |

**Identifiers:** `execution_id`, `action_id` (per node), `device_id`,
`identity_id`, `session_id` (VoiceOS), `workflow_id` (automations),
`ticket` (SSE), `payment_id` (mock payments). **Relationships:** one
execution → many nodes/actions → many events/dispatch rows; one session
→ many steps; one device → many identities (history) with one active.

**Lifecycle:** executions are durable — `execution_body` + leases
(`set_owner`, `release_leases`, `sweep_lapsed_leases`) let a restarted
process resume or reconcile (`reconcile_interrupted`). Terminal records
can be trimmed; events trimmed to 5,000; dispatch to 500.

## Event formats (`core/events.py`, `protocol/models.py`)

```jsonc
// MeshEvent (persisted, non-transient)
{
  "execution_id": "…",
  "type": "execution.node.completed | execution.completed | …",
  "payload": { /* safe result view — allowlisted fields only */ },
  "timestamp": "2026-…Z"
}

// Transient event (live output) — subscribers only + bounded buffer
{ "execution_id": "…", "type": "output.chunk", "payload": {"stream": "stdout", "chunk": "…"} }
```

## SSE streams & tickets

- `GET /events/stream-ticket` mints a **ticket** scoped to the calling
  actor (`api.py::_stream_principal`); anonymous/loopback rules are
  enforced server-side.
- `GET /events?ticket=…` streams Server-Sent Events. `EventHub.subscribe`
  replays from a cursor for the ticket's scope; payloads are bounded
  (`MAX_REQUEST_BYTES` env var, streaming caps).
- **Transient output** (`EventHub.publish(transient=True)`): reaches only
  current subscribers plus a bounded in-memory per-execution buffer
  (`MAX_TRANSIENT_EXECUTIONS`, RRN-collected). It is **never** written to
  the store or the audit chain.

## Serialization

- Protocol models are plain dataclasses (`MeshModel`) with
  `to_dict`/`from_dict`; graphs via `core/graph/serialization.py`.
- **Safe result views:** `core/executor/serialization.py::safe_result_view`
  — an **allowlist** (not denylist) that strips `stdout`/`stderr`/file
  bodies and sensitive content before anything crosses the result
  boundary. Transient content travels on the transient channel
  (`output_callback().emit(stream, chunk)`) while remaining available
  in-memory for the verifier.

## Audit records (`core/audit/log.py`)

`AuditRecord` entries are appended with `compute_hash(record)` chaining
to the previous record's hash; `AuditLog.seal()` + `FileAuditAnchor`
(`anchor.py`) write an external anchor (`records`, `head` hash);
`verify_anchor` detects truncation/fork. `GET /audit` lists records;
`GET /audit/verify` returns the chain status.
