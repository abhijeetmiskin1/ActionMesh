# ActionMesh

> **One intent. Every action.**
> The execution layer for AI.

ActionMesh is a **permission-aware, verifiable, cross-application execution
platform**. It turns natural-language intent into a dependency-aware action
graph, authorizes every node through a central policy engine, requires explicit
user approval for anything risky, executes through provider adapters, verifies
the result independently, and records an audit trail.

It is **not** a chatbot with a tool-calling demo attached.

```text
USER INTENT
    ↓  intent / planner  (C18 — LLM output is UNTRUSTED, proposes only)
STRUCTURED PLAN
    ↓  schema validation
ACTION REGISTRY
    ↓  resolve + describe (C2)
ACTION GRAPH
    ↓  structure, cycles, ordering (C4)
PERMISSION + POLICY ENGINE
    ↓  ALLOW / DENY / ASK (C3)
APPROVAL  (when the decision is ASK)
    ↓
EXECUTOR            (C5 — implemented)
    ↓
APPLICATION PROVIDER (C7 — simulated domain providers)
    ↓
VERIFICATION         (C6 — implemented)
    ↓
AUDIT / MEMORY       (C9/C10/C15 foundations)
```

Core principle, and it is never reversed:

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

---

## Status

| Phase | Scope | State |
|-------|-------|-------|
| C0 | Repository foundation | Implemented (reconstructed) |
| C1 | AMP/1.0 protocol | Implemented (reconstructed) |
| C2 | Action Registry | **Tested — 37 tests** |
| C3 | Permission + Policy Engine | **Tested — 61 tests** |
| C4 | Action Graph + security invariants | **Tested — 50 tests** |
| C5 | Execution Engine | **Tested — 40 tests** |
| C6 | Verification + Recovery | **Tested — 24 tests** |
| C7 | Mock Providers | Implemented |
| C8 | REST API | Initial slice implemented |
| C9 | Realtime events | Initial slice implemented |
| C10 | Memory + entity resolution | Initial slice implemented |
| C11 | Device mesh | Initial slice implemented |
| C12 | Developer SDK | Implemented |
| C13 | Real integrations | Deferred |
| C14 | Security hardening | Threat-model pass complete; API-key boundary and AST primitive guard implemented |
| C15 | Release foundations | Dashboard, CLI, SQLite, observability, packaging, PWA, and signed Android APK implemented |
| C17 | Android APK | **Built — signed (v2+v3) — 16 tests** |
| C18 | Intent → plan (planner) | **Implemented — proposes only — 32 tests** |

**Current test status:** `python -m unittest discover -s tests` → **417 tests,
OK** (0 failures, 0 errors).

### Run it

```powershell
.\.venv\Scripts\python.exe -m actionmesh.cli serve    # http://127.0.0.1:8000
```

### On a phone

Two ways to run the control room as an app:

1. **PWA — no toolchain.** Open `/dashboard` in Chrome and choose **Add to home
   screen**. The control room is a PWA — `manifest.webmanifest`, a service
   worker, and a maskable SVG icon — so it installs to the home screen and
   launches full-screen with no browser chrome. No APK, no app store, no
   signing key.
2. **Android APK — sideload.** `python scripts/build_apk.py` builds and signs
   `dist/actionmesh-0.1.0.apk`, a dependency-free native shell (no AndroidX,
   no Gradle) around the same `/dashboard`. Install with
   `adb install -r dist/actionmesh-0.1.0.apk`; see
   [`android/README.md`](android/README.md).

The service worker caches **only the app shell**. It deliberately never caches
`/audit`, `/executions`, or `/capabilities`: a stale execution record or audit
chain would be a lie in a system built to report what actually happened.


> ### ⚠️ This repository is a reconstruction
>
> The original working tree was deleted during development. `.git/` was lost
> with it. Everything here was **rewritten from the architecture and design**,
> then verified by executing it. It is a reconstruction, not a restoration:
> treat the code as a fresh implementation and re-verify before relying on it.
>
> **The first commit was made after the reconstruction, not during it.** This
> history therefore starts from a verified state — real diffs, real rollback and
> real blame from that commit onward, and no history before it.
> `scripts/snapshot.py` is kept as an independent zip backup; it is belt and
> braces, not the primary safety net.

### What is real vs. simulated

The registry, policy engine, graph layer and **execution engine** are real
logic. The *providers* are not: all 16 seeded actions use `provider="mock"`,
and `MockProvider` keeps state in memory. Every execution result therefore
carries `simulated: true`, and no real message, file or payment has ever been
sent.

The API, realtime transport, dashboard, and SQLite execution, consent, and
idempotency stores exist. The default application wiring remains in memory, and
there is no real vendor integration.


---

## Requirements

```text
Python 3.11+   (verified on 3.12.10)
Runtime: FastAPI; SQLite uses the Python standard library.
```


Explore the platform interactively:

```python
from actionmesh.core.graph import GraphBuilder, execution_order
from actionmesh.core.permissions import (
    PolicyEngine, PermissionGrant, build_context, default_policy,
)
from actionmesh.core.registry import ActionRegistry
from actionmesh.protocol.models import Identity, RiskLevel

registry = ActionRegistry.with_seeds()          # 16 MOCK capabilities
engine = PolicyEngine()

builder = GraphBuilder("tell mom I'll be late")
builder.action({"type": "message.send", "parameters": {"message": "I'll be late"}}, node_id="a1")
builder.action({"type": "reminder.create", "parameters": {"text": "call mom", "time": "8pm"}},
               node_id="a2", depends_on=["a1"])
builder.end(depends_on=["a2"])
graph = builder.build()

print(execution_order(graph))                    # ['a1', 'a2', 'end']

resolved = registry.resolve(graph.nodes[0].action)
outcome = engine.evaluate(build_context(
    resolved.action, resolved.definition, Identity(id="user_1"),
    policy=default_policy(),
    grants=[PermissionGrant(scope="message.send", max_risk=RiskLevel.HIGH)],
))
print(outcome.decision, outcome.requires_approval)   # PolicyDecision.ASK True
```

The release CLI is available through the project entry point:

```powershell
actionmesh capabilities --provider mock
actionmesh dashboard
actionmesh serve  # requires: pip install "actionmesh[server]"
```

The last line is the whole point: **the AI cannot send that message on its own.**

---

## Architecture

Strictly layered, dependencies pointing one way only:

```text
intent → graph → registry → permissions → executor → providers
```

| Package | Phase | Responsibility |
|---------|-------|----------------|
| `actionmesh/protocol/` | C1 | `Action`, `GraphNode`, `ActionGraph`, `Permission`, `Policy`, `ApprovalRequest`, `VerificationResult`, enums, fail-closed validation, version negotiation |
| `actionmesh/core/registry/` | C2 | Register / discover / resolve / version. Describes actions; never executes them, never authorizes. |
| `actionmesh/core/permissions/` | C3 | Closed permission catalog, exact grant matching, risk model, `PolicyEngine`, `ConsentStore`. The trusted authorization boundary. |
| `actionmesh/core/graph/` | C4 | Build, validate, cycle-detect, order deterministically, serialize, and pre-flight authorize. |
| `actionmesh/providers/` | C7 | Simulated domain providers and the real-provider safety boundary. |
| `actionmesh/core/planner.py` | C18 | Intent → an untrusted candidate plan. Emits `{type, parameters}` only — it can never set risk, permissions, dependencies or state, and it never authorizes or executes. |

### Layer rules (enforced by tests)

- The **registry** never executes, never calls a provider, never decides policy.
- The **policy engine** is the only thing that produces an authorization decision.
- The **graph** layer composes the registry and the engine; it re-implements neither.
- The **planner** only proposes. Its output is untrusted until `plan_to_graph`
  re-validates it, and anything it could not map is recorded rather than dropped.
- **LLM output is untrusted data.** It cannot grant itself a permission, lower
  its own risk, or set a lifecycle state — see `tests/security/`.

## Documentation

- [`docs/architecture.md`](docs/architecture.md) — layering and trust boundaries
- [`docs/permissions.md`](docs/permissions.md) — permission & policy model, precedence
- [`docs/registry.md`](docs/registry.md) — action definition contract
- [`docs/action-graph.md`](docs/action-graph.md) — node types, cycles, ordering
- [`docs/providers.md`](docs/providers.md) — simulated providers and boundaries
- [`docs/provider-contract.md`](docs/provider-contract.md) — provider-neutral adapter contract
- [`docs/provider-requirements.md`](docs/provider-requirements.md) — selection requirements
- [`docs/provider-evidence.md`](docs/provider-evidence.md) — official documentation evidence
- [`docs/provider-decision-readiness.md`](docs/provider-decision-readiness.md) — post-selection gate
- [`docs/security.md`](docs/security.md) — threat model and invariants
- [`TASK_BOARD.md`](TASK_BOARD.md) — phase-by-phase status
- [`HANDOFF.md`](HANDOFF.md) — continuation notes and known risks
- [`android/README.md`](android/README.md) — the Android APK shell and how to build it

## License

MIT — see [LICENSE](LICENSE).


Optional dev tools: `pip install -e ".[dev]"` for `pytest`, `ruff`, `mypy`.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1

python -m unittest discover -s tests -v     # 144 tests
```
