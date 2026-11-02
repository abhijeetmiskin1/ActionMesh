# ActionMesh — project state

**Version:** 0.1.0 · **Last updated:** 2026-10-19 · **Gates:** 1168 tests OK, ruff clean, mypy clean (89 modules), suite warning-clean

This document is the whole picture in one place: what the project is for, how it is
built, what is finished and verified, what is left, and where it is going. Per-checkpoint
detail lives in [`TASK_BOARD.md`](TASK_BOARD.md), the change history in
[`CHANGELOG.md`](CHANGELOG.md), and the operating constraints for a new contributor in
[`HANDOFF.md`](HANDOFF.md). This file is the summary; those are the record.

> **Two products, one repository.** ActionMesh is the trusted execution layer described in
> this file. **VoiceOS** is a voice interaction layer built *on* it — a client of the same
> API, planner, consent and approval machinery, with no engine of its own. The conversation
> is the unit: a request becomes a durable **task session**, each step of which is an
> ordinary ActionMesh execution under the caller's own credential — propose, press run,
> approve if asked, verify, speak. Its contract, its privacy evaluation and its unbuilt
> remainder are in [`docs/voiceos.md`](docs/voiceos.md). ActionMesh's unfinished work
> keeps priority over VoiceOS's.

> **A note on provenance.** The repository was rewritten from its architecture after the
> original tree and `.git/` were deleted. Git history is only authoritative from the
> first reconstruction commit onward; `TASK_BOARD.md` is the status record, and it grades
> its own claims (`[x]` implemented **and** runtime-verified, `[~]` implemented but only
> statically reviewed, `[ ]` not started).

---

## 1. What it is

ActionMesh is a **permission-aware, verifiable, cross-application execution layer**. It
sits between an intent and the things that carry it out, and it refuses to let any of
them skip a step:

```text
AI proposes.  Policy decides.  User controls.
Executor acts.  Verifier confirms.  Audit remembers.
```

The pipeline every request walks:

```
intent → planner (untrusted, proposes only)
       → schema validation
       → registry resolve
       → action graph (structure, cycles, ordering)
       → permission + policy engine (ALLOW / DENY / ASK)
       → approval, when the decision is ASK
       → async executor → provider adapter
       → independent verification (re-reads the world, does not trust the executor)
       → hash-chained audit + external anchor
```

**The product it ships as today:** control a laptop from a phone. Not a remote shell and
not a remote desktop — a phone can ask for a bounded set of named capabilities on a
machine it has paired with, every one of them checked against a consent record and a
risk ceiling that the *laptop* chose at pairing time, with the output streamed back live
and the whole event chain verifiable afterwards.

### The invariants that are never reversed

These are the project. Any change that weakens one is a defect regardless of whether the
tests pass.

| # | Invariant | Where it is enforced |
|---|---|---|
| 1 | A client can never define its own authority. No `grants`, `permissions`, `role`, `authorized`, or risk decisions in a request body | `_context()` raises on `grants`; `test_dangerous_primitives.py`, `test_policy_engine.py` |
| 2 | Risk is `max(action, definition)` — trusted metadata can only **raise** it | `core/permissions/`, `test_registry.py` |
| 3 | The executor does not certify its own work. Verification re-reads the world independently | `core/verification/`, `agent/verification.py`; the provider's `verify()` deliberately returns `verified=False` |
| 4 | Content never crosses the result boundary. `stdout`/`stderr`/file bodies are not in events or result payloads | `core/executor/serialization.py::safe_result_view` (allowlist, not denylist) |
| 5 | Real OS access lives in exactly one place: `actionmesh/agent/`. One spawn function is exempted from the dangerous-primitive guard; there is no second policy engine | `tests/security/test_dangerous_primitives.py` (AST guard) |
| 6 | Loopback trust is enforced **server-side**, never in page JavaScript | `api.py::_stream_principal`, `_record_scope` |
| 7 | Detached processes are addressable only by an id this agent issued. No pid-based killing | `agent/processes.py::ProcessRegistry` |
| 8 | A pairing code is minted on the laptop only; the ceiling is fixed at issuance and cannot be redeemed upward | `api.py::pairing_code`, `core/pairing.py` |
| 9 | Live output is transient: never audited, never replayed, bounded, actor-scoped | `core/output.py`, `EventHub.publish(transient=True)` |
| 10 | The package contains **no outbound network client** (`urllib.request`, `socket`, `httpx` are forbidden by test) | `test_dangerous_primitives.py` |
| 11 | Fail closed everywhere: unset root means no file capability, unset shell flag means no terminal, a truncated key is refused rather than regenerated | `agent/config.py`, `core/secret.py` |

---

## 2. How it is built

* **Stdlib-only Python.** 81 modules, ~16.1k lines in `actionmesh/`. FastAPI appears at
  the HTTP boundary and nowhere else. Verified on Python 3.14; `pyproject` targets 3.11.
* **No test framework dependency.** `python -m unittest discover -s tests -t .` — 55 test
  files, 49 unit / 6 security, 1042 tests.
* **The interpreter that works is `.venv/bin/python`.** System `python3` has no
  fastapi/httpx/ruff/mypy.
* **Line endings are load-bearing.** `api.py`, `dashboard.py`, `cli.py`, `storage.py`,
  `.env.example` and the docs are CRLF; files added since C19 are LF. A whole-file
  rewrite that flips them produces an unreviewable diff.

```text
actionmesh/
├── protocol/     AMP/1.0 models, enums, fail-closed validation of untrusted payloads
├── core/
│   ├── registry/      capability definitions and discovery (16 seeded mock actions)
│   ├── permissions/   catalog, matching, risk, PolicyEngine, ConsentStore
│   ├── graph/         builder, validator, authorization, serialization
│   ├── executor/      async engine, idempotency, recovery, safe_result_view
│   ├── verification/  independent, evidence-weighted confirmation
│   ├── audit/         hash-chained log + external anchor
│   ├── events.py      EventHub: replay, cursor resume, transient publish
│   ├── output.py      bounded per-node OutputPublisher (contextvar seam)
│   ├── streaming.py   short-lived, actor-bound stream tickets
│   ├── identity/ credentials/ pairing/ devices/   the C19 device plane
│   ├── config.py      deployment modes with fail-closed startup validation
│   ├── ratelimit.py   bounded sliding-window limiter
│   └── storage.py     durable execution records, PRAGMA user_version migrations
├── agent/        THE ONLY place real OS access lives
│   ├── paths.py       confined filesystem (openat / O_NOFOLLOW, no escape)
│   ├── commands.py    allow-list command policy — argv shape, no shell, no inline code
│   ├── runner.py      the single exempted spawn path: constructed env, capped output,
│   │                  process-group kill, bounded concurrency
│   ├── processes.py   registry of children this agent started; stop-by-issued-id
│   ├── provider.py    20 device.* capabilities (19 at C19d, +logs in C29), each with
│   │                  an independent observe()
│   └── ides.py projects.py definitions.py
├── api.py        35 routes (22 GET, 12 POST, 1 WebSocket): REST + SSE + WS
├── dashboard.py  the installable mobile control plane (no framework, no innerHTML)
├── voice.py      VoiceOS: the conversation, task and live-execution surface. No audio
│                 path anywhere in it; every privileged step is a separate press
├── voiceos/      the reasoning layer: task sessions (SQLite), operator-authored task
│                 templates, a step-at-a-time orchestrator, and an LLM seam that
│                 refuses to pretend a provider is wired up
├── cli.py        network-free only: config validate, doctor
└── sdk.py        in-process client for the same engine
```

---

## 3. What is done

### Core (C0–C18) — implemented and runtime-verified

Protocol and fail-closed validation; registry; permission + policy engine; graph
builder with cycle/ordering checks; async executor; independent verification with
weighted evidence; recovery and compensation (executed, never upgraded to success);
hash-chained audit with an external anchor; mock providers; REST API; the dashboard;
the SDK; the planner (which proposes and authorizes nothing).

### The mobile control plane (C19a–e)

Persistent device identity, keyed-digest credentials (revoking a device kills its
credentials), loopback-minted 6-digit pairing codes with a sliding-window attempt budget
and a ceiling fixed at issuance, and the laptop agent: confined filesystem, allow-list
command policy, argv-only spawning with a constructed child environment, a process
registry that can only address what it launched, and editor/project discovery. 19
`device.*` capabilities, each verified against real files, processes, git and a loopback
listener.

### Platform (P11, P10)

Deployment modes (`development` / `lan` / `production`) that refuse to start misconfigured;
schema-versioned durable storage; `/ready` distinct from `/health`; server-enforced
loopback operator boundary; bounded rate limiting; an adversarial security sweep.

### The recent arc (C20 → C34)

| Checkpoint | What it added | Evidence |
|---|---|---|
| **C20** | Live incremental output: an `OutputPublisher` per node, streamed as transient `execution.output` events (4 KiB/chunk, 64 KiB/node, actor-scoped, never audited) + the mobile output panel | browser-verified; audit chain contained none of the output |
| **C21** | Enforced limits reported instead of implied (`timeout_requested_s` / `timeout_applied_s` / `timeout_clipped`, `output_captured`). **Also fixed a ship-blocking bug:** `load_key` read a binary key with `.strip()`, breaking ~1 install in 20 on every restart after the first | 512-case byte round-trip test |
| **C22** | The phone is *told*: the dashboard reads the authoritative record and prints which limit bound a run | browser-verified |
| **C23** | "Run a command on the laptop" panel — argv one-per-line, no authority fields | browser-verified through the controls |
| **C24** | Read surface scoped to the actor: `/executions`, `/approvals`, `/audit` were readable by any paired device — `/approvals` leaked other actors' action **parameters** | `test_record_scoping.py` |
| **C25** | Decision surface checked: **any paired device could approve another actor's CRITICAL action** and be handed their resumed record; any device could record consent for another actor or revoke another device | 7 tests, RED output captured first |
| **C26** | The dashboard actually works from a phone: every panel was sending a body `actor` that a credential makes invalid, so `/execute` 400'd and the scoped `/consents` broke page load | browser-verified with a stale actor box |
| **C27a** | The flagship flow is reachable: the pairing panel offered no `critical` ceiling, so a phone could never run a command at all | full phone loop verified in a browser |
| **C27b** | Inventory rows projected through the same allowlist (scalars only, ≤20 rows, never two levels deep), and a "What the laptop is running" panel that stops a process by its issued id. Driving it exposed three defects: an operator stream kept after pairing, a row that stayed `running` after an approved stop, and a status message that promised before the request returned | browser-verified as the phone; console clean |
| **C28** | A file body reaches the phone over the transient output channel (`stream: "content"`) instead of a privileged route that would skip the policy engine; `entries` became projected rows so a directory listing shows names; a "Files on the laptop" panel browses and opens | browser-verified as the phone: text body appeared, PNG added 0 chars, audit chain clean |
| **C29** | Launched servers keep a log — a `0600` file outside the workspace, never a pipe, because a pipe with no reader blocks the server — off unless `ACTIONMESH_AGENT_LOG_DIR` is set, trimmed at 512 KiB on every process-table pass, and read back by `device.process.logs` over the transient channel. Also fixed: the consent panel could not grant 6 of the 13 laptop scopes | browser-verified: startup + stderr + request lines returned to the phone; file `-rw-------` outside the root; audit chain clean |
| **C30** | A Cancel control on live executions, plus the parked-run bug it exposed: a run awaiting approval publishes `execution.completed(waiting_permission)`, which the page read as final — hiding Cancel from a streaming run and miscounting "N running". Measured semantics at the time: cancellation applied **between steps**, and the button said so | browser-verified (a cancelled 60s command kept printing 20→70→94 ticks); `TERMINAL_STATES` guard. Superseded by C31 |
| **C31** | Cancel stops the operation, not merely the wait: the executor holds the provider call it is awaiting and interrupts it, then awaits that call's own cleanup — the process-group kill — before reporting. A dispatched action is reported `cancelled` with `verified: null` and `compensation_required`, never as a success and never silently retried; a genuine outer `CancelledError` still propagates | 9 tests (one asserts a real command's parent **and grandchild** pids disappear); browser-verified: cancelled at tick 97 of a 60s stream, output stopped there, LIVE panel and REST agreed, audit chain still `ok` |
| **C32** | **VoiceOS slice 1** — `GET /voice`: push-to-talk, an editable transcript, a text fallback and a plan preview. Risk labels come from `/capabilities` rather than from the proposal, and no audio-capture primitive appears in the page | 31 tests; browser-verified with an injected recognizer stub (no mic hardware here): interim/final lifecycle, no restart after `onend`, hand-edited transcript preserved, missing-API fallback shown, console clean |
| **C34** | **VoiceOS became conversational.** `actionmesh/voiceos/` adds a reasoning layer, durable **task sessions**, an operator-authored task-template loader, a step-at-a-time orchestrator and an LLM provider seam that reports its own absence. `GET /voice` gained the conversation, the task plan and Status / Stop-it controls; six new `/voiceos/*` routes, all scoped by the one existing caller-resolution rule | 21 tests (1148 → 1168); browser-verified: a two-step conditional task parked at CRITICAL, was approved, folded its verified evidence from the record, matched `exit_code: 0` against real output and completed; "read the second one" resolved to `notes.txt` from the listing just shown; "stop it" froze a streaming command at tick 38 and the session reconciled to `cancelled` |
| **C33** | **VoiceOS V0.1 — the loop closed.** Transcript → plan → an explicit *Approve & run* → `/execute` → the existing approval gate → SSE timeline → verification → spoken result, with the laptop-agent phrases in the one planner (`what is my laptop running`, `read this file x`, `run command <argv>`; `run my tests` maps to nothing rather than to an invented command line). Driving it exposed three real defects: a parked run read as an outcome, the stream opened after the response so fast runs lost their output, and `GET /approvals` echoing the caller's default `medium` risk for a CRITICAL command | 33 tests; browser-verified on a real agent: a voice-run command wrote a file, a voice-run read brought its body back, cancellation stopped a stream at tick 29 with the process gone, audit chain `ok`, console empty |

**Current verified state of the flagship path**, from a phone: pair at `critical` →
consent `system.shell` → fill the command panel → run → approve → a real `python3`
process streams twelve live ticks to the handset → the laptop's own timeout cuts it →
`Succeeded · verified`, with the ceiling named in the panel. `/audit` over that
credential returns only that actor's records; the chain verifies; the output appears
nowhere in it. Console clean. Pressing **Cancel** on that same streaming run now stops the
command where it stands — tick 97 of a 60s stream in the C31 pass, parent and grandchild pids
gone — and the run reads `Cancelled · unverified` because an action that was dispatched cannot
claim to have left nothing behind (C31).

---

## 4. What remains

### Resolved in C27b: inventory rows reach the phone

`safe_result_view` deliberately never recursed: a list became `{key}_count` and a dict
under a safe key was dropped, because "a dict under a safe key is exactly where a payload
hides." That rule kept file contents and transcripts off the wire — and also made an
inventory panel useless, because the phone could see "3 processes" and nothing else.

The five inventory keys (`processes`, `servers`, `projects`, `editors`, `services`) are
now **re-projected** through the same allowlist instead of summarized: allowlisted keys
only, scalars only, strings still ≤120 characters, and a list or dict inside a row is
dropped rather than walked. One level deep, never two; at most 20 rows. Every other key
keeps the old behaviour. Four adversarial tests hold the line — including that content
hidden under a safe key *inside a row* still cannot get out. A "What the laptop is
running" panel uses it, and stops a process by its issued id.

### Resolved in C28: how a file body reaches a phone

File *content* is not a row field, so the projection could not carry it. The answer is
not a privileged `GET /file` route — that would be a second execution path skipping
registry, policy, consent, approval and audit, which is the one thing this architecture
forbids. `device.file.read` now emits the bytes it read onto the transient
`execution.output` channel as `stream: "content"`: the channel already built to carry
content to an authorized observer. Live, bounded, actor-scoped, never audited, never
replayed; a binary file emits nothing. Verified in a browser: the text file's three lines
appeared on the phone, the PNG added zero characters, and the 28-record audit chain
verified `ok` while containing neither the body nor a `stdout` key.

### What remains open

Nothing blocks the phone-to-laptop loop now: commands run, files browse and open, launched
servers log and can be read back. What is left is **hardening and reach**, listed below —
cross-process rate limiting and scoping (a proxy and one shared identity store), TLS and
key rotation (deployment), release-signing the APK (toolchain), the audit anchor's external
storage (a service), and C13 vendor adapters (a sandbox contract that does not exist yet).

**VoiceOS has started**, and started small: slice 1 is the microphone, the transcript and a
plan preview. Everything the charter asks it to grow — spoken feedback, interruptible
responses, conversation history, a device picker — is unbuilt, and the two-track plan in
section 5 keeps ActionMesh's unfinished work ahead of it.

### Known gaps, stated honestly

* **Log capture exists but is off by default, and a log is disk-resident program output.**
  A detached child's output goes to `/dev/null` unless the operator sets
  `ACTIONMESH_AGENT_LOG_DIR`; when it is set, each child gets a `0600` file outside the
  workspace, trimmed at 512 KiB (C29). The residual risks are the honest ones: whatever a
  server prints lands in that file, so a printed secret is a file-resident secret until
  the next trim; and trimming can lose a line that arrives between the read and the
  truncate. Both are stated in the code rather than papered over — and the reason it is a
  file at all is that a pipe with no live reader blocks the user's server.
* **Cancellation stops the call this process is awaiting — and says what it cannot undo.**
  Since C31 a provider call in flight is interrupted, and the executor awaits that call's own
  unwind (the runner's process-group kill) before it reports, so a streaming command's whole
  group is gone when the state reads `cancelled`. Three honest residuals: the action *was*
  dispatched, so the node reports `verified: null` with `compensation_required` set rather than
  claiming the world was restored; a child that left its process group (`setsid`, a double
  fork) is outside the kill, and a provider whose request had already reached a third-party
  service can have landed that write before the interrupt; and `_inflight` is loop-local, so a
  run executing in another process stops only at its next boundary — which is why cancelling a
  run that outlived its request stays on the roadmap.
* **No language model is actually called.** VoiceOS has the provider seam, the
  configuration and the validation, and no transport: the package bans outbound network
  imports outside a reviewed allowlist, so naming a provider yields *no proposal plus a
  visible note* and the deterministic reasoning answers. Multi-step plans therefore come
  from operator-authored task templates and the registry, never from a model — stated in
  `docs/voiceos.md` rather than left to be discovered.
* **VoiceOS's microphone belongs to the browser vendor, not to ActionMesh.** Recognition on
  `/voice` is the Web Speech API: on Chrome desktop the audio goes to Google's speech service,
  on Android to Google's, and in a WebView or a browser without the API there is no microphone
  path at all and the user types. ActionMesh receives text — the page has no audio-capture
  primitive in it, and a test pins that — but "voice, and private" would be a false claim, so
  the page says what is true instead. A server-side or on-device recognizer is the open
  alternative, and each was refused in `docs/voiceos.md` for a named reason.
* **Rate limiting is process-local.** A multi-process deployment must enforce the same
  budget at the proxy. Same for actor scoping: it assumes one shared identity store.
* **The audit anchor is only as strong as where it lives.** Point it at storage the audit
  database cannot rewrite; a sibling file protects nothing. External strengthening (KMS
  signing) is unimplemented.
* **No TLS, identity provider, or key rotation** — deployment-environment concerns,
  deliberately out of the package.
* **The Android APK is debug-signed** and assumes cleartext HTTP on a trusted LAN; a
  release keystore and TLS are prerequisites for distribution.
* **All 16 seeded actions are mocks** (`simulated: true`). Real execution exists only on
  the `device.*` plane. C13 vendor adapters (messaging, payments, calendar) remain
  external by policy: no credentials in source, no sandbox claims without a sandbox.
* **`api.py` is the least-typed surface** and the dashboard's JS is verified by string
  assertions plus a real browser, not by a JS type checker.

### Housekeeping

* **Nothing from C19 through C34 is committed.** The whole arc is dirty on `d7c148c`
  (including this file). This
  is the largest outstanding risk in the project: an errant checkout loses every
  verified checkpoint of that arc.

---

## 5. Where it is going

**Near term (in order):**

1. Commit the C19–C34 arc in phase-sized chunks.
2. ~~Resolve the projection decision, then ship the laptop inventory panels~~ — done in
   C27b. Next: the panels for projects, servers and editors, which the projection already
   makes possible and which need no further boundary decision.
3. ~~Bounded log capture for launched servers~~ — done in C29.
4. ~~Decide the content surface, then build the file view~~ — done in C28.
5. ~~Cancel the operation, not just the wait~~ — done in C31.
6. ~~VoiceOS spoken feedback, and the preview-to-run hand-off~~ — done in C33. `/voice` runs
   through `/execute`, the existing approval gate, the SSE timeline and `speechSynthesis`,
   with every privileged step its own press and success claimed only from verified nodes.
7. ~~VoiceOS conversation history and multi-step intents~~ — done in C34: durable task
   sessions, conditional steps evaluated against observed results, references to items the
   user was actually shown.
8. **Wire one real LLM provider** — an approved endpoint, credentials from the environment,
   and a deliberate widening of the outbound-network guard. Until then the seam answers with
   a visible note, which is the honest state and is shown on screen.
9. **VoiceOS device selection** — a picker that changes which laptop an action runs on, once
   one server can drive more than one agent. Until then the header names the agent it has.

**Medium term:**

* **Long-running executions as first-class objects** — the durable store survives a
  restart today; resuming, cancelling and re-attaching a watcher to a run that outlived
  its request is the next capability step.
* **Multi-device**: several laptops under one phone identity, with per-device ceilings and
  a device picker in the control plane.
* **Verification depth** — more strategies per capability, so "verified" means more than
  "the effect was observed once".

**Long term / by design still open:**

* **C13 real provider adapters**, gated behind a sandbox contract that does not exist yet.
* A **relay** for reaching a laptop that is not on the same LAN. This is the one feature
  that could quietly become "a remote shell", so it inherits every invariant above or
  does not ship.
* **Policy as data** — richer rule expression without ever letting a caller supply its
  own grants.

**What ActionMesh will not become:** a remote shell, a general RCE surface, an agent that
grades its own homework, or a system where the client decides what it is allowed to do.

---

## 6. Verifying any claim in this file

```bash
.venv/bin/python -W error::ResourceWarning -m unittest discover -s tests -t .   # 1042, OK
.venv/bin/python -m ruff check actionmesh tests scripts                          # clean
.venv/bin/python -m mypy actionmesh                                             # clean, 81 files
.venv/bin/python actionmesh/cli.py config validate                              # network-free
.venv/bin/python actionmesh/cli.py doctor                                       # network-free
```

Run the suite twice before trusting a green result: this project's last intermittent
failure turned out to be a real bug that broke one install in twenty, not test noise.
