# Project Overview

> One intent. Every action.

ActionMesh is a **permission-aware, verifiable, cross-application execution
layer**. VoiceOS is a **voice interaction layer built on top of it** — a
client of the same API, planner, consent, and approval machinery, with no
execution engine of its own.

Central principle (never reversed):

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

| Stage | What it does | Implementation |
|---|---|---|
| **AI proposes** | Turns intent into an untrusted candidate plan of `{type, parameters}` only | `actionmesh/core/planner.py` (`planner` only proposes; it can never set risk, permissions, dependencies, or state), `actionmesh/voiceos/llm.py:177` `validate_proposal` |
| **Policy decides** | The only component that produces an authorization decision: `ALLOW` / `DENY` / `ASK` | `actionmesh/core/permissions/engine.py` `PolicyEngine`, `actionmesh/core/graph/authorization.py` pre-flight |
| **User controls** | Explicit approval for anything risky; consent records; revocation | `POST /approvals`, `POST|DELETE|GET /consents` in `actionmesh/api.py`; `core/permissions/store.py` `ConsentStore` |
| **Executor acts** | Runs the action graph asynchronously, handles cancellation, retries, idempotency | `actionmesh/core/executor/engine.py` `ExecutionEngine` (2,039 lines) |
| **Verifier confirms** | Re-reads the world independently; does not trust the executor | `actionmesh/core/verification/engine.py` `VerificationEngine`; provider `verify()` deliberately returns `verified=False` |
| **Audit remembers** | Hash-chained, externally anchored audit log | `actionmesh/core/audit/log.py` `AuditLog`, `actionmesh/core/audit/anchor.py` `AuditAnchor` |

## What ActionMesh is

A trusted execution layer between intent and the things that carry it out.
Every request walks: intent → untrusted plan → schema validation → registry
resolve → action graph (structure, cycles, ordering) → permission + policy
engine → approval when `ASK` → async executor → provider adapter → independent
verification → hash-chained audit.

What it ships as today: **control a laptop from a phone** — a bounded set of
named capabilities on a paired machine, each checked against a consent record
and a risk ceiling the *laptop* chose at pairing time, output streamed back
live, the whole event chain verifiable afterwards. (Source: `PROJECT.md`.)

## What VoiceOS is

A voice interaction layer **on** ActionMesh: a request becomes a durable
**task session** (`actionmesh/voiceos/models.py` `TaskSession`), each step an
ordinary ActionMesh execution under the caller's own credential — propose,
press run, approve if asked, verify, speak. VoiceOS "presses; it does not
decide" — it runs through the same `/plan` → `/execute` → approval → executor
path as the control room. See [VOICEOS.md](VOICEOS.md) and `docs/voiceos.md`.

## Why both exist

ActionMesh guarantees *authority and truth*; VoiceOS provides *a natural
interface to ask*. Separating them means the voice layer can never become a
second, weaker execution path — there is exactly one pipeline (enforced as a
non-negotiable rule in `docs/c42-execution-contract.md` §0).

## Target users and use cases

- A person controlling their own laptop from their phone (documented
  product).
- A developer/script driving named capabilities through the SDK
  (`actionmesh/sdk.py`).
- An operator reviewing executions, approvals, and audit in the dashboard /
  desktop app.

## Main product capabilities (implemented, source-grounded)

| Capability | Where | Status |
|---|---|---|
| 16 mock domain actions (`message.send`, `calendar.*`, `file.*`, `payment.*`, `search.web`, `system.launch_app`, …) | `actionmesh/core/registry/seeds.py` | Registered; `provider="mock"`, results carry `simulated: true` |
| Laptop device actions (files, terminal, processes, servers, projects, IDE, apps, screen, GUI) | `actionmesh/agent/definitions.py` `device_definitions()` (819 lines) | Registered + implemented in `actionmesh/agent/provider.py` `LaptopProvider` |
| Browser (open/navigate/search/inspect/snapshot/click/type/select/scroll/back/forward/refresh/wait/stop) | `actionmesh/agent/browser.py` `BrowserEngine` | Implemented; driven through Electron (`desktop/app/browser_window.js`) |
| Files (list/search/read/open/info/create/rename/move/copy/edit/trash) | `actionmesh/agent/files.py` `FilesEngine` | Implemented, workspace-confined |
| Media (status/play/pause/next/previous/volume/open_file/open_url) | `actionmesh/agent/media.py` `MediaEngine` (MPRIS over D-Bus) | Implemented; real MPRIS backend **not live-verified against a real player** (source comment, `media.py:326-328`) |
| Voice tasks, workflows, automations, website workflows | `actionmesh/voiceos/tasks.py`, `workflows.py`, `website_workflows.py` | Implemented |
| AI providers (chat/reasoning/analysis) | `actionmesh/voiceos/llm.py`: `AnthropicProvider`, `AnthropicAnalyst`, `DesktopProvider`, `DesktopAnalyst`; `UnwiredProvider` for configured-but-unwired | Anthropic + desktop bridge real; OpenAI/Gemini **not** in this build (`AVAILABLE_PROVIDERS = {"anthropic", "desktop"}`) |
| Booking/order confirmation gates | `booking.submit`, `order.place` (CRITICAL, `payment.execute`) in `definitions.py` | Definitions exist; payment execution is mock |

## Intended real-world commands vs. current reality

| Intended command | Current state |
|---|---|
| Open and inspect a folder | `device.files.list` / `device.files.info` — implemented |
| Find and read files | `device.files.search` / `device.files.read` — implemented |
| Navigate websites | `browser.open` / `browser.navigate` / `browser.snapshot` — implemented |
| Search for restaurants / trains | `browser.search` + website workflows (`actionmesh/voiceos/website_workflows.py`); fixtures `tests/fixtures/pizza.html`, `train.html` — fixture-tested, **not live-verified** |
| Control media | `media.*` over MPRIS — implemented, **not live-verified** against a real player |
| Create and execute workflows | `/automations/*` routes in `actionmesh/api.py`; `voiceos/workflows.py`, `automation_scheduler.py` — implemented |
| Request approvals for sensitive operations | `PolicyEngine` `ASK` → `POST /approvals` — implemented |

## Design principles

1. **LLM output is untrusted data.** It cannot grant itself a permission,
   lower its own risk, or set lifecycle state (`tests/security/`).
2. **Risk can only be raised, never lowered.** `max(action, definition)`.
3. **Fail closed everywhere.** Unset workspace root → no file capability;
   unset shell flag → no terminal; truncated key refused, not regenerated.
4. **The executor does not certify its own work.**
5. **Content never crosses the result boundary** — `safe_result_view`
   (allowlist) in `actionmesh/core/executor/serialization.py`.
6. **Real OS access lives in exactly one place:** `actionmesh/agent/`
   (AST-guarded by `tests/security/test_dangerous_primitives.py`).
7. **The package contains no outbound network client** except through the
   approved provider transports (test-enforced).
8. **One execution engine.** A capability that skips the registry skips
   policy — that is the failure the architecture exists to prevent.

## What makes it different from a normal AI assistant

A normal assistant calls tools directly and reports what the tool said.
ActionMesh inserts an authority boundary (policy), a human boundary
(approval/consent), and a truth boundary (independent verification + audit)
between the model's proposal and any effect — and each boundary is the only
component allowed to do its job.

## Current application experience

- **Electron desktop app** (`desktop/`): light theme, sidebar shell, Home /
  Voice / Tasks / Automations / History / Devices / Settings views, provider
  configuration (chat/reasoning/image/audio lanes), browser window, live event
  stream.
- **Web dashboard** (`GET /dashboard`, `actionmesh/dashboard.py`) and voice
  page (`GET /voice`, `actionmesh/voice.py`) — also served as a PWA.
- **Android APK** (`android/`, `scripts/build_apk.py`): WebView shell around
  the same dashboard.

## Discrepancies noted

- `README.md`'s status table and test counts (425 tests, unittest) are
  **stale** — they predate the pytest suite, the desktop app, and C42 work.
  `PROJECT.md` (1,168 tests, 2026-10-19) is newer. The C42 historical report
  (Python 1,758 passed / 3 skipped / 888 subtests; Desktop 106 passed) is
  recorded in [TESTING_AND_ACCEPTANCE.md](TESTING_AND_ACCEPTANCE.md) as
  **historical**, not current.
- `README.md` says "all 16 seeded actions use `provider="mock"`" and "no real
  vendor integration" — accurate for the *seed* registry; the laptop agent,
  browser, files, media, and Anthropic/desktop LLM paths are real
  implementations layered on top.
