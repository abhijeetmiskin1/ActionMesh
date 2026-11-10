# AI Handoff

*The entry point for every future AI coding agent. Read this first.*

The 2026-11-08 run closed P0 browser and MPRIS acceptance. See
[Testing and Acceptance](TESTING_AND_ACCEPTANCE.md) for the scope and
commands. The 2026-11-04 checkpoint below records the earlier state.

## Project identity

**ActionMesh** — a permission-aware, verifiable, cross-application
execution layer (Python/FastAPI backend + Electron desktop app). **VoiceOS**
— a voice interaction layer built *on* ActionMesh, with no execution
engine of its own. Central principle:

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

The repository is a **reconstruction** (original tree + `.git` lost);
git history is authoritative only from the first reconstruction commit.
Treat the code as a fresh implementation and re-verify before relying
on it.

## Read first (in order)

1. This file (`docs/project/AI_HANDOFF.md`)
2. [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — what the product is
3. [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) — components, diagrams
4. [SECURITY_MODEL.md](SECURITY_MODEL.md) — invariants (non-negotiable)
5. [ACTIONMESH.md](ACTIONMESH.md) — registry, policy, executor, verification
6. [VOICEOS.md](VOICEOS.md) — the voice layer
7. [CURRENT_STATUS.md](CURRENT_STATUS.md) — honest inventory
8. [DEVELOPMENT_GUIDE.md](DEVELOPMENT_GUIDE.md) — how to add things
9. `docs/c42-execution-contract.md` — the binding capability contract
10. `AGENTS.md` (repo root) — permanent rules

## Architecture understanding

One pipeline only: intent → untrusted plan → validation → registry →
graph → `PolicyEngine` (ALLOW/DENY/ASK) → approval → `ExecutionEngine` →
`LaptopProvider` (browser/files/media/device) → independent verification
→ hash-chained audit → SSE. VoiceOS runs each step as an ordinary graph
through this same path. Real OS access exists **only** in
`actionmesh/agent/`. See [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md)
for the Mermaid diagrams.

## Current repository state (verified 2026-11-04, Phase 4 P0-4)

- Branch: `main`. HEAD: `5df2996` ("C35-C41: the desktop shell, the real
  AI brain, and honest fallbacks").
- Working tree: **dirty** — C42 work in progress (browser/files/media/
  provider agents, `desktop/app/browser_*`, `desktop/test/`, e2e +
  security tests, C42 docs). **Preserve all modifications and untracked
  files. Do not commit unless explicitly requested.**
- **Verified numbers (2026-11-04):** 60 implemented ops
  (`IMPLEMENTED`), `SUPPORTED == IMPLEMENTED`; 49 HTTP routes in
  `actionmesh/api.py` (excl. middleware); CSP present at
  `desktop/ui/index.html:5`; Python providers 2 (`anthropic`, `desktop`,
  `llm.py:115`), desktop JS 5 listed / 4 `implemented: true`
  (`providers.js:49-91`); `.deb` 150 MB present
  (`desktop/release/ActionMesh-0.1.0-amd64.deb`); `booking.submit` /
  `order.place` fail closed (`PERMANENT`); MPRIS **not live-verified**
  (`media.py:326-328`); live acceptance opt-in
  (`ACTIONMESH_LIVE_ACCEPTANCE=1`); focused test `test_voiceos.py` 40
  passed. P0-3 (CSP) and P0-4 (docs refresh) are **done**.

## Completed work (verified in source)

- Core engine: registry (16 mock seeds), permission catalog + policy,
  graph validation, async executor with cancellation/durable resume,
  verification + compensation, hash-chained audit + anchor, SQLite store.
- HTTP API (49 routes), SSE with actor-scoped tickets, device
  identity/pairing/revocation.
- Laptop agent: **60 ops** — files, terminal, processes, servers, projects, IDE,
  apps, screen/GUI — all registered + test-covered.
- Browser agent (Electron-driven), media (MPRIS), VoiceOS sessions +
  orchestration + diagnosis, automations, website workflows.
- AI: Anthropic + desktop bridge providers, deterministic fallback.
- Electron app (light theme, 42 IPC channels), PWA, Android APK shell.
- Renderer CSP verified in source (`desktop/ui/index.html:5`).

## Incomplete work / BLOCKED

- **No live verification** in this pass: MPRIS (explicitly noted in
  source, `media.py:326-328`), real browser sites, real booking/payment,
  real vendor integrations (C13), live device pairing, fresh
  AppImage/NSIS/dmg/APK installs.
- OpenAI/Gemini **Python** providers: not implemented (`AVAILABLE_PROVIDERS =
  {"anthropic", "desktop"}`); desktop JS transports are JS-only.
- Historical C42 counts (1,758 passed / 3 skipped / 888 subtests;
  desktop 106) are **historical**, not current. The full suite was not
  rerun for the docs pass.

## Known issues / limitations

- `actionmesh/agent/media.py:326-328` — `MprisMediaDriver` not exercised
  against a live player.
- `README.md`'s old "425 tests" claim is **removed** (2026-11-04 rewrite);
  see [CURRENT_STATUS.md](CURRENT_STATUS.md).
- Labels are strict: LIVE ≠ INTEGRATED ≠ BLOCKED ≠ NOT IMPLEMENTED.

## External blockers

- Real MPRIS acceptance: Linux desktop + a real player + session bus.
- Real vendor integration: credentials/accounts (intentionally external).
- Live browser acceptance: network access + real bridge (opt-in env).
- Packaging: electron-builder toolchains; APK: Android SDK + device.

## Coding rules

- Inspect before editing. The code wins over any document.
- Preserve existing architecture; one pipeline, one engine, one policy
  engine, one approval system.
- Never discard uncommitted changes. No destructive git
  (`reset --hard`, history rewrite, forced push).
- Do not weaken security — the invariants in
  [SECURITY_MODEL.md](SECURITY_MODEL.md) are defects if violated.
- Do not create duplicate systems or bypass the ActionMesh pipeline.
- Do not fabricate results: unverified ≠ verified; `REQUIRES_USER` is
  never success; never override `verify()` to return `True`.
- Do not change the UI theme without an explicit requirement.
- Do not commit unless explicitly requested.
- Do not rerun full test suites unnecessarily; add focused tests for new
  behavior and verify actual functionality, not just class existence.
- No unrestricted model-generated code execution (`browser.eval`,
  `run_python`, freeform shell) — typed, fixed schemas only.

## How to continue (exact order)

1. `git status` — see the dirty C42 tree; do not disturb it.
2. Read [CURRENT_STATUS.md](CURRENT_STATUS.md) + `docs/c42-execution-contract.md`.
3. Pick the next open task from [ROADMAP.md](ROADMAP.md).
4. Add a focused test, implement, run only the affected test file(s).
5. Update this file's "Completed work" and the session handoff template.

## Session handoff template

```markdown
## Session handoff — <date>

- **Work completed:** …
- **Files changed:** …
- **Tests run:** … (exact commands + results)
- **Live acceptance:** … (device/site/player used, or "none")
- **Remaining tasks:** … (from ROADMAP.md)
- **External blockers:** …
- **Repository state:** branch, HEAD, `git status --short` summary
- **Next action:** the single next task + its first command
```
