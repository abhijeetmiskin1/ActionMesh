# Audit Trail

> **One intent. Every action.** — and afterwards, a record of exactly what
> happened, in a form that cannot be quietly rewritten.

ActionMesh records security-sensitive activity in an **append-only, hash-chained**
log. Every entry carries a hash of its own content and the hash of its
predecessor, so history is a linked chain rather than a list.

```text
record 0   prev_hash = GENESIS     hash = H(content0)
record 1   prev_hash = H(content0)  hash = H(content1)
record 2   prev_hash = H(content1)  hash = H(content2)
```

## Using it

```python
from actionmesh.core.audit import AuditLog
from actionmesh.core.events import EventHub
from actionmesh.core.executor import ExecutionEngine

audit = AuditLog()
hub = EventHub(audit=audit)                 # seal every published event
engine = ExecutionEngine(registry, policy, [provider], events=hub)

result = await engine.execute(graph, context)

status = audit.verify()
if not status:              # ChainStatus is falsy when broken
    print(status.problem, "at seq", status.broken_at)
```

Authorization decisions are chained directly from the policy engine:

```python
record = audit.record_decision(policy_engine.explain(ctx), actor="user_1")
```

## What a record holds

| Field | Meaning |
|-------|---------|
| `seq` | Position in the chain, 0-based, contiguous |
| `kind` | `"event"`, `"decision"`, or another label the caller chooses |
| `actor` | Who caused it — a user id, `"system"`, a device |
| `subject` | What it is about — execution id, action name, device id |
| `decision` | `ALLOW` / `DENY` / `ASK`, or empty for non-authorization entries |
| `result` | Outcome — `"success"`, `"action.completed"`, `"authorized"` |
| `reason` | Human-readable justification, e.g. joined policy `reasons` |
| `payload` | Structured detail; the full `explain()` dict for decisions |
| `prev_hash` / `record_hash` | The chain links |

## What `verify()` catches

Recomputed from the records themselves — stored hashes are never trusted:

```text
edited content        -> content no longer matches its hash
deleted record        -> sequence mismatch at that position
reordered records     -> sequence mismatch
forged genesis link   -> first record is not anchored at GENESIS_HASH
edit + re-seal        -> the NEXT record still points at the old hash
```

`max_records` is a deliberate guard: when the log is full `append()` raises
`AuditError` rather than dropping the oldest entries, because silently
truncating an audit trail is itself tampering.

## Persistence

The chain is not decoration — it survives process death:

```python
from actionmesh.core.audit import AuditLog, SqliteAuditBackend

backend = SqliteAuditBackend("actionmesh.db")
audit = AuditLog.open(backend)          # restores and verifies before use
audit.append(kind="event", subject="hello")
backend.close()                          # the caller owns the backend
```

`open()` **refuses to start on tampered history**:

```python
AuditLog.open(backend)
# AuditIntegrityError: persisted audit chain is broken at seq 0:
#   record 0 content does not match its hash — refusing to append to
#   tampered history
```

That is deliberate. Restoring a chain that does not verify would let an
attacker who edited the store be silently trusted from the very next append
onwards. For forensics, `require_intact=False` still returns the damaged
evidence:

```python
forensic = AuditLog.open(backend, require_intact=False)
forensic.verify().problem   # describe the damage without destroying it
```

## Truncation, and why an anchor exists

The chain detects everything in the list above — and **not** one more case.
Deleting records from the *end* leaves a shorter chain that is internally
perfectly consistent, so `verify()` reports `ok`:

```python
# Attacker with write access to the database:
raw.execute("DELETE FROM audit_records WHERE seq >= 3")
AuditLog.open(SqliteAuditBackend(path)).verify().ok   # True — nothing to fault
```

A chain cannot detect this about itself; it has no way to know how much history
*should* exist. It gets worse — an attacker can append a fabricated record whose
`prev_hash` matches the new tail, and the result verifies cleanly. This was
measured against this implementation, not assumed; see
`tests/unit/test_audit_anchor.py`, which includes a test pinning the
vulnerability so it cannot quietly return.

An **anchor** is the answer: a sealed `(records, head)` checkpoint kept outside
the store.

```python
from actionmesh.core.audit import AuditLog, FileAuditAnchor, SqliteAuditBackend

anchor = FileAuditAnchor("C:/secure/audit-anchor.json")   # NOT beside the .db
backend = SqliteAuditBackend("actionmesh.db")
audit = AuditLog.open(backend, anchor=anchor)  # refuses a truncated chain
...
audit.seal()          # on shutdown, on a schedule — not on every append
backend.close()
```

```python
AuditLog.open(backend, anchor=anchor)
# AnchorMismatchError: audit chain is shorter than its anchor: 3 records on
#   disk but the anchor sealed 5 — history was truncated
```

Over HTTP, set `ACTIONMESH_AUDIT_ANCHOR` alongside `ACTIONMESH_AUDIT_DB`.

### What an anchor does not buy you

**Only as much as its storage is worth.** An anchor file sitting next to the
audit database can be edited by anyone who can edit the database, so it
protects nothing. For a real deployment:

- Put it on storage the audit database **cannot** rewrite — append-only, or
  write-once, or a separate host/medium under different ownership.
- **Sign the checkpoint** with a key held in a KMS or HSM. An unsigned anchor
  only proves the file was not changed; it does not prove *who* sealed it.
- Seal on a deliberate cadence. An anchor rewritten as often as the log is
  worth very little.
- Ship verification somewhere the same attacker cannot reach, or a detected
  breach is still a breach.

Two further guarantees:

- **Append-only is enforced by the schema.** `seq` is a PRIMARY KEY, so a
  second write at an existing position raises `IntegrityError` rather than
  overwriting history.
- **A failed write records nothing.** `append()` persists before updating
  memory, so a full disk leaves no partial record anywhere.

The backend is a two-method protocol — `write()` and `read_all()` — so a
PostgreSQL or remote sink drops in without touching the chain logic.

## Honest limits

- **A chain cannot stop a full rewrite.** It resists partial, accidental or
  single-record tampering. An attacker who can rewrite the *entire* store can
  recompute the whole chain. Production tamper *evidence* needs an anchor
  outside the store: an append-only medium, a signed periodic checkpoint, or
  write-once storage. That is deployment work, not library work.
- **The SQLite backend is local, not remote.** `SqliteAuditBackend` makes the
  chain survive restarts, but it sits on the same disk as the process it
  protects. That is the "rewrites the whole store" case above, and it is why
  an external anchor is still a deployment decision.
- **There is no redaction.** `payload` is caller-supplied. Callers must not put
  credentials in it; nothing in this package filters them.
- **`reset()` starts a new chain** and is intentionally explicit — no automatic
  rotation, because silent rotation would destroy the very history it protects.

## Tests

`tests/unit/test_audit.py` — 27 tests. The interesting ones are in
`TestTamperDetection`, which simulate an attacker with write access;
`TestIntegration`, which chains a **real** execution and a **real**
`PolicyEngine` decision rather than synthetic data; and `TestPersistence`,
which tampers with the SQLite file using a plain `sqlite3` client and proves
the chain continues correctly across a restart.
