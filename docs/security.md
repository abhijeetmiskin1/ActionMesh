# Security Model

## Threat model

ActionMesh exists because "ask an LLM to do things" is a dangerous default.
The threats below are the ones the architecture is built against.

| # | Threat | Attack surface | Mitigation | Test | Residual risk |
|---|--------|----------------|------------|------|---------------|
| 1 | **LLM self-authorization** | LLM emits `"authorized": true` or a policy hint | Policy context is a typed model; the engine never reads action parameters | `test_graph_invariants.py` | **Low** — rests on no layer ever consulting `parameters` for authorization |
| 2 | **Permission escalation via injection** | `parameters.permissions = ["*"]` | Requirements come only from the registry definition | `test_graph_invariants.py` | **Low** |
| 3 | **Risk downgrade** | `"risk_level": "low"` on a CRITICAL action | Effective risk is `max(action, definition)`; grants carry a `max_risk` ceiling | `test_registry.py`, `test_policy_engine.py` | **Low** — trusted metadata can only raise risk |
| 4 | **Wildcard consent** | user (or attacker) grants `*` | `ConsentStore` refuses non-concrete scopes; grants match exactly | `test_permissions.py` | **Low** |
| 5 | **Unknown action execution** | LLM invents an action name | `registry.resolve` raises on unknown actions | `test_registry.py` | **Low** |
| 6 | **Policy shadowing** | an `allow` rule shadowing a `deny` rule | Equally specific rules resolve to the most restrictive decision | `test_policy_engine.py` | **Low** — depends on deterministic specificity ordering |
| 7 | **Lifecycle forgery** | `"state": "success"` in a request | `validate_action` forces `state = CREATED` | `test_registry.py` | **Low** |
| 8 | **Approval bypass** | approving a different action, or approving a DENY | Approval is matched on `action_id`; DENY is terminal | `test_policy_engine.py` | **Low** |
| 9 | **Graph injection** | cycle, dangling dep, orphan node to stall or loop the executor | `validate_graph_structure` rejects; DAG enforced | `test_graph.py` | **Low** |
| 10 | **Idempotency loss** | retry double-sends a message or double-books | `idempotency_key` preserved end-to-end; ledger releases on failure | `test_registry.py`, `test_executor.py` | **Medium** — only protects actions that actually supply a key |
| 11 | **Command injection** | shell-out to a provider or tool | Package imports `subprocess`/`os.system` nowhere | `test_dangerous_primitives.py` | **Low** — AST guard misses a call routed through a locally bound alias |
| 12 | **SSRF / outbound network** | core dials an attacker-chosen URL | Network modules banned package-wide; allowlist empty by default | `test_dangerous_primitives.py` | **Low** — adding a path to `NETWORK_ALLOWED` is a deliberate, reviewable edit |
| 13 | **Unsafe deserialization** | `pickle`/`marshal` loads attacker data | Import ban; untrusted payloads use typed Pydantic-free validation | `test_dangerous_primitives.py`, `test_registry.py` | **Low** |
| 14 | **Dynamic code execution** | `eval` / `exec` / `__import__` | Call ban that the allowlist **cannot** exempt | `test_dangerous_primitives.py` | **Low** |
| 15 | **Malicious provider response** | provider claims success but state disagrees | Weighted evidence: a claim alone is below threshold; any contradiction is decisive | `test_verification.py` | **Medium** — a provider that owns `observe()` is trusted within its own domain |
| 16 | **API key guessing** | unauthenticated non-health route | Optional key, `hmac.compare_digest`, rejected before routing | `test_security_api.py` | **High** — key is optional by default and there is no rate limiting |
| 17 | **Oversized request** | memory/CPU exhaustion | Size limit enforced before routing | `test_security_api.py` | **Medium** — bounded per request only; no global rate limit |
| 18 | **Credential leakage** | secret in source, config, log or response | No credential exists; boundary test asserts config never contains a secret value | `test_real_provider_boundary.py` | **Low today** — C13 must keep this invariant using env/secret stores |
| 19 | **Reversed/wildcard adapter scope** | adapter asks for `*` or an `http://` base | Contract rejects non-HTTPS base URLs and non-concrete scopes | `test_real_provider_boundary.py` | **Low** |
| 20 | **Audit tampering** | attacker edits, deletes or reorders the audit trail | Append-only hash chain: each record holds a hash of its content *and* of its predecessor; `verify()` recomputes the whole chain and never trusts stored hashes | `test_audit.py` | **Medium** — a chain stored beside its own data cannot stop an attacker who rewrites the entire store. Real tamper evidence needs an anchor *outside* the store (append-only medium, signed checkpoint, or write-once storage) |

Threats **not yet addressed** — the relevant layer does not exist yet. The
exposure column is the honest answer to *"how bad is this today, before the
missing layer lands?"* rather than a comfortable "n/a".

| Threat | Why open | Exposure today |
|--------|----------|----------------|
| Prompt injection via the intent box | `POST /plan` and the control room turn **user text** into `ProposedAction` values — the first point where untrusted text becomes structured data. No retrieval layer exists; when one lands, retrieved text must be modelled as untrusted data too | **Low** — `ProposedAction` carries only `{type, parameters}`, so injected text has no risk/permission/state field to occupy; `plan_to_graph` re-validates every action, unmapped clauses surface in `Plan.notes`, and the browser can never hand the server a permission. A plan can still be *wrong*; policy and approval bound the damage |
| Replay of a signed request | No request signing; the API-key boundary is optional by default | **High if exposed** — terminate TLS and require a key at the edge |
| Device impersonation | C11 device identity is in-process only, not a production identity system | **Medium** — the mesh is not yet reachable across machines |
| Resource exhaustion beyond request size | Per-request bounds exist (`wait_s`, `max_attempts`, body size); there is no global rate limiting or per-caller quota | **Medium** — enforce at the reverse proxy / WAF |
| Secret at rest | No secret store exists to protect | **None today** — C13 must use env/secret stores only |

### C14 status

Threat-model pass completed: **20 mitigated threats**, each with a named test
and an explicit residual-risk rating, and **5 open threats** with their real
exposure. The genuinely uncomfortable row is **16** — API-key protection is
optional by default and there is no rate limiting — and it is left rated
**High** rather than talked down.

Additions this pass:

- `tests/security/test_dangerous_primitives.py` (8 tests) converts the prose
  claim "there is no subprocess, eval or exec" into an executable guard. It
  parses every module in the package with `ast`, so comments and docstrings
  cannot make it pass or fail spuriously.
- The guard distinguishes `urllib.parse` (string handling, used by
  `providers/real_base.py` to enforce HTTPS) from `urllib.request` (network
  access). That distinction was found by the guard firing on real code and
  being investigated rather than suppressed.
- Network/process imports are banned package-wide behind an **empty**
  `NETWORK_ALLOWED` allowlist, so a future official adapter (C13) requires an
  explicit, reviewable edit rather than silently gaining network access. The
  allowlist relaxes imports only — `eval`/`exec` stay banned even there.

C15 then closed the largest remaining open item with `actionmesh.core.audit`:
an append-only, hash-chained `AuditLog` whose `verify()` recomputes every hash
rather than trusting stored ones. `EventHub(audit=...)` seals each published
event and `record_decision()` chains a `PolicyEngine.explain()` result. The
chain **persists to SQLite** and reopens fail-closed, so tampered history
stops the process instead of being silently inherited. **Audit tampering moved
out of the open list and into row 20**, with its residual risk stated plainly:
a chain stored beside its own data resists partial tampering, not a full
rewrite — that needs an external anchor.

## Invariants

These are the properties the platform guarantees. Each has a test.

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

1. **An unknown action can never resolve or execute.** `resolve()` raises.
2. **An unknown permission can never authorize.** The catalog is closed;
   anything outside it is a DENY.
3. **The LLM cannot authorize itself.** Authorization is computed from the
   registry definition, the actor, the policy and server-side consent — never
   from action parameters.
4. **Risk cannot be downgraded.** Trusted metadata can only raise it.
5. **DENY cannot execute**, and cannot be upgraded by approval state.
6. **Required approval cannot be bypassed.** CRITICAL always asks; the
   `allow_high_risk` opt-in is server-side only and does not apply to CRITICAL.
7. **Consent cannot be wildcarded**, and expired consent authorizes nothing.
8. **Malformed input fails closed.** Unreadable schemas raise rather than being
   skipped, because a silently-ignored schema is an open door.
9. **Graphs cannot contain cycles**, so the executor cannot be trapped in one.
10. **Ambiguity resolves to the most restrictive outcome** — in policy rules, in
    graph node config, and in deserialization.
11. **Audit history is tamper-evident.** Editing, deleting, reordering or
    re-sealing any record makes `AuditLog.verify()` report the exact `seq` where
    the chain broke.

## Secrets

- No credential, API key, token or private key exists anywhere in this
  repository.
- No module reads `os.environ` for secrets, because no module needs one yet.
- When C13 adds real integrations, credentials must come from environment
  variables or a secret store, must never be committed, and must never appear
  in logs, audit records, error messages or API responses.

## What is honest about the current state

The core and simulated providers are local logic. There is:

- no provider network I/O,
- no provider filesystem I/O outside the interpreter,
- no `subprocess`, `eval` or `exec`,
- no dynamic imports driven by untrusted input,
- no credentials.

That is not an accident — it is what lets the core security properties above be
stated precisely today. The API, SQLite stores, and deployment surfaces now
exist, so each deployment must re-run the threat model and add environment-level
controls (C14/C15).

## Reporting a vulnerability

See [SECURITY.md](../SECURITY.md).
