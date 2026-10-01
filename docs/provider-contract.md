# Provider Contract

This contract defines the boundary between the provider-neutral ActionMesh core and
an action provider. It applies to simulated and official providers; a simulated
provider must remain explicitly marked as such.

## Identity and capabilities

A provider must expose a stable `name` and a `simulated` boolean. `supports(action)`
must be deterministic, side-effect-free, and limited to actions the provider can
actually execute. Capability definitions remain registered in the ActionMesh
registry; providers do not grant permissions or make policy decisions.

Each capability must declare:

- action name and version;
- provider name;
- concrete permission scopes;
- input and output schemas;
- risk level and retry policy;
- timeout and verification strategy;
- whether cancellation and compensation are supported.

## Credentials and transport

Official providers must use `IntegrationConfig` or an equivalent boundary that
requires an HTTPS base URL, explicit provider identity, concrete scopes, and
credentials loaded from an environment-managed secret store. Credentials must not
be stored in source, action payloads, logs, receipts, or API responses.

`supports()` must never load credentials or perform network I/O. A missing or
invalid credential is an authentication failure. A failed official provider must
never silently fall back to a mock provider.

## Execution

`execute(action)` is asynchronous and returns `ProviderResult`. It must:

- validate provider-specific inputs before applying a side effect;
- preserve the supplied `action_id` and `idempotency_key` in receipts;
- use bounded timeouts and documented retry behavior;
- classify failures with `FailureKind`;
- distinguish accepted requests from completed effects;
- report `simulated = True` for local or mock behavior.

The provider must not perform authorization. Registry resolution, permission
checks, risk gates, approval, and graph ordering remain core responsibilities.

## Idempotency

The provider must document the scope and lifetime of its idempotency key. A
repeated completed key must not create a second external effect and should return
the original receipt or an equivalent deterministic result. An in-flight key must
not be reported as completed. Failed or expired claims must have explicit retry
semantics.

The executor ledger claims a key before dispatch. The ledger records completion
only after independent verification succeeds; an executed-but-unverified action
is not a successful replay.

## Verification and observation

A provider must expose an observation mechanism for every action that can be
reported as verified. `observe(action)` returns current state tied to the action,
including stable identifiers sufficient to compare the requested parameters and
idempotency key.

`verify()` may provide provider evidence, but a provider success response alone
is never proof. The C6 verification engine decides among confirmed, refuted,
indeterminate, and unsupported outcomes. A provider without adequate observation
must produce an unverified result rather than claim success.

## Errors and retries

Provider failures use the existing `FailureKind` taxonomy. The adapter must
identify at least invalid input, authentication/authorization, transient/provider
failure, network failure, rate limiting, timeout, and permanent failure where
applicable. Retryability must be explicit and bounded; authorization and invalid
input failures must not be retried automatically.

## Events and cancellation

Providers may emit provider-specific progress or receipt events through the core
event boundary, but event publication must not bypass authorization or audit
rules. Events must not contain credentials or sensitive provider responses.

Cancellation semantics must be documented. Cancellation before dispatch must
prevent dispatch. Cancellation after an external effect has started must report
that the outcome may be uncertain and must use verification/recovery rather than
claiming rollback.

## Compensation and audit

Compensation is an ordinary action: it requires registry resolution, permissions,
risk evaluation, approval where required, idempotency, verification, and audit
events. A provider must not execute compensation automatically as an emergency
bypass.

Receipts and observations should contain only the minimum data needed for
verification and audit. Retention, redaction, deletion, and provider-specific
compliance requirements must be documented before production use.

## Testing gate

An official adapter is not ready until it has deterministic unit tests, failure
injection, idempotency tests, verification tests, sandbox tests where available,
credential-boundary tests, timeout/retry tests, cancellation tests, and a
security review proving that mock behavior and real behavior cannot be confused.
