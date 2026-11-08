# Architecture

## Layering

ActionMesh is layered, and dependencies point **downward only**:

```text
intent → graph → registry → permissions → executor → providers
```

A layer may consume the ones below it. It may never re-implement them, and it
may never skip them.

| Layer | Package | Phase | Owns |
|-------|---------|-------|------|
| Protocol | `actionmesh/protocol/` | C1 | The vocabulary every layer speaks |
| Planner | `actionmesh/core/planner.py` | C18 | Turns intent into an **untrusted candidate plan**; proposes only |
| Registry | `actionmesh/core/registry/` | C2 | What actions exist and what they require |
| Policy | `actionmesh/core/permissions/` | C3 | Whether an action may proceed |
| Graph | `actionmesh/core/graph/` | C4 | The shape and order of a plan |
| Execution | `actionmesh/core/executor/` | C5 | Executes authorized graphs and publishes lifecycle events |
| Providers | `actionmesh/providers/` | C7 | Simulated domain adapters with independent observation |
| Transport | `actionmesh/api.py` | C8-C9 | REST, SSE, and WebSocket boundaries |
| Platform | `actionmesh/core/memory.py`, `devices.py`, `storage.py`, `audit/` | C10-C16 | Memory, presence, SQLite persistence, hash-chained audit |
| Client shell | `android/` | C17 | A native WebView app around the same `/dashboard`; no logic of its own |

## The planner is the only untrusted *producer*

Every other layer begins from data it received. The planner is the one component
that manufactures data from human text, so it is the most likely place for a
prompt-injection to land. Its output type is therefore deliberately impoverished:
`ProposedAction` carries `{type, parameters}` and has **no field** for risk,
permissions, dependencies, provider or lifecycle state. A model that "argues" for
a lower risk or a `permissions: ["*"]` payload has nowhere to put it.

Anything the planner cannot map is returned in `Plan.notes` rather than dropped.
A plan that silently discarded half an intent would look complete, and the user
would approve something other than what they asked for.

## Trust boundaries

The single most important property of this system is that **data coming from an
LLM is never trusted with authority**. It can propose; it cannot authorize.

```text
        UNTRUSTED                    TRUSTED
  ┌────────────────────┐      ┌──────────────────────────┐
  │ natural language   │      │                          │
  │ LLM plan output    │─────▶│ validate_action()        │  schema gate
  │ HTTP request body  │      │ ActionRegistry.resolve() │  capability gate
  │ deserialized graph │      │ PolicyEngine.evaluate()  │  authority gate
  └────────────────────┘      └──────────────────────────┘
                                        │
                                 ┌──────▼───────┐
                                 │ Approval     │  human gate
                                 └──────┬───────┘
                                        ▼
                                   Executor (C5)
```

Every arrow crossing from left to right is a validation that **fails closed**.
Anything unexpected is an error, never a coerced value.

### What untrusted input cannot do

These are enforced by tests, not by convention:

- **Set a lifecycle state.** `validate_action` always forces `state = CREATED`.
  A caller cannot submit `"state": "success"` and have it believed.
- **Grant itself a permission.** Permissions come from the *registry
  definition*, never from action parameters. An injected
  `parameters.permissions = ["*"]` is inert data.
- **Lower its own risk.** Effective risk is `max(action.risk, definition.risk)`.
  A `payment.confirm` submitted as `"risk_level": "low"` is still CRITICAL.
- **Reference an unknown action.** Unknown actions raise; they never resolve.
- **Create a permission cycle.** `depends_on` must form a DAG.

## Why the registry and the policy engine are separate

This separation is the core of the design and is easy to get wrong.

```text
Registry says:  "message.send requires message.send, and is MEDIUM risk"
Policy decides: "this actor, with this consent, may or may not do that, now"
```

If the registry made authorization decisions it would be coupled to user
identity and consent state — and therefore to storage, to sessions, and to
time. It would also become impossible to answer "why was this allowed?" without
re-running policy evaluation.

Keeping them apart means the registry stays a pure description, the policy
engine stays a pure function, and an audit record can name both.

## Determinism

Two graphs with the same logical content must produce the same behaviour, or
idempotent replay and audit comparison become impossible. This is enforced:

- `GraphBuilder._auto` counts ids **per node-type prefix**, not by total node
  count, so generated ids do not depend on insertion order.
- `topological_layers` breaks ties with `sorted()`.
- `graph_to_dict` emits nodes sorted by `node_id`.
- `select_rule` breaks specificity ties toward the most restrictive decision.

## The graph is a DAG, not a control-flow graph

`depends_on` must be acyclic. Unrestricted loops are impossible by
construction. Bounded repetition is expressed with an explicit `RETRY` node
carrying a bounded `max_attempts`, which is inspectable and auditable — a
back-edge in a dependency graph is neither.

## What is deliberately absent today

- No live vendor integrations. All 16 seeded actions use simulated providers.
- **The shipped planner is a deterministic rule-based reference planner, not a
  language model.** No vendor LLM adapter is wired up. The planner is injectable,
  so adding one changes nothing downstream — and its output is untrusted either
  way, which is the point.
- No production identity provider, and no deployment-level rate limiting, backup
  or secret-management policy. The dashboard, CLI, and SQLite-backed
  consent/idempotency/audit stores are implemented; the default app wiring
  remains in memory.
- Version control history begins **at** the reconstruction. The original `.git/`
  was lost with the tree and cannot be recovered, so there is no history before
  the first commit.

Reporting this honestly matters more than shipping a dashboard that implies
otherwise.
