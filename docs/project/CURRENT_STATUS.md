# Current Status

*Evidence-based inventory. Status labels are kept separate — never
combined. Numbers re-verified against source 2026-11-04 (Phase 4 P0-4).*

| Feature | Source exists | Registered | Connected | UI usable | Automated tests | Live verified | Status |
|---|---|---|---|---|---|---|---|
| Core ActionMesh (registry/policy/graph/executor/verification/audit) | ✓ | ✓ | ✓ | ✓ (dashboard/desktop) | ✓ | — | **COMPLETE** (fixture/integration level) |
| Permission engine | ✓ | ✓ | ✓ | ✓ (consents UI) | ✓ | — | **COMPLETE** |
| Execution + cancellation + durable resume | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Verification + compensation | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Event streaming (SSE, tickets) | ✓ | ✓ | ✓ | ✓ (stream pill) | ✓ | — | **COMPLETE** |
| Persistence (SQLite) | ✓ | ✓ | ✓ | — | ✓ | — | **COMPLETE** |
| HTTP API (49 routes) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Device agent (laptop provider, 60 ops) | ✓ | ✓ | ✓ | ✓ (devices view) | ✓ | — | **INTEGRATED** (not live-verified in this pass) |
| Electron app | ✓ | ✓ | ✓ | ✓ | ✓ (desktop tests) | — | **INTEGRATED** |
| VoiceOS (sessions, orchestration, diagnosis) | ✓ | ✓ | ✓ | ✓ (voice view) | ✓ | — | **INTEGRATED** |
| AI providers: Anthropic (Python) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** (needs API key) |
| AI providers: desktop bridge (cwd `desktop`) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| AI providers: OpenAI / Gemini / generic (Python) | ✗ | ✗ | ✗ | ✗ | ✗ | ✗ | **NOT IMPLEMENTED** in Python (`llm.py:115`); JS transports exist (`providers.js`) but are not Python providers |
| Browser agent | ✓ | ✓ | ✓ (via Electron IPC) | ✓ | ✓ (fixtures) | ✗ | **TESTED WITH FIXTURES** |
| Files agent | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ | **TESTED WITH FIXTURES** |
| Media (MPRIS) | ✓ | ✓ | ✓ (D-Bus code) | ✓ | ✓ (fake driver) | ✗ | **TESTED WITH FIXTURES** — real player explicitly not exercised (`media.py:326-328`) |
| Workflows / automations | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| Website discovery (restaurant/train flows) | ✓ | ✓ | ✓ | ✓ | ✓ (fixtures) | ✗ | **TESTED WITH FIXTURES** |
| Booking / ordering (real) | Partial (definitions + gates) | ✓ | Refuses (no backend) | — | ✓ (refusal tested) | ✗ | **PARTIAL / EXTERNALLY BLOCKED** — `booking.submit`/`order.place` fail closed (`provider.py:_submit`, `PERMANENT`); no real vendor |
| Approvals | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| History | ✓ (`/audit`, `/executions`) | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Packaging (deb) | ✓ (electron-builder config) | — | — | — | ✓ (`test_release.py`, `test_android_apk.py`) | deb present (150 MB, 2026-11-04) | **INTEGRATED** — `desktop/release/ActionMesh-0.1.0-amd64.deb`; AppImage/NSIS/dmg/APK install not freshly verified in this pass |
| Renderer CSP | ✓ | — | — | ✓ | — | — | **VERIFIED in source** — `desktop/ui/index.html:5`: `default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'none'; form-action 'none'; base-uri 'none'` |
| Onboarding (PWA/APK) | ✓ | ✓ | ✓ | ✓ | ✓ | — | **INTEGRATED** |
| Credential lifecycle | ✓ | ✓ | ✓ | ✓ | ✓ | — | **COMPLETE** |
| Real vendor integrations (C13) | ✗ (boundary only) | ✗ | ✗ | ✗ | ✗ | ✗ | **NOT IMPLEMENTED** (intentionally external) |

## Verified numbers (2026-11-04)

| Number | Value | Command / source |
|---|---|---|
| Implemented ops | 60 | `.venv/bin/python -c "from actionmesh.agent.definitions import IMPLEMENTED; print(len(IMPLEMENTED))"` → `60` |
| `SUPPORTED == IMPLEMENTED` | True | `.venv/bin/python` — `LaptopProvider.SUPPORTED == IMPLEMENTED` |
| HTTP routes | 49 | `grep "@app\.\(get\|post\|put\|delete\|websocket\)" actionmesh/api.py \| wc -l` → `49` (middleware excluded) |
| CSP | Present | `desktop/ui/index.html:5` |
| Python providers | 2 (`anthropic`, `desktop`) | `actionmesh/voiceos/llm.py:115` |
| Desktop JS providers | 5 listed, 4 `implemented: true`, 1 `false` (`ollama`) | `desktop/app/providers.js:49-91` |
| `.deb` size | 150 MB | `ls -lh desktop/release/ActionMesh-0.1.0-amd64.deb` |
| Focused test | 40 passed | `.venv/bin/python -m pytest tests/unit/test_voiceos.py -q` |

Python/JS provider split: JS implements 4 transports (`anthropic`,
`openai`, `gemini`, `openai_compatible`); Python's `AVAILABLE_PROVIDERS`
exposes only `anthropic` directly plus `desktop`, which routes back into JS
via the C41 bridge. The extra JS transports are not Python providers.

## What is NOT done / BLOCKED

- **MPRIS live verification (P0-1)** — needs Linux desktop + real player + session bus. Label: BLOCKED in this pass, TESTED WITH FIXTURES in source.
- **Live browser acceptance (P0-2)** — fixture/fake-driver only; e2e live slice is opt-in (`ACTIONMESH_LIVE_ACCEPTANCE=1`, `tests/e2e/test_phase2_live_acceptance.py:4,32`) and BLOCKED without a real bridge.
- **Real booking / ordering (P1-2)** — handler refuses (`PERMANENT`, no payment backend). Needs a real vendor + credentials.
- **Real vendor integrations (P1-1, C13)** — NOT IMPLEMENTED by design; `RealProviderBase` boundary only.
- **Second Python AI provider (P1-3)** — OpenAI/Gemini Python transports NOT IMPLEMENTED; JS transports exist but are only reachable via the desktop bridge.
- **Live device pairing, live provider calls, fresh AppImage/NSIS/dmg/APK installs** — not recorded in this pass.
- **Dark theme** — explicitly out of scope (light only).

## Known discrepancies (doc vs. source) — resolved this pass

1. ~~`README.md` test counts (425, unittest) and phase table are stale~~ —
   README rewritten 2026-11-04 with source-verified numbers; historical
   counts are labeled historical in
   [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md).
2. ~~CSP was not verified in source during this documentation pass~~ —
   **verified**: `desktop/ui/index.html:5` (see table above).
3. ~~HTTP routes shown as 47~~ — actual **49** (`api.py`, excl. middleware).
4. The repository is a **reconstruction** (original tree + `.git` lost);
   git history is authoritative only from the first reconstruction commit.
5. The C42 execution contract (`docs/c42-execution-contract.md`) is
   explicitly "a map, not a second source of truth — if the code and
   this document disagree, the code wins."

## How to run

```bash
.venv/bin/python -m pytest tests -q                          # full suite — don't rerun casually
.venv/bin/python -m pytest tests/security -q
.venv/bin/python -m pytest tests/unit/test_voiceos.py -q     # 40 passed (2026-11-04)
cd desktop && npm test
cd desktop && npm run dist:linux   # electron-builder --linux deb --x64 → desktop/release/*.deb
ACTIONMESH_LIVE_ACCEPTANCE=1 .venv/bin/python -m pytest tests/e2e/test_phase2_live_acceptance.py -q  # opt-in live; BLOCKED otherwise
```
