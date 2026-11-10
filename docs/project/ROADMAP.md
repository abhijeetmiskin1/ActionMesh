# Roadmap

*Derived from `TASK_BOARD.md`, `docs/c42-*`, and the source. The
2026-11-08 acceptance run is recorded in
[Testing and Acceptance](TESTING_AND_ACCEPTANCE.md).*

## P0 — Security, execution correctness, essential integrations

| # | Objective | Current state | Required files | Dependencies | Acceptance criteria | Verification | External prerequisites |
|---|---|---|---|---|---|---|---|
| P0-1 | Live-verify MPRIS media against a real player | **DONE (2026-11-08)** — mpv on an isolated Debian D-Bus session | `actionmesh/agent/media.py`, `tests/e2e/test_phase4_live_acceptance.py` | Linux with a dedicated MPRIS player | Status, play, pause and volume confirmed by player readback | Opt-in mpv test passed. A separate run also confirmed local file opening | Linux session bus and test player |
| P0-2 | Live browser acceptance | **DONE (2026-11-08)** — real Electron bridge on macOS | `desktop/app/browser_window.js`, `tests/e2e/` | Electron app + backend | Public-site navigation and search, plus click and sign-in handoff on a controlled page | Real readback from Example Domain, DuckDuckGo, IRCTC and Zomato. Loopback click reached the gate and a second click was refused | Electron bridge and network access for public sites |
| P0-3 | Verify CSP + header posture in the Electron shell | **DONE (2026-11-04)** — CSP verified at `desktop/ui/index.html:5`: `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'` | `desktop/ui/index.html` | — | CSP documented in [SECURITY_MODEL.md](SECURITY_MODEL.md) with a test | Source read, 2026-11-04 | — |
| P0-4 | Refresh stale root docs (`README.md` status table/counts) | **DONE (2026-11-04)** — README/PROJECT/HANDOFF/CURRENT_STATUS/ROADMAP/AI_HANDOFF rewritten with source-verified numbers (60 ops, 49 routes, CSP, 150 MB deb, fail-closed submit, MPRIS not live-verified); "What is NOT done / BLOCKED" added; historical counts labeled | `README.md`, `PROJECT.md`, `HANDOFF.md`, `docs/project/CURRENT_STATUS.md` | This documentation package | Counts match source; historical labeled | Grep cross-check of every number; `test_voiceos.py` 40 passed | — |

## P1 — Actual user-facing capabilities

| # | Objective | Current state | Required files | Acceptance criteria | Verification |
|---|---|---|---|---|---|
| P1-1 | Real vendor integrations (C13) | Intentionally deferred; `real_base.py` boundary only — NOT IMPLEMENTED | `actionmesh/providers/` | At least one real provider behind `RealProviderBase` with credentials + failure behavior | Unit + security-boundary tests; live run with a real account |
| P1-2 | Real booking/ordering | Definitions + confirmation gates exist; `booking.submit`/`order.place` fail closed (`PERMANENT`, no payment backend) — EXTERNALLY BLOCKED | `agent/definitions.py`, `providers/payments.py` | One real booking flow end-to-end (or explicit decision to keep mock-only) | Fixture e2e + live acceptance |
| P1-3 | Additional AI providers (OpenAI/Gemini/compatible) | Python `AVAILABLE_PROVIDERS = {"anthropic", "desktop"}` (`llm.py:115`); `UnwiredProvider` names the gap. Desktop JS has 4 `implemented: true` transports (`providers.js`) but they are JS-only / bridge-routed — NOT IMPLEMENTED as Python providers | `voiceos/llm.py`, `desktop/app/providers.js` | A second real provider selectable in the UI end-to-end | `test_llm_generation.py`, `test_model_bridge.py` extensions |

## P2 — Product completeness and usability

| # | Objective | Current state | Acceptance criteria |
|---|---|---|---|
| P2-1 | Fresh packaging run (deb/AppImage/NSIS/dmg/APK) and artifact check | `.deb` present (150 MB, `desktop/release/ActionMesh-0.1.0-amd64.deb`, 2026-11-04); AppImage/NSIS/dmg/APK install not freshly verified in this pass | `npm run dist:*` succeeds on a clean checkout; APK installs on a device |
| P2-2 | Live device/agent pairing acceptance | Pairing implemented + adversarial tests; no live device run recorded — BLOCKED in this pass | End-to-end phone→laptop pairing + one real execution, recorded |
| P2-3 | Accessibility/contrast pass on the light theme | Tokens exist with contrast comments (`--ink-soft`, `--warn-ink`) | Documented contrast ratios; no visual regression |

## P3 — Optional expansion

| # | Objective | Current state | Notes |
|---|---|---|---|
| P3-1 | Dark theme | Light only (`styles.css`) | Only on explicit requirement |
| P3-2 | Additional website workflow templates | `website_workflows.py` has restaurant/train | Follow the C42 pattern; gate on real demand |
| P3-3 | Multi-device mesh beyond laptop agent | Devices + identity exist; agent is laptop-scoped | Architectural; do not fork the pipeline |

## Explicitly out of scope

- A second execution engine, second browser, or second approval system
  (forbidden by `docs/c42-execution-contract.md` §0).
- Any change that weakens the security invariants in
  [SECURITY_MODEL.md](SECURITY_MODEL.md).
- Claiming live verification from fixture tests; `REQUIRES_USER` or
  unverified is never success.

## Remaining Jarvis work plan

| Order | Deliverable | Completion check | Prerequisite |
|---|---|---|---|
| 1 | Make task progression single-owner and preserve outcome across retries, interruption and approval. | Two simultaneous requests advance a session once. Reading a running task cannot overwrite its checkpoint. A stopped task never dispatches its next step. | Existing executor and session store. The desktop backend uses a single server process. |
| 2 | Make voice sessions reliable on macOS, including follow-up commands, interruption and microphone errors. | Record a live two-turn conversation on the target Mac, verify a task result, then confirm the mic closes on stop. | Microphone permission and working transcription for the active audio model. |
| 3 | Add GitHub and Google Workspace read capabilities. Start with repository and issue lookup, Drive search and Calendar lookup. | Each action identifies its source, respects account scope, handles denied or expired credentials, and has fixture plus live acceptance. | Authorized test accounts and connector credentials. |
| 4 | Add bounded, evidence-driven planning. Feed permitted observations into the next planning decision and pause for missing information. | A multi-step task can change its next action after a real observation, with a step budget and safe stop on denial, ambiguity or unverifiable results. | Typed, independently verifiable tool capabilities from earlier phases. |
| 5 | Build product comparison and order review. | Compare matching products using retailer, delivered price, availability and timestamp. Present the exact cart and total before approval. Confirm completion from a retailer receipt. | A retailer or comparison data source and a test checkout account. |
| 6 | Complete media and release acceptance. | Image output works through a real provider. Live voice, connectors, browser, recovery and permission checks pass on the packaged Mac build. | Image provider endpoint and test credentials. |

The same ActionMesh policy and executor apply at every stage. External reads and
writes use separately registered capabilities, and the planner cannot grant itself
access or approve a purchase.
