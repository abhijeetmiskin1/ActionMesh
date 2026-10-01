# Provider Readiness Evidence

Research date: 2026-09-30

This is an evidence record, not a vendor selection or ranking. Evidence comes
from official public documentation only. No ActionMesh network call, credential,
SDK, or adapter was used.

## Sources examined

### Stripe API family

- [Authentication](https://docs.stripe.com/api/authentication)
- [API keys](https://docs.stripe.com/keys)
- [Idempotent requests](https://docs.stripe.com/api/idempotent_requests)
- [Webhooks](https://docs.stripe.com/webhooks)
- [Testing use cases and sandboxes](https://docs.stripe.com/testing-use-cases)
- [Rate limits](https://docs.stripe.com/rate-limits)
- [Error handling](https://docs.stripe.com/error-handling)
- [Refund and cancel payments](https://docs.stripe.com/refunds)

### Slack platform APIs

- [Authentication overview](https://docs.slack.dev/authentication/)
- [Events API](https://docs.slack.dev/apis/events-api/)
- [Request verification](https://docs.slack.dev/authentication/verifying-requests-from-slack)
- [Web API rate limits](https://docs.slack.dev/apis/web-api/rate-limits)
- [`chat.postMessage`](https://docs.slack.dev/reference/methods/chat.postMessage)

## Evidence matrix

Status meanings:

- **Documented:** stated in the official documentation examined.
- **Provider-specific:** documented, but the adapter must map it to ActionMesh.
- **Unknown:** not established by the examined public documentation.
- **Requires sandbox:** cannot be responsibly verified without an account,
  credentials, or live test delivery.

| Requirement | Stripe API family | Slack platform APIs | ActionMesh consequence |
|---|---|---|---|
| HTTPS/TLS | Documented; API calls use HTTPS and plain HTTP fails. | Documented for HTTP request URLs; request signing and TLS deployment are separate concerns. | C13 HTTPS boundary is sufficient as a prerequisite. |
| Credential model | Documented API keys, restricted keys, separate webhook signing secrets, environment/vault guidance, and rotation. | Documented OAuth 2.0 scopes, user/bot/app tokens, token rotation, and signing secrets. | `IntegrationConfig` needs credential plus inbound-signing-secret metadata for event adapters. |
| Provider identity | Documented account, sandbox, and webhook endpoint identity concepts. | Documented app, workspace/team, installation, and authorization identity fields. | Adapter configuration must bind provider identity to account/workspace context. |
| Capability discovery | Public API references enumerate resources and methods. | Public method/event references enumerate capabilities and scopes. | Discovery is documentation/configuration-driven; neither source establishes runtime capability discovery. |
| Permission/scopes | Restricted API keys expose per-key API permissions; exact mapping to ActionMesh scopes is adapter work. | OAuth scopes govern Web API and Events API access; method docs identify required scopes. | Keep ActionMesh exact grants separate from provider scopes. Mapping is provider-specific. |
| Idempotency | Documented `Idempotency-Key`; results are retained and replayed, including failures, with parameter consistency checks and pruning. | No general idempotency contract found for `chat.postMessage` in examined docs; response includes message timestamp. | Stripe adapter can map provider replay semantics; Slack message creation needs an adapter-level deduplication design. |
| Retry behavior | Documented exponential retry/backoff guidance for rate limits and webhook delivery retries. | Events API retries failed deliveries up to three times; Web API returns `429` and `Retry-After`. | Map provider retries to typed failures; do not blindly retry non-idempotent actions. |
| Duplicate behavior | Webhooks may duplicate events; event IDs should be tracked. | Event delivery retries are documented; duplicate suppression semantics for all event types are not fully established here. | Event ingestion needs authenticated event IDs and durable deduplication. |
| Verification | Retrieve objects, inspect status, and consume signed webhook events; exact confirmation flow is action-specific. | `chat.postMessage` returns a timestamp/message object; independent read-back and event semantics vary by method. | Provider result is never proof; adapter must provide `observe()` or a documented read-back/event strategy. |
| Async operations | Payment, refund, dispute, and webhook flows are explicitly asynchronous in documented cases. | Events API is asynchronous and requires rapid acknowledgement plus deferred processing. | Executor must represent uncertain/pending outcomes and event correlation. |
| Webhooks/events | HTTPS endpoints, signing secrets, raw-body verification, retries, duplicate events, out-of-order delivery, and replay/resend are documented. | HTTP request URLs or Socket Mode, signed secrets, timestamps, HMAC verification, retries, and rate-limited event delivery are documented. | Inbound events need a separate authenticated boundary; current API does not implement one. |
| Event authenticity | Stripe signatures, timestamp tolerance, constant-time comparison, and replay mitigation documented. | Slack HMAC signing secret, timestamp freshness, and constant-time comparison documented. | Authenticated event envelopes are an integration prerequisite, not ordinary `MeshEvent` input. |
| Event ordering/replay | Stripe explicitly does not guarantee ordering and documents manual/automatic resend. | Retry behavior is documented; global ordering/replay guarantees were not established. | Correlate by provider event ID; never rely on arrival order. |
| Cancellation | PaymentIntent and some refund cancellation states documented; availability depends on state/method. | No general cancellation for a posted message established; deletion is a separate method and not universal rollback. | Compensation/cancellation must be action-specific and never assumed reversible. |
| Compensation/refund | Refunds and cancellation are documented but have asynchronous failure, pending, and requires-action states. | Message deletion exists as a separate capability; semantic rollback is not equivalent to cancellation. | Model compensation as a new authorized action with independent verification. |
| Error taxonomy | HTTP/authentication, invalid request, rate limit, lock timeout, card/payment, and provider errors documented. | Method-specific `ok=false` errors include auth, scope, permission, invalid input, rate limit, transient, and service errors. | Adapter must map provider-specific errors to `FailureKind` without converting failure into authorization. |
| Rate limits | Global, endpoint, resource, concurrency, and `429` reason headers documented; sandbox limits differ. | Tiered per-method/workspace limits, special message limits, event delivery limits, and `Retry-After` documented. | Rate limits belong in provider adapter policy and retry metadata, not core permission grants. |
| Sandbox | Test mode and general sandboxes, test keys, simulated objects, and test cards documented. | A dedicated sandbox/test credential model was not established by the examined docs. | Stripe-like testing is documented; Slack testing requires workspace/app credentials and remains sandbox-required. |
| Test credentials | Official sandbox/test keys and test primitives documented. | Requires actual app/workspace installation and token/signing-secret setup; no credentials available here. | No compatibility claim can be made without sandbox testing. |
| Auditability/observability | Request logs, event delivery status, event IDs, and receipt/status objects documented. | Event IDs, authorization context, response timestamps, and method errors documented. | Store minimum provider IDs/statuses; retention and redaction remain deployment requirements. |
| Credential rotation | API-key rotation, expiry, restricted keys, access policies, and webhook-secret rolling documented. | Token rotation and signing-secret regeneration documented. | Secret rotation must remain outside request payloads and core authorization. |
| Failure/recovery semantics | Payment/refund state machines and webhook retries documented, but exact action-level recovery is provider-specific. | Event retry/failure-disable behavior documented; message recovery semantics remain action-specific. | Recovery plans must remain conservative and use `REQUIRES_USER` when outcome is uncertain. |

## Security cross-check

The evidence supports preserving the current ActionMesh boundaries:

- Provider scopes cannot replace ActionMesh registry permissions or grants.
- Provider responses and webhook payloads cannot authorize actions.
- Signed event verification must occur before event-driven execution.
- Provider event IDs and timestamps are required for replay/deduplication, but
  ordering cannot be assumed.
- Idempotency and verification remain separate: Stripe's idempotent response
  replay does not itself prove the external business outcome.
- Slack's documented absence of general message idempotency is an integration
  prerequisite, not permission to weaken ActionMesh idempotency rules.
- Mock providers remain offline and cannot be treated as evidence of live
  compatibility.

## Unknowns and sandbox-required facts

The examined public documentation does not establish:

- a provider-neutral mapping from either provider's scopes to ActionMesh's
  concrete permission catalog;
- complete action-by-action observation strategies for all capabilities;
- exact cancellation/compensation semantics for every action;
- a general Slack message idempotency guarantee;
- provider-specific credential rotation behavior in a deployed ActionMesh
  environment;
- actual latency, failure, retry, webhook ordering, or replay behavior under
  sandbox credentials;
- whether an adapter can satisfy ActionMesh's verification threshold without
  additional read-back APIs or durable event correlation.

These remain **REQUIRES SANDBOX** or **REQUIRES DECISION**, not compatibility
claims.
