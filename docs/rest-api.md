# REST API

The initial C8 transport is exposed by `actionmesh.api:create_app`.
`app = create_app()` provides the default seeded registry, policy engine, and
simulated providers. Deployments and tests may inject an `ExecutionEngine`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service and protocol status; the only route outside the API-key boundary |
| GET | `/dashboard` | The control room, also installable as a PWA |
| GET | `/manifest.webmanifest`, `/sw.js`, `/icon.svg` | Installable-app shell assets |
| POST | `/plan` | **Propose** candidate actions from natural-language intent (C18) |
| POST | `/execute` | Validate and execute a graph |
| GET | `/executions` | Recent execution records; `limit` is 1–100 |
| GET | `/executions/{execution_id}` | Retrieve one execution record |
| POST | `/executions/{execution_id}/cancel` | Request cancellation of a known execution |
| POST | `/approvals` | Submit an approval decision keyed by `action_id` |
| POST | `/consents` | Record a consent **server-side** — the only way to grant a permission |
| GET | `/consents` | List consents, filtered by `actor_id` |
| GET | `/capabilities` | Discover registered capabilities; optional `provider` filter |
| GET | `/metrics` | Process counters |
| GET | `/audit` | Audit chain records |
| GET | `/audit/verify` | Recompute the hash chain and report where it breaks |
| GET | `/events` | SSE stream; optional `execution_id` |
| WS | `/ws/events` | WebSocket event stream; optional `execution_id` |

## `POST /plan` — proposing, not deciding

The only route that accepts model-shaped input. It returns a **proposal**:

```json
{
  "intent": "tell mom I'll be late then pay alice 20",
  "actions": [
    {"type": "message.send", "parameters": {"to": "mom", "message": "I'll be late"}},
    {"type": "payment.prepare", "parameters": {"recipient": "alice", "amount": 20.0}}
  ],
  "notes": []
}
```

Each action carries **only** `type` and `parameters`; a proposal has no field for
risk, permissions, dependencies or lifecycle state. Anything the planner could not
map is reported in `notes` rather than silently dropped, and an empty or
non-string intent is a 400. **Calling this route runs nothing.**

`POST /execute` expects a JSON object containing `graph`. The optional `actor`,
`policy`, and `allow_high_risk` fields become a typed policy context. Permission
grants are never accepted from this request: call `POST /consents` first so the
server-side `ConsentStore` can record them. The graph and every embedded action
are validated at the API boundary; caller-supplied lifecycle state and action
identity are not trusted.

Example policy context (note: there is deliberately **no** `grants` key — see
`POST /consents`):

```json
{
  "actor": {"id": "user-1"},
  "policy": {
    "rules": [{"action": "message.send", "decision": "allow"}]
  }
}
```

Consent example:

```json
{
  "actor_id": "user-1",
  "scope": "message.send",
  "max_risk": "high",
  "reason": "approved messaging workflow"
}
```

Unknown actions, malformed graphs, invalid enums, injected grants, and invalid
request shapes are rejected with HTTP 400. Missing or insufficient authorization
is represented by the executor's normal denied result, not by bypassing the
authorization core.

## Two gates, both required

A consent is necessary but **not** sufficient. `PolicyEngine` matches active
grants first, then selects a rule, then falls back to `policy.default_decision`,
which is **DENY** when nothing matches. A request that omits `policy` is therefore
denied even when a valid server-side consent exists.

That is measured, not theoretical: an earlier revision of the dashboard planned
and then executed with no policy, and every plan came back `denied`. The correct
shape is a policy rule for the action **plus** a recorded consent.

The browser may declare what the workspace is willing to consider. It may never
hand the server a permission — grants come from `POST /consents` only, and
CRITICAL risk forces an approval regardless of both gates.

## Durability

The default wiring is in-memory. Realtime SSE and WebSocket are implemented (C9),
not "later phases". Set `ACTIONMESH_AUDIT_DB` to persist the audit chain to SQLite,
where it reopens **fail-closed**, and `ACTIONMESH_AUDIT_ANCHOR` to add truncation
detection — see [`audit.md`](audit.md). No endpoint talks to a real provider.
