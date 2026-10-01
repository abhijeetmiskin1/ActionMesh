# Provider Decision Readiness

This document prepares a deliberate provider-selection decision. It does not
select, rank, or recommend a provider.

## Three separate questions

1. **Contract compatibility:** public documentation suggests that a provider can
   expose the required boundary. This is the only question with meaningful
   current evidence.
2. **Runtime compatibility:** ActionMesh can implement and verify the behavior
   in a sandbox. This requires credentials and controlled tests.
3. **Product suitability:** the provider meets product, legal, operational, and
   business requirements. This requires an explicit owner decision.

"Contract-compatible" must never be read as "runtime-compatible" or
"product-suitable".

## Decision-readiness matrix

Status meanings: **EXISTS** is implemented in ActionMesh; **DOCUMENTED** is
supported by public provider evidence; **UNKNOWN** is not established;
**SANDBOX REQUIRED** needs real test credentials; **DECISION REQUIRED** needs an
owner or product choice.

| Requirement | ActionMesh requirement | Current evidence | Remaining uncertainty | Required next action |
|---|---|---|---|---|
| Identity | Stable provider, environment, account/project, and capability identity | `ActionProvider.name`, registry provider, and C13 config exist; public docs expose provider-specific account/workspace identities | Exact identity model and multi-account behavior | DECISION REQUIRED: define selected provider identity model |
| Credentials | Environment/secret-store loading; never request-body credentials | `IntegrationConfig` validates environment-managed credential names; official docs describe secret handling | Secret manager, rotation, revocation, and deployment isolation | DECISION REQUIRED, then SANDBOX REQUIRED |
| Permissions | Registry scopes and ActionMesh grants remain authoritative | Core exact grant matching and provider scope evidence exist | Complete action-to-provider-scope mapping | Create mapping review after selection |
| Idempotency | Local claim, provider key, replay, in-flight, failed, and verified-completion states | Core ledger and verified-only replay exist; Stripe semantics documented; Slack general message semantics unknown | Scope, retention, parameter binding, and failure behavior per provider action | SANDBOX REQUIRED: duplicate/concurrent/retry tests |
| Verification | Submission is distinct from independently verified outcome | C6 observation/evidence engine exists; provider read-back varies by action | Read-back, webhook, or receipt evidence for each action | Define per-action verification plan after selection |
| Events | Authenticated, validated, correlated, deduplicated provider events | Core bounded event hub exists; signed events and retries documented for examined APIs | Event envelope, provider ID, ordering, replay, and durable ingestion | Define inbound event adapter and persistence boundary |
| Event authenticity | Signature/HMAC, timestamp freshness, replay protection, raw-body handling | Official signed-event patterns documented | Secret rotation, clock policy, event parser, and deployment termination | SANDBOX REQUIRED: valid, stale, altered, duplicate, and unknown events |
| Replay protection | Separate authorization, idempotency, event deduplication, and verification | Core request idempotency and bounded event replay exist | Provider-specific event replay and cross-process persistence | Define durable event-deduplication record |
| Retries | Typed, bounded, provider-aware retries | Core retry policy exists; official rate-limit/retry behavior documented | Safe retry set per action and provider response | Map provider failures to `FailureKind` after selection |
| Rate limiting | Provider limits must not weaken authorization or cause unsafe retries | Provider rate limits are documented at a high level | Deployment-wide throttling, fairness, and distributed coordination | DECISION REQUIRED: operational throttling model |
| Cancellation | Cancellation before dispatch and uncertain post-dispatch outcome | Core cancellation exists; provider cancellation is action-specific | Which actions can actually be canceled | SANDBOX REQUIRED per action |
| Compensation | Compensation is a new authorized, idempotent, verified action | Recovery planning exists; no automatic compensation | Reversal/refund semantics, partial failure, approval, and audit | DECISION REQUIRED before enabling execution |
| Error mapping | Provider errors map to `FailureKind` without becoming authorization success | Core taxonomy and provider-specific error references exist | Complete action/error mapping and retryability | Build adapter mapping tests after selection |
| Sandbox | No production effects during validation | Stripe-like sandbox evidence exists; Slack credentials/environment remain required | Sandbox feature parity, limits, event delivery, and test-data reset | SANDBOX REQUIRED |
| Auditability | Minimum provider IDs/statuses, redaction, retention, and correlation | Core records/events/metrics exist; provider IDs documented in places | Retention, sensitive fields, and external audit requirements | DECISION REQUIRED: deployment audit policy |
| Credential rotation | Rotation/revocation without unsafe downtime | Public documentation describes rotation for examined APIs | Runtime rollout, dual-secret windows, and incident procedure | Define deployment runbook after selection |

## Existing ActionMesh audit

| Boundary | Status | Evidence |
|---|---|---|
| Executor/provider boundary | EXISTS | `ActionProvider`, dispatch-time registry/policy re-check, typed `ProviderResult` |
| Idempotency | EXISTS | In-memory and SQLite ledgers; in-flight is not success; verification-gated completion |
| Verification | EXISTS | C6 observation/evidence model; unsupported and uncertain outcomes are not success |
| Events | EXISTS | Bounded history and bounded subscriber queues; slow consumers disconnect |
| Event authenticity | MISSING | No provider webhook ingestion or signature verifier exists |
| Permissions | EXISTS | Registry definitions and exact ActionMesh grants remain authoritative |
| Credentials | PARTIALLY EXISTS | HTTPS/environment credential boundary exists; deployment secret management is external |
| API security | EXISTS | Request limits, headers, constant-time API-key checks, server-side consent |
| Persistence | PARTIALLY EXISTS | SQLite stores exist; default application wiring remains in memory |
| Audit | PARTIALLY EXISTS | Execution records, events, and metrics exist; tamper evidence/retention policy is open |
| Cancellation | PARTIALLY EXISTS | Core cancellation exists; provider post-dispatch semantics are unknown |
| Compensation | PARTIALLY EXISTS | Plans are modeled; automatic execution is intentionally deferred |
| Provider health/status | PROVIDER-SPECIFIC | No generic live health contract is asserted by the current boundary |

## Post-selection implementation gate

No adapter implementation begins until every item below has an owner, evidence,
and a test plan.

### Provider identity

- provider ID and stable name;
- sandbox and production environment identifiers;
- account/project/workspace identity;
- region or residency constraints;
- capability set and version policy.

### Credential strategy

- secret source and storage system;
- loading mechanism and isolation boundary;
- least-privilege scope configuration;
- rotation and revocation procedure;
- failure behavior for missing, expired, or revoked credentials.

### Permission mapping

For every action, document:

```text
ActionMesh permission
        -> provider capability
        -> provider credential scope
```

The provider scope is a prerequisite check only. It never becomes ActionMesh
authority, and provider responses never override the ActionMesh policy engine.

### Idempotency and verification

For every externally observable action, document and test:

```text
submit
  -> provider acknowledgement
  -> provider state/read-back/event
  -> ActionMesh verification
  -> verified result
```

The test plan must cover request identity, provider key, local record, retry,
duplicate response, in-flight state, completed state, failed state, replay, and
verification after replay.

### Event security

The selected provider adapter must define signature verification, raw-body
handling, timestamp tolerance, replay protection, event ID, deduplication,
ordering assumptions, retry handling, parser validation, and untrusted-payload
handling.

### Failure model

Map authentication, authorization, validation, rate limiting, timeout,
transport failure, provider unavailability, duplicate request, already-completed
request, cancellation, compensation failure, and verification failure to the
existing `FailureKind` and recovery model.

### Sandbox validation

Before production credentials or external effects, test successful and rejected
actions, duplicate requests, retries, timeout, authenticated and invalid events,
stale events, duplicate/out-of-order events, read-back verification,
cancellation, and compensation where applicable.

## Current stop condition

The repository is contract-ready and evidence-researched. It is not provider-
compatible, provider-integrated, or production-connected. The next meaningful
step requires provider selection, product requirements, sandbox credentials, or
an explicit deployment decision. No ActionMesh code should cross that boundary
by inference.
