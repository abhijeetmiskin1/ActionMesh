# Handoff

## Read this first

**This repository is a reconstruction, not a restoration.** The original working
tree was deleted during development. `.git/` was lost with it and no backup
exists on the machine. The code here was rewritten from the architecture and
design, then verified by actually executing it. Do not assume any behaviour
carries over from the lost tree — it does not.

## Verified state

```text
Command:  python -m unittest discover -s tests
Result:   Ran 417 tests — OK (0 failures, 0 errors)
Python:   3.14.4 (Linux); originally verified on 3.12.10
Deps:     FastAPI at runtime; httpx in the development environment
```

C0–C12 and C14–C17 are implemented and runtime-verified. C13 vendor adapters and
production deployment policy remain intentionally external.

## Layout

```text
actionmesh/
├── protocol/            C1  models, enums, fail-closed validation, AMP/1.0 versioning
├── core/
│   ├── registry/        C2  register / get / versions / discover / resolve, 16 mock seeds
│   ├── permissions/     C3  catalog, matching, risk, context, policy, ConsentStore, PolicyEngine
│   ├── graph/           C4  builder, nodeconfig, validator, serialization, authorization
│   ├── executor/        C5  lifecycle, idempotency, async engine
│   └── planner.py       C18 intent → untrusted plan (proposes; never authorizes)
├── providers/           C7  adapter interface + domain-specific mock providers
├── api.py               C8  FastAPI transport boundary
├── sdk.py               C12 developer SDK facade
├── dashboard.py         C15 dependency-free dashboard page
├── cli.py               C15 capabilities/dashboard/serve CLI
android/                 C17 dependency-free WebView shell (no AndroidX/Gradle)
tests/
├── unit/                core, provider, API, SDK, persistence, CLI, release tests
└── security/            graph and transport security tests

scripts/
├── snapshot.py          offline zip backup — run after every significant change
├── build_apk.py         build + sign the Android APK with the SDK build-tools
└── handoff_pdf.py       renders this handoff pack to PDF (stdlib only)
```

## How to run

```bash
python -m unittest discover -s tests
python -m actionmesh.cli capabilities --provider mock
python -m actionmesh.cli dashboard
python scripts/snapshot.py
python scripts/handoff_pdf.py
python scripts/build_apk.py            # signed Android APK -> dist/
```

For a local HTTP server, install the optional server extra and run
`actionmesh serve --host 127.0.0.1 --port 8080`. The API health check is
`GET /health`; the dashboard is `GET /dashboard`.

## C7 — Mock Providers (complete)

The `ActionProvider` interface, generic fallback, and domain-specific mocks are
implemented without touching the core:

1. One provider class per capability family (messaging, calendar, files,
   notifications, contacts, payments) over a shared base. Each keeps its own
   typed state — a sent message, a created event, a payment state machine.
2. **Each must implement `observe()`.** C6's verification engine depends on it.
   A provider without `observe` can never be confirmed — the correct behaviour,
   but it means a missing `observe` is a feature gap, not a bug.
3. **Realistic failure modes per domain**: a payment provider should fail with
   `AUTHORIZATION` for a declined card and `PROVIDER` for a gateway outage; a
   messaging provider should not return `INVALID_INPUT` for a network blip.
4. **Payment state transitions must be explicit** (`prepared` → `confirmed` →
   `settled` / `failed`) so `payment.verify` has something real to check.
5. Keep `simulated = True` on everything. Nothing here talks to a network.

## C8 — REST API (initial slice complete)

The API is built around the registry, authorization, executor, and verification
boundaries. It exposes health, metrics, dashboard, capability discovery,
server-side consent, validated graph execution, execution lookup, approvals,
cancellation, SSE events, and WebSocket events. Request-body permission grants
are rejected; grants must be recorded through `POST /consents`.

The `/dashboard` page is the first application surface. It lets a user record a
scope for their actor ID, **plan from natural language and inspect the proposed
actions**, execute a plan or the simulated `message.send` workflow, browse
capabilities and counters, inspect recent executions and consent history, and
watch live SSE activity. Consent is filtered by actor ID in
`ConsentStore.grants_for`; the UI cannot authorize a different user.

## C9–C12, C14–C15 — implemented foundations

The event hub and SSE/WebSocket streams are in `core/events.py`; memory/entity
resolution is in `core/memory.py`; device presence is in `core/devices.py`; the
developer facade is `sdk.py`; API-key hardening is optional via
`ACTIONMESH_API_KEY`; SQLite execution, consent, and idempotency persistence are
in `core/storage.py`, `core/permissions/store.py`, and `core/executor/idempotency.py`;
and counters are in `core/observability.py`. C15 also includes `Dockerfile`,
`.dockerignore`, `docs/deployment.md`, the wheel build configuration, and the
release smoke test.

## C17 — Android APK (complete)

The phone story no longer depends on Chrome's "Add to home screen". `android/`
is a dependency-free `WebView` shell — one activity, the single `INTERNET`
permission, an adaptive icon, and no AndroidX/AppCompat — pointed at the
server's `/dashboard`. It contains no application logic, so the server stays the
single source of truth for policy, execution and audit.

`scripts/build_apk.py` builds and signs it with only the SDK build-tools
(`aapt2` compile/link, `javac`, `d8`, `zipalign`, `apksigner`), downloading a
portable JDK and the command-line tools into the git-ignored `android-toolchain/`.
There is no Gradle and no Maven resolution. The result is
`dist/actionmesh-<version>.apk` (`app.actionmesh.control`, minSdk 26, targetSdk
34, signed with APK Signature Scheme v2+v3).

The build signs with a local **debug** keystore. That is fine for sideloading; a
store release needs its own keystore held outside the repository.

## C18 — Intent to plan

The pipeline's `intent / planner` stage is now implemented. `core/planner.py`
defines `Planner` (`plan(intent) -> Plan`) and a deterministic, offline
`RuleBasedPlanner` — the planner counterpart of the mock providers — plus
`plan_to_graph()`, the crossing from an untrusted proposal to a trusted graph.

The boundary is the point: `ProposedAction` carries **only** `{type, parameters}`.
There is no field for risk, permissions, dependencies, provider or lifecycle state,
so a plan cannot assert its own authority even if the model emits one. Anything
the planner cannot map lands in `Plan.notes` rather than being dropped, and an
empty plan is rejected instead of executed as a no-op.

Exposed as `ActionMeshClient.plan()` / `.plan_graph()` and `POST /plan`, which
returns the proposal and deliberately executes nothing. The planner is injectable,
so a real model replaces the rule-based one without touching the SDK or anything
downstream.

The control room has a **Plan from natural language** panel: it shows the proposed
actions and planner notes, and gates execution behind a separate, initially
disabled button. The browser builds the graph but sends only policy rules bounded
to the proposed action types — never a permission or a grant — so consent still
comes from the server-side store. Measured outcome: consented message and
reminder plans succeed; `pay alice 20` is denied even though the browser proposed
it *and* declared the policy rule for it.

## C13 — Real integrations (boundary complete; vendor adapters deferred)

`providers/real_base.py` defines HTTPS, credential, and concrete-scope checks
for official adapters. No vendor integration is claimed: official API contracts,
credentials, scopes, and deployment/network policy must be selected before
implementing real providers. All current executable providers remain simulated.

## Invariants that must keep holding

These are covered by `tests/security/`, `tests/unit/test_executor.py` and
`tests/unit/test_verification.py`, and must not be broken by C7+:

```text
An unknown action can never resolve or execute.
A plan carries only action types and parameters — never risk, permissions,
  dependencies or lifecycle state, no matter what the planner emits.
A plan that maps nothing is reported with reasons, never silently empty and
  never executed as a no-op.
Planning is not executing: nothing runs until the policy engine has decided.
A wildcard grant never satisfies a permission requirement.
A DENY can never be upgraded into ALLOW — not even by approval state.
Risk is only ever raised, never lowered, by trusted metadata.
Untrusted input cannot set a lifecycle state.
CRITICAL risk always requires explicit approval.
Missing context (actor, definition, permissions) fails closed to DENY.
A malformed graph is rejected, never partially repaired.
A node awaiting approval does not execute; approval is matched on action_id.
An executed-but-unverified action is NEVER reported as SUCCESS.
A COMPENSATION node is never forward work; an undo runs only after a failure.
A rollback is never reported as SUCCESS, whatever the undo did.
An irreversible action is never compensated, even if an undo is declared.
Nested compensation is rejected at validation, never silently dropped.
An action dispatched but not verified is always reported as a possible
standing side effect — never silently omitted, never speculatively undone.
A test that never runs is not a passing test — counts are verified.
A hash chain alone cannot detect tail truncation; an external anchor must, and
an anchor an attacker can rewrite protects nothing.
The same idempotency key never produces two side effects.
Non-retryable failures (INVALID_INPUT, AUTHORIZATION, PERMANENT) are not retried.
A provider's own success claim can never, by itself, confirm an action.
An action with no verification strategy is UNSUPPORTED, not successful.
```

## Known gaps and risks (be honest about these)

- **No git repository exists**, and git cannot be installed — this machine's
  network blocks github.com and git-scm.com (only python.org was reachable).
  `scripts/snapshot.py` writes a timestamped zip outside the tree; **run it
  after every significant change**. The original tree was lost once already.
- **Docker/ASGI packaging is available.** Docker was not installed locally, but
  the wheel build succeeded. The `actionmesh` CLI exposes
  capability, dashboard, and optional serve commands; deployment still needs
  environment-specific identity, backup, and secret-management policy.
- **Verification is delegated to the provider.** C5 correctly refuses to call an
  unverified action a success, but the *decision* is still the provider's.
  C6 makes that independent.
- **All 16 seeded actions are `provider="mock"`.** Every execution result
  carries `simulated: true`. No real message, file or payment has been sent.
- **SQLite persistence is available but not the default.** Deployments must
  explicitly inject the durable stores and configure backup/retention policy.
- **API-key hardening is optional.** Production deployments still need a full
  identity provider, key rotation, rate limiting, and audit policy.
- **Compensation is now executed, not merely planned.** C6 classifies recovery;
  C16 actually performs the undo when a forward run fails. It runs in reverse
  execution order, only for nodes that produced a *verified* side effect, never
  for an action in `IRREVERSIBLE`, and the result is **never** upgraded to
  `SUCCESS`. `ExecutionResult.compensations` and `compensation_required` report
  what happened, so a refused or failed undo is visible rather than implied away.
  Authorization already covered compensation actions, so rollback cannot become
  a policy bypass.
- An action that was dispatched but not verified is reported as a possible
  standing side effect and sets `compensation_required`, but is never
  auto-undone. An unconfirmed effect may not exist, and undoing speculatively
  would be a second guess at the user's data.
- **A truncated audit chain needs an external anchor to be detectable.** This was
  measured, not assumed: deleting the tail leaves a valid prefix that
  `verify()` calls `ok`, and a forged record can be re-anchored onto it. C16
  adds `FileAuditAnchor` / `AnchorMismatchError` and `AuditLog.open(anchor=...)`
  (API: `ACTIONMESH_AUDIT_ANCHOR`). It is only as good as where the anchor file
  lives — **point it at storage the audit database cannot rewrite**, and sign it
  with a KMS-held key in production. A sibling file protects nothing.
- **Compensation still runs against mock providers**, so the undo path is proven
  against in-process state, not a real external system.
- **Idempotent replay requires independent verification.** Executed-but-
  unverified actions are released for deliberate retry and never replayed as
  success.
- **Event subscribers are bounded.** A slow live subscriber is disconnected on
  queue overflow and must reconnect to replay retained history.

## Next owner

1. Review `docs/provider-contract.md`, `docs/provider-requirements.md`,
  `docs/provider-evidence.md`, and `docs/provider-decision-readiness.md`.
  Do not select a vendor, add credentials to source, or claim `simulated =
  False` until sandbox requirements and an approved adapter contract are
  established.
2. Put production identity, TLS termination, API-key rotation, rate limiting,
   SQLite backup, and secret management in the deployment environment.
3. If compensation execution is required, extend the executor with explicit
   rollback tests; do not silently treat a recovery plan as completed work.
4. To distribute the APK beyond sideloading, replace the debug keystore with a
   release key held outside the repository, and decide `usesCleartextTraffic`
   (put the server behind TLS for anything past a trusted LAN).

## Bugs found and fixed during verification

Kept here because they document real design decisions:

| Bug | Fix |
|-----|-----|
| `select_rule` sorted restrictiveness ascending, so an `allow` rule could shadow a `deny` | Sort descending — most restrictive wins on ties |
| `validate_action` dropped a caller-supplied `idempotency_key` | Preserve it; only `action_id` is system-assigned |
| Empty/garbage `version` passed `version_key()` and could register | Require a non-empty version with a numeric component |
| `ConsentStore.grants(max_risk=...)` conflated a filter ceiling with the grant's own ceiling | Renamed to `for_risk`, defaulting to "return all non-expired" |
| `CARRIES_ACTION` included `COMPENSATION`, which actually carries `compensation=` | Removed; compensation runs only during rollback |
| WebSocket API-key comparison used ordinary equality | Use `hmac.compare_digest` at the WebSocket boundary and test the rejection path |
| Executor dropped the trusted high-risk opt-in during dispatch re-check | Preserve `PolicyContext.allow_high_risk`; HTTP request bodies cannot set it |
| Idempotency was completed before verification | Complete the ledger only after independent verification succeeds |
| `snapshot.py` wrote each backup *inside* the tree it protects (the default was a filename beginning with `..`, not a parent path) and copied older archives into newer ones, reaching **39.8 GB** across 16 files | Default target now resolves beside the tree; prior archives excluded by name pattern; inside-tree writes warn. Same run is now 377 KiB |
| A COMPENSATION node ran as ordinary forward work: `builder.compensate()` sets no `depends_on`, so Kahn layering saw every undo as a **root node**. A create+delete pair fired in order and the engine reported `success` for a net no-op | Exclude COMPENSATION from the forward schedule, and drop edges pointing at one (an END depending on its compensation looked like a cycle). C16 runs undos in reverse order, only after a failure |
| `tests/unit/test_graph.py` defined `class TestCycleDetection` twice; the second definition shadowed the first, so its four cycle tests were collected by no test loader — tests that were counted as passing but never ran | Renamed the second to `TestDeepChain`; the suite grew from 375 to 379. A reminder that a duplicate class name is a silent hole, not a style nit |
| An Android `strings.xml` comment contained the literal `--` (from `--url`); an XML comment may not contain `--`, so `aapt2` rejected the resource | Reworded the comment. The resource tests parse every XML file, so a malformed resource now fails a test rather than only the APK build |
