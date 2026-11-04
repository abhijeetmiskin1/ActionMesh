# ActionMesh + VoiceOS

> **One intent. Every action.**
> The trusted execution layer for AI.

ActionMesh is a **permission-aware, verifiable, cross-application execution
platform**. VoiceOS is a **voice interaction layer on top of it** — a client,
not a second engine. Nothing the AI says authorizes anything; the pipeline
decides.

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

One execution pipeline, always:

```text
intent → plan (untrusted) → validate → registry → graph →
PolicyEngine (ALLOW/DENY/ASK) → approval → ExecutionEngine →
LaptopProvider → verification → audit
```

Real OS access exists only in `actionmesh/agent/`. The policy engine is the
only producer of authorization decisions. No second engine, second browser,
or second approval system is permitted.

---

## Verified state (2026-11-04, Phase 4 P0-4 documentation refresh)

Every number below was checked against source on this date. "Verified"
means read from the file / runtime, not copied from an older doc.

| Claim | Verified value | How checked |
|---|---|---|
| Implemented laptop ops | **60** (`IMPLEMENTED` in `actionmesh/agent/definitions.py`) | `.venv/bin/python -c "from actionmesh.agent.definitions import IMPLEMENTED; print(len(IMPLEMENTED))"` → `60` |
| `SUPPORTED == IMPLEMENTED` | **True** (invariant, test-asserted) | `.venv/bin/python` importing `LaptopProvider.SUPPORTED` vs `IMPLEMENTED` |
| HTTP routes | **49** `@app.get/post/put/delete/websocket` in `actionmesh/api.py` (excl. `@app.middleware`) | `grep -c` + manual count of the 49 decorator lines |
| Renderer CSP | **Present** — `desktop/ui/index.html:5` | `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'` |
| Python AI providers | **2**: `anthropic`, `desktop` | `actionmesh/voiceos/llm.py:115` — `AVAILABLE_PROVIDERS = frozenset({"anthropic", "desktop"})` |
| Desktop JS providers | **5 listed, 4 `implemented: true`** (`anthropic`, `openai`, `gemini`, `openai_compatible`; `ollama` is `implemented: false`) | `desktop/app/providers.js:49-91`. Mismatch is intentional: JS implements 4 transports, Python only exposes `anthropic` + the `desktop` bridge back to JS |
| Linux package | **150 MB** `desktop/release/ActionMesh-0.1.0-amd64.deb` (built 2026-11-04) | `ls -lh desktop/release/` |
| `booking.submit` / `order.place` | **Fail-closed** — raise `PERMANENT` "this build has no payment backend…" (`provider.py:_submit`, ~1941) | Read handler; no fabricated reference, no retry (`max_attempts=1`) |
| MPRIS live verification | **NOT live-verified** | `actionmesh/agent/media.py:326-328` — "NOT exercised against a live player" |
| Live acceptance gate | **Opt-in only** — `ACTIONMESH_LIVE_ACCEPTANCE=1` | `tests/e2e/test_phase2_live_acceptance.py:4,32`; `test_phase3_live_acceptance.py:42`. Without it, tests skip as BLOCKED |
| Focused test check | `tests/unit/test_voiceos.py` — **40 passed** (1.41 s) | `.venv/bin/python -m pytest tests/unit/test_voiceos.py -q` |

Historical counts (C42: 1,758 passed / 3 skipped / 888 subtests; desktop
106) are **historical**, recorded in
[`docs/project/TESTING_AND_ACCEPTANCE.md`](docs/project/TESTING_AND_ACCEPTANCE.md).
They are not a claim about the current tree. The full suite was not rerun
for this documentation pass.

---

## What is NOT done / BLOCKED

Do not mistake the table above for live verification.

- **MPRIS media is not live-verified.** `MprisMediaDriver` (`media.py:326`)
  was written against the MPRIS 2.0 spec and tested only with
  `FakeMediaDriver`. Driving it against a real player (VLC/Elisa/mpv) on a
  Linux desktop with a session bus is P0-1.
- **Browser is fixture-tested only.** Real-site navigation/search/click
  acceptance is P0-2; the e2e live slice is opt-in
  (`ACTIONMESH_LIVE_ACCEPTANCE=1`) and reports BLOCKED without a bridge.
- **Real booking / ordering does not exist.** `booking.submit` and
  `order.place` are CRITICAL-gated definitions whose handler deliberately
  refuses: no payment backend, no fabricated confirmation. A real vendor
  integration (C13) is intentionally external — boundary only.
- **OpenAI / Gemini / generic Python providers are NOT IMPLEMENTED.**
  Python exposes only `anthropic` and `desktop`. The desktop JS layer
  implements `openai`/`gemini`/`openai_compatible` transports, but they are
  reachable from Python only via the desktop bridge, and no second real
  provider is selectable end-to-end (P1-3).
- **No live device pairing, live provider calls, or fresh multi-format
  packaging run** (AppImage/NSIS/dmg/APK install) is recorded in this
  pass. The 150 MB `.deb` above is the one artifact verified present.
- **Dark theme** is explicitly out of scope (light theme only).

Labels used across these docs: **LIVE** (exercised against a real
device/player/site/provider in a recorded run), **INTEGRATED**
(wired end-to-end, tested with fakes/fixtures), **BLOCKED**
(requires an external prerequisite — credentials, player, network, device),
**NOT IMPLEMENTED** (no source). Live-verified, fixture-tested, integrated,
and not-implemented are never merged.

---

## How to run

```bash
# Python suite (full — don't rerun casually)
.venv/bin/python -m pytest tests -q

# Security invariants only
.venv/bin/python -m pytest tests/security -q

# One file
.venv/bin/python -m pytest tests/unit/test_voiceos.py -q

# Lint / types
.venv/bin/python -m ruff check actionmesh tests scripts
.venv/bin/python -m mypy actionmesh

# Desktop
cd desktop && npm test          # node --test
cd desktop && npm run dist:linux   # electron-builder --linux deb --x64
```

Serve the control room:

```bash
.venv/bin/python -m actionmesh.cli serve    # http://127.0.0.1:8000
```

Live acceptance (browser/e2e only; everything else skips honestly):

```bash
ACTIONMESH_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/e2e/test_phase2_live_acceptance.py tests/e2e/test_phase3_live_acceptance.py -q
```

Without that env var these tests report BLOCKED (skipped with a reason),
which is the honest result when no real bridge/player/network is present.
Do not claim live verification from a skipped run.

---

## Architecture in one screen

| Layer | Location | Job |
|---|---|---|
| Protocol | `actionmesh/protocol/` | `Action`, `GraphNode`, envelopes, risk enums |
| Registry | `actionmesh/core/registry/` | Describes capabilities; never executes, never authorizes |
| Permissions | `actionmesh/core/permissions/` | Closed catalog; `PolicyEngine` alone decides ALLOW/DENY/ASK |
| Graph | `actionmesh/core/graph/` | Compose, cycle-detect, order, pre-flight authorize |
| Planner | `actionmesh/core/planner.py` | Proposes `{type, parameters}` only; output is untrusted |
| Executor | `actionmesh/core/executor/engine.py` | Runs approved graphs; does not certify its own work |
| Verification | `actionmesh/core/verification/` | Independent re-read of the world; `verify()` stays `verified=False` until observed |
| Audit | `actionmesh/core/audit/` | Hash-chained, tamper-evident record |
| Laptop agent | `actionmesh/agent/` | **Only** place with real OS access (60 ops) |
| VoiceOS | `actionmesh/voiceos/` | Voice client through `/plan` → `/execute` → approval; cannot reach the consent surface |
| Desktop | `desktop/` | Electron shell; single `contextBridge` in `preload.js`; `sandbox`/`contextIsolation` on, `nodeIntegration` off; renderer CSP above |

Security invariants (never weakened): a client cannot define its own
authority (no `grants` in bodies); risk = `max(action, definition)`; CRITICAL
needs explicit per-`action_id` approval; unverified is never success; content
never crosses the result boundary; workspace paths are confined; fail closed;
no model-generated freeform code execution. Full list in
[`AGENTS.md`](AGENTS.md) and [`docs/project/SECURITY_MODEL.md`](docs/project/SECURITY_MODEL.md).

---

## Documentation

Start at [`docs/project/AI_HANDOFF.md`](docs/project/AI_HANDOFF.md) for
agents, or [`PROJECT.md`](PROJECT.md) for the whole picture.

- [`docs/project/CURRENT_STATUS.md`](docs/project/CURRENT_STATUS.md) — evidence-based inventory (LIVE / INTEGRATED / BLOCKED / NOT IMPLEMENTED)
- [`docs/project/ROADMAP.md`](docs/project/ROADMAP.md) — P0–P3 remaining work
- [`docs/project/TESTING_AND_ACCEPTANCE.md`](docs/project/TESTING_AND_ACCEPTANCE.md) — suite map; historical counts labeled as such
- [`docs/project/SYSTEM_ARCHITECTURE.md`](docs/project/SYSTEM_ARCHITECTURE.md) — components and diagrams
- [`docs/project/SECURITY_MODEL.md`](docs/project/SECURITY_MODEL.md) — controls and invariants
- [`docs/project/API_REFERENCE.md`](docs/project/API_REFERENCE.md) — the 49 routes
- [`docs/c42-execution-contract.md`](docs/c42-execution-contract.md) — binding capability contract (code wins on disagreement)
- [`docs/architecture.md`](docs/architecture.md), [`docs/security.md`](docs/security.md), [`docs/permissions.md`](docs/permissions.md), [`docs/registry.md`](docs/registry.md), [`docs/action-graph.md`](docs/action-graph.md), [`docs/providers.md`](docs/providers.md), [`docs/provider-contract.md`](docs/provider-contract.md), [`docs/voiceos.md`](docs/voiceos.md), [`docs/audit.md`](docs/audit.md), [`docs/rest-api.md`](docs/rest-api.md), [`docs/deployment.md`](docs/deployment.md)
- [`HANDOFF.md`](HANDOFF.md) — short continuation state
- [`android/README.md`](android/README.md) — Android APK shell

> ### ⚠️ This repository is a reconstruction
>
> The original working tree was deleted during development. `.git/` was lost
> with it. Everything here was **rewritten from the architecture and design**,
> then verified by executing it. It is a reconstruction, not a restoration:
> treat the code as a fresh implementation and re-verify before relying on it.
>
> **The first commit was made after the reconstruction, not during it.** This
> history therefore starts from a verified state — real diffs, real rollback
> and real blame from that commit onward, and no history before it.

## License

MIT — see [LICENSE](LICENSE).
