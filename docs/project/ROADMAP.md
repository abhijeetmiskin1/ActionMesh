# Roadmap

*Derived from the existing project record (`TASK_BOARD.md`,
`docs/c42-*`) and current source. Numbers re-verified 2026-11-04
(Phase 4 P0-4): 60 implemented ops, 49 routes, CSP present at
`desktop/ui/index.html:5`, 150 MB `.deb` present, submit fail-closed,
MPRIS not live-verified. Completed work is not listed. Missing live
credentials ≠ missing source.*

## Latest release scope — 2026-11-06

Booking, ordering and payment execution are excluded by the operator's explicit
choice: discovery/review only. The real VLC live acceptance and fresh Linux package
are recorded in CURRENT_STATUS.md; earlier rows below are historical checkpoints.
Backend duplicate-task protection, simulation provenance and per-lane provider
probe caching now have focused regression coverage. External-site browser and
phone/APK acceptance still need their own evidence.

## P0 — Security, execution correctness, essential integrations

| # | Objective | Current state | Required files | Dependencies | Acceptance criteria | Verification | External prerequisites |
|---|---|---|---|---|---|---|---|
| P0-1 | Live-verify MPRIS media against a real player | `MprisMediaDriver` implemented, explicitly not exercised (`media.py:326-328`) — TESTED WITH FIXTURES | `actionmesh/agent/media.py`, `tests/unit/test_media_agent.py` | Linux desktop with a real MPRIS player | `media.status/play/volume` verified against a live player; result recorded | New live test + manual run; honest `unavailable` if no player | Linux desktop + player (e.g. mpv/VLC) |
| P0-2 | Live browser acceptance (real sites) | Fixture-tested only (`tests/fixtures/`) — TESTED WITH FIXTURES | `actionmesh/agent/browser.py`, `desktop/app/browser_window.js` | Electron app + backend | Real navigation/search/click on a live site; gates (`REQUIRES_USER`) behave | `tests/e2e/` live slice (opt-in `ACTIONMESH_LIVE_ACCEPTANCE=1`) + manual run; BLOCKED otherwise | Network access; no credentials |
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
