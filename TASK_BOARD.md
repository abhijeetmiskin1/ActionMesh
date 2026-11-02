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

## C19 — Mobile-to-laptop control plane (Phases 1–9) `[x]`

- **C19a — Device identity + registration:** `core/identity.py` — 256-bit opaque
  identity ids, `IdentityStore` (+ durable `SQLiteIdentityStore`), revocation that
  mints a *new* record so history still attributes the old one. Presence is asserted
  only by the agent that owns the laptop; there is no HTTP route to mark a device
  online.
- **C19b — Credentials:** `core/credentials.py` — keyed digests (a copied database
  cannot be offline-brute-forced), expiry, revocation, and `authenticate` resolving
  through the identity store so **device revocation kills its credentials**.
- **C19c — Pairing:** `core/pairing.py` — 6-digit codes, loopback-only minting, a
  sliding-window attempt budget, a ceiling chosen **at issuance** (a limit the
  requester picks is not a limit), and `core/secret.py` durable-key resolution that
  never writes on import.
- **C19d — Laptop agent (`actionmesh/agent/`):** the only place real OS access lives —
  confined filesystem (`openat`+`O_NOFOLLOW`), allow-listed command policy, argv-only
  spawning with constructed child environments, a process/server registry that can
  only address what it launched, and editor/project discovery. 19 `device.*`
  capabilities, all verified against real files, processes, git and a loopback
  listener. Enforced by an AST security guard (`test_dangerous_primitives.py`).
- **C19e — Realtime streaming:** `node.started`/`execution.progress`/`approval.required`
  on the existing hub; a safe result boundary (`executor/serialization.py`) that never
  ships file contents or command output; actor-scoped SSE/WS with a short-lived
  `EventSource` watch ticket (`core/streaming.py`) and `Last-Event-ID` resume. The
  mobile dashboard drives a real laptop process end to end and recovers state on
  reconnect.

## P11 — Production platform `[x]`

- **Configuration + deployment modes:** `core/config.py` — `AppConfig`/`DeploymentMode`
  (development · lan · production) with fail-closed startup: a `production` process
  without durable execution storage, a durable audit chain, a durable signing key, a
  shared key, or (when bound off-loopback) named trusted proxies **refuses to start**
  rather than coming up quietly insecure.
- **Durable execution + schema versioning:** `SQLiteStore` is now wired into the app
  when a database is configured, versioned via `PRAGMA user_version` with a fixed
  migration list (a newer file is refused, never down-migrated), and round-trips
  timestamps/results. A completed execution is readable after the process restarts.
- **Health vs readiness:** `/health` stays cheap liveness; `/ready` reports mode,
  durable-store and audit reachability and online-device count — and never gates on
  device connectivity, because a healthy server with every laptop offline is healthy.

## P10 — Security hardening, adversarial sweep, cleanup `[x]`

- **Stream authorization is server-enforced:** the unscoped "operator" view on
  `/events` and `/ws/events` is now gated to **loopback only**; a remote caller with no
  credential or valid ticket is refused (401 / WS close 1008), so a rejected-and-cleared
  phone token can never silently escalate to operator authority. Regression tests cover
  the denial and the operator path.
- **Rate limiting:** `core/ratelimit.py` — a bounded sliding-window limiter on
  pairing-mint, redeem, ticket-mint, execute and approval, keyed by actor/source, with
  expired-key pruning and a hard cap on tracked keys; `ACTIONMESH_RATE_LIMIT=0` for
  local dev. Documented as process-local (proxy enforces globally).
- **Unused-file audit:** every module in `actionmesh/` is imported, re-exported through
  a package `__init__`, an entry point, or is a tested+documented subsystem
  (`core/memory.py`, C10). **No files were proven safe to delete; none were removed**
  (§14.4).

## C20 — Live output streaming + operational CLI/SDK `[x]`

- **Provider→hub incremental output:** `core/output.py` — an `OutputPublisher` the
  engine installs around each node's provider dispatch; the runner calls it per chunk;
  the laptop provider streams a real command's stdout/stderr as `execution.output`
  events. They are **transient** (`EventHub.publish(transient=True)`: never audited,
  never replayed), bounded (≤ 4 KiB/chunk, ≤ 64 KiB/node, then `truncated`), and
  actor-scoped. Verified in a real browser: a `python3 chatter.py` streamed its lines
  live to the dashboard, reached `Succeeded · verified`, and left no trace in the audit.
- **Mobile live output panel:** the dashboard renders chunks as text into a bounded
  `<pre>` with a follow toggle that preserves the reader's scroll; no `innerHTML`,
  change-guard intact.
- **CLI:** `actionmesh config validate` and `actionmesh doctor` (network-free — the
  security guard forbids an outbound client in the package, so operational commands
  against a running server stay on the HTTP API / SDK).
- **SDK:** in-process `executions()`, `execution(id)`, `approvals()`, `metrics()`,
  `event_stream()` — the same authoritative state the API serves, no new HTTP stack.

## C21 — Enforced limits stated, not implied `[x]`

- **The timeout ceiling is reported, not applied in silence:** `CommandOutcome` carries
  `timeout_requested_s`, `timeout_applied_s` and `timeout_clipped`. A caller's
  `timeout_s` can only lower the agent's configured ceiling
  (`ACTIONMESH_COMMAND_TIMEOUT_S`), never raise it — and a run stopped at 60s after
  asking for 600s now says which of those happened, instead of reading as a command that
  hung. All three survive the safe-result allowlist, so they reach `POST /execute` and
  `GET /executions/{id}`; the engine-level assertion pins them by exact equality, so
  removing one from the allowlist is a failing change.
- **A detached launch states its output posture:** every process and server record
  carries `output_captured: false`. A launched child has nothing reading its pipes —
  a consumer that died with the request that started it would leave a dev server blocked
  on its next write — so its output is discarded at launch, and watching a program's
  output means `device.terminal.execute`, which streams.
- **Investigated and deliberately not built:** live log streaming for detached
  `device.server.start`. Doing it needs a consumer that lives as long as the process
  (a bounded log file plus a tailer owned by the application's lifespan); partially
  draining a pipe and stopping is how the user's server hangs at its next 64 KiB, which
  is a worse defect than the missing feature.
- **Fixed — a ship-blocking bug found as an intermittent test failure:**
  `secret.load_key` read the key file with `.strip()` on **bytes**. Generated keys are
  random, so in about one install in twenty the first or last byte is one `strip()`
  treats as whitespace; the file then measured 31 bytes, the "truncated key" guard fired
  (rightly refusing to regenerate), and the server **could not start again after its
  first restart**. Keys are read as undelimited bytes and sliced to `KEY_BYTES`, which
  keeps a hand-written file's trailing newline harmless too. `test_secret.py` now
  round-trips all 256 byte values at both ends of a key. The suite had been failing
  about one run in ten; it is deterministic now.

## C22 — The phone is told which limit bound a run `[x]`

- The live panel reads the **authoritative record** — on `execution.completed`, and for
  everything already watched on reconnect through the existing `reconcile` — and puts the
  answer in the output panel's own note line:
  `a1: stopped by the laptop's own 3s ceiling, not the 60s that was asked for` for a
  clipped timeout, and `p1: launched detached, so nothing captures its output — run it as
  a command to watch it` for a detached launch. Rendered with `textContent`; no
  `innerHTML`; the panel's change-guards are intact.
- Verified in a real browser against a real laptop agent running with
  `ACTIONMESH_COMMAND_TIMEOUT_S=3`: a real `python3 chatter.py` streamed twelve live
  ticks, was cut at the 3s ceiling after operator approval, reached
  `Succeeded · verified`, and the panel printed the ceiling sentence naming both numbers.
  A second run started a detached child, and the empty panel explained its own silence.
  No console messages.
- Caught by that run: the note first read `record.nodes`, which is the `POST /execute`
  key — `GET /executions/{id}` calls the same node views `results`. Reading the wrong one
  fails *silently* (no note at all), so the key and the anchor are both asserted in
  `test_dashboard.py` now.

## C23 — The phone can start a laptop command `[x]`

- **The loop was closed from the wrong end before.** The dashboard could pair, consent,
  approve, watch and reconcile a laptop execution, but nothing on the page could *begin*
  one — the only way to run a command was to POST to `/execute` yourself. A new "Run a
  command on the laptop" panel does it through the same endpoint, the same policy check
  and the same approval path, with no new API surface.
- **argv, not a command line.** Arguments are entered one per line and sent as a list;
  the panel never joins them into one string, because that shape is only meaningful to a
  shell and the laptop's command policy refuses it. The request carries the program, its
  argv, a working directory and a timeout — and no authority: no grants, no role, no
  risk, no `allow_high_risk`. Consent stays in the server-side store, and the CRITICAL
  scope still parks for a decision.
- **The consent panel can now name the laptop's scopes.** It offered only `message.send`,
  so a command started from this page could never be consented from the same page.
  `file.read`, `file.write`, `device.process.control` and `system.shell` are offered now.
- **Verified end to end in a real browser, through the controls** (not by calling the
  API): record consent for `system.shell`, fill the panel, press *Run on laptop*, watch it
  park with "Submitted. It is waiting on your approval…", press the approval panel's own
  *Approve*, and the real `python3 chatter.py` streamed twelve live ticks before the
  agent's 3s ceiling cut it — `Succeeded · verified`, with the note line naming both
  numbers. Console clean.
- **Caught while building it:** the page's JS is embedded in a non-raw Python string, so
  `split('\n')` was consumed by *Python* and shipped a literal newline inside a JS string
  literal — a syntax error no string-assertion test would have caught. Escaped in source,
  and `jsParsed` is now confirmed in a real browser. Suite grew to 1021 tests.

## C24 — The read surface is as scoped as the stream `[x]`

- **A curtain, not a wall.** P10 pinned the *event* channel to the authenticated actor,
  but `GET /executions`, `GET /executions/{id}`, `GET /approvals`, `GET /audit` and
  `POST /executions/{id}/cancel` never looked at the credential at all. A phone that
  could not watch another device's execution could fetch its record by id; worse,
  `/approvals` returns each waiting action's **parameters**, so a pending message body or
  command line was readable by every paired device, and any device could cancel any run.
- **One rule, reused.** `_record_scope` calls the same `_stream_principal` the stream
  uses rather than restating it: a credential sees only its own actor, a foreign
  execution id answers **404** (not 403 — the difference confirms which ids are real),
  and a remote caller with no credential is denied. The loopback operator's unscoped view
  is unchanged and is still tested.
- **Cancel stopped grading its own homework.** It answered `status: "cancelled"` while
  `engine.cancel()` had only recorded the request; it now answers `requested`, and it is
  throttled like the mutating routes around it (the limiter had no `cancel` budget).
- **Consequence worth knowing:** the tightened rule means an unset transport peer is no
  longer an accidental operator view — `TestClient`'s default peer is `"testclient"`, not
  loopback, so eleven existing tests had to say `client=LOCAL` explicitly to keep
  asserting what they always meant to assert. The alternative was leaving the boundary
  open for anyone whose proxy did not fill in `client`.
- `tests/security/test_record_scoping.py` (8 tests, two paired phones) is the guard.
  Suite grew to 1029 tests; ruff, mypy and warning-clean all green.

## C25 — The decision surface checks whose human it is `[x]`

- **The worst hole found so far, and it was not a read.** `POST /approvals` never asked
  who was calling. Any paired phone could POST another actor's parked `action_id` with
  `status: "approved"` and the server not only accepted it but returned **that actor's
  resumed execution record** in the response. The human-in-the-loop was enforced by
  nobody: the ceremony happened, and the person deciding was whichever device asked
  first. `POST /consents` had the mirror image (a caller named any `actor_id` to record a
  consent for them), and `POST /devices/{id}/revoke` let any device unpair any other.
- **Same rule as C24, one implementation.** All three go through `_record_scope`: a
  device may decide only an action sitting in its own waiting set (403 otherwise, 404 for
  an id it has none of), may name only its own actor when recording or reading consent,
  and may revoke only its own device -- a foreign name gets the same 404 an unknown one
  does, because device ids are names the caller chose. The operator at the laptop is
  unchanged and still asserted by test.
- **Consequence for the fixtures:** several long-standing tests had been approving and
  consenting as an *anonymous remote* client, because that worked. They now post from the
  laptop client, which is what they always claimed to model -- the human at the machine.
  One test (`test_the_token_decides_the_actor`) got strictly stronger: the consent for
  another actor is now genuinely recorded by the operator, and the credential still
  cannot use it.
- 7 new tests in `TestWriteScoping`; suite grew to 1036. Ruff, mypy and warning-clean
  all green.

## C26 — The dashboard works from a paired phone, not just the laptop `[x]`

- **Two self-inflicted wounds found, one of them older than C25.** Every panel sent
  `actor: {id: <the #actor box>}`. With a device token that is refused by design ("a
  credential cannot act as someone else"), so `POST /execute` answered 400 — a flaw C23
  shipped with and C22 never noticed, because both were verified as the loopback operator.
  Then C25 scoped `/consents`, and the page's own refresh began asking for the actor in
  the box, which a paired phone may not name: the dashboard would have shown "Refresh
  failed" on every load from a phone.
- **The credential is now the only identity that matters when there is one.**
  `identityBody()`, `consentsPath()` and `consentBody()` omit the actor whenever a token
  is present, and `GET`/`POST /consents` accept an omitted `actor_id` for a credential,
  taking it from the token. The operator — which has no actor of its own — still must
  name one, and still does.
- **Verified in a real browser as a phone**: pair through the panel, store the token,
  then deliberately leave the actor box stale. Consent records, refresh succeeds, and
  `POST /execute` is accepted. The CRITICAL command it started was then **denied by the
  pairing ceiling** (`exec_denied_… · Denied`, streamed to the live panel) — the
  restriction from the earlier credential-ceiling work doing its job, not a crash.
- Suite grew to 1041 tests; ruff, mypy and warning-clean all green.

## C27 — The phone becomes a real client `[x]`

- **C27a — the flagship flow was unreachable, and nobody had noticed.** The pairing
  panel offered `low`/`medium`/`high`, but `device.terminal.execute` is CRITICAL, so a
  phone minted from this page could never run a command: every attempt was denied at the
  door, before consent or approval entered it. C23 shipped a command panel and C26 proved
  it worked *as the operator* -- the phone's denial was visible in that run and read as
  correct, which it was. The fix is one option in the ceiling select, chosen only by the
  human standing at the laptop (issuance stays loopback-only), labelled with what it
  unlocks and honest that anything below it leaves the panel unable to run.
- **Verified in a browser as the phone, start to finish**: pair at `critical` through the
  panel, consent `system.shell`, fill the command panel, press Run, approve from the
  approval panel -- a real `python3 chatter.py` streamed twelve live ticks to the phone,
  was cut by the laptop's 3s ceiling, and the row read `Succeeded · 1/1 · verified` with
  the ceiling sentence. Same run, over the wire: `/audit` returned **only** `abhi`'s
  records (C24's scoping), the chain verified `ok`, and the streamed output appeared
  nowhere in it. Console clean.
- **Docs corrected**: `rest-api.md` claimed a paired credential carries "a risk ceiling of
  MEDIUM". It carries whatever the laptop chose at mint (default `low`, up to
  `critical`), and risk can only be raised, never lowered.
- **Still open for C27**: no panel for the metadata-only laptop capabilities
  (projects, servers, processes, editors), and no authorized content surface, which is
  what keeps `device.file.read` off the phone entirely.

- **C27b — the phone can see what the laptop is running.** `safe_result_view` now
  re-projects inventory rows (`processes`, `servers`, `projects`, `editors`, `services`)
  through the **same** allowlist instead of summarizing them away: scalars only, strings
  still capped at 120 characters, and a list or dict inside a row is dropped rather than
  walked — one level deep, never two — with at most 20 rows. Every other key keeps the old
  `{key}_count` behaviour, so this is the same rule applied deeper, not a hole in it. A new
  "What the laptop is running" panel lists this agent's children and stops one by its
  **issued id**; a finished row loses its Stop control rather than offering a lie.
- **Three defects found by driving it in a browser as a paired phone**, none of them
  visible from a unit test:
  - the page kept its **pre-pairing operator stream** after pairing, so it went on
    receiving other actors' executions and then 404'd reading the records behind them
    (console showed five such errors). Pairing now closes the stream and re-opens it with
    a ticket bound to the new credential.
  - an approved stop left the row saying `running`, because the completion event can
    arrive while the registry has not yet reaped the child. The inventory now re-reads
    twice, shortly and after a beat; the row disappears and the count falls to 0.
  - a "Asked the laptop to stop…" placeholder was overwritten by that refresh, so it was
    removed: the answer is the result, not a promise made before the request returned.
- **Verified end to end as the phone**: pair at `critical` → consent `device.process.read`
  and `device.process.control` → start a detached `watcher.py` → approve → the panel shows
  `watcher · process · running` → Stop → parks for approval → approve → row gone, count 0.
  Server-side check confirmed the process really died. **Console clean.**

## C28 — The phone can browse and open a laptop file `[x]`

- **The content-surface question, answered without a new door.** `device.file.read` was
  implemented, confined, consent-gated and verified, and a phone still could not see a
  byte of it: the safe result view strips `content` by design, and that design is the
  reason file bodies don't leak into event streams. The fix is not a privileged route —
  a raw `GET /file` would be a second execution path that skips registry, policy, consent,
  approval and audit, which is the one thing this architecture forbids. Instead the
  provider emits the bytes it read onto the transient `execution.output` channel with
  `stream: "content"`, which was already built to carry content to an authorized observer:
  live, bounded, actor-scoped, never audited, never replayed.
- **`entries` joined the projected rows**, so `device.file.list` finally shows names,
  types and sizes instead of `entries_count: 3`. A list of bare strings still cannot
  become rows, so the pre-existing "entries must not leak" test was updated to say what it
  now means: names surface only as allowlisted scalars on rows the provider itself built.
- **A "Files on the laptop" panel**: directory field, list, one Open button per file, and
  the body lands in the live output panel.
- **Verified in a browser as a paired phone**: pair, consent `device.file.list` +
  `file.read`, list `notes` → `notes/todo.txt · file · 39 B` and `notes/photo.png · file ·
  264 B`; open the text file → the panel holds exactly `buy coffee\nfix the ceiling bug\n
  ship it\n`; open the PNG → **0 characters added**. The audit chain (28 records, verifies
  `ok`) contains neither the body nor a `stdout` key, and the console is clean.
- **Emitted on the loop, not in the worker thread**: the sync handlers run under
  `asyncio.to_thread`, and waking an `asyncio.Queue` from another thread is not safe, so
  `_stream_content` is called after the thread returns.

## C29 — Launched servers keep a log, and the phone can read it `[x]`

- **The last hole in "start my dev server and show me what it printed" is closed.**
  Capture is a file, not a pipe, and that choice is the whole design: a pipe nobody drains
  fills at ~64 KiB and the server blocks in `write`, so a reader that died with its
  request would stop the user's machine. A file applies no backpressure — verified by a
  test that writes 400 KB through a detached child and asserts it **exits 0** rather than
  hanging.
- **Off unless the operator opens it.** `ACTIONMESH_AGENT_LOG_DIR` unset means a child is
  launched exactly as before, with its output discarded (`output_captured: false`), which
  is the project's "nothing enabled by absence" rule applied to disk. When set: each child
  gets a `0600` file with an unguessable name, the directory is **refused at startup if it
  is inside the workspace** (otherwise the confined `device.file.read` could collect
  whatever a server printed), and a relative value is refused rather than silently
  resolved against the current directory.
- **Bounded without a background reader**: every pass over the process table trims any log
  past 512 KiB back to its tail, behind a marker so a shortened log never reads as a
  complete one. The documented cost is that a write landing between the read and the
  truncate is lost — log lines, bounded — because the alternatives are a full disk or a
  blocked server.
- **`device.process.logs`** (MEDIUM, `device.process.read`, gated behind the same shell
  boundary as the other process capabilities): addressed by the issued `process_id` only,
  capped at 64 KiB per read, and its bytes travel the transient channel as
  `stream: "logs"` — actor-scoped, never audited, never replayed. The result payload keeps
  `{process_id, bytes, lines, truncated}`. A process launched without capture is refused
  with the reason, and an id the registry never issued is refused outright.
- **A gap this exposed and fixed**: the consent panel could not grant
  `device.server.control` (nor five other laptop scopes), so server capabilities were
  unreachable from the page that advertises them. The select now lists all 13 scopes the
  device definitions require, and a test derives that set from `device_definitions()` so
  the list cannot silently fall behind it again.
- **Verified in a browser as a paired phone**: pair at `critical`, consent
  `device.server.control` + `device.process.read`, start `python3 server.py 45787`,
  approve → row reads `demo · server · running · :45787` with Stop **and** Logs enabled;
  pressing Logs brought back `booting the demo server`, the stderr startup warning,
  `listening`, and both request lines. On disk the file was `-rw-------` outside the
  workspace; the audit chain (300 records, verifies `ok`) contained none of the log text.
- Suite grew to 1073 tests; ruff, mypy and warning-clean all green.

## C30 — The phone can ask a run to stop, and is told what that means `[x]`

- **`POST /executions/{id}/cancel` had existed since C19 with no control on the page**, so
  "User controls" stopped at starting things. A Cancel button now sits on each live row and
  disappears when the run reaches a terminal state.
- **A real bug in that button, caught by using it.** The first version hid Cancel on a run
  that was streaming, because a run that *parks for approval* publishes
  `execution.completed(state: waiting_permission)` — the request finished, the execution
  had not. The page recorded that as final, so the control vanished on resume and the
  "N running" count was wrong too. Fixed with an explicit `TERMINAL_STATES` set, and by
  clearing `final` when a node starts.
- **Measured, then documented: cancellation is honoured between steps, not inside a running
  provider call.** A 60s `python3 chatter.py` was cancelled mid-stream and kept printing
  (20 → 70 → 94 ticks, still `Executing`). The button therefore says "takes effect at the
  next step" rather than implying the command stopped, and `rest-api.md` says the same.
  Making it interrupt the in-flight call is possible — run the provider call as a task,
  cancel it, and route the `CancelledError` through the existing unconfirmed-effect path
  since the child's group-kill already runs on cancellation — but it touches the ledger and
  the "dispatched but not verified" reporting, so it is listed as a deliberate open item
  rather than done in passing.

## C34 — VoiceOS is conversational: sessions, steps, references `[x]`

- **New package `actionmesh/voiceos/`**: `models` (TaskSession/TaskStep/Conversation),
  `store` (versioned SQLite, own schema, opened lazily and closed on shutdown), `tasks`
  (operator-authored task templates, refused at load if they name an unregistered action
  or condition on something that is not observed evidence), `llm` (provider seam +
  validation + honest refusal), `reasoning` (intents, references, decomposition),
  `orchestrator` (advance one authorised step at a time).
- **Six new routes**, all actor-scoped through the one existing rule
  (`_stream_principal` via `_record_scope`): `POST /voiceos/messages`, `GET
  /voiceos/conversation`, `GET /voiceos/sessions`, `GET /voiceos/sessions/{id}`, `POST
  .../run`, `POST .../cancel`. A session that is not yours answers 404, the same shape
  as an execution that is not yours.
- **The authority rule held.** A message proposes and never runs (`ran: false`); running
  is a press on a session step, and each step is a fresh registry → policy → consent →
  risk → approval → executor → verifier pass built from the caller's own credential.
  Conversation state is stored in a separate table from every authority-bearing store.
- **Conditional continuation is evidence-driven.** A verified demo: task `tests` = run
  `python3 write_note.py`, then read `made-by-voice.txt` `when: {step: 0, exit_code:
  0}`. Step 1 parked as CRITICAL, was approved, folded in as `succeeded · verified`, the
  condition matched the real exit code, step 2 ran and verified, session `completed`, `2
  of 2 steps settled`.
- **References.** `show my files` → run → `read the second one` resolved to
  `device.file.read path=notes.txt` from the listing the user had just been shown, and
  its body streamed back. With nothing observed the same sentence answers "I have no
  list to take that from".
- **Three defects found by driving the page, all pinned.** (1) The conversation read had
  no actor for the local view, so history never rendered — the GET now names the actor
  in the query, as `/consents` already does, and a credential that tries to name someone
  else is refused 403. (2) The orchestrator read `NodeOutcome` dataclasses as dicts, so
  a verified step was recorded `succeeded_unverified`: evidence silently lost. (3) A
  step that ended out of band left the session reading `awaiting_approval` beside a
  cancelled execution; `fold_records` now reconciles from the record on read, reading
  only.
- **Cancellation through the task.** `run command python3 chatter.py` → approve → "Stop
  it" → the stream froze at tick 38, the record read `cancelled`, the step folded to
  `cancelled · the action was dispatched, so its effect is unconfirmed`, and no
  `chatter.py` process survived.
- **+21 tests (1148 → 1168)**: 20 on the voiceos layer (proposal-only messages, per-step
  authorisation, actor scoping across two paired phones, reference resolution with and
  without evidence, template refusal, LLM validation and honest absence, durable reopen,
  schema refusal, record folding) and one tightened voice-surface test. Browser-verified
  on a real agent with the console clean and a network log limited to the sanctioned
  endpoints.

## C33 — VoiceOS V0.1: the loop is closed `[x]`

- **Voice → real laptop → verified → spoken now works end to end**, and it works through
  ActionMesh rather than beside it: `/plan` → **Approve & run** (its own press) →
  `/execute` → the existing approval gate → SSE → verification → speech.
- **The device phrases live in the one planner** (`actionmesh/core/planner.py`), not in a
  browser-side intent mapper: inventory questions, `read this file <path>`,
  `stop the process <id>`, `stop the dev server <name>`, `run command <argv>`. Rules that
  need a target only match when the caller said it, so `stop the server` maps to nothing
  rather than to a guess, and `run my tests` maps to nothing at all — the planner will not
  invent a command line, and the voice page says it could not safely map the request.
- **A parked run is not an outcome.** The page spoke "finished as waiting_permission"
  because a run that parks publishes `execution.completed` when the *request* ends — the
  C30 trap, rebuilt in a new surface. One `TERMINAL` set, referenced everywhere, and the
  completion handler returns early on anything that has not ended.
- **Subscribe before you ask.** Output is transient and never replayed, so opening the
  stream after the response had already missed the run it was reporting. `runPlan` now
  opens the stream first and adopts the execution id from the first event that names it.
- **`GET /approvals` reported the caller's risk, not the registry's.** `Action.risk_level`
  defaults to `medium` when a body omits it, so a CRITICAL command was labelled "medium" on
  the approval prompt itself. It now reports `effective_risk`, the same raise-only rule
  authorization uses. Found by reading the screen the user reads.
- **Speech output**: browser `speechSynthesis` behind a `SPEAK_ADAPTERS` seam, a toggle and
  a stop button, short phrases only, command output never spoken, and success claimed only
  from the record's verified node count.
- **+33 tests (1115 → 1148)**: 23 on the voice surface, 9 planner device rules, 1 for the
  approval risk label.
- **Browser-verified on a real laptop agent** (`127.0.0.1:45203`, `device.*` registered,
  agent online): `what is my laptop running` → `device.process.list` (low · needs
  `device.process.read`) → run → `success · 1 node(s) verified by an independent check`,
  timeline correct, `Done. Verified by an independent check.` spoken.
  `run command python3 write_note.py` → previewed as `critical risk · needs system.shell` →
  run → parked (`waiting_permission`, approval card reading **critical**, orb in the
  approval state, no outcome claimed) → Approve → the real subprocess ran, `writing 0…3,
  done` streamed live → verified → spoken once. Then `read this file made-by-voice.txt`
  returned `written by the voice-driven command` — the file the voice-driven command had
  just created, so the machine really changed state. `run my tests then align the struts` →
  `I could not safely map that request…` with both clauses named and run disabled.
  `run command python3 chatter.py` → approved → cancelled at tick 29 → `cancelled`,
  `Cancellation confirmed`, `Cancelled — effect unconfirmed`, and `pgrep` showed no
  `chatter.py` alive. The network log held only `/health`, `/capabilities`, `/devices`,
  `/plan`, `/execute`, `/events`, `/approvals`, `/executions/{id}` and its `/cancel`; no
  `/consents`, no pairing, no audit. Audit chain verified `ok` (41 records) containing
  neither the streamed output nor the file body. Console empty.
- **Not built:** conversation history, multi-turn context, a device picker (one agent per
  server still), barge-in, wake words, non-`en-US` recognition.

## C32 — VoiceOS slice 1: a microphone on the pipeline `[x]`

- **`GET /voice` is a second surface, not a second authority.** Speech → transcript →
  `POST /plan`. There is no execution control on the page at all, and that is asserted
  against the served HTML rather than claimed: fetch targets must be a subset of
  `/health`, `/capabilities`, `/plan`.
- **The transcript is untrusted input of exactly the kind `/plan` already assumes.** It
  cannot name a permission, a risk level, a grant or an approval — the plan object has no
  such field, and the page sends `{intent}` and nothing beside it.
- **Risk and permission text are read from `GET /capabilities`** — registry metadata — so
  what a proposal claims about itself never reaches the screen. An unregistered type is
  labelled as unregistered rather than implied safe.
- **No audio path exists to abuse:** no `getUserMedia`, `mediaDevices`, `MediaRecorder`,
  `FormData`, base64, `ArrayBuffer` or `blob:` in the page, no `localStorage`, and the only
  thing that can leave the browser is text. `docs/voiceos.md` records the alternatives that
  were evaluated and refused — server-side STT (outbound network + a credential + ActionMesh
  becoming the thing that hears you), a WASM model (a dependency for one slice), and a
  `MediaRecorder` upload (storing audio by default).
- **Listening is user-initiated and bounded.** One tap opens the microphone for one
  utterance; `continuous = false` explicitly; `onend` deliberately does not restart; the
  button stays clickable while listening because the second tap is the only way to stop;
  `visibilitychange` stops it.
- **Text is the first-class path.** The transcript box is never disabled, so a browser
  without recognition loses the microphone and nothing else, and a hand-edited transcript is
  never overwritten by a late result.
- **The offline shell precaches `/voice` beside `/dashboard`**, because the worker's fallback
  is the dashboard and a handset that asked for the microphone page must not land in the
  control room.
- **Two defects found by driving the real page.** `setBusy` cleared `micButton.textContent`,
  which deleted the microphone icon on the first tap, and it set `disabled = true` while
  listening — which would have stranded the microphone open with no control left to press.
- **31 tests (1084 → 1115).** Browser-verified on the real server (`127.0.0.1:45202`): a
  typed compound intent produced two ordered proposals with registry risk labels; an
  unmatched clause surfaced as `note · no rule matched: 'quantize the struts'`; a CRITICAL
  payment inside a multi-step intent read `critical risk · needs payment.prepare`. The PTT
  flow was driven with an **injected fake recognizer** — no microphone hardware was available
  in that environment — and the page's own settings overrode the stub's
  (`continuous=false`, `interim=true`, `lang=en-US`, `maxAlternatives=1`), interim text
  appeared without touching the box, the final result filled it and previewed, and `onend`
  left the start count at **1**: no self-restart. A hand-edited transcript survived a later
  result; the no-recognition path read "This browser has no speech recognition here — type
  your request above"; this Chrome's real recognizer path opened and closed with the state
  back at off; the network log held only `/capabilities`, `/health` and `/plan`; the console
  was empty; `/dashboard` still rendered all twelve panels; and at a forced 360px column the
  only overflow was the decorative pulse ring, with the 112px mic far above the 44px tap
  minimum.
- **Not built, deliberately:** speech synthesis and spoken feedback, barge-in, conversation
  history, a device picker, wake words, language negotiation. Running a plan stays in the
  control room.

## C31 — Cancelling a run stops the operation, not merely the wait `[x]`

- **The gap C30 measured and deliberately left open is closed.** The executor now keeps
  the provider call it is awaiting (`_inflight`, keyed by execution), so `cancel()` sets
  the flag *and* interrupts that call. A single long command stops instead of running to
  its ceiling.
- **Cleanup is awaited before the state is reported.** `_await_cancelled` waits for the
  cancelled call to finish unwinding — which is where the runner's existing
  process-group kill runs — so `cancelled` means the children came down, not that the
  wait gave up.
- **A genuine outer `asyncio.CancelledError` still propagates.** The re-raise is gated
  on `is_cancelled(execution_id)`: a cancellation the user did not ask for is not
  converted into a benign `CANCELLED` result, which would hide a shutdown from the
  caller.
- **Dispatched work is never re-described as clean.** The node reports `CANCELLED` with
  `attempts: 1` and `verified: null`; the ledger key is failed, so a deliberate retry
  may re-run it but a replay-as-success cannot; and `_report_unconfirmed` now matches
  `{requires_user, cancelled}`, which sets `compensation_required` and publishes
  `compensation.unconfirmed`. The cancellation is a user choice, not a transient fault,
  so no retry is issued.
- **No second state system.** `ActionState.CANCELLED` existed at `d7c148c` and was not
  touched; the executor reports it through the same record the timeout and denial paths
  use.
- **9 tests (1075 → 1084).** Eight executor tests on a provider that blocks inside the
  call: the running call is interrupted; the result reports the unconfirmed effect and
  sets `compensation_required`; `calls == 1`, so it was not retried; the record never
  reads back as success; an outer `task.cancel()` propagates and records no user
  cancellation; a run cancelled between steps stops the next node; a cancel that races
  completion is first-wins; a parked run cancelled and *then* approved reports
  `cancelled` with `calls == 0`. One real-subprocess integration test asserts the
  command's parent **and its grandchild** are gone after the cancellation — that the
  operation stopped, not that the API answered.
- **Browser-verified on the real server, from the actual dashboard** (`python3
  chatter.py`, `127.0.0.1:45201`): parked → approved → streamed to tick 97 → Cancel
  clicked while the row still read `Executing`. Output stopped at tick 97; the LIVE row
  became `Cancelled · unverified` with the Cancel button hidden and the count `idle`;
  the SSE feed carried `device.terminal.execute · cancelled` then `Execution cancelled`;
  `GET /executions/{id}` agreed — `cancelled`, `attempts 1`, `verified null`, empty
  `result`, `duration_ms 24309` against a 60s ceiling; the shell confirmed pid 45768 and
  pid 45769 both gone; the audit chain still verified `ok` across 49 records and
  contained `compensation.unconfirmed`. A `python3 --version` run re-verified approval →
  resume → `Succeeded · verified` with its output streamed, and the console stayed
  empty. (`python3 -c …` correctly *failed* — the interpreter-code guard refused it,
  which is the pre-existing command policy working, not a regression.)
- **The wording changed with the behaviour.** The Cancel control no longer promises
  "takes effect at the next step"; it reads `Cancellation requested`, and
  `docs/rest-api.md` describes interruption rather than a step boundary.

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

Last run: **1168 tests, OK** (0 failures, 0 errors) on Python 3.14 (Linux), with
`ruff check` and `mypy actionmesh` clean.
(The reconstruction was originally verified with 358 tests; C17 added 16 for the
APK plus one snapshot guard, a lint pass un-shadowed 4 cycle tests, C18 added
46 for the planner and its dashboard panel, the C19 mobile control plane
(C19a–e), the P10 security-hardening pass and the P11 production-platform work grew
the suite past a thousand, C20 added the live-output coverage, C21 added 10 for
reported limits and the key-file read that a `.strip()` was corrupting, C22 added 2 for
surfacing those limits on the phone, C23 added 5 for starting a laptop command from
it, C24 added 8 for scoping the read surface to its owner, C25 added 7 for the
decision surface, C26 added 5 for making the dashboard work from a paired phone, and
C27a added 1 for the pairing ceiling that makes the phone's command flow reachable at
all, C27b added 8 for inventory rows reaching the phone and the three stream/refresh
defects that driving it exposed, and C28 added 5 for the file body arriving over the
transient output channel, and C29 added 18 for captured logs of launched servers plus the
consent scopes that had been missing, C30 added 2 for a Cancel control plus the
parked-run state fix it exposed, and C31 added 9 for interrupting a provider call already
in flight and reporting the effect it may have already had, and C32 added 31 for the
VoiceOS surface — its fetch targets, its microphone lifecycle, and the absence of any
audio path, and C33 added 33 more for the closed loop — its device phrases, the
parked-run and stream-order defects driving it exposed, and the approval risk label, and C34 added 21 for the conversational task layer — its actor scoping, its per-step authorisation, and the three reconciliation defects driving it exposed.)

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
  Since C0 the repository has been on git (`main`); this is now an independent
  second copy, not the only defence against losing the work.
  Covered by 11 tests in `tests/unit/test_snapshot.py`.
