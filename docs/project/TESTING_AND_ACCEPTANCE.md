# Testing and Acceptance

## Suite map

| Suite | Location | What it verifies |
|---|---|---|
| **Unit** | `tests/unit/` (~73 files) | Registry, permissions/policy, graph, executor, verification, audit, storage, events, devices, pairing, providers (mock + real boundary), API, CLI, SDK, browser/files/media/provider agents, planners, LLM generation, model bridge, VoiceOS, automations, credentials, secret, ratelimit, snapshot, durable execution, execution identity, compensation, command guide/policy/runner, observability, output, memory, PWA, deployment, release, Android APK, docs freshness |
| **Security** | `tests/security/` (9 files) | `test_dangerous_primitives.py` (AST guard: no OS access outside `actionmesh/agent/`, no outbound network client), `test_model_authority.py` (LLM cannot grant/elevate), `test_submit_bypass.py`, `test_browser_security.py`, `test_device_end_to_end.py`, `test_event_stream.py`, `test_graph_invariants.py`, `test_pairing_adversarial.py`, `test_record_scoping.py` |
| **Integration** | `tests/integration/test_ai_voiceos_pipeline.py` | The full AI→VoiceOS→ActionMesh pipeline end to end |
| **E2E (C42)** | `tests/e2e/test_c42_vertical_slices.py` | Vertical slices against fixtures (`pizza.html`, `train.html`, `checkout.html`, `evil.html`) |
| **Desktop** | `desktop/test/` + `desktop/app/*.test.js` | Pipeline, backend claim, purpose IPC (current + legacy), UI render, providers, provider bridge — with stubs (`electron_stub.js`, `dom_harness.js`, `main_harness.js`, `backend_stub.js`, `browser_bridge_stub.js`, `provider_bridge_stub.js`) |

## Fakes, fixtures, mock boundaries

- `FakeDriver` (browser), `FakeMediaDriver` (media) — in-source fakes.
- Mock providers (`providers/mock_base.py::MockHarnessMixin`) implement
  `observe()` (re-reads harness state) and `verify()` → `verified=False`
  (never self-certifies).
- HTML fixtures in `tests/fixtures/` for realistic (including hostile —
  `evil.html`) page content.
- Desktop tests run against stubs, not a live Electron.

## Classification

| Category | Coverage |
|---|---|
| Unit-tested | Core engine, policy, registry, executor, verification, audit, agents, VoiceOS, API |
| Integration-tested | AI→VoiceOS→execute pipeline (`tests/integration/`) |
| Fixture-tested | C42 vertical slices (browser/files/media) |
| Live-tested | **None claimed in this pass.** MPRIS explicitly not exercised against a real player (`agent/media.py:326-328`). No live provider, live browser, or live device acceptance is recorded in source. |

## Historical results (labeled historical — do not treat as current)

- **PROJECT.md / HANDOFF.md (2026-10-19):** 1,168 tests OK (unittest
  discover), ruff clean, mypy clean (89 modules), warning-clean.
- **C42 historical report** (from the project brief): Python **1,758
  passed, 3 skipped, 888 subtests**; Desktop **106 passed**.
- `pytest-run.txt` in the repo root: 40 passed — a single-file fragment,
  not a suite result.
- `README.md`'s "425 tests" is **stale** (predates pytest migration).

## Commands

```bash
python -m pytest tests -q                          # full Python suite
python -m pytest tests/security -q                 # security invariants
python -m pytest tests/unit/test_<name>.py -q      # one file
ruff check actionmesh tests scripts
mypy actionmesh
cd desktop && npm test                             # node --test
```

Do not rerun the full suites just to write documentation. New behavior
needs a focused test; run the affected file(s).
