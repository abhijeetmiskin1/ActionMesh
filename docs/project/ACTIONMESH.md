# ActionMesh Engine

## Registry

- **Registration:** `ActionRegistry` (`core/registry/registry.py`) —
  register / get / versions / discover / resolve. It *describes* actions;
  it never executes them and never authorizes.
- **Definition:** `ActionDefinition` (`protocol/models.py`; validated by
  `core/registry/defs.py::validate_definition`). Fields: `name`
  (`domain.verb`), `risk_level`, `permissions` (concrete catalog scopes
  only — unknown/wildcard permissions are a *registration error*),
  `properties`/`required` (argument schema), `timeout_s`, `max_attempts`
  (1 for anything with side effects).
- **Validation:** `check_params` (`core/registry/defs.py`) **fails closed
  on any parameter a definition does not declare** — a smuggled `command`
  beside `{"to","message"}` is refused, not carried through.
- **Seeds:** `seed_definitions()` (`core/registry/seeds.py`) — 16 mock
  actions (`message.send/read`, `calendar.create/update/delete`,
  `reminder.create`, `file.read/move/share`, `device.notify/send_file`,
  `payment.prepare/confirm/verify`, `search.web`, `system.launch_app`).
- **Device actions:** `device_definitions()` (`agent/definitions.py`) —
  device.*, browser.*, device.files.*, media.*, `booking.submit`,
  `order.place`. `register_device_actions` refuses anything outside
  `IMPLEMENTED`; `advertise()` surfaces a capability only when its
  `REQUIRING_*` gate is open **and** it is implemented.
  `LaptopProvider.SUPPORTED == definitions.IMPLEMENTED` is test-asserted.

## Permission engine

- **Catalog:** `core/permissions/catalog.py` — the *complete* set of
  scopes (`calendar.read/write/delete`, `message.read/send`, `file.read/
  write/share/delete`, `device.notify/file_transfer/control/register/
  file.list/project.read/process.read/control/server.read/control/
  ide.open/screen.read/control`, `location.read`, `payment.prepare/
  execute/verify`, `system.app.launch`, `system.shell`, `search.web`,
  `reminder.write`, `browser.control`, `media.control`). Namespaces
  (`namespace.*`) are policy-rule-only; `*` is wildcard.
- **Actors/devices:** `core/identity.py` (`DeviceIdentity`,
  `SQLiteIdentityStore`), `core/devices.py`. Every surface is scoped to
  its actor (`_record_scope`, `_stream_principal` in `api.py`).
- **Credentials:** `core/credentials.py` — a grant/credential is a
  *ceiling*, not an approval for a specific execution.
- **Grants/consents:** `ConsentStore` (`core/permissions/store.py`);
  `POST /consents` (create), `DELETE /consents/{scope}` (revoke),
  `GET /consents` (list). `_context()` in `api.py` **raises on any
  client-supplied `grants`** — a client can never define its own
  authority.
- **Approval decisions:** `POST /approvals` decides a pending `ASK`;
  `GET /approvals` lists; `ExecutionEngine.submit_approval` /
  `approved(action_id)` / `awaiting_approval()`.

## Policy engine

`PolicyEngine.evaluate` (`core/permissions/engine.py`) is the **only**
component that produces an authorization decision.

- **Risk escalation:** effective risk = `max(action.risk_level,
  definition.risk_level)` (`core/permissions/risk.py`). Trusted metadata
  can only *raise* risk. A model labeling a booking `LOW` is a no-op —
  the definition's `CRITICAL` wins.
- **Levels:** `RiskLevel.LOW / MEDIUM / HIGH / CRITICAL`
  (`protocol/models.py`). `CRITICAL` (e.g. `payment.execute`,
  `device.terminal.execute`, `booking.submit`, `order.place`,
  `system.shell`) always requires explicit approval.
- **ASK behavior:** decision `ASK` → execution parks in
  `awaiting_approval()` until `POST /approvals` names the `action_id`.
- **DENY behavior:** no grant / ceiling exceeded → denied, audited,
  never executed.
- **Fail-closed:** unknown action → validation fails; unknown parameter →
  `check_params` fails; unknown scope → `KeyError` from `permission()`;
  unknown provider → `UnwiredProvider` proposes nothing.

## Executor

`ExecutionEngine` (`core/executor/engine.py`, 2,039 lines) — the only
execution engine.

- **Lifecycle:** `prepare_execution` → `execute` → per-node `_run_node` →
  `ExecutionResult`. States via `core/executor/lifecycle.py`.
- **Nodes/dependencies:** `ActionGraph` with deterministic
  `execution_order` (`core/graph/`); depends-on ordering and cycle
  detection in `core/graph/validator.py`.
- **Async execution:** full asyncio surface — `execute`, `resume`,
  `run_lease_sweeper`, `_watch_cancellation`, `_supervise_reowned`.
- **Cancellation:** `cancel()` / `request_cancel()`; `POST
  /executions/{id}/cancel`; cross-process via `mark_cancel_requested`;
  durable reconciliation `reconcile_interrupted()` +
  `sweep_lapsed_leases()` on restart.
- **Retries:** `max_attempts` per definition (1 for side effects);
  idempotency keys in `core/executor/idempotency.py`; dispatch log in
  `agent_dispatch` (`core/storage.py`).
- **Error handling:** `ProviderError` per node → `NodeOutcome`; compensation
  via `RecoveryPlanner`/`CompensationRunner`
  (`core/verification/recovery.py`).

## Verification

- **Effects:** handlers record `self._effects[action.action_id]` in
  `LaptopProvider` so `observe()` can re-read the world.
- **Journal:** `agent_dispatch` table + in-memory `_effects`.
- **Observation:** `observe()` (e.g. `agent/verification.py`,
  `FilesEngine.info`, browser `state()`).
- **Independent verification:** `VerificationEngine.verify`
  (`core/verification/engine.py`) + `verdicts.decide`
  (`core/verification/verdicts.py` → `VerificationOutcome` /
  `VerificationStatus`). The provider's `verify()` returns
  `verified=False` — **never override it to `True`**.
- **Unconfirmed results:** `REQUIRES_USER` (executed-but-unconfirmed) is
  reported as such, never as success; `_report_unconfirmed` in the
  executor.
- **Stale observations:** observations are re-read per execution; SSE
  replay uses ticket-scoped cursors (see [DATA_AND_EVENTS.md](DATA_AND_EVENTS.md)).

## Persistence

`SQLiteStore` (`core/storage.py`): `execution_records`, `mesh_events`,
`execution_body` (graph + context JSON), `agent_dispatch`; schema
migrations v0→v3. Audit: `SqliteAuditBackend` (`audit_records`) + hash
chain (`AuditLog.verify`) + external anchor (`FileAuditAnchor`).

## API

See [API_REFERENCE.md](API_REFERENCE.md) for the full route list. Auth:
API key / actor identity at the transport boundary (`api.py`); every
route checks authorization through the same `_context()` + `PolicyEngine`
path — there is no second policy engine.

## Invariants (enforced by tests)

1. A client can never define its own authority (`grants` in a body →
   error). 2. Risk = `max(action, definition)`. 3. The executor does
   not certify its own work. 4. Content never crosses the result
   boundary (`safe_result_view`). 5. Real OS access only in
   `actionmesh/agent/`. 6. Loopback trust enforced server-side. 7.
   Detached processes addressable only by agent-issued id. 8. Pairing
   ceiling fixed at issuance. 9. Live output transient, never audited.
   10. No outbound network client in the package (except approved
   provider transports). 11. Fail closed everywhere.

Source: `PROJECT.md` §1 invariant table; `docs/c42-execution-contract.md` §0.
