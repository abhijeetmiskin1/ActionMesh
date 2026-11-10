# Action Graph (C4)

A plan is a **directed acyclic graph** of typed nodes. The graph layer builds it,
proves it is sound, orders it deterministically, serializes it, and pre-flight
authorizes it. It never executes anything.

## Node types

| Type | Carries | Meaning |
|------|---------|---------|
| `ACTION` | `action` | one side-effecting action |
| `SEQUENCE` | `action` | an action run as a step of an ordered chain |
| `PARALLEL` | `action` | an action that may run concurrently with siblings |
| `CONDITION` | `condition` | branch on an expression |
| `WAIT` | `wait_s` | pause for a bounded duration |
| `APPROVAL` | *(guards via `depends_on`)* | human gate |
| `RETRY` | `max_attempts` | bounded re-run of its target |
| `COMPENSATION` | `compensation` + `compensates` | undo of another node |
| `END` | — | terminal; required, and must not carry an action |

`COMPENSATION` is the only type that does **not** require `action`: its
undo-action lives in `compensation`, and it runs only during rollback, never
during forward execution.

## Per-node rules

```text
node_id            non-empty, unique across the graph
ACTION/SEQ/PAR     must carry an action
END                must NOT carry an action; graph must have one
CONDITION          condition must be non-blank
WAIT               wait_s > 0 and ≤ 86400
RETRY              1 ≤ max_attempts ≤ 10, and must depend on its target
COMPENSATION       must name `compensates` and carry `compensation`
APPROVAL           must guard at least one node
depends_on         no self-reference; every id must exist
```

Bounds on `wait_s` and `max_attempts` are resource guards: an untrusted graph
cannot ask the executor to wait a week or retry forever.

## Why a DAG and not a control-flow graph

Unrestricted loops are impossible by construction. `depends_on` must be acyclic
and `find_cycle` rejects it.

Bounded repetition uses an explicit `RETRY` node carrying `max_attempts`. That
is strictly better than a back-edge: the bound is inspectable, auditable, and
enforced by validation rather than by convention. A cycle in a dependency graph
is neither, and would let a malformed plan trap the executor.

`find_cycle` uses an **iterative** DFS with a colour map, so a very deep graph
cannot exhaust the Python recursion limit. A 2000-node chain is covered by a
test.

## Deterministic ordering

`topological_layers` returns concurrency groups: every node in layer *i*
depends only on nodes in layers `< i`, so a whole layer can run concurrently.

Ties are broken with `sorted()`, so two logically identical graphs always
produce byte-identical order and byte-identical serialization. That is what
makes graph caching and idempotent replay safe.

```python
b.action(msg(), node_id="a1")
b.action(msg("b"), node_id="a2")
b.end(depends_on=["a1", "a2"])
topological_layers(b.build())
# [['a1', 'a2'], ['end']]      ← a1 and a2 may run concurrently
```

`execution_order` flattens the layers; `dependents` gives the reverse map
(what becomes runnable when a node succeeds); `root_nodes` gives the entry
points; `compensation_map` gives rollback wiring.

## Serialization

`graph_to_dict` emits a canonical form with nodes sorted by `node_id`.
`graph_from_dict` / `graph_from_json` accept **untrusted** input and reject:

- non-object payloads, non-list `nodes`
- unknown `node_type` values
- non-string `depends_on` entries, non-object `metadata`
- unparseable numeric fields

```python
restored = graph_from_json(graph_to_json(graph))
execution_order(restored) == execution_order(graph)   # True
```

Identity fields (`graph_id`, `action_id`, `idempotency_key`) are preserved
verbatim — they are identity, not semantics. Structural validation can be
skipped with `validate=False`, but only explicitly; the default is to validate.

## Pre-flight authorization

`authorize_graph(graph, engine, registry, context)` composes C2 and C3:

```text
for each node with a side effect:
    registry.resolve(action)        → unknown action/invalid params ⇒ block
    PolicyEngine.evaluate(...)      → DENY ⇒ block
                                 → ASK  ⇒ flag as needing approval
report
if any node blocked: raise GraphAuthorizationError   (all-or-nothing)
```

Two things this deliberately gets right:

- **`ASK` does not block.** It is a pause at the human gate, not a refusal. The
  node is reported in `GraphAuthorization.needs_approval` and C5 must not run
  it until a real approval exists.
- **Authorization is all-or-nothing.** One denied node rejects the whole graph.
  A partially-authorized plan has no coherent meaning.

Control nodes (`END`, `WAIT`, `CONDITION`, `APPROVAL`) carry no side effect and
are authorized trivially.

## Visualization

`visualization(graph)` returns a JSON-safe, deterministic view for the
dashboard: layers, execution order, roots, compensation wiring and per-node
summaries.

## Example

```python
b = GraphBuilder("share report and remind me")
b.action({"type": "message.send", "parameters": {"message": "report sent"}}, node_id="m1")
b.action({"type": "reminder.create", "parameters": {"text": "follow up", "time": "tomorrow 10"}},
         node_id="r1", depends_on=["m1"])
b.end(depends_on=["r1"])
graph = b.build()          # raises if anything is structurally unsound
```
