# Handoff — current continuation state

> Read [AGENTS.md](AGENTS.md) first. Comprehensive version:
> [docs/project/AI_HANDOFF.md](docs/project/AI_HANDOFF.md).

## Where we are

- Milestone: **C42 in progress** — per-purpose providers + browser/files/media
  agents + browser website workflows, on top of the completed C35–C41 build
  (desktop shell, real AI brain, honest fallbacks).
- Branch `main`, HEAD `5df2996` ("C35-C41: the desktop shell, the real AI
  brain, and honest fallbacks").
- Working tree is **dirty** (C42 work): many modified + untracked files
  including `actionmesh/agent/browser*.py`, `files.py`, `media.py`,
  `voiceos/website_workflows.py`, `docs/c42-*.md`, new tests under
  `tests/unit/`, `tests/security/`, `tests/e2e/`. See `git status`.
  **Preserve all of it. Do not commit.**
- **Phase 4 P0-4 (2026-11-04): documentation refresh done.** README, PROJECT,
  this handoff, `CURRENT_STATUS.md`, `ROADMAP.md`, `AI_HANDOFF.md`, and the
  C42 contract now state only source-verified numbers (60 ops, 49 routes,
  CSP present, 150 MB `.deb`, fail-closed submit, MPRIS not live-verified).
  Details in [README.md](README.md) and
  [docs/project/CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md).

## Completed (C35–C41, in HEAD + verified 2026-11-04)

- Electron shell with Voice/Tasks/Automations/History/Devices/Settings.
- Real AI brain: Anthropic or desktop-approved provider models propose,
  `validate_proposal` (`voiceos/llm.py:177`) and the ActionMesh pipeline
  still decide.
- Deterministic fallback, honest failure reporting, payment + booking gates
  (`actionmesh/agent/definitions.py`, C42 §4/§6).
- 42 IPC channels; single preload bridge; sandbox/contextIsolation enforced.
- **60 implemented laptop ops** (`IMPLEMENTED`), `SUPPORTED == IMPLEMENTED`.
- **49 HTTP routes** in `actionmesh/api.py` (excl. middleware).
- Renderer **CSP verified** at `desktop/ui/index.html:5`.
- Linux package present: `desktop/release/ActionMesh-0.1.0-amd64.deb` (150 MB).
- `booking.submit` / `order.place` fail closed (`PERMANENT`, no backend).
- Focused check: `tests/unit/test_voiceos.py` — 40 passed.

## Remaining (see [docs/project/ROADMAP.md](docs/project/ROADMAP.md))

| Task | Why |
|---|---|
| P0-1 Live MPRIS verification | `MprisMediaDriver` tested only with `FakeMediaDriver`; `media.py:326-328` says it is **not exercised against a live player** |
| P0-2 Live browser acceptance | Browser flows are fixture/fake-driver tested only; live slice is opt-in (`ACTIONMESH_LIVE_ACCEPTANCE=1`) |
| P1 Real vendor integrations | C13 real providers **not implemented** (mock/harness only) |
| P1 Real booking/ordering | `booking.submit`/`order.place` are gated definitions whose handler refuses — never live |
| P1 Second Python AI provider | Python exposes only `anthropic` + `desktop`; JS has 4 transports but no second Python provider is end-to-end (P1-3) |

P0-3 (verify CSP) and P0-4 (refresh stale README) are **done** in this pass.

## Known limitations (documented, not hidden)

- No live verification claimed for browser, files, media, or voice capture.
  MPRIS explicitly not live-verified (`media.py:326-328`).
- OpenAI/Gemini Python provider options are **not** in this build
  (`voiceos/llm.py`: `AVAILABLE_PROVIDERS = frozenset({"anthropic", "desktop"})`).
  Desktop JS lists 5 providers, 4 `implemented: true` — see
  [CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md) for the Python/JS split.
- Historical counts (C42: 1,758 passed / 3 skipped / 888 subtests; desktop
  106) are **historical**, not current. The full suite was not rerun for the
  docs pass; only `tests/unit/test_voiceos.py` (40 passed) was run.
- This repo is a reconstruction; git history is authoritative only from the
  first reconstruction commit. See README warning.

## What is NOT done / BLOCKED

- MPRIS live run (needs Linux desktop + real player + session bus).
- Live browser acceptance (needs network + real bridge; BLOCKED otherwise).
- Real booking/ordering and real vendor integrations (external credentials).
- Live device pairing and fresh multi-format packaging (AppImage/NSIS/dmg/APK install).
- Dark theme (explicitly out of scope).

## Exact next task

**P0-1: live MPRIS verification** — bring up a real MPRIS player
(e.g. VLC/Elisa/mpv), drive `media.status/play/pause/next/previous/volume`
through `MprisMediaDriver`, record results, and update
[docs/project/CURRENT_STATUS.md](docs/project/CURRENT_STATUS.md) and
[docs/project/INTEGRATIONS.md](docs/project/INTEGRATIONS.md). No source
rewrite needed unless it fails.

## Verification

```bash
git status --short
.venv/bin/python -m pytest tests/unit/test_voiceos.py -q   # 40 passed (2026-11-04)
.venv/bin/python -m pytest tests/unit/test_media_agent.py tests/security/test_browser_security.py -q
.venv/bin/python -m pytest tests -q          # full suite (historical: 1,758 passed, 3 skipped; do not treat as current)
cd desktop && npm test             # node --test
```

## Rules for whoever continues

Full rules in [AGENTS.md](AGENTS.md): inspect before editing, preserve
architecture, never discard uncommitted changes, never weaken security,
never fabricate results, never bypass the ActionMesh pipeline, do not commit
unless explicitly requested, add focused tests for new behavior.
