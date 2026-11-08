# Provider Requirements Matrix

This matrix is vendor-neutral. It defines what must be learned and verified
before selecting or implementing an official provider. No vendor is selected by
this document.

| Requirement | Why it matters | Required | How verified | Current status |
|---|---|---:|---|---|
| HTTPS transport | Protect credentials and action data in transit | Yes | Official API and deployment documentation | C13 boundary requires HTTPS |
| Environment-managed credentials | Keep secrets out of source and payloads | Yes | Secret-loading and missing-secret tests | Boundary implemented; deployment policy open |
| Explicit provider identity | Prevent routing ambiguity and mock fallback | Yes | Provider name and routing tests | Supported by `ActionProvider` |
| Concrete permission scopes | Preserve exact grant and policy boundaries | Yes | Scope mapping review and authorization tests | Core catalog exists; provider mapping pending |
| Capability and action definitions | Allow registry validation before execution | Yes | Versioned schemas and registry registration | Core registry exists; adapter definitions pending |
| Idempotency semantics | Prevent duplicate external effects | Yes where supported | Repeated-key, failure, restart, and concurrency tests | Core ledger exists; provider contract pending |
| Verification mechanism | Distinguish accepted from completed effects | Yes | Read-back, receipt, webhook, or equivalent evidence | Core observation contract exists |
| Error taxonomy | Make retry and escalation safe | Yes | Map official errors to `FailureKind` | Mock mappings exist; vendor mapping pending |
| Timeouts and retries | Bound work and avoid duplicate effects | Yes | Official limits plus deterministic adapter tests | Core policy exists; vendor values pending |
| Rate-limit documentation | Prevent uncontrolled retries and service rejection | Yes | Official quota and response documentation | Not researched |
| Webhook/event support | Handle asynchronous or uncertain outcomes | Preferred; required if async | Signature, ordering, replay, and delivery tests | Core event hub exists; provider behavior unknown |
| Cancellation | Report whether an external effect can still occur | Preferred | Official cancellation semantics and tests | Core cancellation exists; provider behavior unknown |
| Compensation | Define safe reversal for reversible actions | Preferred | Explicit action, authorization, and verification tests | Core planning exists; execution deferred |
| Sandbox environment | Test without production side effects | Preferred | Separate endpoint/account and credential tests | Required before live implementation |
| Official API documentation | Avoid speculative behavior | Yes | Contract and version review | Not researched |
| Credential rotation | Limit exposure and support operations | Yes for production | Deployment and rotation procedure | Deployment policy open |
| Audit and retention | Meet traceability and data-minimization needs | Yes | Receipt fields, redaction, retention review | External audit policy open |
| Deterministic mock | Preserve offline core tests | Yes | Mock parity and failure-injection tests | Existing mocks remain required |
| Production/mock separation | Prevent simulated effects being presented as real | Yes | `simulated` propagation and routing tests | Core boundary implemented |

## Selection gate

No official adapter should be implemented until the candidate's documented
behavior is available for permission scopes, credentials, idempotency, errors,
verification, rate limits, sandbox access, and cancellation. Unknown behavior
must remain an explicit open item rather than being guessed in the core.
