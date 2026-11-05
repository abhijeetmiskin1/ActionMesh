# Phase 6 — Functional Delivery Record

**Date:** 2026-11-05
**Scope:** wire the Phase 6 desktop modules into the one existing application and
prove the five journeys through it.
**Not in scope:** new engines, new approval systems, a second desktop shell.

Every claim below is labelled with the kind of evidence behind it. The four
labels are kept distinct on purpose:

| Label | Means |
|---|---|
| **LIVE** | Observed against a running backend / real OS surface in this session. |
| **INTEGRATED** | Proven by driving the real page scripts through the DOM harness against a live backend. Wiring and behaviour, not pixels. |
| **FIXTURE-TESTED** | Proven only against test doubles. |
| **BLOCKED** | Attempted, could not be done honestly in this environment. |

A DOM harness proves integration. It never proves rendering. Where this document
says the interface works, it means the scripts and controls do — not that a human
looked at a screenshot. Graphical acceptance is **BLOCKED**; see §6.

---

## 1. What was actually wired

`index.html` loaded nine classic scripts in one shared global lexical scope:

```
voice.js → onboarding.js → history.js → activity.js → workbench.js
→ connections.js → savedtasks.js → onboarding_flow.js → app.js
```

An audit of the call sites (not merely of the `<script>` tags) found four of the
five named modules already initialised exactly once from `boot()`:

| Module | Call site | State |
|---|---|---|
| `workbench.js` | `app.js` `boot()` → `ActionMeshWorkbench.initWorkbench` | wired, 1× |
| `activity.js` | `app.js` `boot()` → `ActionMeshActivity.initActivity` | wired, 1× |
| `connections.js` | `app.js` `boot()` → `actionmeshConnections.initConnections` | wired, 1× |
| `savedtasks.js` | **deliberately dormant** | see ruling R1 |
| `onboarding_flow.js` | **nothing called it** | defect — fixed below |

### The one genuine defect

`onboarding_flow.js` published `window.ActionMeshFirstRun.renderFirstRun` and was
referenced by `index.html`, but no production path ever called it. It passed
every "is it referenced" check and was still dead code.

`#onboardingMount` is owned by the *other* module (`onboarding.js`, the boot
wizard), so the guided tour could not reuse it. It was given its own host:

- `desktop/ui/index.html` — a `Guided tour` button (`#firstRunOpen`) in the
  Settings App card, and `<div id="firstRunMount" hidden></div>` beside
  `#onboardingMount`.
- `desktop/ui/app.js` — `openFirstRun()` / `closeFirstRun()` after `showView`,
  bound once via `on('firstRunOpen', 'click', …)` and exported on
  `window.ActionMeshApp`.
- `desktop/ui/styles.css` — `#firstRunMount` overlay at `z-index: 41` (one above
  the boot wizard's 40) plus its `[hidden]` rule.

Four properties the wiring had to get right, each pinned by a test:

1. **Single mount.** Pressing the button twice reuses the controller instead of
   appending a second copy of the panel.
2. **Finished ≠ live.** The module's own `complete()` hides its root and the
   mount without telling `app.js`. A re-open after Finish detects the stale root
   and re-mounts rather than showing an empty overlay with dead controls.
3. **Closing is not completing.** The module offers Back / Skip / Next / Finish;
   none of those is "go away without deciding". Escape and a backdrop click close
   it and write **no** setting. `onboardingCompleted` is written only by the
   module's Finish.
4. **Init failure cannot crash boot.** A throw inside `renderFirstRun` is caught,
   the mount is hidden, and a toast reports it.

The module is mounted **without** `initiallyCompleted`, deliberately: that option
hides the panel for a returning user, which is correct for a boot overlay and
wrong for a button a person just pressed. The button *is* the request.

---

## 2. Milestones

| # | Milestone | State | Evidence |
|---|---|---|---|
| M1 | Phase 6 modules load | **INTEGRATED** | 9 scripts evaluate in one shared scope; every module publishes its surface; `boot()` reaches the shell without a refusal artefact. `integration_boot.test.js`, `live_gui_path.js` |
| M2 | Workbench plans and executes | **INTEGRATED + LIVE** | Real `/plan`, real `/execute`. Ungranted ⇒ `denied` naming `file.read`, `verified` absent; after granting from the Approvals view, `simulated:false`, `verified:true` via `observed_state`, card reads "Succeeded and verified." |
| M3 | VoiceOS completes a typed turn | **INTEGRATED + LIVE** | `POST /voiceos/messages` returns `taskKind: taskPlan`; reply renders `Nothing ran. Proposing is not executing.` |
| M4 | Five journeys reachable | **INTEGRATED** | 13 nav entries, 13 matching `section.view[data-view]` containers; each journey driven end-to-end |
| M5 | Release validation | **PARTIAL** | Python + desktop suites green, fresh `.deb` built and verified from this tree; graphical acceptance **BLOCKED** |

M5 is not marked complete: M1–M4 are recorded, but §6's graphical gate could not
be met honestly in this environment.

---

## 3. The five journeys

All five were driven through the real page scripts (the test harness loads the
same nine files `index.html` declares, in the same order, and boots `boot()`).

### A — Dashboard / Activity — **INTEGRATED**

Activity reads the live audit surface and reports what it finds: chain verified,
record count, head hash, and individual records. Observed on the final run:
`chain verified`, `154 records · head 3e3578359fc4590e`, and the list naming the
execution that had just run (`graph_2ec59870440f`). It does not claim a browser
or a media session it does not have.

### B — Workbench — **INTEGRATED + LIVE**

Typed instruction → `/plan` → rendered plan naming the real action type
(`device.file.read`) and the permission it will need (`file.read`) → execute →
terminal state.

The plan card states `decision … decided at execute`, because `/plan` does not
carry a decision and the workbench must not invent one. This is the policy
engine's authority being respected at the presentation layer.

**The refusal half.** The journey runs the same instruction twice, and the
difference between the runs is the point: if the first run had succeeded, the
consent store would not be deciding anything.

```
run 1 — no standing permission for file.read
  → state: denied
  → "graph nodes not authorized: n0 (n0: missing permission(s): ['file.read'])"
  → no node reports verified:true; the card does not read "Succeeded and verified"

then, from the Approvals view's own controls (#consentScope, #consentRisk,
#consentGrant) the user grants file.read up to high-risk

run 2 — same plan, same instruction
  → state: success, simulated: false
  → verified: true via verification.method "observed_state"
  → card reads "Succeeded and verified."
```

Two independent authorization sources are at work here, and the journey proves
the second: a plan can be policy-legal and still be refused because no
*consent* exists for the actor. `POST /plan` carries no decision, so a client
cannot pre-authorize itself.

Separately verified against the live API this session:

```
POST /plan     {"intent": "list the files"}
  → [{"type": "device.file.list", "parameters": {}}]

POST /execute
  → state: success, simulated: false
  → n0: provider "laptop", attempts 1, verified true
        verification.method "observed_state"
        detail "confirmed by observed_state: observed state matches"
  → result carries name/size/type/confined only — no file content
```

`simulated:false` and `verified:true` are separate facts and are rendered as
separate facts. Invariant 4 held: file *content* never crossed the result
boundary.

### C — VoiceOS — **INTEGRATED + LIVE**

The typed-text path is `voice.js` + `app.js` `propose()`, not a separate module —
see ruling R2. Send is bound exactly once; typed text reaches
`/voiceos/messages` and renders a reply whose kind is `taskPlan`. The reply says
`Nothing ran. Proposing is not executing.` — a proposal is never dressed as a run.

### D — Connections — **INTEGRATED + LIVE**

Reports the backend as running, and reports Browser/Media as unavailable or not
configured rather than inventing a connection. It offers no fake pairing, no fake
playback, no fake browser connectivity.

### E — Saved Tasks — **INTEGRATED + LIVE**

Draft → real planner → "not saved" → Save → fresh read shows it in the library →
reopen shows the stored plan → Run now → honest terminal state.

A saved task is **stored disarmed**: it carries `every day at 09:00` and `1
step(s)`, and is marked `disabled`. The trigger is *stored*, not *armed*. The
detail view says so explicitly — *"It is disabled, so no schedule or event will
start it"* — and shows no due time, because a disarmed plan has none.

The draft-save seam posts `draft.proposal` verbatim; the edit-save seam emits
`enabled: Boolean(t.enabled)` and preserves stored state. These are two different
seams and both are covered.

---

## 4. The guided tour

Reachable from Settings. It walks the real screens and performs no I/O until a
button inside it is pressed. Its first step states that it **grants nothing**.
It is not one of the five required journeys; it is the module §1 named that was
not wired.

---

## 4b. The executor rejection defect, fixed

Found and fixed this session, with red-green evidence.

**Symptom.** An approval that the user *rejected* left the run parked at
`WAITING_PERMISSION` forever. `POST /approvals {status: "rejected"}` re-evaluated
to the same "requires approval", so it was a no-op: the execution stayed listed
as needing an answer nobody could give. A refusal is a decision, and the engine
was treating it as the absence of one.

**Fix.** `actionmesh/core/executor/engine.py` (+53/−1) now checks the recorded
approval for that `action_id` before re-evaluating. A rejection ends the run at
that node as `DENIED` with `error: "rejected by the user"` and
`failure_kind: AUTHORIZATION` — which keeps it out of the retry/escalation path,
because "no" means nothing is retried. The error message is carried onto the
final result instead of being flattened to `denied: a1`, so the record still says
*who* refused and why.

The rule is scoped to the exact sentence a rejection produces, deliberately. An
`AUTHORIZATION`-kind failure is not automatically a refusal: a laptop declining a
command it will not run (`rm`, say) is a *capability* refusal, and re-labelling
that `DENIED` would collapse "the human said no" into "the machine could not" —
the distinction this platform exists to keep.

**Red-green.** The existing test `test_rejected_approval_still_blocks` asserted
the defect (`WAITING_PERMISSION`, provider not called). It was replaced, not
merely supplemented, by two tests that assert the corrected behaviour:

- `test_rejected_approval_ends_the_run_as_a_refusal`
- `test_rejecting_a_parked_run_resumes_it_to_denied`

`tests/unit/test_executor.py` is +33/−3. The cycle was re-run to confirm it, by
restoring `HEAD`'s engine (the pre-fix version) and running only those two tests:

```
FAILED test_rejected_approval_ends_the_run_as_a_refusal
FAILED test_rejecting_a_parked_run_resumes_it_to_denied
2 failed, 50 deselected in 0.12s
  AssertionError: <ActionState.WAITING_PERMISSION> is not <ActionState.DENIED>
```

With the fixed engine restored they pass, and `engine.py` hashes back to
`93a1ed2a…`. The 10-minute expiry
(`expires_at = now + 10min`, stamped only on `status == "approved"`,
`actionmesh/api.py:1542-1544`) and the secret redaction both still pass their
tests unchanged — the fix touched neither.

### The acceptance environment

The evidence above was produced against a real backend running from:

```
.demo/phase6/env.sh      ACTIONMESH_AGENT enabled, workspace root .demo/phase6/workspace,
                         shell deliberately closed, SQLite stores under .demo/phase6/
.demo/phase6/workspace/note.txt      "phase 6 acceptance note"
.demo/phase6/tasks.json  operator template → device.files.list {path: "."}
```

Without `ACTIONMESH_AGENT` the agent never registers and `/plan` silently drops
every `device.*` action — which is why an acceptance run against a backend
started without it looks hung rather than refused.

### Two API shapes worth knowing

`GET /approvals` returns a **JSON list**, not an object — code that assumes
`.get()` crashes. `GET /consents` requires `actor_id`; an operator must name the
actor it is recording consent for, and a device may only record its own
(`403 a device may only record its own consent`).

`safe_result_view` is a strict allowlist, and `event_id` is not on it — a
calendar event's identifier never crosses the result boundary. Any test that
needs to address a created event must use the deterministic id the mock assigns
(`evt_0001`, `evt_0002`) rather than reading it back from the result.

---

## 5. Verification runs (fresh, this session)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests -q` | **2032 passed, 22 skipped, 894 subtests passed** — exit 0 |
| `.venv/bin/python -m pytest tests/security -q` | **223 passed, 41 subtests passed** — exit 0 |
| `.venv/bin/python -m mypy actionmesh` | **Success: no issues found in 109 source files** — exit 0 |
| `.venv/bin/python -m ruff check actionmesh tests scripts` | **All checks passed!** — exit 0 |
| `cd desktop && npm test` | **200/200 pass** — exit 0 |
| `node test/live_gui_path.js` | **65/65 checks passed** — exit 0, twice consecutively |

`live_gui_path.js` is worth naming precisely: it loads the same nine scripts
`index.html` declares, boots the page, and drives every journey against the
**running backend** — plan, execute, dashboard, audit, voice, connections, saved
tasks, and the tour. It is integration evidence, not fixture evidence.

It is also **re-runnable**. The Workbench journey asserts a refusal *before* it
asserts a success, and a refusal can only be observed if no leftover grant is
sitting in the consent store. The driver therefore opens with a withdrawal
through the page's own Remove control and waits for the *server* to confirm the
scope is gone — an empty rendered list is not the same as a list that finished
loading, and treating the two as equal is what made an earlier version of this
driver pass on a dirty store. It closes by withdrawing the second scope it
granted. Two consecutive runs of `65/65` with no manual reset in between is the
verification of that property.

The Workbench controls are exercised as a person uses them: the driver writes
into `#wbAskText` and dispatches a click on `#wbAskSend`, then clicks
`#wbExecute` for both the refused and the granted run. It does not call
`submitInstruction()`/`runExecution()` to stand in for the press. Each control is
also asserted to carry exactly one handler, so a duplicate binding fails the run
rather than quietly double-submitting.

Tests added for the tour wiring in `desktop/test/integration_boot.test.js`:

- `the guided tour is reachable from Settings and mounts on its own host`
- `opening the tour twice does not stack a second copy`
- `closing the tour decides nothing`

and, in `live_gui_path.js`, three checks that the control exists, that the tour
opens on its own host, and that it grants nothing and can be closed.

Honest limitation: the three guided-tour tests were written **after** the
implementation. They pass, but they were not watched fail first, so they are
regression coverage rather than red-green proof that the wiring was absent
before the change. The wiring defect itself is documented in §1 with the code
path that was dead.

---

## 6. Package

Built from these changes in this session.

| Fact | Value |
|---|---|
| Path | `desktop/release/ActionMesh-0.1.0-amd64.deb` |
| SHA-256 | `cf9f4d535962b6c64c4c26dee3786e1570fccccedf525b44a5ba7a7070443439` |
| Size | 156,584,816 bytes |
| Built | 2026-11-05 11:27:52 +0530 |

Built from the current tree after the executor rejection fix and the idempotent
acceptance driver. All ten Phase 6 UI assets were extracted from `app.asar` and
compared byte-for-byte with source — ten of ten **IDENTICAL**:

```
activity.css  activity.js  app.js  connections.js  features.css
index.html    onboarding_flow.js     savedtasks.js   styles.css  workbench.js
```

Both new anchors (`id="firstRunOpen"`, `id="firstRunMount"`) are present in the
packaged `index.html` (one occurrence each), and the packaged script order is the
nine-file order above with `app.js` last. The packaged Python runtime carries the
fixed executor: `runtime/app/actionmesh/core/executor/engine.py` hashes
`93a1ed2a…`, identical to source and to the staging tree.

Two earlier packages are **not** evidence for this record:
`ea77ecd8…` (156,585,268 bytes, 10:51) predates the rejection fix, and
`f59dd39e…` (156,583,828 bytes) predates the module wiring entirely.

### Graphical acceptance — **BLOCKED**

```
DISPLAY=:0 WAYLAND_DISPLAY=wayland-0 XDG_SESSION_TYPE=wayland
Xvfb / xvfb-run / scrot / import / xdotool / xvfb / electron: NOT FOUND
BLOCKED: display env present but no screenshot tool … refusing to fake a screenshot.
```

A live Wayland session exists but no capture tool does, and installing system
packages was not authorized. No screenshot is claimed. The GUI scripts were
nevertheless exercised headlessly through the DOM harness against the live
backend — that is what §3 and §5 report, and it is not the same thing as seeing
the window.

---

## 7. Rulings

**R1 — `savedtasks.js` stays dormant.** `app.js` already owns the automations
surface end to end. Calling `initSavedTasks()` from `boot()` as well would
double-bind `#automationDraftForm` and post every draft twice. The module is kept
whole and unit-tested; its `taskBody` helper is the edit-save seam. A test asserts
`initSavedTasks` is called **0** times at boot so a future change here fails
loudly instead of silently double-submitting.

*Cost if wrong:* the module's own list rendering is not on screen; the journey is
served by `app.js`'s implementation instead. The journey itself is proven either
way.

**R2 — there is no `desktop/ui/voiceos.js`, and none is needed.** The typed-text
journey is `voice.js` for input and `app.js` `propose()` for the request. Adding a
module would create a second path to `/voiceos/messages`. The deliverable is the
working journey, not a file named after the feature.

**R3 — the guided tour lives in Settings, not at boot.** A 14th nav entry or a
second boot overlay would either duplicate navigation or cover the wizard
`onboarding.js` owns. Settings is an existing, reachable surface.

---

## 8. Repository state

All uncommitted work preserved. No commits, no resets, no stashes, no discards,
no history rewrites, no pushes. `git diff --check` exits 0.

`git diff --ignore-cr-at-eol --stat` over the fourteen tracked modifications:
468 insertions, 17 deletions. The largest are `desktop/ui/app.js` (+172, the
wiring and consent controls), `desktop/ui/index.html` (+137, the five section
containers, nav entries, and the tour host), and `actionmesh/core/executor/engine.py`
(+53/−1, the rejection fix).

All newly added Phase 6 files are **untracked**, not staged:

```
desktop/ui/    activity.js  activity.css  connections.js  features.css
               onboarding_flow.js  savedtasks.js  workbench.js
desktop/test/  live_gui_path.js  workbench.test.js  activity.test.js
               connections.test.js  savedtasks.test.js  integration_boot.test.js
               scope_smoke.js
docs/          PHASE6_FUNCTIONAL_DELIVERY.md
scripts/       gui-smoke.sh
tests/         test_phase6_workbench.py  test_phase6_dashboard.py
               test_phase6_connections.py  test_phase6_saved_tasks.py
               security/test_approval_expiry.py
```

Large diffs are largely CRLF churn: with `git diff --ignore-cr-at-eol`,
`protocol/models.py` is +12/−1 and `core/permissions/context.py` is 2/2. The
10-minute approval expiration and the secret redaction are untouched.