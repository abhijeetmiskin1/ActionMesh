# AGENTS.md — Permanent Repository Rules

**Product:** ActionMesh (trusted execution layer) + VoiceOS (voice layer
on top). Principle: *AI proposes. Policy decides. User controls.
Executor acts. Verifier confirms. Audit remembers.*

## Architecture constraints

- **One execution pipeline.** Intent → plan → validate → registry →
  graph → `PolicyEngine` → approval → `ExecutionEngine` →
  `LaptopProvider` → verification → audit. Never add a second engine,
  second browser, or second approval system.
- **Real OS access only in `actionmesh/agent/`.** Enforced by
  `tests/security/test_dangerous_primitives.py` (AST guard).
- **The policy engine is the only authorization decision producer.**
  The registry describes; the graph composes; the planner only proposes.
- **No outbound network client** in the package except approved
  provider transports (test-enforced).
- **VoiceOS is a client, not an engine.** It runs through the same
  `/plan` → `/execute` → approval path; it cannot reach the consent
  surface.

## Security invariants (never weaken)

1. A client can never define its own authority (no `grants` in bodies).
2. Risk = `max(action, definition)` — it can only be raised.
3. The executor does not certify its own work; `verify()` stays
   `verified=False`; observation re-reads the world.
4. Content never crosses the result boundary (`safe_result_view`
   allowlist).
5. CRITICAL operations require explicit per-`action_id` approval; a
   grant/consent is not an approval.
6. Unverified/`REQUIRES_USER` is never reported as success.
7. Browser/page content is untrusted; sensitive transient content never
   reaches audit, logs, or the renderer.
8. Workspace confinement: model-generated paths cannot escape
   `Workspace`; fd-pinned opens.
9. Fail closed: unset root ⇒ no files; unset flag ⇒ no terminal;
   truncated key refused.
10. No unrestricted model-generated code (no `browser.eval`, no
    freeform shell) — typed, fixed schemas only.

## Important source locations

| Concern | Location |
|---|---|
| API routes | `actionmesh/api.py` (51 routes, excl. middleware; verified 2026-11-07) |
| Registry + seeds | `actionmesh/core/registry/` |
| Permission catalog + policy | `actionmesh/core/permissions/` |
| Graph | `actionmesh/core/graph/` |
| Executor | `actionmesh/core/executor/engine.py` |
| Verification | `actionmesh/core/verification/` |
| Audit | `actionmesh/core/audit/` |
| Storage | `actionmesh/core/storage.py` |
| Laptop agent (only OS access) | `actionmesh/agent/` |
| VoiceOS | `actionmesh/voiceos/` |
| AI providers | `actionmesh/voiceos/llm.py` (`anthropic`, `desktop` only) |
| Desktop main/preload/renderer | `desktop/app/main.js`, `preload.js`, `desktop/ui/` |
| Capability contract | `docs/c42-execution-contract.md` |
| Full docs | `docs/project/` (start at `AI_HANDOFF.md`) |

## Test commands

```bash
python -m pytest tests -q                          # full suite — don't rerun casually
python -m pytest tests/security -q
python -m pytest tests/unit/test_<name>.py -q      # one file
ruff check actionmesh tests scripts
mypy actionmesh
cd desktop && npm test
```

## Working-tree preservation

- The tree is a **reconstruction**; git history starts at the first
  reconstruction commit (HEAD `5df2996` at handoff time).
- **Never discard uncommitted changes.** No `git reset --hard`, no
  history rewrite, no forced push.
- **Do not commit unless explicitly requested.**

## No fake acceptance

- Never claim live verification from fixture tests. MPRIS is
  explicitly not live-verified (`agent/media.py:326-328`; formerly cited
  as `:300`).
- Historical counts (C42: 1,758 passed / 3 skipped / 888 subtests;
  desktop 106) are historical, not current.
- Live-verified, fixture-tested, integrated, and not-implemented are
  distinct states — never merge them.

## Coding conventions (from the repo)

- Python ≥ 3.11, `ruff` line-length 100, target py311; `mypy`
  `disallow_untyped_defs = true` for `actionmesh/`.
- Capability pattern: one `ActionDefinition` (`agent/definitions.py`)
  + one `LaptopProvider` handler (`agent/provider.py`); `SUPPORTED ==
  IMPLEMENTED` is test-asserted; `max_attempts=1` for side effects.
- Desktop: single `contextBridge` in `preload.js`; `sandbox: true`,
  `contextIsolation: true`, `nodeIntegration: false` everywhere.
- Light theme only (`desktop/ui/styles.css` tokens) — do not change
  without an explicit requirement.
- Add a focused test for new behavior; verify actual functionality.
- Prefer clear standard-library code, bounded work, and efficient data structures.
  Measure performance changes on the affected path and avoid duplicate I/O, repeated
  scans, and artificial delays.
