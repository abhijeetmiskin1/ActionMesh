# ActionMesh — Project Index

> **One intent. Every action.** The trusted execution layer for AI.

ActionMesh is a permission-aware, verifiable, cross-application execution
platform. VoiceOS is a voice interaction layer built on it. Principle:
*AI proposes. Policy decides. User controls. Executor acts. Verifier
confirms. Audit remembers.*

**New AI agent? Start at [docs/project/AI_HANDOFF.md](docs/project/AI_HANDOFF.md).**
Permanent rules: [AGENTS.md](AGENTS.md). Quick handoff: [HANDOFF.md](HANDOFF.md).

## Verified facts (2026-11-04, Phase 4 P0-4)

| Fact | Value | Source |
|---|---|---|
| Implemented laptop ops | 60 | `actionmesh/agent/definitions.py` (`IMPLEMENTED`); `SUPPORTED == IMPLEMENTED` |
| HTTP routes | 49 | `@app.get/post/put/delete/websocket` in `actionmesh/api.py` (excl. middleware) |
| Renderer CSP | Present | `desktop/ui/index.html:5` — `default-src 'none'; script-src 'self'; … connect-src 'none'` |
| Python AI providers | 2 (`anthropic`, `desktop`) | `actionmesh/voiceos/llm.py:115` |
| Desktop JS providers | 5 listed, 4 `implemented: true` | `desktop/app/providers.js:49-91` (`ollama` is `implemented: false`) |
| Linux package | 150 MB `.deb` | `desktop/release/ActionMesh-0.1.0-amd64.deb` |
| `booking.submit` / `order.place` | Fail-closed `PERMANENT` refusal | `actionmesh/agent/provider.py:_submit` (~1941) — no payment backend |
| MPRIS | **NOT live-verified** | `actionmesh/agent/media.py:326-328` |
| Live acceptance | Opt-in `ACTIONMESH_LIVE_ACCEPTANCE=1` | `tests/e2e/test_phase2_live_acceptance.py:4,32` |
| Focused test | `test_voiceos.py` 40 passed | `.venv/bin/python -m pytest tests/unit/test_voiceos.py -q` |

Full evidence table: [README.md](README.md). Historical C42 counts
(1,758 / 3 / 888; desktop 106) are labeled historical in
[TESTING_AND_ACCEPTANCE.md](docs/project/TESTING_AND_ACCEPTANCE.md) — not
current claims.

## What is NOT done / BLOCKED

- **MPRIS live verification** (P0-1): driver tested only with `FakeMediaDriver`; needs a real player + session bus.
- **Live browser acceptance** (P0-2): fixture-tested only; e2e live slice is opt-in and BLOCKED without a bridge.
- **Real booking / ordering**: definitions + gates exist; the handler refuses — no payment backend, nothing submitted. Real vendor integrations (C13) are **NOT IMPLEMENTED** (boundary only).
- **Second Python AI provider** (P1-3): OpenAI/Gemini transports exist in desktop JS but are not exposed as Python providers; only `anthropic` + `desktop` bridge.
- **Live device pairing / fresh multi-format packaging / live provider calls**: not recorded in this pass.
- Labels: **LIVE** ≠ **INTEGRATED** (fakes/fixtures) ≠ **BLOCKED** (needs external prereq) ≠ **NOT IMPLEMENTED**. See [CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md).

## Documentation package (`docs/project/`)

| Document | Contents |
|---|---|
| [PROJECT_OVERVIEW.md](docs/project/PROJECT_OVERVIEW.md) | What ActionMesh and VoiceOS are; capabilities vs. planned |
| [SYSTEM_ARCHITECTURE.md](docs/project/SYSTEM_ARCHITECTURE.md) | Components, process boundaries, Mermaid diagrams, lifecycles |
| [REPOSITORY_MAP.md](docs/project/REPOSITORY_MAP.md) | Every important file, symbol, and dependency |
| [ACTIONMESH.md](docs/project/ACTIONMESH.md) | Registry, policy, executor, verification, persistence, API |
| [VOICEOS.md](docs/project/VOICEOS.md) | Voice input → executed operation; references; what it cannot do |
| [SECURITY_MODEL.md](docs/project/SECURITY_MODEL.md) | Controls table with enforcement points; invariants |
| [INTEGRATIONS.md](docs/project/INTEGRATIONS.md) | Browser, files, media, AI providers, external services |
| [DESKTOP_APPLICATION.md](docs/project/DESKTOP_APPLICATION.md) | Electron architecture, IPC channels, theme tokens |
| [DEVELOPMENT_GUIDE.md](docs/project/DEVELOPMENT_GUIDE.md) | Setup, adding capabilities, debugging, packaging |
| [API_REFERENCE.md](docs/project/API_REFERENCE.md) | All 49 routes + SSE formats |
| [DATA_AND_EVENTS.md](docs/project/DATA_AND_EVENTS.md) | SQLite schema, events, tickets, audit chain |
| [TESTING_AND_ACCEPTANCE.md](docs/project/TESTING_AND_ACCEPTANCE.md) | Suite map; historical counts (labeled) |
| [CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md) | Evidence-based implementation inventory |
| [ROADMAP.md](docs/project/ROADMAP.md) | P0–P3 work with acceptance criteria |
| [AI_HANDOFF.md](docs/project/AI_HANDOFF.md) | **The AI entry point** |

## Existing documents (kept, not duplicated)

- [HANDOFF.md](HANDOFF.md) — continuation notes (quick handoff).
- [TASK_BOARD.md](TASK_BOARD.md) — phase-by-phase status record.
- [docs/architecture.md](docs/architecture.md), [docs/security.md](docs/security.md),
  [docs/permissions.md](docs/permissions.md), [docs/registry.md](docs/registry.md),
  [docs/action-graph.md](docs/action-graph.md), [docs/providers.md](docs/providers.md),
  [docs/provider-contract.md](docs/provider-contract.md),
  [docs/voiceos.md](docs/voiceos.md), [docs/audit.md](docs/audit.md),
  [docs/rest-api.md](docs/rest-api.md), [docs/deployment.md](docs/deployment.md),
  [docs/c42-execution-contract.md](docs/c42-execution-contract.md),
  [docs/c42-per-purpose-providers.md](docs/c42-per-purpose-providers.md),
  [docs/c41-model-bridge-contract.md](docs/c41-model-bridge-contract.md).

## Quick facts

- Python ≥ 3.11, FastAPI only at runtime; SQLite (stdlib); Electron 45.
- Commands: `.venv/bin/python -m actionmesh.cli serve`, `cd desktop && npm start`,
  `.venv/bin/python -m pytest tests -q`, `cd desktop && npm test`,
  `cd desktop && npm run dist:linux` (electron-builder `--linux deb --x64`).
- Live acceptance is opt-in: `ACTIONMESH_LIVE_ACCEPTANCE=1` for the e2e live slice; otherwise BLOCKED/skipped.
- This tree is a reconstruction — see [CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md).
