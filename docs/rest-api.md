# REST API

The initial C8 transport is exposed by `actionmesh.api:create_app`.
`app = create_app()` provides the default seeded registry, policy engine, and
simulated providers. Deployments and tests may inject an `ExecutionEngine`.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/health` | Service and protocol status; the only route outside the API-key boundary |
| GET | `/ready` | Readiness: mode, durable-store and audit reachability, online-device count (never gates on device connectivity) |
| GET | `/dashboard` | The control room, also installable as a PWA |
| GET | `/voice` | VoiceOS: the microphone surface. Transcript → `/plan` → an explicit run press → `/execute` → the existing approval gate → `/events` → `/executions/{execution_id}`. It cannot record a consent or mint a pairing, and every privileged step is its own press — `docs/voiceos.md` is the contract, `tests/unit/test_voice.py` pins it |
| GET | `/manifest.webmanifest`, `/sw.js`, `/icon.svg` | Installable-app shell assets |
| POST | `/plan` | **Propose** candidate actions from natural-language intent (C18) |
| POST | `/voiceos/messages` | One VoiceOS turn: intent, conversational references and a proposed task session. It never runs anything — the answer carries `ran: false` always (C34) |
| POST | `/voiceos/guide` | Which of a caller-supplied list of example phrasings this deployment's planner would actually route. A **measurement**, not a promise: each phrase runs through the same planner a real turn uses. Proposes nothing, authorizes nothing, runs nothing (C41) |
| GET | `/voiceos/conversation` | This actor's conversation context: history, active session, and the observed items a later turn may refer to |
| GET | `/voiceos/status` | Whether the next turn is answered by a model or by the deterministic planner: `mode` is `ai_model` or `deterministic`, with a `mode_label` for the UI chip and a `reason` when a configured provider could not be used. `state` is the shell's last verified connection state (empty when there is no bridge). **`configured` is not `usable`**: a provider whose key the provider rejected still reads configured and implemented, and a refusing `state` (`missing_key`, `invalid_credentials`, `rate_limited`, `timeout`, `provider_unavailable`, `malformed`) forces `mode: deterministic` with the shell's own sentence as the reason. A built provider is not a proven one — `/voiceos/messages` returns the per-turn note that says what actually happened (C41) |
| GET | `/voiceos/sessions` | Recent task sessions — only the caller's own; `limit` is 1–100 |
| GET | `/voiceos/sessions/{session_id}` | One session with its steps and what was observed. 404 for a session that is not yours |
| POST | `/voiceos/sessions/{session_id}/run` | Advance the session one authorised step. Every step is an ordinary `/execute` under the caller's own credential, so a parked step stops here and asks |
| POST | `/voiceos/sessions/{session_id}/cancel` | Ask ActionMesh to stop the live step. Answers `requested` (or `nothing_running`); the record confirms what actually stopped |
| POST | `/voiceos/sessions/{session_id}/diagnose` | Explain the log this session's `device.process.logs` step actually read. Redacts and bounds the excerpt before anything leaves the machine, and answers `unavailable` — with the evidence, not a guess — when no analyst is configured |
| POST | `/automations/draft` | Read a sentence into a **draft** for review (C39). Nothing is stored and nothing runs: `created` is always `false`, the steps can only come from an operator template, and an unmatched sentence gets a trigger plus the reason it has no steps |
| GET | `/automations` | The library — **only the caller's own**, with the local ticker's status and a note when automations are in memory only and will not survive a restart |
| POST | `/automations` | Save a plan. It grants nothing and starts nothing; a body carrying `grants` or `permissions` is refused. Every step is authorised again when it runs |
| GET | `/automations/{workflow_id}` | One automation. 404 for one that is not yours |
| PUT | `/automations/{workflow_id}` | Replace a plan, validated exactly as a create is. The stored owner is kept; an edit cannot transfer it or rewrite the last run's outcome |
| DELETE | `/automations/{workflow_id}` | Remove a plan. Run history is kept — it is a record of what happened, not a child of the plan |
| POST | `/automations/{workflow_id}/enabled` | Arm or disarm. Disabling stops future occurrences; it does not kill a run already in progress |
| POST | `/automations/{workflow_id}/run` | Press run: one fresh session under the **caller's own** credential, so every step goes through consent, risk and the approval gate again. A previous approval does not carry over |
| GET | `/automations/status` | Whether the in-process ticker is alive, its grace window, and the closed list of event types that can trigger a run |
| GET | `/automations/runs` | Automation history — scheduled, event and manual runs, including `missed` occurrences that were recorded rather than run late. Optional `workflow_id` |
| POST | `/execute` | Validate and execute a graph |
| GET | `/executions` | Recent execution records — **only the caller's own**; `limit` is 1–100 |
| GET | `/executions/{execution_id}` | One record. 404 for an execution that is not yours, so the answer cannot confirm which ids exist |
| POST | `/executions/{execution_id}/cancel` | Request cancellation of your own execution. Answers `status: "requested"`. A provider call already in flight is interrupted, and the executor awaits that call's own cleanup before it reports the run; a run that is between steps stops at the next boundary. The run's real state comes from the record or the stream |
| POST | `/approvals` | Submit an approval decision keyed by `action_id`; with `execution_id`, resumes a waiting run. **A device may decide only its own waiting actions** — 403 otherwise, 404 for an id it has none of |
| GET | `/approvals` | Executions stopped for a human, with the action ids they await (C19) — **only the caller's own**, because an approval entry carries the action's parameters |
| POST | `/consents` | Record a consent **server-side** — the only way to grant a permission. A paired device may omit `actor_id` and the server takes it from the credential; naming another actor is the operator's act |
| GET | `/consents` | List consents. A device omits `actor_id` (or repeats its own); the operator must name the actor it means |
| DELETE | `/consents/{scope}` | Withdraw a standing consent. Revoking is the safe direction, so it is not gated like granting — but the actor is still named: a device withdraws only its own, an operator says whose, and a scope two actors hold keeps the other's grant |
| GET | `/capabilities` | Discover registered capabilities; optional `provider` filter |
| GET | `/devices` | Registered devices, with presence derived from heartbeats (C19) |
| GET | `/devices/{device_id}` | One device record; 404 when unknown |
| POST | `/devices/{device_id}/revoke` | Forget a device **and** revoke the identity bound to it. A paired device may do this only to itself; any other name answers the same 404 as an unknown one |
| POST | `/pairing/code` | Mint a pairing code for an enrolled laptop — **loopback only** |
| POST | `/pairing/revoke` | Burn every outstanding code — **loopback only** |
| POST | `/pair` | Spend a code: binds a phone identity and returns its token, once |
| GET | `/metrics` | Process counters |
| GET | `/audit` | Audit chain records — **only the caller's own**; the chain verdict and head are shared |
| GET | `/audit/verify` | Recompute the hash chain and report where it breaks |
| GET | `/events` | SSE stream; optional `execution_id`, `ticket`, resumes on `Last-Event-ID` |
| WS | `/ws/events` | WebSocket event stream; optional `execution_id`, `ticket`, `last_event_id` |
| GET | `/events/stream-ticket` | Exchange a device credential for a short-lived, watch-only stream ticket |

## `POST /plan` — proposing, not deciding

The only route that accepts model-shaped input. It returns a **proposal**:

```json
{
  "intent": "tell mom I'll be late then pay alice 20",
  "actions": [
    {"type": "message.send", "parameters": {"to": "mom", "message": "I'll be late"}},
    {"type": "payment.prepare", "parameters": {"recipient": "alice", "amount": 20.0}}
  ],
  "notes": []
}
```

Each action carries **only** `type` and `parameters`; a proposal has no field for
risk, permissions, dependencies or lifecycle state. Anything the planner could not
map is reported in `notes` rather than silently dropped, and an empty or
non-string intent is a 400. **Calling this route runs nothing.**

`parameters` is checked against the resolved action's own `input_schema`, and the
check is fail-closed: a required field that is missing, a value of the wrong type,
and a parameter the action **does not declare** are all rejected. The last one is
deliberate. A capability that declares `{"to", "message"}` must not accept
`{"to", "message", "command": {...}}` — the extra value would still be bound into
the call when the action dispatches. A caller that wants to pass more must register
an action that declares it.

`POST /execute` expects a JSON object containing `graph`. The optional `actor`,
`policy`, and `allow_high_risk` fields become a typed policy context. Permission
grants are never accepted from this request: call `POST /consents` first so the
server-side `ConsentStore` can record them. The graph and every embedded action
are validated at the API boundary; caller-supplied lifecycle state and action
identity are not trusted.

Example policy context (note: there is deliberately **no** `grants` key — see
`POST /consents`):

```json
{
  "actor": {"id": "user-1"},
  "policy": {
    "rules": [{"action": "message.send", "decision": "allow"}]
  }
}
```

Consent example:

```json
{
  "actor_id": "user-1",
  "scope": "message.send",
  "max_risk": "high",
  "reason": "approved messaging workflow"
}
```

Unknown actions, malformed graphs, invalid enums, injected grants, and invalid
request shapes are rejected with HTTP 400. Missing or insufficient authorization
is represented by the executor's normal denied result, not by bypassing the
authorization core.

## Two gates, both required

A consent is necessary but **not** sufficient. `PolicyEngine` matches active
grants first, then selects a rule, then falls back to `policy.default_decision`,
which is **DENY** when nothing matches. A request that omits `policy` is therefore
denied even when a valid server-side consent exists.

That is measured, not theoretical: an earlier revision of the dashboard planned
and then executed with no policy, and every plan came back `denied`. The correct
shape is a policy rule for the action **plus** a recorded consent.

The browser may declare what the workspace is willing to consider. It may never
hand the server a permission — grants come from `POST /consents` only, and
CRITICAL risk forces an approval regardless of both gates.

## Devices (C19)

A device record carries the metadata a control plane needs to name a machine —
`device_id`, `name`, `kind`, `platform`, `os_version`, `agent_version`,
`capabilities`, `online`, `last_seen` — and **no address**. There is no `host` or
`ip` field because an address is not an identity: it changes per network and two
laptops on one Wi-Fi look identical.

`kind` is one of `phone`, `laptop`, `server`, or `unknown`. `unknown` is the
fail-closed default, and a device that has not declared a role cannot be enrolled
at all. `online` in a response is **derived from the last heartbeat**, never the
flag the device asserted, so a record left claiming `online = true` after the
machine went away reads as offline.

The listing also omits `identity_id`. That handle is how the server attributes an
action; a client names a device by `device_id`.

**There is no registration route and no heartbeat route, and that is the
security property.** A device announces itself through the local agent, and a
phone is admitted only by pairing. Were a client able to register a device over
HTTP — no such route exists — the first unauthenticated step of using the port
would be minting the principal every later audit record trusts. Revocation is the
one HTTP mutation the device surface exposes, because cutting a stolen phone off
has to be reachable from the machine it controlled.

## Capabilities the agent registers (C19)

With `ACTIONMESH_AGENT=1`, `GET /capabilities` also lists `device.*` actions — but
**only the ones the configuration opened**. A capability whose boundary is closed is
not registered at all, so it fails at the registry as an unknown action rather than
reaching a policy decision, an approval prompt, or a device. That is the strongest
place to fail, and it is why `ACTIONMESH_WORKSPACE_ROOT` and
`ACTIONMESH_ALLOW_SHELL` gate capability *existence*, not merely execution.

Two reporting rules the device actions follow, because a control plane that breaks
them is just a remote desktop with extra steps:

* `device.server.status` re-probes the declared port on every call. A server that died
  after starting is reported as stopped, not replayed as the launch that succeeded.
* A refused start refuses **before** the child exists. A spawn rejected after it ran
  would leave a process holding a port with nothing that can name it — and the only
  way to stop it would be to guess its pid.

## Pairing (C19c)

Reaching port 8000 is not admission. A pairing code is the ceremony that admits a
phone: the **laptop produces it, the phone spends it**.

```json
POST /pair            {"code": "742-381", "device_id": "phone-1", "actor_id": "abhi"}
```

The response carries the device token **once** — there is no route that reads a
credential back — alongside the identity, actor, and expiry it was bound to:

```json
{
  "device_id": "phone-1",
  "actor_id": "abhi",
  "credential_id": "cred_…",
  "token": "…",
  "max_risk": "medium",
  "expires_at": "2026-11-06T12:00:00+00:00"
}
```

Minting a code is restricted to a request that arrived over loopback, because a code
is the thing a human reads off the laptop's screen; a caller on the other side of the
room who can mint one has removed the human from a ceremony whose whole job is to
include one. `ACTIONMESH_ALLOW_REMOTE_PAIRING` widens it deliberately, for a laptop
reachable only through a relay you control. Spending a code is never loopback-restricted —
that would defeat the point.

Six digits is barely 20 bits, so the length is not the protection. A code lives five
minutes, is single-use, is one-at-a-time per laptop, and every redemption attempt —
including malformed ones — counts against a window of 20 per minute. Exceed it and
every live code is revoked and pairing locks for five minutes; **minting is locked
too**, so the recovery path is to wait and ask again, and the honest cost of that is
five minutes of inconvenience against an unbounded guessing budget. The bound an
attacker gets is roughly 100 guesses per 10⁶-code space (**about 1 in 10,000**) before
the lockout trips. There is deliberately **no per-code attempt counter**: codes are
stored as keyed digests, so a wrong guess cannot be attributed to the code it was
aimed at. A limit that cannot be enforced is worse than none, because it reads like
protection.

Two rules about what pairing does *not* do:

* **A paired phone gets no permissions.** The credential carries an identity, an
  actor, and a **risk ceiling chosen when the code was minted** — `low` by default, up to
  `critical` if the person at the laptop asks for it — and never a scope list. The
  redeeming phone cannot set or raise `max_risk`; `POST /pair` has no such field. What the
  phone may later ask for comes from `POST /consents` and the policy engine, like any
  other actor. Below `critical` a phone cannot reach `device.terminal.execute` at all,
  because that capability is CRITICAL by definition and risk can only be raised, never
  lowered.
* **Revoking a device invalidates its credentials.** Authentication resolves through
  the identity store, so `POST /devices/{device_id}/revoke` is enough to cut a stolen
  phone off — you do not have to find and revoke each token first.

A request may carry `Authorization: Bearer <token>`. When it does, the actor is the
credential's actor and the request body cannot name a different one; the shared API
key, when configured, is still required. An unknown, expired, or revoked token all
answer `401 unauthorized` with no detail, because which-one-it-was leaks that a
guessed credential is real.

The key material behind all of this comes from `ACTIONMESH_SECRET_KEY`, else a
`0600` file at `ACTIONMESH_SECRET_FILE`, else an ephemeral random key. With an
ephemeral key a restart unpairs every device, so `/pairing/code` answers
`pairing_durable: false` rather than letting you discover it later.

## Realtime event stream (C19d)

Both transports emit the same `MeshEvent` objects the audit chain seals, so what a
phone watches live is the same record it can read back afterwards. Each SSE frame
carries an `id:` line equal to the event's `event_id`.

**Event types.** `execution.started`, `node.started`, `node.completed`,
`execution.progress`, `execution.output`, `approval.required`, `authorization.decided`,
`execution.completed`, `execution.denied`, and `compensation.{unconfirmed,completed,refused}`.
A failed node is a `node.completed` whose `state` is `failed`; a failed execution is an
`execution.completed` whose `state` is `failed` — there is no separate `*.failed` event,
because the state field already carries the distinction and a second name would only be
a second place for the two to disagree.

**Incremental output (`execution.output`, C20).** While a laptop terminal command runs,
its stdout/stderr stream to the authorized observer as `{node_id, device_id, stream,
sequence, chunk, truncated}`. These events are **transient**: they reach only
subscribers connected at that moment, are never sealed into the audit chain, and are
never kept in the replay history — a control plane records decisions and outcomes, not a
copy of whatever a program printed. They are bounded (≤ 4 KiB per chunk, ≤ 64 KiB per
node, then a `truncated` flag) and actor-scoped, so a watcher sees output only for an
execution it could already watch. A phone that reconnects mid-command recovers the
authoritative execution state via `GET /executions/{id}` and is told plainly that
earlier output is not replayed. Output is treated as potentially sensitive: streaming it
does not mean ActionMesh has checked it for secrets — a command can print its own
credentials — so it is live-only, to the authorized observer, and never audited.

Every event has `event_id`, `event_type`, `timestamp`, `execution_id`, `action_id`,
`actor`, and a `payload`. Payloads are **metadata only**: `node.completed` and
`execution.progress` never carry a provider's raw result. For a terminal command that
means no stdout/stderr; for a file read it means no file contents. The full per-node
result view (state, provider, `verified`, `duration_ms`, and a safe projection of the
payload) is on `POST /execute`'s response and `GET /executions/{id}`, filtered through
the same allowlist.

**Inventory rows (C27b).** Six keys — `processes`, `servers`, `projects`, `editors`,
`services`, `entries` — are lists of *rows* rather than payloads, so each row is
re-projected through the same allowlist: only allowlisted **scalar** fields survive, a
string must still be under 120 characters, and a list or dict inside a row is dropped
rather than summarized. One level deep, never two. The row count is capped at 20. Every
other key keeps the old rule: a list becomes `{key}_count` and is never walked. This is
what lets a phone see which process has which issued `process_id` — the only handle by
which it may be stopped — and what a directory listing looks like at all.

**A file body travels as output, not as a result (C28).** `device.file.read` still puts
nothing in the result payload; the safe view strips `content` exactly as before. What
changed is that the provider now emits the bytes it read onto the transient
`execution.output` channel with `stream: "content"`, which was built for exactly this:
live, bounded (4 KiB per chunk, 64 KiB per node), actor-scoped, never audited and never
replayed. So the phone that asked to read a file sees it, the audit trail never does, and
no new route, scope or authority check had to be invented to get there. A binary file
emits nothing at all — a PNG into a `<pre>` is noise wearing someone's filename.

**Which limit stopped a run.** A terminal result carries `timeout_requested_s`,
`timeout_applied_s` and `timeout_clipped`, and a detached process or server record
carries `output_captured`. Both are there because a bound that is not stated reads as a
hung program. The ceiling a command is actually given is the agent's own
(`ACTIONMESH_COMMAND_TIMEOUT_S`), and a caller's `timeout_s` can only lower it, never
raise it: a run stopped at 60s after asking for 600s has to be able to say that the
laptop declined to wait, rather than leave the reader blaming the command.

**Captured logs (`device.process.logs`, C29).** `output_captured` is `false` unless the
operator set `ACTIONMESH_AGENT_LOG_DIR`, and `true` when a launched child is writing to a
`0600` file the agent owns. A file and not a pipe, deliberately: a pipe nobody drains
fills at ~64 KiB and the server blocks in `write`, so a reader that died with its request
would stop the user's machine — a file applies no backpressure, and the log is bounded
afterwards by trimming its front (512 KiB) on every pass over the process table. Reading
it is an ordinary capability — `device.process.logs`, addressed by the issued
`process_id`, never by a path — and its bytes come back over the same transient,
actor-scoped, never-audited channel as a file body, as `stream: "logs"`. The result
payload carries only `{process_id, bytes, lines, truncated}`.

**Scoping.** A stream is pinned to an actor, and only narrows from there:

* a request that presents a **device credential** (`Authorization: Bearer`) — or a
  **ticket** minted from one — receives only events whose `actor` matches;
* `execution_id` and `Last-Event-ID` narrow further, and can never widen the set;
* no credential and no ticket means the **local operator's** view (everything), which
  is the trusted case; when `ACTIONMESH_API_KEY` is set the HTTP middleware rejects
  such a request before it reaches the stream, so a protected port is never anonymous.

**The same rule covers the read surface (C24).** `GET /executions`,
`GET /executions/{id}`, `GET /approvals`, `GET /audit` and
`POST /executions/{id}/cancel` resolve their caller through the one function the stream
already used, and filter or 404 by it. A record is no less sensitive than the events
about it: it names the actions run, the paths touched, the states reached — and an
approval entry carries the action's **parameters**, which is the message body or the
command line. Left unscoped, a phone that could not *watch* another device's execution
could simply fetch it, and any paired device could stop another device's run. A remote
caller with no credential is denied on every one of them, as on the stream; losing a
token can never widen what you can read.

**What a cancellation reports (C31).** `POST /executions/{id}/cancel` sets the flag and
interrupts the provider call the executor is awaiting, then awaits *that* call's own
cleanup before it records anything — so when a reader sees `cancelled`, the command's
process group has already been taken down. It cannot make the past untrue: the action
*was* dispatched, so the node comes back `cancelled` with `verified: null`, an error
that names the effect as unconfirmed, `compensation_required` set and a
`compensation.unconfirmed` event. A cancelled run never reads back as a success and is
never silently retried. A genuine outer cancellation — the server shutting the task
down, not a user pressing Cancel — still propagates instead of being swallowed as an
ordinary stop. Measured from the dashboard: a 60s streaming command cancelled at tick 97
stopped printing at tick 97, `duration_ms` 24309, and both the parent and the grandchild
pid were gone.

**And the decision surface (C25).** `POST /approvals`, `POST /consents`,
`GET /consents` and `POST /devices/{id}/revoke` resolve their caller the same way,
because these are not reads -- they are the guarantees. Before it, any paired phone
could approve another actor's parked action and be handed back that actor's resumed
execution record, could record a consent naming someone else's actor, and could revoke a
device it does not own. Approving is the human-in-the-loop, so *whose human* is the
whole question; the server now answers it, in the same 403/404 shape as everywhere else.

Because `EventSource` cannot send an `Authorization` header, a paired phone calls
`GET /events/stream-ticket` with its bearer token and gets back a random, watch-only
ticket bound to its actor that expires after ~2 minutes. The ticket is reusable until
it expires (so a reconnecting stream can re-present it) and grants nothing except the
right to read the stream — it is not the device credential and never appears in a URL
for any other purpose.

**Reconnect.** `EventSource` resends `Last-Event-ID` automatically; the server replays
only the retained events after that id from its bounded history, then resumes live. If
the id has aged out of the window the whole retained window is replayed instead — a
duplicate the client de-duplicates by `event_id` is safer than a silent gap. After
reconnecting the dashboard also re-reads `GET /executions/{id}` so its view of a running
execution is reconciled against the server's authoritative state, not the last event it
happened to receive.

**Size.** A serialized event larger than 16 KiB has its `payload` replaced with a
`{"truncated": true}` notice; the identity fields survive so the client can still
correlate it. Subscriber queues and retained history are bounded, and a subscriber that
cannot keep up is disconnected rather than allowed to grow the process.

**Events are observational.** Receiving `approval.required` authorizes nothing; granting
still goes through `POST /approvals` and the server-side consent path. No event payload
can carry a grant, a permission, or a risk decision.

## Durability

The default wiring is in-memory. Realtime SSE and WebSocket are implemented (C9),
not "later phases". Set `ACTIONMESH_AUDIT_DB` to persist the audit chain to SQLite,
where it reopens **fail-closed**, and `ACTIONMESH_AUDIT_ANCHOR` to add truncation
detection — see [`audit.md`](audit.md). No endpoint talks to a real provider.

## Automations (C39)

An automation is a **stored plan**, not a standing permission. `POST /automations`
writes a workflow and starts nothing; the body has no field a grant could live in, and
one that carries `grants` or `permissions` is refused outright. Every step is
authorised afresh on every run — against the caller's credential for a manual press,
and against **no grants at all** for a scheduled or event run, because nobody is
present to consent. A workflow that needs a permission therefore fails at 09:00 with
`missing permission(s): [...]` rather than inheriting yesterday's approval.

### What the ticker does, and does not, do

`GET /automations/status` reports the single in-process `asyncio` ticker. There is no
cloud scheduler, no daemon and no queue: a laptop or desktop running this program is
the whole scheduling infrastructure. Two durable mechanisms make an occurrence run **at
most once**, including across a restart:

- `next_run_at` is advanced as part of the same step that claims the occurrence, so a
  workflow with a claimed run is no longer due.
- The run row is unique on `(workflow_id, slot)`, where the slot is the due time. The
  claim is an `INSERT OR IGNORE`; only the caller whose insert took effect proceeds.

An occurrence claimed by a process that died before starting it is reconciled at
startup as **`missed`** — one row, never re-run, with the reason recorded — because a
replay would be a run at a time nobody chose. An occurrence more than `missed_after_s`
late (default 15 minutes) is likewise recorded `missed` with the lateness stated. A
laptop that slept through 09:00 and woke at 15:00 is told so; it is not handed the
morning's job six hours late.

`triggerable_events` in the status and list responses is a **closed list** of the event
types that may start a workflow (`execution.completed`, `execution.denied`,
`execution.device_refused`, `execution.cancelled`, `execution.interrupted`). A
workflow may optionally narrow one of them by `state`, which is the executor's own
verdict — "when the build fails" is `execution.completed` narrowed to `state: failed`,
not a fabricated `execution.failed` event. Matching never reads an event payload: a
provider's output cannot choose what runs next. An event starts a workflow only for
that workflow's own owner.

### Choosing a computer

A step runs on the computer it names. Where the plan names none, the manual run route
supplies **one** reading: the press came from this machine, so the target is the same
declared-local-or-single-online device a VoiceOS conversation with no selection would
use. That answer is never a guess — several online computers, or none, leaves the step
unaimed and the orchestrator refuses it by name. A **scheduled** run supplies no
default at all: a tick has no operator behind it, so an untargeted plan is refused and
reported rather than aimed.

If the named device is offline the step is reported unavailable and **the action is not
sent to any other computer**. `default_device_id` is a fallback for a step that names
nothing; it never overwrites one that names a device.

### Drafts

`POST /automations/draft` reads a sentence into a proposal for review. It always returns
`created: false` and stores nothing. Its steps can come only from an operator-authored
template (`ACTIONMESH_VOICEOS_TASKS`); with no matching template the draft has a
recognised trigger, no steps, and a stated reason. A model is never asked to emit a step
— only, at most, to recognise a schedule — so "create a workflow from this sentence"
cannot introduce an action that the operator did not configure.

## `POST /voiceos/guide` — what the guide measures

The Voice screen lists example phrasings, and the right-hand column beside each one
used to be a hand-typed "supported". It was wrong: on a service with no laptop agent,
several listed phrases matched no rule at all, the page said supported, and pressing
one answered "I could not safely map that". A label nobody measures is a claim.

This route makes it a measurement. The caller sends its own list of phrases — the
page sends the ones it is about to display, so there is one list in the product and
not two that drift — and each is run through the **same** planner object a real turn
uses (`app.state.planner`, with the same registry filter):

```json
{
  "phrases": [
    {"phrase": "tell mom I'll be late", "actions": ["message.send"], "notes": []},
    {"phrase": "show my projects", "actions": [],
     "notes": ["dropped unregistered action type 'device.project.list'"]},
    {"phrase": "book me a table", "actions": [], "notes": ["no rule matched: 'book me a table'"]}
  ]
}
```

The three outcomes are distinct on purpose, because they call for different advice:

- **`actions` non-empty** — the planner routes it, and these are the action types it
  lands on. The page renders that list.
- **`dropped unregistered action type …`** — the *wording* was understood; this build
  does not register the capability that would carry it out. The user is told to enable
  the laptop agent, not that their sentence was unparseable.
- **`no rule matched …`** — no rule recognised this wording. When `/voiceos/status`
  reports `mode: ai_model`, the page softens this to "no built-in rule; the configured
  model may still interpret it", because a live model is a second proposal source —
  and it is still honest, because the guide cannot ask the model without spending a
  call.

Phrases are untrusted body text like any other: bounded to `GUIDE_MAX_PHRASES` (40)
entries of `GUIDE_MAX_PHRASE_CHARS` (200) characters, blank entries refused with a
400, and each string echoed back only as the string that was sent. **The route
proposes nothing, authorizes nothing and runs nothing** — it creates no session and
no execution, which `tests/unit/test_command_guide.py` asserts directly.
