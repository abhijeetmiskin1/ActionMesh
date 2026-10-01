# REST API

The initial C8 transport is exposed by `actionmesh.api:create_app`.
`app = create_app()` provides the default seeded registry, policy engine, and
simulated providers. Deployments and tests may inject an `ExecutionEngine`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service and protocol status |
| GET | `/capabilities` | Discover registered capabilities; optional `provider` filter |
| POST | `/execute` | Validate and execute a graph with actor, grants, and policy |
| GET | `/executions/{execution_id}` | Retrieve an in-memory execution record |
| POST | `/approvals` | Submit an approval decision keyed by `action_id` |
| POST | `/executions/{execution_id}/cancel` | Request cancellation of a known execution |

`POST /execute` expects a JSON object containing `graph`. The optional `actor`,
`policy`, and `allow_high_risk` fields become a typed policy context. Permission
grants are never accepted from this request: call `POST /consents` first so the
server-side `ConsentStore` can record them. The graph and every embedded action
are validated at the API boundary; caller-supplied lifecycle state and action
identity are not trusted.

Example policy context:

```json
{
  "actor": {"id": "user-1"},
  "grants": [{"scope": "message.send", "max_risk": "high"}],
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
request shapes are rejected with HTTP 400. Missing or insufficient authorization is represented by
the executor's normal denied result, not by bypassing the authorization core.

The default API is intentionally in-memory. Authentication, persistent records,
rate limiting, and realtime events are later phases; no endpoint talks to a real
provider.
