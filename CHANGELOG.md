# Changelog

All notable changes to this project are documented here.

This project adheres to a phased delivery model. A phase is only recorded as
complete when its tests have actually been executed and passed.

---

## [Unreleased] — 2026-09-30

## C18 — Intent to plan (the pipeline stage that was missing)

### Added — a planner, and the trust boundary it draws

- The documented pipeline began `USER INTENT → intent / planner → STRUCTURED
  PLAN`, but nothing implemented it: `validate_plan` could validate a plan and
  nothing produced one. `actionmesh/core/planner.py` fills that gap.
- `Planner` is the structural interface (`plan(intent) -> Plan`);
  `RuleBasedPlanner` is a deterministic, offline reference planner — the planner
  counterpart of the mock providers. It matches a small, documented rule table
  (message, reminder, calendar, search, files, payments, notifications, app
  launch) and splits a compound intent on `;` / `then` into an ordered chain.
- `plan_to_graph()` is the trust-boundary crossing. Proposals are re-validated as
  untrusted input and chained with a terminal node, so the result is a graph the
  executor accepts, with deterministic node ids. An empty plan is **rejected**
  rather than executed as a no-op.
- **A plan may carry only `{type, parameters}`.** `ProposedAction` has no
  `risk_level`, `permissions`, `depends_on`, `provider` or `state` field: a
  proposal that could name its own risk or grant itself a permission would be
  exactly the vulnerability this boundary exists to prevent.
- Anything the planner could not map is recorded in `Plan.notes`, never silently
  dropped — a plan that quietly ignored half an intent would look complete.
- Wired into the SDK (`plan`, `plan_graph`) and the API (`POST /plan`, which
  returns the proposal and deliberately executes nothing). The planner is
  injectable, so a real model can replace the rule-based one without touching the
  SDK or anything downstream.
- **The control room can now plan.** A "Plan from natural language" panel takes
  an intent, shows the proposed actions and any planner notes, and offers
  *Execute planned actions* as a deliberately separate, initially-disabled step.
  The plan path builds the graph in the browser but sends **no permissions or
  grants** — only policy rules bounded to the proposed action types, which is what
  the existing message flow already does. Consent still comes from the
  server-side store, so the measured behaviour is: `tell mom I'll be late` and
  `remind me to call mom at 5pm` succeed once consented, while `pay alice 20` is
  **denied** even though the browser proposed it *and* declared the policy rule.
  That is the whole thesis, end to end, in three clicks.
- 6 new tests in `tests/unit/test_dashboard.py` pin that the browser cannot hand
  the server a permission or grant, that its policy rules stay bounded to the
  proposal, and that planner text is rendered via `textContent` rather than
  `innerHTML`. The suite is now **417 passing tests**.
- 32 new tests (`tests/unit/test_planner.py` plus SDK/API coverage), including
  one proving that a **planned** payment is still denied by the policy engine with
  no grant: the strongest thing the planner can say does not buy it authority. The
  suite is now **411 passing tests** (417 with the control-room planner panel).

## Quality — lint-clean, and a masked test surfaced

- Ran the project's own linter (`ruff`, already configured in `pyproject.toml`)
  across the tree for the first time: **112 findings**. Fixed all of them —
  import order, unused imports and unsorted `__all__`, deprecated imports,
  redundant f-strings, `itertools.pairwise`, `next(iter(...))`, and collapsed
  `with`/`if` nesting. `ruff check .` now reports **All checks passed**.
- **Found a test that never ran.** `tests/unit/test_graph.py` defined
  `class TestCycleDetection` twice; the second definition silently shadowed the
  first, so its four cycle tests were collected by nothing. Renamed the second to
  `TestDeepChain`. The suite went from 375 to **379 passing tests** — a live
  example of this project's own rule, *a test that never runs is not a passing
  test*.
- Narrowed two blind `assertRaises(Exception)` in `test_graph.py` to the actual
  `ValidationError`, and a WebSocket assertion to `WebSocketDisconnect`: a blind
  assertion cannot distinguish the intended failure from an unrelated one.
- `[tool.ruff] extend-exclude` now skips `build/` and `android-toolchain/`, so the
  linter stops reading generated trees.

## C17 — Android APK (delivered)

### Added — a real APK, built without Gradle

- `android/` is a dependency-free native shell: one `WebView` pointed at the same
  `/dashboard` the PWA installs, with a configurable server URL, a reload/back
  affordance, an in-app error page, and an overflow *Server…* dialog. It holds
  **no application logic** — it renders only what the trusted server returns, so
  policy, execution and audit keep a single source of truth. There is no
  AndroidX, no AppCompat, no Gradle and no Maven resolution.
- `scripts/build_apk.py` builds and signs it using only the SDK build-tools:
  `aapt2 compile`/`link`, `javac`, `d8`, `zipalign`, `apksigner`. It downloads a
  portable JDK and the Android command-line tools into `android-toolchain/`
  (git-ignored), installs the platform and build-tools through `sdkmanager`, and
  writes `dist/actionmesh-<version>.apk`. The two Windows-only PowerShell
  helpers are superseded for Linux/macOS and left in place for Windows.
- The manifest requests **exactly one** permission (`INTERNET`), and cleartext
  is enabled explicitly (a LAN server is plain HTTP) with the trade-off stated
  in `android/README.md` rather than hidden.
- Verified output: `app.actionmesh.control`, minSdk 26, targetSdk 34, signed with
  APK Signature Scheme **v2 + v3**. `aapt2 dump badging` confirms the package,
  the single permission and the launcher activity.
- `tests/unit/test_android_apk.py` (16 tests) pins the manifest's permission
  surface, the brand palette, the build-script helpers, and — when an APK exists
  in `dist/` — that it contains a manifest, resources and a signed dex. The
  suite is now **379 passing tests**.

### Fixed — a snapshot would have swallowed the ~775 MiB toolchain

- `scripts/snapshot.py` excluded the venv and caches but not the new
  `android-toolchain/` (JDK + SDK, ~775 MiB) or `build/`. The first backup after
  C17 would have been enormous for no benefit — the same failure mode as the
  39.8 GB of compounding archives fixed earlier. Both directories are now
  excluded, with a regression test asserting real sources are still kept. The
  backup is 376 KiB.

## C16 — Compensation execution

### Fixed — a compensation node ran as ordinary forward work

- `builder.compensate()` sets no `depends_on`, so Kahn layering saw every undo
  node as a **root**. A `calendar.create` + `calendar.delete` pair fired in
  order during the forward pass and the engine reported `success` for a net
  no-op — the most dangerous outcome available, because the user's intent was
  silently discarded while the audit trail showed everything worked.
- `topological_layers` now excludes `COMPENSATION` and drops edges *pointing at*
  an excluded node (an END depending on its compensation otherwise looked like a
  cycle). Compensation actions were already covered by `authorize_graph`, so
  rollback cannot become a policy bypass.

### Added — undos are actually performed

- `CompensationRunner` (`core/verification/recovery.py`) stays pure and decides
  *which* undos are legitimate; the executor performs them. An undo runs only
  when the target produced a **verified** side effect, a compensation is
  declared, and the action is **not** in `IRREVERSIBLE`.
- Order is reverse of forward execution, so dependencies unwind as built.
- `ExecutionResult` gains `compensations` and `compensation_required`. A rollback
  **never** upgrades the result to `SUCCESS`; a refused or failed undo sets
  `compensation_required` so a human learns an effect may still be standing.
- Events `compensation.completed` / `compensation.refused`; metrics
  `compensations.attempted` / `.failed` / `.refused`.

### Fixed — a silently dropped rollback

- Nested compensation (an undo declaring an undo) is now **rejected** by
  `validate_graph_structure`. It was accepted, then silently ignored at
  runtime — the author would believe they had a rollback and have none, which
  is the same class of failure as the forward-execution bug above.
- **An unconfirmed side effect was invisible to the caller.** A node that was
  dispatched but not verified ends as `REQUIRES_USER`; its real-world effect may
  exist. The compensation filter correctly required *verified* successes, but
  that left the unverified node in neither the undone set nor the report — the
  caller saw `compensation_required: False` and could conclude the system was
  clean. Such nodes are now recorded as possible standing effects
  (`compensation.unconfirmed` event, `compensations.unconfirmed` metric) and set
  `compensation_required`. They are deliberately **not** auto-undone: undoing an
  effect that may not exist is a second guess at the user's data. A node denied
  before dispatch (`attempts == 0`) cannot have left an effect and is not
  reported.
- 11 new tests in `tests/unit/test_compensation.py`.

### Added — installable on a phone (PWA)

- `GET /manifest.webmanifest`, `GET /sw.js`, `GET /icon.svg`, plus
  `apple-touch-icon` / `theme-color` / `viewport-fit` meta and a service-worker
  registration on the dashboard. A phone can now **Add to home screen** and
  launch the control room full-screen with no browser chrome — the supported
  route to "an app on my phone" for a Python server, and it needs no APK, no
  signing key, and no store.
- The service worker caches **only the shell** (`/dashboard`, `/icon.svg`).
  It never caches `/audit`, `/executions`, `/capabilities`, or any other JSON: a
  stale execution record or audit chain would be a lie in a system whose entire
  purpose is truthful reporting. Network-first, so a phone online always shows
  live data. `tests/unit/test_pwa.py` asserts the absence of those routes from
  the worker source, so the unsafe variant cannot come back unnoticed.
- 12 new tests. The suite now has **358 passing tests**.

### Measured, not assumed — API performance

Benchmarked all routes (60 iterations each, p50/p95):

```text
/health          5.76ms p50   25.14ms p95
/capabilities    6.98ms p50   11.41ms p95
/metrics         4.83ms p50    7.35ms p95
/dashboard       4.74ms p50    7.49ms p95
/audit?limit=100 3.01ms p50    4.66ms p95
```

`/audit` stays ~2.5ms from `limit=1` to `limit=1000`, so the chain walk is not
a scaling problem at this size. **No optimisation was made**: there is no hot
path, and adding a cache in front of the audit chain would trade correctness for
milliseconds that are already imperceptible. The real mobile friction was
installability, not speed.

### Not done — Android APK (now delivered in C17)

The APK that was attempted here did **not** ship from the Windows machine: it
needs a JDK (~182 MB) and the Android SDK, and that machine's download stalled
repeatedly at ~94 MB of the JDK before the ranged-resume fetch could make
progress. It is now built on Linux — see **C17** — by `scripts/build_apk.py`;
the PowerShell helpers remain only for Windows.

- **Audit truncation was silently accepted.** Measured, not assumed: with raw
  SQL access, editing a payload, the actor, or deleting a *middle* record is
  refused — but `DELETE ... WHERE seq >= n` leaves a valid prefix that
  `verify()` reports as `ok`. An attacker can then append a forged record
  re-anchored onto the truncated tail and the whole chain verifies again
  (`last actor = mallory`, `ok = True`). A hash chain cannot detect this; it has
  no way to know how much history *should* exist.
- New `actionmesh/core/audit/anchor.py`: `AuditAnchor` protocol,
  `FileAuditAnchor` (a sealed `(records, head)` checkpoint), and
  `AnchorMismatchError`. `AuditLog.open(..., anchor=...)` now refuses a chain
  that is shorter than, or contradicts, its anchor; `AuditLog.seal()` takes the
  checkpoint. Enabled in the API with `ACTIONMESH_AUDIT_ANCHOR`.
  **This only helps if the anchor lives somewhere the audit database cannot
  rewrite** — a sibling file protects nothing, and that is stated in the module
  docstring rather than glossed. An anchor should also be signed with a
  KMS-held key in a real deployment.
- 6 new tests in `tests/unit/test_audit_anchor.py`, including one that pins the
  original defect (truncation *is* invisible without an anchor) so it cannot
  quietly regress. The suite now has **346 passing tests**.

### Added — durable, fail-closed audit persistence

- `SqliteAuditBackend` persists the chain using only the standard library,
  matching the existing `SQLiteStore` and `SQLiteConsentStore` patterns. Plain
  relational SQL with `seq` as PRIMARY KEY, so a PostgreSQL move is a driver
  change rather than a redesign.
- **Append-only is enforced by the schema, not by convention:** a second write
  at an existing `seq` raises `IntegrityError` instead of overwriting history.
- `AuditLog.open(backend)` restores a persisted chain and **refuses to start
  on tampered history**, raising `AuditIntegrityError` naming the broken `seq`.
  Restoring a broken chain would let an attacker who edited the store be
  silently trusted from the next append onwards. `require_intact=False` gives
  an explicit forensic path that still returns the damaged evidence.
- `append()` persists **before** updating memory, so a failed durable write
  leaves no partial record anywhere rather than letting memory and disk
  disagree.
- `reset()` is refused on a persisted chain — erasing durable history in place
  destroys what it exists to preserve. Start a new store instead.
- 9 new tests, including one that tampers with the file using a **plain
  `sqlite3` connection and no knowledge of ActionMesh**, and one proving the
  chain continues correctly across a simulated process restart.
- The suite now has **329 passing tests**.

### Fixed — a denied execution left no audit trail

- The executor returned `DENIED` **before publishing any event**, so the single
  most security-relevant outcome in the system produced no trace at all. A
  denial is now published as `execution.denied` like every other terminal
  outcome, and a test asserts it reaches the chain.
- `create_app()` opened the SQLite audit backend and, when
  `AuditLog.open()` rejected a tampered chain, the handle was never closed —
  leaving the file locked on Windows. The backend is now closed on that path
  too, and ownership is published as `app.state.audit_backend` so a shutdown
  handler or test can release it.

### Fixed — audit backend ownership

- `AuditLog.close()` was closing a **caller-owned** backend connection, which
  leaked the handle and left the SQLite file locked on Windows. The log holds
  no resources of its own, so `close()` was removed and the ownership rule is
  now stated on `open()`: the caller keeps the backend, including when `open()`
  raises.

### Added — C15 tamper-evident audit trail

- New `actionmesh/core/audit/` provides `AuditLog`: an **append-only,
  hash-chained** store. Each record carries a SHA-256 of its own content and
  the hash of its predecessor, so history is a linked chain rather than a list.
- `verify()` **recomputes every hash instead of trusting stored ones** and
  returns the exact `seq` where the chain broke. It detects: edited content,
  deleted records, reordered records, a forged genesis link, and — the case
  that actually matters — an edit that re-seals its own hash, which still
  breaks because the *next* record points at the old one.
- `EventHub` gained an optional `audit=` sink, so every published event is
  sealed into the chain before it reaches subscribers. It is optional and
  off by default, so existing wiring is unchanged.
- `AuditLog.record_decision()` chains the output of `PolicyEngine.explain()`,
  so authorization decisions land in the same chain as execution events.
- `AuditLog(max_records=...)` **raises `AuditError` rather than dropping the
  oldest entries** — silently truncating an audit trail is itself tampering.
- New protocol contract `AuditRecord` plus `GENESIS_HASH`, exported from
  `actionmesh.protocol`.
- `docs/audit.md` documents the contract and, explicitly, its limits.
- 18 new tests. The tamper tests reach into `log._records` deliberately, to
  simulate an attacker with write access; the integration tests chain a real
  execution and a real `PolicyEngine` decision rather than synthetic data.
- **Honest residual risk, not oversold:** a chain stored beside its own data
  resists partial tampering, not an attacker who rewrites the whole store.
  Production tamper *evidence* needs an anchor outside it — append-only
  medium, signed checkpoint, or write-once storage. The threat model rates
  this **Medium** and says so.

### Added — C14 threat-model pass and an executable security guard

- `docs/security.md` now carries the full C14 threat model in the required
  shape — **threat / attack surface / mitigation / test / residual risk** —
  covering **19 mitigated threats**, and an open-threat table with an explicit
  **exposure today** column instead of a comfortable "n/a".
- Honest ratings: API-key protection is optional by default and there is no
  rate limiting, so that row is rated **High** residual risk and left there.
  Audit tampering is recorded honestly: `PolicyEngine.explain()` produces a
  JSON decision record, but there is still **no append-only or tamper-evident
  audit store**.
- New `tests/security/test_dangerous_primitives.py` (8 tests) turns the
  previously prose-only claim "the core contains no subprocess, eval or exec"
  into a regression guard. It parses every module in `actionmesh/` with `ast`,
  so comments and docstrings cannot influence the result, and includes a
  self-test proving the guard actually rejects a violation.
- The guard covers four threats that previously had **no test at all**:
  command injection, SSRF, unsafe deserialization, and dynamic code execution.
- Network/process imports are banned package-wide behind a deliberately
  **empty** `NETWORK_ALLOWED` allowlist, so granting a future official adapter
  network access is an explicit, reviewable edit. The allowlist relaxes
  imports only; `eval`/`exec` remain banned even on allowlisted paths.
- The guard initially flagged `providers/real_base.py`. Investigation showed
  this was a **false positive** — `urlparse` is string handling used to enforce
  HTTPS, not network access — so the deny-list was made dotted-name precise
  (`urllib.request` banned, `urllib.parse` legal) rather than suppressing the
  finding. Two tests now pin that distinction.
- The suite now has **292 passing tests**.

### Added — regenerable handoff PDF

- `scripts/handoff_pdf.py` renders the engineering handoff to PDF using only the
  standard library: it emits the PDF cross-reference table, page tree, and
  base-14 font resources itself. No third-party package, no network call.
- The generator **validates its own output** before writing: header, `startxref`,
  every cross-reference offset, and the trailer. A malformed document is refused
  rather than handed over.
- Cover-page test count is parsed from `TASK_BOARD.md`, so the deliverable cannot
  drift from the recorded evidence.
- Text outside WinAnsi (box-drawing layout diagrams, em dashes, check marks) is
  transliterated so the content stream stays valid Latin-1.
- `tests/unit/test_handoff_pdf.py` adds 13 tests covering escaping, wrapping,
  generated structure, and rejection of a corrupted cross-reference table.
- The suite now has **284 passing tests**.

### Fixed — snapshot backup script (real data-loss risk)

The backup script — the only substitute for version control — had two defects
that together produced **39.8 GB** of archives in the working tree (16 files):

- Its default target was `Path(f"..actionmesh-snapshot-{stamp}.zip")`, a
  **filename beginning with `..`**, not a parent-directory path. Every backup
  was therefore written *inside* the tree it existed to protect, so losing the
  tree would have lost the backup with it.
- Snapshots were not excluded from later snapshots, so each archive contained
  all previous ones and the size compounded on every run.

Fixed: the default target now resolves beside the tree (`default_target()`),
previous archives are excluded by name pattern, the output file is never
included in itself, writing inside the tree prints a warning, and the script
reports the resulting size. The same run is now **377 KiB instead of ~500 MB**.
`tests/unit/test_snapshot.py` adds 10 tests, including one that asserts archive
size does not compound across runs.

### Fixed — C5/C6 audit hardening

- Idempotency entries are completed only after independent verification, so an
  executed-but-unverified action cannot replay as `SUCCESS`.
- Subscriber queues are bounded; slow consumers are disconnected explicitly
  instead of allowing unbounded memory growth.
- Added vendor-neutral provider contract and requirements-matrix documents;
  no vendor or external side effect was introduced.
- Added an evidence matrix based on official Stripe and Slack documentation;
  unknown and sandbox-required requirements remain explicit, with no provider
  ranking or compatibility claim.
- Added a decision-readiness matrix and post-selection implementation gate that
  separate contract compatibility, runtime compatibility, and product
  suitability.
- Added regression coverage for replay and WebSocket delivery; the suite now
  has **261 passing tests**.

---

## [0.1.0] — 2026-09-29

### ⚠️ Reconstruction notice

The original working tree was deleted during development and `.git/` was lost
with it; no backup exists. This release is a **reconstruction** of the design
from memory, not a restoration of the lost code. Every claim below was
verified by executing the reconstructed code.

### Added — C1: AMP/1.0 protocol (`actionmesh/protocol/`)

- `models.py` — `Action`, `GraphNode`, `ActionGraph`, `ActionDefinition`,
  `Permission`, `Policy`, `PolicyRule`, `ApprovalRequest`, `VerificationResult`,
  `MeshEvent`, `ExecutionRecord`, `Capability`, `DeviceInfo`, `Identity`.
- Enums: `RiskLevel` (4), `ActionState` (16), `NodeType` (9), `PolicyDecision`
  (4), `FailureKind` (10).
- `validate.py` — fail-closed validation for untrusted payloads
  (`validate_action`, `validate_target`, `validate_identity`). Untrusted input
  can never set a lifecycle state.
- `version.py` — `AMP/1.0` negotiation. An unsupported version is an error, not
  a silent downgrade.
- `graphs.py` — `validate_graph`, `validate_plan`, `classify_failure`.

### Added — C2: Action Registry (`actionmesh/core/registry/`)

- `ActionRegistry` — `register`, `get`, `versions`, `list`, `discover`, `resolve`.

### Added — C7: Domain-specific mock providers (`actionmesh/providers/`)

- Shared deterministic mock harness with call tracking, failure injection,
  idempotent receipts, and C6-compatible observation.
- Messaging, calendar, files, notifications, contacts, and payment providers
  with domain state and realistic failure kinds.
- Explicit payment transitions from `prepared` to `confirmed`, then `settled`
  or `failed`; no network, credentials, or real side effects.
- Focused provider tests covering routing, state, failures, and observation.

### Added — C8: REST API initial slice (`actionmesh/api.py`)

- FastAPI application with health and capability discovery endpoints.
- Validated graph execution, execution-record lookup, approval submission, and
  cancellation endpoints backed by the existing executor.
- HTTP tests for authorization, malformed graphs, and endpoint behavior.

### Added — C9-C12, C14-C15 foundations

- Bounded in-memory event hub with executor lifecycle events, SSE replay/live
  streams, and WebSocket subscriptions.
- Exact-match memory/entity resolution, device presence and capability registry,
  and an in-process developer SDK.
- Optional API-key protection for HTTP and WebSocket transport boundaries.
- SQLite persistence for execution records, consent, and idempotency, plus
  process-local execution metrics; live vendor integrations remain deferred
  until official contracts and credentials are selected.
- Dependency-free dashboard page and release smoke coverage for health,
  metrics, capabilities, and the public app surface.
- CLI commands for capability JSON, dashboard URL discovery, and optional
  Uvicorn serving via the `server` extra.
- Docker/ASGI deployment metadata, health checks, environment guidance, and
  package-wheel validation.
- C13 official-provider boundary requiring HTTPS, environment-managed
  credentials, and concrete scopes without claiming a live vendor integration.
- C14 request-size limits, constant-time API-key checks, security headers, and
  focused transport-hardening tests.
- API permission grants now come only from server-side `ConsentStore`; request
  bodies cannot self-authorize execution.
- Dashboard upgraded from read-only status page to an application console for
  actor-scoped consent and the guarded simulated messaging workflow.
- Dashboard now includes recent execution history, actor-filtered consent
  history, and live SSE activity updates.
- Definition validation: name, version, risk, timeout, known + concrete
  permissions only, schema shape, retry policy.
- `check_params` — required-parameter and type checking, with a fail-closed path
  for malformed property specs.
- `resolve` — shape-validates untrusted input, binds the definition, and raises
  risk to `max(action, definition)`; risk is never lowered.
- 16 seeded `provider="mock"` definitions, all described `MOCK:`.
- **37 tests.**

### Added — C3: Permission + Policy Engine (`actionmesh/core/permissions/`)

- Closed catalog of 20 concrete permission scopes with namespace helpers.
- Two distinct matching regimes, and the split is a security boundary:
  **grants match exactly** (never wildcards); **policy rules may use wildcards**.
- `PolicyEngine` — 7-step deterministic precedence, auditable reasons,
  `explain()` for JSON audit records.
- Risk gate: CRITICAL always requires approval; HIGH requires approval unless
  the policy context opts in server-side.
- `ConsentStore` — server-side grant ledger; refuses wildcard and unknown consent.
- `policy_from_rules` — accepts untrusted dicts or typed `PolicyRule` objects;
  malformed entries become DENY rather than being dropped.
- **61 tests.**

### Added — C4: Action Graph (`actionmesh/core/graph/`)

- `GraphBuilder` — all 9 node types; action dicts validated as untrusted input.
- `validate_graph_structure` — unique ids, per-node config rules, dependency
  existence, no self-dependency, compensation targets, terminal node required.
- `find_cycle` — iterative DFS with a colour map, so a deep graph cannot exhaust
  the recursion limit (verified with a 2000-node chain).
- `topological_layers` / `execution_order` — deterministic; ties break on
  `sorted()` so identical graphs produce identical order.
- Deterministic serialization with a verified round trip.
- `authorize_graph` — composes C2 (registry) and C3 (policy) without
  re-implementing either. All-or-nothing: one denied node blocks the graph.
  `ASK` does not block; it flags the node as needing approval.
- **46 tests, including 11 graph security-invariant tests.**

### Security

- `tests/security/test_graph_invariants.py` — LLM output is untrusted data:
  an injected `authorized: true`, `permissions: ["*"]` or `role: "admin"` in
  action parameters grants nothing. Unknown actions, missing permissions,
  missing actors and wildcard grants all fail closed.
- **3 real bugs found by these tests and fixed:**
  - `select_rule` sorted restrictiveness ascending, letting an `allow` rule
    shadow a `deny` — a genuine authorization bypass.
  - `validate_action` silently dropped a caller-supplied `idempotency_key`,
    defeating idempotent replay.
  - `ConsentStore.grants()` conflated a filter ceiling with the grant's own
    risk ceiling, so legitimate consents were dropped.

### Known limitations

- CLI and dashboard release surfaces are implemented and covered by smoke tests.
- All capabilities are `provider="mock"`; nothing performs a real side effect.
- The default `ConsentStore` and `IdempotencyLedger` are in-memory; SQLite-backed
  stores exist for deployments that inject them and survive a restart.
- Real vendor integrations are intentionally deferred.
- No git history exists.

[0.1.0]: https://example.invalid/actionmesh/0.1.0
