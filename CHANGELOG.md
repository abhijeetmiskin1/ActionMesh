# Changelog

All notable changes to this project are documented here.

This project adheres to a phased delivery model. A phase is only recorded as
complete when its tests have actually been executed and passed.

---

## [Unreleased] — 2026-10-19

## C41 — the real AI brain, wired into the VoiceOS pipeline

- **The configured provider now plans a turn, not just passes a key test.** The
  Electron shell exposes generation over an authenticated **Unix socket** (`status` and
  `generate` frames, one JSON object per connection, ≤256 KiB, constant-time token
  check). The Python backend asks it to plan, and the answer is validated through the
  *same* two untrusted-input doors as anything else: `validate_proposal()` and the
  registry's `check_params`. A model that invents a capability, an actor's identity, a
  permission, or a risk level is refused by the existing policy, approval and executor
  pipeline — **AI proposes, policy decides, the user controls, the executor acts, the
  verifier confirms, the audit remembers.**
- **The API key never leaves the shell.** It is read from the OS keyring in the main
  process and sent only to the provider over HTTPS; it is never passed to the renderer,
  the Python backend, an environment variable, or the bridge socket. A renderer
  `providerTest()` with a typed-but-unsaved key takes the value from the IPC argument
  into `providers.test()` and nowhere else. There is no key getter on the renderer
  surface.
- **Eight honest states, not a green dot.** The closed vocabulary is
  `ok / missing_key / not_configured / provider_unavailable / invalid_credentials /
  rate_limited / timeout / malformed`; a key on disk is `missing_key` until a call is
  made, and an unknown provider state is folded into `provider_unavailable`. **Ollama
  reports `implemented: false`** with a reason, and stays visibly unavailable until a
  real probe and tests exist.
- **A provider connection does not grant execution authority.** `tests/security/
  test_model_authority.py` pins it: a provider can propose, but cannot grant itself a
  permission, select another actor or another user's device, or invent a registered
  capability. Model output, conversation history, screen contents and tool results are
  all treated as untrusted input.
- **Three honesty defects found by driving the packaged page, all fixed.** (1) The
  command guide's right-hand column was a hand-typed "supported" — a label nobody
  measures — and it was wrong: several listed phrases matched no rule while the page
  said supported. `POST /voiceos/guide` now runs each phrase the *page* is about to show
  through the *same* planner object a real turn uses, and the quick-command buttons are
  measured too (they had the same delusion). The route proposes nothing and runs
  nothing. (2) A sentence-verdict in a `nowrap` grid column rendered as overlapping
  fragments — the row now stacks. (3) **The status chip claimed "AI model active" while
  every turn fell back**: `/voiceos/status` read `configured` and `implemented` but
  ignored `state`, so a key the provider rejected was still reported as a live model. A
  refusing `state` now forces `mode: deterministic` with the shell's own sentence as the
  reason, and an unverified-but-configured provider carries the "press Test Connection"
  caveat into the chip's tooltip.
- **No successful live generation was made, and none is claimed.** No valid provider
  credential exists in this environment. Testing was mocked/adapter-level plus the
  packaged app's real `missing_key` fallback and **one real outbound call with a
  deliberately-invalid key that the provider correctly rejected** (`invalid_credentials`,
  "The provider rejected the stored API key.") — a real network round trip and an honest
  failure, verified end to end in the packed build. To test against a live provider:
  Settings → Provider → enter a real key → **Test Connection** → confirm `connected`,
  then send a sentence on the Voice screen and read the turn's note, which names the
  call that actually went out.
- **Gates.** Python `1462 passed, 3 skipped, 886 subtests`; desktop `64 pass`. C41
  suites: `test_llm_generation.py` 16, `test_model_bridge.py` 14, `test_voiceos_status.py`
  9, `test_model_authority.py` 11 (+19 subtests), `test_ai_voiceos_pipeline.py` 12,
  `test_command_guide.py` 10.

## C34 — VoiceOS became conversational, and the task became the unit

- **A new package carries the reasoning.** `actionmesh/voiceos/` now holds a reasoning
  layer (intents, conversational references, decomposition), a durable **task session**
  store, an orchestrator that advances one
  authorised step at a time, and an LLM provider seam. `GET /voice` gained a
  conversation panel, a task plan with per-step state, and Status / Stop-it / Clear
  buttons.
- **A sentence still cannot run anything.** `/voiceos/messages` answers with a proposal
  and `ran: false`; running is a separate press on one session step, and each step is an
  ordinary `/execute` under the caller's own credential — so a resumed task can never
  carry more authority than the person pressing the button.
- **Multi-step with real conditions.** An operator-authored task file supplies the
  details no planner may invent (a project's test command), and a step's `when` clause
  is evaluated against the *observed* result of an earlier step — `exit_code: 0` from a
  run that actually happened, not a claim.
- **References resolve only to what was shown.** "read the second one" became
  `device.file.read path=notes.txt` because a previous step had listed the files; with
  nothing observed, the same sentence gets "I have no list to take that from" rather
  than a guess.
- **The LLM seam refuses to pretend.** `ACTIONMESH_VOICEOS_LLM_PROVIDER=some-vendor`
  yields no proposal and a visible note — *this build has no approved outbound transport
  for it* — and the deterministic planner answers. Model output is validated to `{type,
  parameters}`; an entry asserting a risk level or a permission is dropped whole.
- **Three defects found by driving the real page:** the conversation read lost the local
  actor (history never appeared); the orchestrator read the engine's node outcomes as
  dicts, so a verified step was recorded as "succeeded, not verified" — a lost piece of
  evidence; and a step that ended while nobody was looking left the task saying
  `awaiting_approval` beside a cancelled execution. Sessions are now folded from the
  record on read.
Suite grew to 1168 tests; ruff, mypy (89 files) and warning-clean all green.

## C33 — VoiceOS closes the loop: voice → real laptop → verified → spoken

- **The voice surface can now run what it previewed, through the same pipeline as the
  control room.** `/plan` → an explicit **Approve & run** press → `/execute` → the existing
  approval gate → SSE events → verification → spoken result. Nothing chains automatically:
  running is its own press, and each approval is its own press on the action it names.
- **The laptop-agent phrases the demo needs are in the one planner**, not in a browser-side
  mapper: `what is my laptop running`, `show my projects`, `read this file notes.txt`,
  `stop the process <id>`, `run command <argv>`. A command that needs a target only matches
  when the target was said out loud, and "run my tests" deliberately maps to **nothing** —
  the planner will not invent a command line.
- **Two real bugs, both found by driving the page rather than by reading it.** A parked run
  publishes `execution.completed`, and the voice page read that as an outcome — it spoke
  "finished as waiting_permission" over a run that had not run. And the page subscribed to
  the event stream *after* the response returned, so a fast run's live output was gone
  before there was anything to receive it (output is transient and never replayed). Both
  fixed, both pinned.
- **`GET /approvals` used to echo the risk the caller carried**, which defaults to `medium`
  when a request omits it — so a CRITICAL command execution was labelled "medium" on the
  screen where a human decides to approve it. It now reports the registry's effective risk,
  the same raise-only rule authorization already used.
- **Spoken feedback is short, honest, and off-switchable**: a toggle, a stop button, no
  command output ever spoken, and success claimed only from the record's verified state.
- Suite grew to 1148 tests; ruff, mypy (82 files) and warning-clean all green.

## C32 — VoiceOS slice 1: a microphone on the pipeline, no hand on the trigger

- **`GET /voice` opens the VoiceOS track.** Push-to-talk, an editable transcript, a text
  fallback and an ActionMesh plan preview — and nothing else. The page contains no
  `/execute`, `/approvals`, `/consents` or `/cancel` call, so "voice never runs what it
  heard" is an absence the tests pin rather than a promise: the request targets are asserted
  to be a subset of `/health`, `/capabilities` and `/plan`.
- **Risk labels come from the registry.** Each proposed action is annotated from
  `GET /capabilities` — `medium risk · needs message.send` — never from the transcript or
  the proposal, which have no such field to speak of.
- **No audio is collected by this product.** No `getUserMedia`, `mediaDevices`,
  `MediaRecorder`, `FormData`, base64, `ArrayBuffer` or `blob:` anywhere in the served page,
  and no `localStorage`. What leaves the browser is the recognizer's text.
- **The uncomfortable part is on the page, not buried in a doc:** browser-native recognition
  is the *browser vendor's* service. Server-side STT, a WASM model and a `MediaRecorder`
  upload were each evaluated and refused for a named reason in `docs/voiceos.md`.
- **Listening is bounded by construction.** One tap is one utterance, `continuous` is
  explicitly false, `onend` does not restart, the control stays clickable while listening
  because the second tap is the only way to stop, and hiding the page stops it. A
  hand-edited transcript is never overwritten by a late result.
- **Two defects found by driving the page:** the first `setBusy` wiped the button's
  `textContent`, deleting the microphone icon, and disabled the button *while listening* —
  which would have stranded the microphone open with no way to close it.
- Suite grew to 1115 tests; ruff, mypy (82 files) and warning-clean all green.

## C31 — A Cancel that stops the operation, not just the wait

- **An in-flight provider call is interrupted.** The executor registers the call it is
  awaiting for each execution, so `POST /executions/{id}/cancel` reaches a command that
  is already running instead of waiting for it to finish. C30 measured the old behaviour
  — a cancelled 60s command printed all 60 seconds — and this closes exactly that gap.
- **The outcome is reported after the cleanup, not alongside it.** The cancelled call is
  awaited while it unwinds, and that unwind is where the runner kills the child's
  process group, so a reader who sees `cancelled` is looking at a command whose children
  came down.
- **Honest about what a cancellation cannot undo.** The action *was* dispatched, so the
  node reports `cancelled` with `verified: null`, the error names the effect
  unconfirmed, `compensation_required` is set and `compensation.unconfirmed` is
  published. A cancelled run is never replayed as a success, and cancelling it is not
  treated as a transient fault worth retrying.
- **A real cancellation is still a real cancellation.** An outer
  `asyncio.CancelledError` — a shutdown, not a user pressing a button — propagates after
  cleanup rather than being turned into a benign-looking result.
- **Proven where it matters.** Nine tests, including a subprocess test that waits for
  the command's parent and its grandchild to actually disappear, and a browser pass on
  the real server: cancelled at tick 97 of a streaming command, the output stopped
  there, the LIVE panel and REST agreed, and the audit chain still verified.
- Suite grew to 1084 tests; ruff, mypy and warning-clean all green.

## C30 — A Cancel control that says what it does

- **The phone can now ask a run to stop.** `POST /executions/{id}/cancel` had been
  available since C19 with nothing on the page offering it. Each live execution row carries
  a Cancel button, and it disappears once the run reaches a terminal state.
- **A bug in that button, found by pressing it:** a run parked for approval publishes
  `execution.completed` with `state: waiting_permission` — the *request* finished, the
  execution had not. The page treated that as final, so Cancel was hidden from a run that
  was actively streaming and the "N running" count was wrong. Fixed with an explicit
  `TERMINAL_STATES` set and by clearing the final marker when a node starts.
- **Cancellation semantics measured rather than assumed:** cancelling a 60s command
  mid-stream did not stop it (20 → 70 → 94 ticks, still `Executing`) because the executor
  honours cancellation between steps. The control therefore reads "takes effect at the next
  step", and `docs/rest-api.md` documents the same.
- **Open, deliberately:** interrupting an in-flight provider call. The pieces exist — run
  the call as a task, cancel it, and the runner's existing group-kill fires on
  `CancelledError` — but routing that through the idempotency ledger and the
  "dispatched but not verified" reporting is executor surgery that should be done on its
  own, not appended to a UI change.
- Suite grew to 1075 tests; ruff, mypy and warning-clean all green.

---

## C29 — Launched servers keep a log, and the phone can read it

- **Capture is a file, never a pipe.** A pipe nobody drains fills at ~64 KiB and the
  writer blocks — so a reader that died with its request would stop the user's dev server.
  A detached child now writes to a `0600` file with an unguessable name, and a test proves
  a child printing 400 KB **exits 0** instead of hanging.
- **Off unless the operator opens it.** `ACTIONMESH_AGENT_LOG_DIR` unset keeps the old
  behaviour exactly (`output_captured: false`). Set, the directory is refused at startup if
  it is inside the workspace — otherwise the confined `device.file.read` could collect
  whatever a server printed — and a relative value is refused rather than quietly resolved
  against the current directory.
- **Bounded without a background task**: every pass over the process table trims a log past
  512 KiB back to its tail behind a marker, so a shortened log never reads as complete. The
  accepted cost — a write landing between the read and the truncate is lost — is stated in
  the code, because the alternatives were a full disk or a blocked server.
- **`device.process.logs`** (MEDIUM, `device.process.read`, behind the same shell gate as
  its siblings): addressed only by the issued `process_id`, capped at 64 KiB per read, bytes
  delivered over the transient channel as `stream: "logs"`. Result payload keeps
  `{process_id, bytes, lines, truncated}`. An id the registry never issued is refused.
- **Fixed — the consent panel could not grant 6 of the 13 laptop scopes**, including
  `device.server.control`, so the server capabilities were unreachable from the page that
  advertises them. The select now covers every scope, and a test derives the required set
  from `device_definitions()` so it cannot fall behind again silently.
- **Verified as a paired phone in a browser**: started `server.py` on a real port, approved
  it, saw `demo · server · running · :45787` with Logs enabled, and pressing Logs returned
  the startup line, the stderr warning, `listening` and both request lines. The file on disk
  was `-rw-------` outside the workspace; the 300-record audit chain verified `ok` and held
  none of the log text.
- Suite grew to 1073 tests; ruff, mypy and warning-clean all green.

---

## C28 — The phone can browse and open a laptop file

- **The content surface, solved without opening a second door.** A raw file route would
  have bypassed registry, policy, consent, approval and audit — the one thing this
  architecture forbids — so `device.file.read` now emits the bytes it read onto the
  transient `execution.output` channel as `stream: "content"`. That channel already exists
  to carry content to an authorized observer: live, bounded, actor-scoped, never audited,
  never replayed. The result payload still strips `content`, exactly as before.
- **`entries` became projected rows**, so `device.file.list` shows names, types and sizes
  rather than a count. A list of bare strings still cannot become rows; the long-standing
  "entries must not leak" test was rewritten to say what it now means rather than deleted.
- **A "Files on the laptop" panel**: directory, list, Open per file, body in the live
  output panel.
- **Verified as a paired phone in a browser**: `notes/todo.txt · file · 39 B` listed,
  opened, and the panel held the file's three lines exactly; opening `photo.png` added
  **zero characters** (binary emits nothing). The audit chain verified `ok` across 28
  records and contained neither the body nor a `stdout` key. Console clean.
- **Why the emit happens after the worker thread returns**: the sync handlers run under
  `asyncio.to_thread`, and waking an `asyncio.Queue` from a foreign thread is unsafe.
- Suite grew to 1055 tests; ruff, mypy and warning-clean all green.

---

## C27b — Inventory reaches the phone, and driving it found three defects

- **The safe view now projects inventory rows.** `processes`, `servers`, `projects`,
  `editors` and `services` are lists of rows rather than payloads, so each row is
  re-projected through the **same** allowlist: allowlisted keys only, scalars only,
  strings still capped at 120 characters, and a list or dict inside a row is dropped
  rather than walked. One level deep, never two; at most 20 rows. Every other key keeps
  the old `{key}_count` behaviour. This is the same rule applied deeper, not a hole in it,
  and four adversarial tests pin it (content under a safe key inside a row still cannot
  get out).
- **A "What the laptop is running" panel** lists this agent's children and stops one by
  its issued id — never a pid. A finished row loses its Stop control instead of offering
  an action that cannot happen.
- **Three defects only a real browser could find:**
  - the page kept its **pre-pairing operator stream** after pairing, so it went on
    receiving other actors' executions and then 404'd fetching the records behind them
    (five such errors in the console). Pairing now closes the stream and re-opens it with
    a ticket bound to the new credential.
  - an approved stop left the row reading `running`, because the completion event can
    arrive before the registry has reaped the child. The inventory re-reads twice —
    shortly, and again after a beat.
  - a "Asked the laptop to stop…" placeholder was being overwritten by that refresh, so it
    was removed: the message should be the answer, not a promise made before the request
    returns.
- **Verified as the phone**: pair at `critical`, consent `device.process.read` +
  `device.process.control`, start a detached `watcher.py`, approve, see
  `watcher · process · running`, press Stop, approve again, row disappears and the count
  falls to 0 — with a server-side check that the process really died. Console clean.
- Suite grew to 1050 tests; ruff, mypy and warning-clean all green.

---

## C27a — The phone can actually reach the flagship flow

- **Fixed — a paired phone could never run a command.** The pairing panel's ceiling
  select offered `low`, `medium` and `high`, while `device.terminal.execute` is CRITICAL,
  so every attempt from a phone minted through this page was denied before consent or
  approval was ever consulted. The command panel C23 added, and the phone-side path C26
  repaired, were both unreachable in practice from the device they exist for.
- **One option, chosen only at the laptop.** `critical — run programs` is now offerable
  when minting a code; issuance stays loopback-only, the label states what it unlocks, and
  the note says plainly that anything below it leaves the command panel unable to run. It
  buys reach, not permission: consent and per-run approval still decide.
- **Verified in a browser as the phone**: paired at `critical`, consented `system.shell`,
  ran, approved from the approval panel -- twelve live ticks streamed to the phone, cut by
  the laptop's 3s ceiling, `Succeeded · verified`. Over the same credential, `/audit`
  returned only that actor's records, the chain verified ok, and the streamed output was
  absent from it. Console clean.
- **Docs corrected**: `docs/rest-api.md` said a paired credential carries "a risk ceiling
  of MEDIUM". It carries the ceiling the laptop chose at mint -- default `low`, up to
  `critical` -- and risk is only ever raised, never lowered.
- Suite grew to 1042 tests; ruff, mypy and warning-clean all green.

---

## C26 — The dashboard works from a paired phone, not only on the laptop

- **Fixed — every panel was laptop-only, and had been for a while.** Each one sent
  `actor: {id: <the #actor box>}`. When a device token is present the server refuses a
  body actor that disagrees with it, so `POST /execute` answered **400** from a phone:
  C23's command panel shipped with that flaw, and the older plan panel had it too --
  neither was caught because both were verified as the loopback operator. C25 then made
  it worse: the page's refresh read `/consents?actor_id=<box>`, which a paired device may
  not name, so the dashboard would have shown "Refresh failed" on every phone load.
- **The credential decides, when there is one.** `identityBody()`, `consentsPath()` and
  `consentBody()` omit the actor whenever a token is held, and `GET`/`POST /consents`
  accept an omitted `actor_id` for a credential, resolving it from the token. The
  operator has no actor of its own and still must name one.
- **Verified in a real browser acting as the phone** -- paired through the panel, token
  in `sessionStorage`, actor box left deliberately stale: consent records, refresh
  succeeds, `/execute` is accepted. The CRITICAL command it submitted was then denied by
  the **pairing ceiling** (`exec_denied_… · Denied`, streamed to the live panel), which is
  the credential-ceiling invariant working rather than a defect.
- Suite grew to 1041 tests; ruff, mypy and warning-clean all green.

---

## C25 — The decision surface checks whose human it is

- **Fixed — any paired device could approve another actor's action.** `POST /approvals`
  never looked at the caller: a phone naming another actor's parked `action_id` with
  `status: "approved"` was accepted, and the response handed back *their* resumed
  execution record. The approval ceremony -- the platform's whole human-in-the-loop --
  was being satisfied by whichever device asked first. `POST /consents` let a caller
  record a consent naming anyone's actor, `GET /consents` let them read anyone's, and
  `POST /devices/{id}/revoke` let any device unpair any other.
- **One rule, again.** All four resolve their caller through `_record_scope` (the
  stream's own `_stream_principal`): a device decides only an action in its own waiting
  set (403; 404 for an id it has none of), names only its own actor in `/consents`, and
  revokes only its own device -- a foreign device id answers the same 404 as an unknown
  one, since device ids are names callers choose. The loopback operator is unchanged.
- **Fixtures had to stop being anonymous remote callers.** Several tests approved and
  consented from a tokenless remote client because that worked; they now post from the
  laptop client, which is the human they always claimed to model.
  `test_the_token_decides_the_actor` got stronger: the other actor's consent is now
  genuinely recorded by the operator, and the credential still cannot spend it.
- 7 new tests (`TestWriteScoping`); suite grew to 1036. Ruff, mypy and warning-clean green.

---

## C24 — The read surface is as scoped as the stream

- **Fixed — every paired device could read another device's work.** P10 scoped the event
  stream to the authenticated actor, but `GET /executions`, `GET /executions/{id}`,
  `GET /approvals`, `GET /audit` and `POST /executions/{id}/cancel` never examined the
  credential. `GET /approvals` was the sharpest of them: an approval entry carries the
  waiting action's **parameters**, so a pending message body or command line was readable
  by any paired phone. Any device could also cancel any other device's execution.
- **The stream's rule was reused rather than restated.** `_record_scope` resolves the
  caller through `_stream_principal`: a credential sees only its own actor; a foreign
  execution id answers 404, not 403, because the difference would confirm which ids
  exist; a remote caller with no credential is denied. The loopback operator's unscoped
  view is unchanged, and still asserted.
- **`POST /executions/{id}/cancel` answers `requested`, not `cancelled`.** It had been
  announcing an outcome the executor had not reached yet, and it had no rate-limit budget
  of its own; it now has one, like the mutating routes around it.
- **Consequence:** an unset transport peer is no longer an accidental operator view.
  `TestClient`'s default peer is `"testclient"`, not loopback, so eleven existing tests
  now pass `client=LOCAL` explicitly — which is what they always meant to assert.
- New guard: `tests/security/test_record_scoping.py` (8 tests, two paired phones).
  Suite grew to 1029 tests; ruff, mypy and warning-clean all green.

---

## C23 — The phone can start a laptop command

- **A "Run a command on the laptop" panel**, using the existing `/execute`, policy check
  and approval path — no new endpoint. Before this the dashboard could consent, approve,
  watch and reconcile a laptop execution but could not begin one; starting a command
  meant POSTing it yourself.
- **Arguments travel as argv.** One per line in the form, sent as a list, never rejoined
  into a single string — that shape means something only to a shell, and the command
  policy refuses it. The request carries program, argv, working directory and timeout and
  no authority at all: no grants, no role, no risk level, no high-risk opt-in. Consent
  stays server-side and a CRITICAL scope still parks for the operator's decision.
- **The consent panel now offers the laptop's scopes** (`file.read`, `file.write`,
  `device.process.control`, `system.shell`). It listed only `message.send`, so a command
  started from the page could not be consented from the page.
- **Verified in a real browser through the controls themselves** — consent, fill, *Run on
  laptop*, the parked-run message, the approval panel's own *Approve* button — after which
  a real `python3 chatter.py` streamed twelve live ticks and was cut by the agent's 3s
  ceiling, ending `Succeeded · verified` with the limit named in the panel's note line.
  Console clean.
- **A shipped-JS syntax error, caught on the first load:** the page is a non-raw Python
  string, so `split('\n')` was consumed by Python and emitted a real newline inside a JS
  string literal. No string-asserting test could see it; the browser could. Escaped, and
  the page's parse is now confirmed in a browser as part of the check.
- Suite grew to 1021 tests; ruff, mypy and warning-clean all green.

---

## C22 — The phone is told which limit bound a run

- **The live panel now explains its own silence.** On `execution.completed` — and for
  every execution already being watched, on reconnect via the existing `reconcile` — the
  dashboard fetches the authoritative record and renders what bound the run:
  *"a1: stopped by the laptop's own 3s ceiling, not the 60s that was asked for"* when a
  timeout was clipped, and *"p1: launched detached, so nothing captures its output — run
  it as a command to watch it"* for a detached process or server. Text via `textContent`,
  no `innerHTML`, existing change-guards intact.
- **Verified in a real browser against the real agent** with
  `ACTIONMESH_COMMAND_TIMEOUT_S=3`: a real `python3 chatter.py` streamed twelve live
  ticks, was cut at the ceiling after operator approval, reached `Succeeded · verified`,
  and the note line carried the ceiling sentence with both numbers. A detached launch
  printed the no-output explanation instead of an unexplained empty panel. Console clean.
- **Found and fixed during that verification:** the helper read `record.nodes` — the
  `POST /execute` key — while `GET /executions/{id}` serves the same node views under
  `results`. The mismatch was silent (no note ever appeared), which is exactly why the
  key is asserted by test now, not just used.
- Suite grew to 1016 tests; ruff, mypy and warning-clean all green.

---

## C21 — Enforced limits are reported, not implied

- **A clipped timeout now says so.** The agent's configured ceiling
  (`ACTIONMESH_COMMAND_TIMEOUT_S`) is the timeout a command actually gets; a caller's
  `timeout_s` can only lower it, never raise it. A terminal result carries
  `timeout_requested_s`, `timeout_applied_s` and `timeout_clipped`, so "stopped at 60s"
  is distinguishable from "the command hung for the two minutes I asked for". All three
  are allowlisted scalars, so they reach `POST /execute` and `GET /executions/{id}`; the
  engine test asserts that safe view by exact equality.
- **A detached launch states that nothing captures its output.** Process and server
  records carry `output_captured: false`, which is structural: nothing outlives the
  request that started a long-lived process to drain its pipes, so launching discards
  output at launch. Watching output as it happens is `device.terminal.execute`, which
  streams.
- **Not built, on purpose:** live streaming of detached `device.server.start` logs. It
  needs a consumer as long-lived as the process (a bounded log file plus a
  lifespan-owned tailer); draining a pipe partway and stopping leaves the user's server
  blocked on its next 64 KiB, which is a worse defect than the missing feature.
- **Fixed — `load_key` corrupted about one generated key in twenty.** The key file was
  read with `.strip()` on **bytes**, and a random 32-byte key begins or ends in a byte
  the whitespace set matches roughly once in twenty installs. The strip dropped it, the
  file measured 31 bytes, and the guard that (correctly) refuses to regenerate a
  truncated key fired — so the server started fine once and then **failed to start on
  every restart**. Keys are now read as undelimited bytes and take the first
  `KEY_BYTES`, which also keeps a hand-written file's trailing newline harmless. Found
  as an intermittent full-suite failure reproducing about one run in ten; the new test
  round-trips all 256 byte values at both ends of a key, and the suite is deterministic.
- Suite grew to 1014 tests; ruff, mypy and warning-clean all green. Corrected stale
  wording in `docs/security.md` row 31, which still claimed mid-command output was not
  streamed at all, and in `HANDOFF.md`, whose verified-state block still reported 425
  tests and no rate limiting.

---

## [Unreleased] — 2026-10-09

## C20 — Live output streaming, and operational CLI/SDK

- **Incremental terminal output** now reaches the phone while a command runs: the
  engine installs an `OutputPublisher` per node, the runner calls it per chunk, and the
  laptop provider streams real stdout/stderr as `execution.output` events. They are
  transient (never audited, never replayed), bounded (4 KiB/chunk, 64 KiB/node, then a
  `truncated` flag), and actor-scoped. Verified end to end in a real browser against a
  `python3` process whose lines appeared live, the run reached `verified`, and the audit
  chain contained none of the output.
- **Mobile live-output panel** renders chunks as text in a bounded `<pre>` with a
  follow toggle that preserves scroll; no `innerHTML`, change-guard intact.
- **CLI** gained `config validate` and `doctor` (network-free, per the security guard);
  **SDK** gained in-process `executions/execution/approvals/metrics/event_stream`.
- Suite grew to 1004 tests; ruff, mypy and warning-clean all green.

---

## [Unreleased] — 2026-10-09

## C19 — Mobile-to-laptop control plane, and P11 production platform

- **Device plane (C19a–c):** persistent device identity, keyed credential digests
  (revoking a device kills its credentials), and loopback-minted 6-digit pairing
  codes with a sliding-window attempt budget and a ceiling chosen at issuance.
- **Laptop agent (C19d):** `actionmesh/agent/` is the only place real OS access lives —
  a confined filesystem, an allow-listed command policy, argv-only spawning with
  constructed child environments, a process/server registry that can only address
  what it launched, and editor/project discovery. 19 `device.*` capabilities, each
  verified against real files, processes, git and a loopback listener, and guarded by
  an AST check that fails the suite if a new spawn/eval/exec path appears.
- **Realtime streaming (C19e):** `node.started` / `execution.progress` /
  `approval.required` on the existing hub; a safe result boundary that never ships
  file contents or command output; actor-scoped SSE/WS with a short-lived
  `EventSource` watch ticket and `Last-Event-ID` resume. The mobile dashboard drives a
  real laptop process end to end and recovers authoritative state after reconnect.
- **Production platform (P11):** `core/config.py` adds explicit
  development/lan/production deployment modes with **fail-closed startup** — a
  production process missing durable storage, a durable audit chain, a durable
  signing key, a shared key, or trusted proxies (when bound off-loopback) refuses to
  start. `SQLiteStore` is now wired in when configured and **schema-versioned** (a
  newer file is refused, never down-migrated); execution records round-trip and
  survive a restart. `/ready` reports mode and store/audit reachability without
  conflating server health with device connectivity.
- Suite grew to 973 tests; ruff, mypy and a warning-clean run are all green.

---

## [Unreleased] — 2026-09-30

## Docs — the published docs were stale, and now cannot silently rot again

- **Found by audit, not by reading.** `docs/rest-api.md` still said "realtime
  events are later phases" and listed 6 of the 21 registered routes. It omitted
  `/plan`, `/consents`, `/metrics`, `/dashboard`, `/audit`, `/audit/verify`, the
  PWA assets and both event transports — all shipped long ago.
- It also showed a request body containing `"grants"`, **which the API
  rejects**. The doc contradicted the very boundary it was describing.
- Rewrote `docs/rest-api.md` (full route table, a `POST /plan` section, and a
  "two gates, both required" section recording the measured finding that a
  consent without a matching policy rule is still denied).
- `docs/architecture.md` gained the missing **Planner** layer, a note on why the
  planner is the only untrusted *producer*, and corrected its "deliberately
  absent" list (audit persistence shipped; the shipped planner is rule-based, not
  a model; history begins at the reconstruction).
- `docs/deployment.md` gained an environment-variable table, portable shell
  commands, and the Android client section.
- `docs/security.md`'s open-threat row said "no untrusted content reaches the
  planner" — true only because no planner existed. Replaced with the real
  injection surface (`POST /plan` and the intent box) and its concrete
  mitigations, rated **Low** with the caveat that a plan can still be *wrong*.
- **8 new tests in `tests/unit/test_docs_freshness.py` compare the documented
  surface against the routes the app actually registers, in both directions** — a
  route nobody documented is a gap, and a documented path that does not exist is
  a lie. Proven to fail: deleting one row from the table produced two failures.
  The suite is now **425 passing tests**.

## Repository — version control restored (the history still does not exist)

- `HANDOFF.md` claimed git "cannot be installed" because this machine's network
  blocked github.com and git-scm.com. **That is no longer true.** github.com is
  reachable, and git 2.53.0 is now installed into `~/.local/git` without root —
  the Ubuntu `.deb` fetched with `apt-get download` and unpacked with
  `dpkg-deb -x` — and added to `~/.bashrc`.
- The repository was initialised on `main` with one first commit capturing the
  verified C0–C18 state: **131 tracked files**, `ruff` clean, **417 tests OK**.
  Nothing from `.venv/`, `build/`, `dist/`, `android-toolchain/`, caches, the
  generated PDF or any keystore is tracked; `git fsck` reports a valid object
  store.
- **This history starts after the reconstruction, not during it.** The original
  tree and its `.git/` are gone and cannot be recovered, so there is no history
  before this commit — a real limitation, not a formality.
- `scripts/snapshot.py` is kept as an independent zip backup (belt and braces).
  Its "run it after every significant change" advice is now a secondary habit
  rather than the only defence against losing the work.

## C18 — Intent to plan (the pipeline stage that was missing)

### Added — a planner, and the trust boundary it draws

- The documented pipeline began `USER INTENT → intent / planner → STRUCTURED
  PLAN`, but nothing implemented it: `validate_plan` could validate a plan and
  nothing produced one. `actionmesh/core/planner.py` fills that gap.
- `Planner` is the structural interface (`plan(intent) -> Plan`);
  `RuleBasedPlanner` is a deterministic, offline reference planner — the planner
  counterpart of the mock providers. It matches a small, documented rule table
  (message, reminder, calendar, search, files, payments, notifications, app
  launch) and splits a compound intent on `;` / `then` into an ordered chain.
- `plan_to_graph()` is the trust-boundary crossing. Proposals are re-validated as
  untrusted input and chained with a terminal node, so the result is a graph the
  executor accepts, with deterministic node ids. An empty plan is **rejected**
  rather than executed as a no-op.
- **A plan may carry only `{type, parameters}`.** `ProposedAction` has no
  `risk_level`, `permissions`, `depends_on`, `provider` or `state` field: a
  proposal that could name its own risk or grant itself a permission would be
  exactly the vulnerability this boundary exists to prevent.
- Anything the planner could not map is recorded in `Plan.notes`, never silently
  dropped — a plan that quietly ignored half an intent would look complete.
- Wired into the SDK (`plan`, `plan_graph`) and the API (`POST /plan`, which
  returns the proposal and deliberately executes nothing). The planner is
  injectable, so a real model can replace the rule-based one without touching the
  SDK or anything downstream.
- **The control room can now plan.** A "Plan from natural language" panel takes
  an intent, shows the proposed actions and any planner notes, and offers
  *Execute planned actions* as a deliberately separate, initially-disabled step.
  The plan path builds the graph in the browser but sends **no permissions or
  grants** — only policy rules bounded to the proposed action types, which is what
  the existing message flow already does. Consent still comes from the
  server-side store, so the measured behaviour is: `tell mom I'll be late` and
  `remind me to call mom at 5pm` succeed once consented, while `pay alice 20` is
  **denied** even though the browser proposed it *and* declared the policy rule.
  That is the whole thesis, end to end, in three clicks.
- 6 new tests in `tests/unit/test_dashboard.py` pin that the browser cannot hand
  the server a permission or grant, that its policy rules stay bounded to the
  proposal, and that planner text is rendered via `textContent` rather than
  `innerHTML`. The suite is now **417 passing tests**.
- 32 new tests (`tests/unit/test_planner.py` plus SDK/API coverage), including
  one proving that a **planned** payment is still denied by the policy engine with
  no grant: the strongest thing the planner can say does not buy it authority. The
  suite is now **411 passing tests** (417 with the control-room planner panel).

## Quality — lint-clean, and a masked test surfaced

- Ran the project's own linter (`ruff`, already configured in `pyproject.toml`)
  across the tree for the first time: **112 findings**. Fixed all of them —
  import order, unused imports and unsorted `__all__`, deprecated imports,
  redundant f-strings, `itertools.pairwise`, `next(iter(...))`, and collapsed
  `with`/`if` nesting. `ruff check .` now reports **All checks passed**.
- **Found a test that never ran.** `tests/unit/test_graph.py` defined
  `class TestCycleDetection` twice; the second definition silently shadowed the
  first, so its four cycle tests were collected by nothing. Renamed the second to
  `TestDeepChain`. The suite went from 375 to **379 passing tests** — a live
  example of this project's own rule, *a test that never runs is not a passing
  test*.
- Narrowed two blind `assertRaises(Exception)` in `test_graph.py` to the actual
  `ValidationError`, and a WebSocket assertion to `WebSocketDisconnect`: a blind
  assertion cannot distinguish the intended failure from an unrelated one.
- `[tool.ruff] extend-exclude` now skips `build/` and `android-toolchain/`, so the
  linter stops reading generated trees.

## C17 — Android APK (delivered)

### Added — a real APK, built without Gradle

- `android/` is a dependency-free native shell: one `WebView` pointed at the same
  `/dashboard` the PWA installs, with a configurable server URL, a reload/back
  affordance, an in-app error page, and an overflow *Server…* dialog. It holds
  **no application logic** — it renders only what the trusted server returns, so
  policy, execution and audit keep a single source of truth. There is no
  AndroidX, no AppCompat, no Gradle and no Maven resolution.
- `scripts/build_apk.py` builds and signs it using only the SDK build-tools:
  `aapt2 compile`/`link`, `javac`, `d8`, `zipalign`, `apksigner`. It downloads a
  portable JDK and the Android command-line tools into `android-toolchain/`
  (git-ignored), installs the platform and build-tools through `sdkmanager`, and
  writes `dist/actionmesh-<version>.apk`. The two Windows-only PowerShell
  helpers are superseded for Linux/macOS and left in place for Windows.
- The manifest requests **exactly one** permission (`INTERNET`), and cleartext
  is enabled explicitly (a LAN server is plain HTTP) with the trade-off stated
  in `android/README.md` rather than hidden.
- Verified output: `app.actionmesh.control`, minSdk 26, targetSdk 34, signed with
  APK Signature Scheme **v2 + v3**. `aapt2 dump badging` confirms the package,
  the single permission and the launcher activity.
- `tests/unit/test_android_apk.py` (16 tests) pins the manifest's permission
  surface, the brand palette, the build-script helpers, and — when an APK exists
  in `dist/` — that it contains a manifest, resources and a signed dex. The
  suite is now **379 passing tests**.

### Fixed — a snapshot would have swallowed the ~775 MiB toolchain

- `scripts/snapshot.py` excluded the venv and caches but not the new
  `android-toolchain/` (JDK + SDK, ~775 MiB) or `build/`. The first backup after
  C17 would have been enormous for no benefit — the same failure mode as the
  39.8 GB of compounding archives fixed earlier. Both directories are now
  excluded, with a regression test asserting real sources are still kept. The
  backup is 376 KiB.

## C16 — Compensation execution

### Fixed — a compensation node ran as ordinary forward work

- `builder.compensate()` sets no `depends_on`, so Kahn layering saw every undo
  node as a **root**. A `calendar.create` + `calendar.delete` pair fired in
  order during the forward pass and the engine reported `success` for a net
  no-op — the most dangerous outcome available, because the user's intent was
  silently discarded while the audit trail showed everything worked.
- `topological_layers` now excludes `COMPENSATION` and drops edges *pointing at*
  an excluded node (an END depending on its compensation otherwise looked like a
  cycle). Compensation actions were already covered by `authorize_graph`, so
  rollback cannot become a policy bypass.

### Added — undos are actually performed

- `CompensationRunner` (`core/verification/recovery.py`) stays pure and decides
  *which* undos are legitimate; the executor performs them. An undo runs only
  when the target produced a **verified** side effect, a compensation is
  declared, and the action is **not** in `IRREVERSIBLE`.
- Order is reverse of forward execution, so dependencies unwind as built.
- `ExecutionResult` gains `compensations` and `compensation_required`. A rollback
  **never** upgrades the result to `SUCCESS`; a refused or failed undo sets
  `compensation_required` so a human learns an effect may still be standing.
- Events `compensation.completed` / `compensation.refused`; metrics
  `compensations.attempted` / `.failed` / `.refused`.

### Fixed — a silently dropped rollback

- Nested compensation (an undo declaring an undo) is now **rejected** by
  `validate_graph_structure`. It was accepted, then silently ignored at
  runtime — the author would believe they had a rollback and have none, which
  is the same class of failure as the forward-execution bug above.
- **An unconfirmed side effect was invisible to the caller.** A node that was
  dispatched but not verified ends as `REQUIRES_USER`; its real-world effect may
  exist. The compensation filter correctly required *verified* successes, but
  that left the unverified node in neither the undone set nor the report — the
  caller saw `compensation_required: False` and could conclude the system was
  clean. Such nodes are now recorded as possible standing effects
  (`compensation.unconfirmed` event, `compensations.unconfirmed` metric) and set
  `compensation_required`. They are deliberately **not** auto-undone: undoing an
  effect that may not exist is a second guess at the user's data. A node denied
  before dispatch (`attempts == 0`) cannot have left an effect and is not
  reported.
- 11 new tests in `tests/unit/test_compensation.py`.

### Added — installable on a phone (PWA)

- `GET /manifest.webmanifest`, `GET /sw.js`, `GET /icon.svg`, plus
  `apple-touch-icon` / `theme-color` / `viewport-fit` meta and a service-worker
  registration on the dashboard. A phone can now **Add to home screen** and
  launch the control room full-screen with no browser chrome — the supported
  route to "an app on my phone" for a Python server, and it needs no APK, no
  signing key, and no store.
- The service worker caches **only the shell** (`/dashboard`, `/icon.svg`).
  It never caches `/audit`, `/executions`, `/capabilities`, or any other JSON: a
  stale execution record or audit chain would be a lie in a system whose entire
  purpose is truthful reporting. Network-first, so a phone online always shows
  live data. `tests/unit/test_pwa.py` asserts the absence of those routes from
  the worker source, so the unsafe variant cannot come back unnoticed.
- 12 new tests. The suite now has **358 passing tests**.

### Measured, not assumed — API performance

Benchmarked all routes (60 iterations each, p50/p95):

```text
/health          5.76ms p50   25.14ms p95
/capabilities    6.98ms p50   11.41ms p95
/metrics         4.83ms p50    7.35ms p95
/dashboard       4.74ms p50    7.49ms p95
/audit?limit=100 3.01ms p50    4.66ms p95
```

`/audit` stays ~2.5ms from `limit=1` to `limit=1000`, so the chain walk is not
a scaling problem at this size. **No optimisation was made**: there is no hot
path, and adding a cache in front of the audit chain would trade correctness for
milliseconds that are already imperceptible. The real mobile friction was
installability, not speed.

### Not done — Android APK (now delivered in C17)

The APK that was attempted here did **not** ship from the Windows machine: it
needs a JDK (~182 MB) and the Android SDK, and that machine's download stalled
repeatedly at ~94 MB of the JDK before the ranged-resume fetch could make
progress. It is now built on Linux — see **C17** — by `scripts/build_apk.py`;
the PowerShell helpers remain only for Windows.

- **Audit truncation was silently accepted.** Measured, not assumed: with raw
  SQL access, editing a payload, the actor, or deleting a *middle* record is
  refused — but `DELETE ... WHERE seq >= n` leaves a valid prefix that
  `verify()` reports as `ok`. An attacker can then append a forged record
  re-anchored onto the truncated tail and the whole chain verifies again
  (`last actor = mallory`, `ok = True`). A hash chain cannot detect this; it has
  no way to know how much history *should* exist.
- New `actionmesh/core/audit/anchor.py`: `AuditAnchor` protocol,
  `FileAuditAnchor` (a sealed `(records, head)` checkpoint), and
  `AnchorMismatchError`. `AuditLog.open(..., anchor=...)` now refuses a chain
  that is shorter than, or contradicts, its anchor; `AuditLog.seal()` takes the
  checkpoint. Enabled in the API with `ACTIONMESH_AUDIT_ANCHOR`.
  **This only helps if the anchor lives somewhere the audit database cannot
  rewrite** — a sibling file protects nothing, and that is stated in the module
  docstring rather than glossed. An anchor should also be signed with a
  KMS-held key in a real deployment.
- 6 new tests in `tests/unit/test_audit_anchor.py`, including one that pins the
  original defect (truncation *is* invisible without an anchor) so it cannot
  quietly regress. The suite now has **346 passing tests**.

### Added — durable, fail-closed audit persistence

- `SqliteAuditBackend` persists the chain using only the standard library,
  matching the existing `SQLiteStore` and `SQLiteConsentStore` patterns. Plain
  relational SQL with `seq` as PRIMARY KEY, so a PostgreSQL move is a driver
  change rather than a redesign.
- **Append-only is enforced by the schema, not by convention:** a second write
  at an existing `seq` raises `IntegrityError` instead of overwriting history.
- `AuditLog.open(backend)` restores a persisted chain and **refuses to start
  on tampered history**, raising `AuditIntegrityError` naming the broken `seq`.
  Restoring a broken chain would let an attacker who edited the store be
  silently trusted from the next append onwards. `require_intact=False` gives
  an explicit forensic path that still returns the damaged evidence.
- `append()` persists **before** updating memory, so a failed durable write
  leaves no partial record anywhere rather than letting memory and disk
  disagree.
- `reset()` is refused on a persisted chain — erasing durable history in place
  destroys what it exists to preserve. Start a new store instead.
- 9 new tests, including one that tampers with the file using a **plain
  `sqlite3` connection and no knowledge of ActionMesh**, and one proving the
  chain continues correctly across a simulated process restart.
- The suite now has **329 passing tests**.

### Fixed — a denied execution left no audit trail

- The executor returned `DENIED` **before publishing any event**, so the single
  most security-relevant outcome in the system produced no trace at all. A
  denial is now published as `execution.denied` like every other terminal
  outcome, and a test asserts it reaches the chain.
- `create_app()` opened the SQLite audit backend and, when
  `AuditLog.open()` rejected a tampered chain, the handle was never closed —
  leaving the file locked on Windows. The backend is now closed on that path
  too, and ownership is published as `app.state.audit_backend` so a shutdown
  handler or test can release it.

### Fixed — audit backend ownership

- `AuditLog.close()` was closing a **caller-owned** backend connection, which
  leaked the handle and left the SQLite file locked on Windows. The log holds
  no resources of its own, so `close()` was removed and the ownership rule is
  now stated on `open()`: the caller keeps the backend, including when `open()`
  raises.

### Added — C15 tamper-evident audit trail

- New `actionmesh/core/audit/` provides `AuditLog`: an **append-only,
  hash-chained** store. Each record carries a SHA-256 of its own content and
  the hash of its predecessor, so history is a linked chain rather than a list.
- `verify()` **recomputes every hash instead of trusting stored ones** and
  returns the exact `seq` where the chain broke. It detects: edited content,
  deleted records, reordered records, a forged genesis link, and — the case
  that actually matters — an edit that re-seals its own hash, which still
  breaks because the *next* record points at the old one.
- `EventHub` gained an optional `audit=` sink, so every published event is
  sealed into the chain before it reaches subscribers. It is optional and
  off by default, so existing wiring is unchanged.
- `AuditLog.record_decision()` chains the output of `PolicyEngine.explain()`,
  so authorization decisions land in the same chain as execution events.
- `AuditLog(max_records=...)` **raises `AuditError` rather than dropping the
  oldest entries** — silently truncating an audit trail is itself tampering.
- New protocol contract `AuditRecord` plus `GENESIS_HASH`, exported from
  `actionmesh.protocol`.
- `docs/audit.md` documents the contract and, explicitly, its limits.
- 18 new tests. The tamper tests reach into `log._records` deliberately, to
  simulate an attacker with write access; the integration tests chain a real
  execution and a real `PolicyEngine` decision rather than synthetic data.
- **Honest residual risk, not oversold:** a chain stored beside its own data
  resists partial tampering, not an attacker who rewrites the whole store.
  Production tamper *evidence* needs an anchor outside it — append-only
  medium, signed checkpoint, or write-once storage. The threat model rates
  this **Medium** and says so.

### Added — C14 threat-model pass and an executable security guard

- `docs/security.md` now carries the full C14 threat model in the required
  shape — **threat / attack surface / mitigation / test / residual risk** —
  covering **19 mitigated threats**, and an open-threat table with an explicit
  **exposure today** column instead of a comfortable "n/a".
- Honest ratings: API-key protection is optional by default and there is no
  rate limiting, so that row is rated **High** residual risk and left there.
  Audit tampering is recorded honestly: `PolicyEngine.explain()` produces a
  JSON decision record, but there is still **no append-only or tamper-evident
  audit store**.
- New `tests/security/test_dangerous_primitives.py` (8 tests) turns the
  previously prose-only claim "the core contains no subprocess, eval or exec"
  into a regression guard. It parses every module in `actionmesh/` with `ast`,
  so comments and docstrings cannot influence the result, and includes a
  self-test proving the guard actually rejects a violation.
- The guard covers four threats that previously had **no test at all**:
  command injection, SSRF, unsafe deserialization, and dynamic code execution.
- Network/process imports are banned package-wide behind a deliberately
  **empty** `NETWORK_ALLOWED` allowlist, so granting a future official adapter
  network access is an explicit, reviewable edit. The allowlist relaxes
  imports only; `eval`/`exec` remain banned even on allowlisted paths.
- The guard initially flagged `providers/real_base.py`. Investigation showed
  this was a **false positive** — `urlparse` is string handling used to enforce
  HTTPS, not network access — so the deny-list was made dotted-name precise
  (`urllib.request` banned, `urllib.parse` legal) rather than suppressing the
  finding. Two tests now pin that distinction.
- The suite now has **292 passing tests**.

### Added — regenerable handoff PDF

- `scripts/handoff_pdf.py` renders the engineering handoff to PDF using only the
  standard library: it emits the PDF cross-reference table, page tree, and
  base-14 font resources itself. No third-party package, no network call.
- The generator **validates its own output** before writing: header, `startxref`,
  every cross-reference offset, and the trailer. A malformed document is refused
  rather than handed over.
- Cover-page test count is parsed from `TASK_BOARD.md`, so the deliverable cannot
  drift from the recorded evidence.
- Text outside WinAnsi (box-drawing layout diagrams, em dashes, check marks) is
  transliterated so the content stream stays valid Latin-1.
- `tests/unit/test_handoff_pdf.py` adds 13 tests covering escaping, wrapping,
  generated structure, and rejection of a corrupted cross-reference table.
- The suite now has **284 passing tests**.

### Fixed — snapshot backup script (real data-loss risk)

The backup script — the only substitute for version control — had two defects
that together produced **39.8 GB** of archives in the working tree (16 files):

- Its default target was `Path(f"..actionmesh-snapshot-{stamp}.zip")`, a
  **filename beginning with `..`**, not a parent-directory path. Every backup
  was therefore written *inside* the tree it existed to protect, so losing the
  tree would have lost the backup with it.
- Snapshots were not excluded from later snapshots, so each archive contained
  all previous ones and the size compounded on every run.

Fixed: the default target now resolves beside the tree (`default_target()`),
previous archives are excluded by name pattern, the output file is never
included in itself, writing inside the tree prints a warning, and the script
reports the resulting size. The same run is now **377 KiB instead of ~500 MB**.
`tests/unit/test_snapshot.py` adds 10 tests, including one that asserts archive
size does not compound across runs.

### Fixed — C5/C6 audit hardening

- Idempotency entries are completed only after independent verification, so an
  executed-but-unverified action cannot replay as `SUCCESS`.
- Subscriber queues are bounded; slow consumers are disconnected explicitly
  instead of allowing unbounded memory growth.
- Added vendor-neutral provider contract and requirements-matrix documents;
  no vendor or external side effect was introduced.
- Added an evidence matrix based on official Stripe and Slack documentation;
  unknown and sandbox-required requirements remain explicit, with no provider
  ranking or compatibility claim.
- Added a decision-readiness matrix and post-selection implementation gate that
  separate contract compatibility, runtime compatibility, and product
  suitability.
- Added regression coverage for replay and WebSocket delivery; the suite now
  has **261 passing tests**.

---

## [0.1.0] — 2026-09-29

### ⚠️ Reconstruction notice

The original working tree was deleted during development and `.git/` was lost
with it; no backup exists. This release is a **reconstruction** of the design
from memory, not a restoration of the lost code. Every claim below was
verified by executing the reconstructed code.

### Added — C1: AMP/1.0 protocol (`actionmesh/protocol/`)

- `models.py` — `Action`, `GraphNode`, `ActionGraph`, `ActionDefinition`,
  `Permission`, `Policy`, `PolicyRule`, `ApprovalRequest`, `VerificationResult`,
  `MeshEvent`, `ExecutionRecord`, `Capability`, `DeviceInfo`, `Identity`.
- Enums: `RiskLevel` (4), `ActionState` (16), `NodeType` (9), `PolicyDecision`
  (4), `FailureKind` (10).
- `validate.py` — fail-closed validation for untrusted payloads
  (`validate_action`, `validate_target`, `validate_identity`). Untrusted input
  can never set a lifecycle state.
- `version.py` — `AMP/1.0` negotiation. An unsupported version is an error, not
  a silent downgrade.
- `graphs.py` — `validate_graph`, `validate_plan`, `classify_failure`.

### Added — C2: Action Registry (`actionmesh/core/registry/`)

- `ActionRegistry` — `register`, `get`, `versions`, `list`, `discover`, `resolve`.

### Added — C7: Domain-specific mock providers (`actionmesh/providers/`)

- Shared deterministic mock harness with call tracking, failure injection,
  idempotent receipts, and C6-compatible observation.
- Messaging, calendar, files, notifications, contacts, and payment providers
  with domain state and realistic failure kinds.
- Explicit payment transitions from `prepared` to `confirmed`, then `settled`
  or `failed`; no network, credentials, or real side effects.
- Focused provider tests covering routing, state, failures, and observation.

### Added — C8: REST API initial slice (`actionmesh/api.py`)

- FastAPI application with health and capability discovery endpoints.
- Validated graph execution, execution-record lookup, approval submission, and
  cancellation endpoints backed by the existing executor.
- HTTP tests for authorization, malformed graphs, and endpoint behavior.

### Added — C9-C12, C14-C15 foundations

- Bounded in-memory event hub with executor lifecycle events, SSE replay/live
  streams, and WebSocket subscriptions.
- Exact-match memory/entity resolution, device presence and capability registry,
  and an in-process developer SDK.
- Optional API-key protection for HTTP and WebSocket transport boundaries.
- SQLite persistence for execution records, consent, and idempotency, plus
  process-local execution metrics; live vendor integrations remain deferred
  until official contracts and credentials are selected.
- Dependency-free dashboard page and release smoke coverage for health,
  metrics, capabilities, and the public app surface.
- CLI commands for capability JSON, dashboard URL discovery, and optional
  Uvicorn serving via the `server` extra.
- Docker/ASGI deployment metadata, health checks, environment guidance, and
  package-wheel validation.
- C13 official-provider boundary requiring HTTPS, environment-managed
  credentials, and concrete scopes without claiming a live vendor integration.
- C14 request-size limits, constant-time API-key checks, security headers, and
  focused transport-hardening tests.
- API permission grants now come only from server-side `ConsentStore`; request
  bodies cannot self-authorize execution.
- Dashboard upgraded from read-only status page to an application console for
  actor-scoped consent and the guarded simulated messaging workflow.
- Dashboard now includes recent execution history, actor-filtered consent
  history, and live SSE activity updates.
- Definition validation: name, version, risk, timeout, known + concrete
  permissions only, schema shape, retry policy.
- `check_params` — required-parameter and type checking, with a fail-closed path
  for malformed property specs.
- `resolve` — shape-validates untrusted input, binds the definition, and raises
  risk to `max(action, definition)`; risk is never lowered.
- 16 seeded `provider="mock"` definitions, all described `MOCK:`.
- **37 tests.**

### Added — C3: Permission + Policy Engine (`actionmesh/core/permissions/`)

- Closed catalog of 20 concrete permission scopes with namespace helpers.
- Two distinct matching regimes, and the split is a security boundary:
  **grants match exactly** (never wildcards); **policy rules may use wildcards**.
- `PolicyEngine` — 7-step deterministic precedence, auditable reasons,
  `explain()` for JSON audit records.
- Risk gate: CRITICAL always requires approval; HIGH requires approval unless
  the policy context opts in server-side.
- `ConsentStore` — server-side grant ledger; refuses wildcard and unknown consent.
- `policy_from_rules` — accepts untrusted dicts or typed `PolicyRule` objects;
  malformed entries become DENY rather than being dropped.
- **61 tests.**

### Added — C4: Action Graph (`actionmesh/core/graph/`)

- `GraphBuilder` — all 9 node types; action dicts validated as untrusted input.
- `validate_graph_structure` — unique ids, per-node config rules, dependency
  existence, no self-dependency, compensation targets, terminal node required.
- `find_cycle` — iterative DFS with a colour map, so a deep graph cannot exhaust
  the recursion limit (verified with a 2000-node chain).
- `topological_layers` / `execution_order` — deterministic; ties break on
  `sorted()` so identical graphs produce identical order.
- Deterministic serialization with a verified round trip.
- `authorize_graph` — composes C2 (registry) and C3 (policy) without
  re-implementing either. All-or-nothing: one denied node blocks the graph.
  `ASK` does not block; it flags the node as needing approval.
- **46 tests, including 11 graph security-invariant tests.**

### Security

- `tests/security/test_graph_invariants.py` — LLM output is untrusted data:
  an injected `authorized: true`, `permissions: ["*"]` or `role: "admin"` in
  action parameters grants nothing. Unknown actions, missing permissions,
  missing actors and wildcard grants all fail closed.
- **3 real bugs found by these tests and fixed:**
  - `select_rule` sorted restrictiveness ascending, letting an `allow` rule
    shadow a `deny` — a genuine authorization bypass.
  - `validate_action` silently dropped a caller-supplied `idempotency_key`,
    defeating idempotent replay.
  - `ConsentStore.grants()` conflated a filter ceiling with the grant's own
    risk ceiling, so legitimate consents were dropped.

### Known limitations

- CLI and dashboard release surfaces are implemented and covered by smoke tests.
- All capabilities are `provider="mock"`; nothing performs a real side effect.
- The default `ConsentStore` and `IdempotencyLedger` are in-memory; SQLite-backed
  stores exist for deployments that inject them and survive a restart.
- Real vendor integrations are intentionally deferred.
- No git history exists.

[0.1.0]: https://example.invalid/actionmesh/0.1.0
