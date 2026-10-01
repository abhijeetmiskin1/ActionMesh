# Task Board

> **This board describes a reconstruction.** The original repository was deleted
> during development; `.git/` was lost with it. Everything below was rewritten
> from the architecture and then verified by execution.

Legend: `[x]` implemented **and** runtime-verified · `[~]` implemented,
statically reviewed only · `[ ]` not started

---

## Verified

### C0 — Repository Foundation `[x]`
- Stdlib-only package layout, zero runtime dependencies.
- `pyproject.toml`, `tests/` scaffolding, `python -m unittest` runner.

### C1 — Protocol (AMP/1.0) `[x]`
- `protocol/models.py` — `Action`, `GraphNode`, `ActionGraph`, `ActionDefinition`,
  `Permission`, `Policy`, `PolicyRule`, `ApprovalRequest`, `VerificationResult`,
  `MeshEvent`, `ExecutionRecord`, `Capability`, `DeviceInfo`.
- Enums: `RiskLevel`, `ActionState` (16 states), `NodeType` (9 types),
  `PolicyDecision` (4), `FailureKind` (10).
- `protocol/validate.py` — fail-closed validation for untrusted payloads.
- `protocol/version.py` — `AMP/1.0` negotiation; unsupported → error, never a
  silent downgrade.
- `protocol/graphs.py` — `validate_graph`, `validate_plan`, `classify_failure`.

### C2 — Action Registry `[x]` — 37 tests
- `register` (validates metadata, rejects duplicate `(name, version)`).
- `get` / `versions` — latest-by-default, explicit version, unknown → error.
- `discover` — filter by name, provider, minimum risk, permission; latest only.
- `resolve` — shape-validates untrusted input, looks up the definition,
  type-checks parameters, **raises risk to `max(action, definition)`**.
- 16 seeded `provider="mock"` definitions, all described `MOCK:`.
- Fail-closed: unknown permission, wildcard permission requirement, malformed
  schema, untyped/unreadable property spec, missing/typed-wrong parameter.

### C3 — Permission + Policy Engine `[x]` — 61 tests
- Closed catalog of 20 concrete scopes; `is_known` / `is_concrete` /
  `is_namespace` / `is_wildcard`.
- `scope_satisfied` is **exact-only** — a grant can never be a wildcard.
- Policy *rules* may use wildcards; equally specific rules resolve to the **most
  restrictive** decision.
- `PolicyEngine` — 7-step deterministic precedence, auditable reasons,
  `explain()` for JSON audit records.
- `ConsentStore` — server-side only; refuses wildcard/unknown consent.
- Risk gate: CRITICAL always ASK; HIGH ASK unless server-side opt-in.
- Grants carry a `max_risk` ceiling and an optional expiry; both enforced.

### C4 — Action Graph + security invariants `[x]` — 50 tests
- `GraphBuilder` — all 9 node types; action dicts validated as untrusted input.
- `validate_graph_structure` — unique ids, node config, dependency existence,
  no self-dependency, compensation targets, terminal node required.
- `find_cycle` — iterative DFS (no recursion limit; verified with a 2000-node chain).
- `topological_layers` / `execution_order` — deterministic (ties break on `sorted()`).
- Serialization — canonical (nodes sorted by `node_id`); round-trip verified.
- `authorize_graph` — C2 + C3 composition; all-or-nothing; `ASK` does not
  block but flags the node as needing approval.

---

### C5 — Execution Engine `[x]` — 40 tests
- `lifecycle.py` — 16-state machine; illegal transitions raise, terminal states
  permit nothing. `PLANNED → EXECUTING` is refused: authorization cannot be
  skipped.
- `idempotency.py` — the ledger claims a key *before* dispatch and releases it
  on failure, so a retry cannot double a side effect.
- `engine.py` — async, layer-scheduled with `asyncio.gather`; re-resolves every
  action at dispatch and **re-runs the policy check**; bounded retry with
  exponential backoff; timeout and cancellation; `simulated` propagated.
- **Execution success ≠ verification success.** An action that ran but did not
  verify is `REQUIRES_USER`, never `SUCCESS`.
- A node awaiting approval does not execute, and an approval for a *different*
  `action_id` does not unblock it.
- `providers/base.py` — `ActionProvider` interface, `ProviderError` carrying a
  `FailureKind`, and `MockProvider` with deterministic failure injection.

### C6 — Verification + Recovery `[x]` — 24 tests
- `verdicts.py` — weighted evidence (`observed_state` 3, `provider_claim` 1,
  `caller_assertion` 0) with `CONFIRM_THRESHOLD = 2`, so a provider's own claim
  **can never** confirm by itself. Any contradiction is decisive.
- `engine.py` — per-action-type strategies that inspect `provider.observe()`
  directly. Tri-state assessment: `match` / `mismatch` / `uninformative`. An
  action type with no registered strategy is `UNSUPPORTED`, never `CONFIRMED`.
- `recovery.py` — `RETRY` / `COMPENSATE` / `ESCALATE` / `NOT_COMPENSATABLE`, with
  an explicit `IRREVERSIBLE` set (`message.send`, `payment.*`,
  `device.send_file`) so a rollback is never promised for what cannot be undone.
- Wired into the executor: `REFUTED` → `FAILED`, `INDETERMINATE` →
  `REQUIRES_USER`, and every failed node carries a `RecoveryPlan`.

---

## Phase status

- **C7** — Mock Providers: complete. Added the shared deterministic harness,
  domain-specific providers for messaging, calendar, files, notifications,
  contacts, and payments, explicit payment lifecycle transitions, and focused
  provider tests.
- **C8** — REST API: initial slice complete. Added FastAPI health, capability,
  execution, approval, cancellation, and execution-record endpoints with
  boundary validation and HTTP tests.
- **C9** — Realtime events: complete initial in-memory hub, SSE, WebSocket,
  replay history, and executor lifecycle publication.
- **C10** — Memory + entity resolution: complete exact-match, ambiguity-safe
  in-memory entities and aliases.
- **C11** — Device mesh: complete in-memory registration, heartbeat, presence,
  and capability gating.
- **C12** — Developer SDK: complete in-process typed facade over the core engine.
- **C13** — Real integrations: safety boundary complete in `real_base.py`;
  vendor adapters remain deferred until official contracts and credentials are
  selected. No simulated adapter is presented as real.
- **C14** — Security hardening: API-key boundary, constant-time comparison,
  request-size rejection, security headers, and focused tests complete.
  **Threat-model pass complete** (`docs/security.md`): 19 mitigated threats,
  each with a named test and a residual-risk rating, plus 6 open threats with
  real exposure — API-key protection is left rated **High** rather than talked
  down. Added `tests/security/test_dangerous_primitives.py` (8 tests), an AST
  guard making the old prose claim "no subprocess, eval or exec" executable.
  Identity provider, key rotation and global rate limiting remain and are
  deployment-level concerns.
- **C15** — Release: dashboard, CLI, release smoke coverage, SQLite execution,
  consent, and idempotency persistence, process-local observability, a
  **tamper-evident audit trail** (`actionmesh.core.audit`: append-only,
  hash-chained, independently verifiable — see `docs/audit.md`), and a
  Docker/ASGI deployment path are complete; live integrations remain deferred.
  The audit trail **persists to SQLite** through `SqliteAuditBackend` and
  reopens fail-closed, so the chain survives process restarts. Only an
  external chain anchor remains a deployment decision.
- **C16** — Compensation execution: `CompensationRunner` decides which undos are
  legitimate and the executor performs them after a failed forward run, in
  reverse execution order, only for verified side effects and never for an
  irreversible action. Fixed a bug where a COMPENSATION node ran as ordinary
  forward work (`builder.compensate()` set no `depends_on`, so Kahn saw the undo
  as a root node and a create+delete pair reported `success` for a net no-op).
  A rollback never reports SUCCESS; refused or failed undos set
  `compensation_required`. 6 new tests (`tests/unit/test_compensation.py`).
- **C16b** — Audit chain anchoring: added `core/audit/anchor.py` after measuring
  that tail truncation was silently accepted (`verify()` returned `ok` on a
  truncated chain, and a forged record could be re-anchored onto it).
  `FileAuditAnchor` seals `(records, head)`; `AuditLog.open(anchor=...)` raises
  `AnchorMismatchError` on a short or contradicting chain; `AuditLog.seal()`
  takes the checkpoint; the API wires it through `ACTIONMESH_AUDIT_ANCHOR`.
  Only as strong as where the anchor file is stored. 6 new tests
  (`tests/unit/test_audit_anchor.py`).
- **C17** — Android APK: an installable native shell around the control room,
  built and signed **without Gradle**. `android/` is a dependency-free `WebView`
  (no AndroidX/AppCompat/Maven) and `scripts/build_apk.py` drives only the SDK
  build-tools (`aapt2`, `javac`, `d8`, `zipalign`, `apksigner`), fetching a
  portable JDK and the command-line tools into the git-ignored
  `android-toolchain/`. Output `dist/actionmesh-<version>.apk`: package
  `app.actionmesh.control`, minSdk 26, targetSdk 34, the single `INTERNET`
  permission, signed with APK Signature Scheme v2+v3. 16 new tests
  (`tests/unit/test_android_apk.py`).
- **C18** — Intent → plan: the pipeline's `intent / planner` stage, previously
  documented but unimplemented (`validate_plan` existed with no producer).
  `core/planner.py` defines the `Planner` interface and a deterministic, offline
  `RuleBasedPlanner` that emits **only** `{type, parameters}` — no risk, no
  permissions, no lifecycle state — plus `plan_to_graph()` for the untrusted
  proposal → trusted graph boundary. Anything unmapped is recorded in
  `Plan.notes`, an empty plan is rejected, and node ids stay deterministic.
  Exposed as `ActionMeshClient.plan/plan_graph` and `POST /plan` (proposes, never
  executes). 32 new tests (`tests/unit/test_planner.py`, SDK, API), one of which
  proves a planned payment is still denied without a grant.
- **C18b** — Planner in the control room: a "Plan from natural language" panel
  takes intent, lists the proposed actions and planner notes, and gates execution
  behind a separate disabled step. The browser declares policy for the proposed
  types only and sends **no** permissions or grants, so consent still comes from
  the server-side store. Measured: consented message/reminder plans succeed,
  `pay alice 20` is denied. 6 new tests (`tests/unit/test_dashboard.py`).

## Post-C15 provider readiness

- **P1 — Provider contract freeze:** complete in `docs/provider-contract.md`.
- **P2 — Requirements matrix:** complete in `docs/provider-requirements.md`;
  vendor-neutral and not a ranking.
- **P3 — Provider evidence research:** initial official-documentation pass
  recorded in `docs/provider-evidence.md`; no compatibility or ranking claim.
- **P4 — Provider decision readiness:** complete in
  `docs/provider-decision-readiness.md`; contract, runtime, and product
  questions are separated.
- **P5 — Provider selection:** not started; requires review of the evidence,
  official documentation, sandbox access, and deployment requirements.
- **P6+ — Adapter implementation and production readiness:** deferred until P5
  is complete. No credentials or external side effects are permitted yet.

---

## Verification command

```bash
python -m unittest discover -s tests
```

Last run: **417 tests, OK** (0 failures, 0 errors) on Python 3.14.4 (Linux).
(The reconstruction was originally verified with 358 tests; C17 added 16 for the
APK plus one snapshot guard, a lint pass un-shadowed 4 cycle tests, and C18 added
38 for the planner and its dashboard panel.)

## Deliverables

- **`ActionMesh-Handoff.pdf`** — a regenerable handoff pack: cover page,
  `HANDOFF.md`, and `TASK_BOARD.md`. Produced by `scripts/handoff_pdf.py`,
  which is standard-library only, writes the PDF base-14 fonts itself, validates
  its own cross-reference table, and **refuses to write a malformed file**.
  The test count on the cover is parsed out of this file, so it cannot silently
  drift. Covered by 13 tests in `tests/unit/test_handoff_pdf.py`.
- **`dist/actionmesh-<version>.apk`** — a signed Android app around the control
  room, built by `scripts/build_apk.py` (standard-library only). Covered by 16
  tests in `tests/unit/test_android_apk.py` (the end-to-end checks skip until an
  APK has been built).
- **`scripts/snapshot.py`** — offline zip backup written *beside* the tree.
  Run it after every significant change; the original tree was lost once.
  Covered by 11 tests in `tests/unit/test_snapshot.py`.
