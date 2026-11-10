# C42 — Real-World Computer, Browser & Voice Execution: Shared Contract

**Status:** binding for all C42 workers. Reconnaissance is complete against the committed
C41 tree (`main` @ `5df2996`). Every capability name, schema, file owner and risk label in
this document is authoritative. If the code and this document disagree, **the code wins** —
re-read it; this document is a map, not a second source of truth.

**One line:** AI proposes, policy decides, user controls, executor acts, verifier
confirms, audit remembers. We add **capabilities** to the single existing pipeline. We do
**not** add a second execution engine, a second browser, or a second approval system.

---

## 0. Non-negotiable rules (all agents)

1. **One execution engine.** The graph → `ExecutionEngine` → registry → policy → consent →
   approval → provider → verifier path is the only path. A capability that skips the registry
   skips policy; that is the entire failure this architecture exists to prevent.
2. **Risk is set at the definition, and can only be raised.** `ActionDefinition.risk_level`
   is trusted template metadata. The policy engine computes
   `max(action.risk_level, definition.risk_level)` and never lowers it. A model that labels a
   booking `LOW` is a no-op: the definition's `CRITICAL` wins.
3. **Three validation doors, all preserved, none weakened.**
   - `validate_action` (`protocol/validate.py`) — protocol layer; never accepts `action_id`,
     forces `state=CREATED`.
   - `ActionRegistry.resolve → check_params` (`core/registry/defs.py`) — **fails closed on
     any parameter a definition does not declare.** `{"command":"rm -rf /"}` smuggled beside
     `{"to","message"}` is refused, not carried through. This is the door that stops a model
     inventing a shell command inside a file op.
   - `validate_proposal` (`voiceos/llm.py:177`) — the C41 LLM door. It accepts only
     `{"actions":[{"type","parameters"}]}`; **any entry with fields beyond `type`/`parameters`
     is dropped whole** (that is exactly the shape a prompt-injected answer arrives in), and
     unregistered types are dropped. It returns `LlmProposal(actions, notes)`.
4. **No unrestricted model-generated JS/Python/shell.** A capability exposes a fixed,
   typed argument schema. There is no `browser.eval`, no `browser.run_python`, no
   `device.shell_freeform`. Driving a page is a closed set of named operations.
5. **Secrets never cross a boundary.** No API key, cookie, password, OTP, payment data, or
   sensitive page content reaches Python logs, the renderer, an audit record, or a result
   payload. The provider bridge already enforces the key boundary; the new transient channel
   (below) enforces the content boundary.
6. **Verification is independent.** A handler *records an effect*; `observe()` *re-reads the
   world*. The provider's default `verify()` returns `verified=False` ("does not
   self-certify"). Never override `verify()` to return `True` — that silently turns
   independent verification back into "the executor said so."
7. **Honest reporting.** The app never says a task succeeded that failed or was only
   proposed. `REQUIRES_USER` (executed-but-unconfirmed) is never success.
8. **Never destructive Git.** No `reset --hard`, no history rewrite, no forced push. The
   tree is clean at start; keep it that way until the integration lead commits.

---

## 1. The registration pattern every capability copies

A capability = **one `ActionDefinition` + one `LaptopProvider` handler**. No third thing.

### 1a. The definition (copy `actionmesh/agent/definitions.py`)

`_def(name, risk, permissions, properties, required, description, *, timeout_s, max_attempts)`.

- `name` — `domain.verb` (e.g. `browser.click`).
- `risk` — `RiskLevel` (see §3 for the full table). Set here, here only.
- `permissions` — **concrete catalog scopes only** (`core/permissions/catalog.py`). An unknown
  or wildcard permission is a *registration error* (`validate_definition` fails closed).
- `properties` / `required` — the argument schema, `{"type":"object","properties":{...},"required":[...]}`.
  Use the existing type tags: `_S` string, `_ARGS` array, `_NUM` number, `_OBJ` object, `_BOOL`.
- `timeout_s` — generous for anything that touches a network or a GUI; default 30s.
- `max_attempts` — **1 for anything with side effects.** A retry is only safe when idempotent
  *and* the failed attempt left no effect. "Click this button" and "submit this booking" fail
  that test.

Then register it: add the name to `CAPABILITIES` (derived from `device_definitions()`) and,
if implemented, to `IMPLEMENTED`; add it to the relevant `REQUIRING_*` gate frozenset (§4).
`register_device_actions` already refuses to register anything outside `IMPLEMENTED`, and
`advertise()` only surfaces a capability when its gate is open **and** the capability is
implemented.

### 1b. The handler (copy `actionmesh/agent/provider.py`)

- Add the id to `LaptopProvider.SUPPORTED` **and** (if it blocks on a network/D-Bus/socket)
  to `ASYNC_ACTIONS`. A test asserts `SUPPORTED == definitions.IMPLEMENTED` — keep them equal.
- **Sync handler:** add a branch to the `_perform` if-chain → return a `dict[str, Any]`.
- **Async handler:** add an entry to the `_execute_async` `handlers` dict mapping id →
  `lambda: self._x(action)` where `_x` is an `async def`.
- The handler **returns a plain dict** that becomes `ProviderResult.data`. `status` is an
  *inner* field of that dict (`"ok"`, `"unavailable"`, ...), not on the envelope.
- The handler **records its effect**: `self._effects[action.action_id] = {...}` so `observe()`
  can re-read it.
- The handler **raises** `ProviderError(message, FailureKind.X)` on failure. A confinement
  refusal (`AgentError`/`OSError`) is wrapped as `FailureKind.AUTHORIZATION` by the dispatch.
  A missing path is `INVALID_INPUT`; an escape is `AUTHORIZATION` (`_classify`).
- **Transient content** (large, sensitive, or non-stored payloads) goes out on the transient
  channel via `_stream_content` → `output_callback().emit(stream, chunk)` — see §6. It is kept
  in `data` (the verifier needs it) but stripped from the safe result view.
- **Do not touch `verify()`.** Leave it as the honest `verified=False` default.

---

## 2. New capability IDs (exact) and argument schemas

Type tags: `S`=string, `A`=array, `N`=number, `O`=object, `B`=boolean. **`required`** = the
`required` list. Every param not listed is refused by `check_params`.

### 2.1 Browser (Agent A) — provider `laptop`, new module `actionmesh/agent/browser.py`

The browser is a **single controlled session** in the Electron main process (the same
`actionmesh-web-pages` partition family, hardened), driven by named operations. **No
`executeJavaScript` on arbitrary model-supplied code.** Each operation below is a fixed
capability. Element targeting is by **ref** (an opaque, session-scoped, monotonically
increasing id minted by the snapshot) — never by fixed coordinate, never by a selector string
the model invented (a selector is a form of untrusted code; a ref is a pointer the snapshot
guaranteed exists).

| id | risk | scope(s) | args (required) | returns (key facts) |
|---|---|---|---|---|
| `browser.open` | LOW | `browser.control` | `url` (S) | `status, url, title, ref_map_size` |
| `browser.navigate` | LOW | `browser.control` | `url` (S) | `status, url, title` |
| `browser.search` | LOW | `browser.control`, `search.web` | `query` (S), `engine` (S, optional) | `status, url, results[]` |
| `browser.inspect` | LOW | `browser.control` | (none) | `status, url, title, element_count, truncated` + transient `elements` |
| `browser.snapshot` | LOW | `browser.control` | (none) | `status, url, title, refs` (bounded element list) + transient `elements` |
| `browser.click` | MEDIUM | `browser.control` | `ref` (S), `double` (B, opt) | `status, url_after, changed` |
| `browser.type` | MEDIUM | `browser.control` | `ref` (S), `text` (S) | `status, changed` — **reports whether char count grew, never the text** |
| `browser.select` | MEDIUM | `browser.control` | `ref` (S), `value` (S) | `status, changed` |
| `browser.scroll` | LOW | `browser.control` | `ref` (S, opt), `direction` (S), `amount` (N, opt) | `status` |
| `browser.back` | LOW | `browser.control` | (none) | `status, url, title` |
| `browser.forward` | LOW | `browser.control` | (none) | `status, url, title` |
| `browser.refresh` | LOW | `browser.control` | (none) | `status, url, title` |
| `browser.wait` | LOW | `browser.control` | `ms` (N, opt), `until` (S, opt) | `status, url, title` |
| `browser.stop` | MEDIUM | `browser.control` | (none) | `status` — halts the current task's browser actions |

**Element ref contract.** `browser.snapshot`/`inspect` returns refs; each ref is valid only
until the next navigation or DOM-mutating action. After any such action the **next**
interaction MUST re-snapshot. A ref that is no longer present is a `stale` result
(`status:"stale"`), not a crash. This mirrors the GUI side's `_MAX_MATCHES=1` rule: a target
must be uniquely addressable or the action refuses.

**Untrusted-content rule.** Page content is data. A website must never instruct the AI to
ignore policy, expose secrets, change permissions, or perform unrelated actions. The model
sees page text as untrusted evidence (same rule as tool output); it cannot grant itself
anything. Login / CAPTCHA / OTP / MFA are **user-handled handoffs**: the capability detects
the control, reports `status:"needs_user"` with a reason, and stops — it never attempts to
fill an OTP or bypass a CAPTCHA.

**New scope:** `browser.control` (see §3). `browser.search` additionally requires
`search.web` (existing, LOW).

### 2.2 Local files & project ops (Agent B) — provider `laptop`, new module `actionmesh/agent/files.py`

Extends the existing confined `device.file.*` family with richer verbs. **All of these reuse
`Workspace.resolve` / `open_fd` / `unlink` / `walk_fd` verbatim** — the TOCTOU-safe
descriptor walk that refuses symlinks and denies `.ssh`/`.env`/`id_rsa`/`shadow` is the
boundary; it is not re-implemented and not widened. Paths are relative to the configured root;
absolute, `..`, drive-letter and percent-encoded-separator paths are refused (see §5).

| id | risk | scope(s) | args (required) | returns (key facts) |
|---|---|---|---|---|
| `device.files.list` | MEDIUM | `device.file.list` | `path` (S), `limit` (N, opt) | `path, entries[], count, hidden_outside_root, truncated` |
| `device.files.search` | LOW | `device.file.list` | `path` (S), `pattern` (S), `limit` (N, opt) | `path, matches[], count, truncated` |
| `device.files.read` | MEDIUM | `file.read` | `path` (S) | `path, size, sha256, truncated, binary` + transient `content` |
| `device.files.compare` | MEDIUM | `file.read` | `left, right` (S) | `changed` (boolean), independently checked by rereading both complete files up to 256 KiB each |
| `device.files.open` | MEDIUM | `device.file.list` | `path` (S) | `status, path, kind, opened` — opens in the default app/OS |
| `device.files.info` | LOW | `device.file.list` | `path` (S) | `path, exists, is_dir, size, mtime, kind` |
| `device.files.create` | HIGH | `file.write` | `path` (S), `content` (S) | `path, bytes, sha256` (a write) |
| `device.files.rename` | HIGH | `file.write` | `path` (S), `to` (S) | `path, to, moved` |
| `device.files.move` | HIGH | `file.write` | `path` (S), `to` (S) | `path, to, moved` |
| `device.files.copy` | MEDIUM | `file.read`, `file.write` | `path` (S), `to` (S) | `path, to, copied, bytes` |
| `device.files.edit` | HIGH | `file.write` | `path` (S), `find` (S), `replace` (S), `all` (B, opt) | `path, replacements, sha256` — **diff/preview emitted transiently first** |
| `device.files.trash` | HIGH | `file.delete` | `path` (S) | `path, trashed, to` — **prefers the OS trash over delete** |

**Edit/trash rules.** `device.files.edit` and any `HIGH` mutation MUST show the proposed
change (a diff/preview on the transient channel) before it applies; destructive ops prefer the
OS trash; `device.files.trash` is preferred over a hard delete and a hard delete is a separate,
explicit `HIGH` (existing `device.file.delete`) — not invented here.

**No new shell route.** None of these shells out to edit/move/copy; they are in-process file
operations inside `Workspace`. The only way to run a program remains `device.terminal.execute`
/ `device.process.*` under the existing `CommandPolicy` allowlist.

### 2.3 Media & desktop app control (Agent C) — provider `laptop`, new module `actionmesh/agent/media.py`

"Play this" resolves "this" from the **current selection / conversation reference** (see §8);
if the reference is absent or ambiguous the capability returns `status:"ambiguous"` with the
candidates, it does not guess. **A capability never claims playback started unless it checked
the real player/session state.** Where a supported local player is not reachable, the honest
answer is `status:"unavailable"` with a reason — never a simulated success.

| id | risk | scope(s) | args (required) | returns (key facts) |
|---|---|---|---|---|
| `media.status` | LOW | `media.control` | (none) | `status, player, track, state, position_s, duration_s` |
| `media.play` | LOW | `media.control` | `ref` (S, opt) | `status, player, track, state` — **state read back after** |
| `media.pause` | LOW | `media.control` | `ref` (S, opt) | `status, state` |
| `media.next` | LOW | `media.control` | (none) | `status, track` |
| `media.previous` | LOW | `media.control` | (none) | `status, track` |
| `media.volume` | LOW | `media.control` | `level` (N, opt), `muted` (B, opt) | `status, level, muted` |
| `media.open_file` | MEDIUM | `media.control`, `system.app.launch` | `path` (S) | `status, path, opened` — opens a local media file in the default app (root-confined like file ops) |
| `media.open_url` | LOW | `browser.control` | `url` (S) | `status, url` — opens a media website through the browser capability |

**Unsupported services are named as unsupported.** If the media backend cannot reach a given
player/service, the result says so; it does not claim a capability it does not have, and it
never downloads a secret (a DRM-protected stream is not a capability).

**New scope:** `media.control` (see §3).

### 2.4 Workflow capabilities (Agent D) — no new provider; **task/workflow templates**

Train and food are **not new providers**. They are *multi-step plans* composed from the
capabilities above, expressed in the existing template shapes, so a new site is a new
template, not a special case threaded through the LLM planner.

- **Train search/booking** and **Zomato food** are operator-authored **task templates**
  (`voiceos/tasks.py`) and/or stored **workflows** (`voiceos/workflows.py`) with
  `source: "template:<name>"`. A model may *propose* a run; it never authors a stored plan.
- Each step is `{label, action, parameters, when?, device_id?}` where `action` is one of the
  registered capability ids above. `when` may name **only** `CONDITION_KEYS`
  (`{exit_code, state, verified, error}`) on a **strictly earlier** step — a condition is
  evidence, never a claim. Max 8 steps.
- **Missing details are asked conversationally, one at a time** (journey, date, class,
  passenger) — the planner emits a clarification turn, it does not fill a default.
- **Search is read-only; it never books or orders.** `browser.*` + `browser.inspect`
  discover options; presenting them is a read. The *only* step that submits is a `CRITICAL`
  capability gated behind explicit final confirmation (§3, §7).
- **Booking/payment confirmation is a capability, not a statement.** The submit step is
  declared `CRITICAL` (e.g. a `booking.confirm`/`order.place` capability, or the existing
  `payment.execute` path), so the PolicyEngine forces `WAITING_PERMISSION`. A model sentence
  "the user has approved" is **not** an approval — only the user's Confirm on the approval
  card, matched on the server-minted `action_id`, is.

**Extensibility rule:** adding a new site = adding a new template (a list of registered
capability ids + `when` conditions + the `CRITICAL` submit step). The planner is unchanged.

---

## 3. Risk classification (maps onto the existing PolicyEngine)

The PolicyEngine forces a real approval (`WAITING_PERMISSION`) on any node whose **effective**
risk is `CRITICAL` (always ASK) or `HIGH` (ASK unless the actor has opted into high-risk).
This is how the "final confirmation gate" is enforced with **no new gate**:

| Class | Risk | Approval behaviour | C42 examples |
|---|---|---|---|
| Ordinary (read/observe) | LOW / MEDIUM | **No approval** | open folder, list/search files, search a site, inspect/snapshot a page, read a permitted doc, open an app, view restaurants, search trains, play selected media, `media.status` |
| Changes / reversible | HIGH (ASK) | **Propose + confirm** | meaningful file edit/rename/move/copy/create, `browser.click`/`type`/`select`, config change |
| Consequential | **CRITICAL** (always ASK, un-upgradeable-by-deny) | **Explicit final review + confirmation** | book a ticket, place a food order, payment/checkout, send an external message/email, **permanent** delete |

**New concrete scopes to add to `core/permissions/catalog.py`** (integration lead owns this
edit; every agent names scopes from this list, never invents a scope):

| scope | risk | purpose |
|---|---|---|
| `browser.control` | MEDIUM | open/navigate/inspect/snapshot a page and drive it by ref |
| `media.control` | LOW | read and control supported local playback state |

Booking/order **submit** reuses `payment.execute` (**already CRITICAL**) or a new
`booking.submit`/`order.place` declared **CRITICAL** — never a lower label. File mutations
reuse the existing `file.write`/`file.delete` (HIGH/MEDIUM as already set). **Do not lower any
existing scope's risk.**

---

## 4. Capability gates (so an unconfigured capability is never resolvable)

`AgentConfig.advertise()` only surfaces a capability when (a) it is in `IMPLEMENTED`, and
(b) its gate is open. Add the new ids to the correct frozenset in
`actionmesh/agent/definitions.py`:

- Files → `REQUIRING_FILE_ROOT` (gated by `workspace_root`).
- Media local file open / browser file open → `REQUIRING_FILE_ROOT` too (they touch the root).
- Media control (player probe) → gated by a new `allow_media` boundary (Agent C adds the
  config field + `REQUIRING_MEDIA` frozenset) so a headless/unconfigured agent does not
  advertise a player it cannot reach.
- Browser → gated by a new `allow_browser` boundary (Agent A adds the config field +
  `REQUIRING_BROWSER` frozenset) so a build without a browser partition does not advertise
  `browser.*`.
- Shell/process/server/app/ide stay under `REQUIRING_SHELL`; screen/GUI under
  `REQUIRING_DESKTOP` (unchanged).

`advertised_ceiling(advertised)` is unchanged — it derives the device's risk ceiling from what
it advertises, so advertising `CRITICAL` booking capability correctly raises the ceiling.

---

## 5. File & path confinement (Agent B reuses, never rewrites)

`Workspace.resolve(supplied, must_exist=...)` (`agent/paths.py`) is the single boundary:
- refuses non-string, empty, NUL, **absolute**, drive-letter, percent-encoded-separator,
  over-4096-char, over-component, and `..` paths;
- `os.path.realpath` then `_contains` (path must stay under `root`);
- refuses a final symlink unless `follow_final_symlink`;
- `_check_denied` against `DENIED_BASENAMES` / `DENIED_RELATIVE_PREFIXES` /
  `DENIED_SECRETFLE_PREFIXES` (`.ssh`, `.aws`, `id_rsa`, `shadow`, `.env`, ...).

Mutations re-check at open time with `open_fd` (a per-component `os.O_NOFOLLOW` descriptor
walk, TOCTOU-safe) and `unlink`/`walk_fd`. **New file capabilities must call these, not
`os.path`/`shutil` directly.** The read ceiling (`MAX_READ_BYTES`), write ceiling
(`MAX_WRITE_BYTES`) and list ceiling (`MAX_LIST_ENTRIES`) stay.

---

## 6. The transient channel (content that must not be stored or audited)

`_stream_content(action, data)` → `output_callback().emit(stream, chunk)`. Existing streams:
`content` (file body), `elements` (+ `elements.note`) (accessibility/DOM elements), `logs`
(process output). Content stays in `data` (the verifier needs it) and is **stripped from the
safe result view** before it reaches clients. **New streams, all agent-owned:**

- Agent A: `browser.elements` (the snapshot/inspect element list, one JSON document per
  event), `browser.title`, `browser.url` (safe navigation facts), and `browser.page` (bounded
  visible text — **truncated, redacted of anything that looks like a credential**, and capped).
- Agent B: `files.preview` (a diff/preview before an `edit`/mutation applies), `files.content`
  (reuse the `content` stream name for reads).
- Agent C: `media.meta` (current track/player identity — a label, never a file's secret
  contents).

**Never** stream: cookies, passwords, OTPs, payment data, full sensitive page text, or the
provider API key. Stream a **bounded, redacted** view and a count; the result payload carries
the count and the refusal reason, which is what a caller needs to know an observation
happened at all.

---

## 7. Approval / confirmation policy (reuses existing infra)

- **Ordinary ops need no approval** — the PolicyEngine's risk gate decides; a `LOW`/`MEDIUM`
  node with satisfied grants runs.
- **Changes/reversible** — the renderer shows the proposed change + effect and, when risk is
  `HIGH`, the executor parks the node in `WAITING_PERMISSION`; the user's decision is posted
  to `/approvals` on the **server-minted** `action_id`.
- **Consequential** — a `CRITICAL` node *always* parks. The approval card shows the **exact**
  reviewed operation (item, price/total, journey/order details, what will be submitted) with
  real **Confirm / Cancel** controls — never a generic "Continue". Final confirmation is tied
  to the exact reviewed operation: if price/destination/selection/passenger details change,
  the prior confirmation is **invalidated** and re-shown.
- **No model-generated approval.** Only the user's Confirm, matched on the server-minted
  `action_id`, counts. `validate_proposal` already strips any self-asserted approval/risk
  field, and the PolicyEngine's `DENY` can never be upgraded by approval.

The renderer control-view pattern is the **C38 visual view blueprint**: stage (send nothing) →
submit one two-node graph (`POST /execute` with `graph`, `actor: IDENTITY`,
`policy:{rules:[{action, decision}]}`) → server policy gate decides → a
`waiting_permission` answer is `POST /approvals` with `{action_id, status, execution_id}` →
read back **verified vs applied-but-unverified as separate facts**.

---

## 8. Reference resolution ("this" / "that 4 PM train")

`Conversation.references` (per-actor ordinal memory of the last-observed labels) is the
resolver for "Play this" and "book that 4 PM train". It is **a memory of labels, never a
grant.** `TaskStep.bind` (narrow: index+key+field) copies an observed id into a later step's
parameter, and `analysis` (read-only views of earlier observations) both — **both are
storage-only, package-authored, and NOT in `to_dict()`** so they never ride out to a client.
The resolved action still flows through registry → policy → consent → approval. A model can
point at "that" but cannot grant itself the right to act on it.

---

## 9. UI (integration lead + agents) — reuse the C40/C41 premium shell

**Do not redesign.** Reuse the exact `:root` palette (`--bg #F7F9F5`, `--surface #FFFFFF`,
`--green #16834A`, `--green-dark #11643A`, `--green-soft #E7F4EA`, `--yellow #F5CB4B`,
`--yellow-soft #FFF6D9`, `--ink #203329`, spacing/radius vars). Add views via the
established recipe:

1. `index.html`: a `nav-item` `button` with `data-view="X"` (glyph + label + optional badge).
2. `index.html`: a `section.view[data-view="X"] hidden` inside `#mainScroll`.
3. `app.js`: `case 'X': return refreshX();` in `refreshCurrent()`.
4. `app.js`: `wireX()` called from `wire()`.
5. Live updates ride the existing 5s `refreshCurrent()` poll + the `onStreamEvent` SSE bridge.
6. Element ids follow `<viewName><PascalCaseSuffix>`; reuse `card`/`card-head`/`card-title`/
   `chip`/`btn-row`/`grid two|four`/`rows`/`field`/`fine` classes.

**CSP constraint (browser view):** `connect-src 'none'` and `img-src 'self' data:` mean the
desktop page **cannot fetch or iframe arbitrary web content**. A live web session rides a
**separate hardened partition** (`actionmesh-web-pages` family, `sandbox:true`,
`contextIsolation:true`, no preload, memory-only cookie jar) in a **kept `BrowserWindow`**,
and streams observations (title, url, element list, bounded redacted text) back over IPC to
this CSP-restricted page. The existing `openWebPage` is a read-only child window that keeps
no handle; Agent A adds the kept-handle window + the drive channels (§2.1) — a new IPC
surface, not a weakening of the existing one.

Views to add/improve: (A) Browser Control; (B) File Explorer; (C) Media Controls; (D) the
persistent Voice Command Bar (existing, extend); (E) Task timeline
(Understanding→Planning→Inspecting→Waiting for approval→Executing→Verifying→Completed/Failed)
**only showing stages backed by real events**, with a reliable Stop; (F) Approval cards
(exact item, price/total, journey/order details, what will be submitted, Confirm/Cancel).

---

## 10. File ownership (strict; worktrees not needed — tree is clean, in-place is simpler)

The tree is clean at `main` @ `5df2996`. **No worktrees.** Strict in-place ownership below;
**an agent edits only files it owns.** Shared files are owned by the integration lead (me) to
avoid a 5-way merge conflict; agents *report* needed shared edits and I apply them.

**Integration lead (me):** `core/permissions/catalog.py` (new scopes),
`core/registry/defs.py`+`seeds.py` (if shared registration needs touching), `api.py` (new
routes), `voiceos/orchestrator.py` (planner/orchestrator wiring), `voiceos/llm.py` (only to
extend the catalog the model sees — not to weaken `validate_proposal`), `desktop/ui/index.html`
+ `app.js` + `styles.css` (view wiring, shared), `actionmesh/agent/definitions.py`
(`CAPABILITIES`/`IMPLEMENTED`/`REQUIRING_*`), `actionmesh/agent/provider.py` (`SUPPORTED`/
`ASYNC_ACTIONS` dispatch entries), `agent/config.py` (new gate fields), CHANGELOG + C42 report.
I do the startup wiring and the final integration.

**Agent A — Browser:** owns `actionmesh/agent/browser.py` (new), the browser BrowserWindow +
drive IPC in `desktop/app/main.js` **and** `desktop/app/preload.js` (browser-specific
channels only — no other agent touches these two), `tests/unit/test_browser_agent.py` (new),
`tests/e2e/browser_*` fixtures (new). Reports needed `definitions.py`/`provider.py`/catalog
entries; I apply them.

**Agent B — Files:** owns `actionmesh/agent/files.py` (new), `tests/unit/test_files_agent.py`
(new). Reuses `paths.py`/`Workspace` (read-only). Reports `definitions.py`/`provider.py`
entries; I apply them.

**Agent C — Media:** owns `actionmesh/agent/media.py` (new), the `allow_media` config field in
`agent/config.py` **is a shared edit — I apply it**, `tests/unit/test_media_agent.py` (new).
Reports `definitions.py`/`provider.py`/`REQUIRING_MEDIA` entries; I apply them.

**Agent D — Workflows:** owns `voiceos/workflows.py` additions (new templates), a new
`voiceos/website_workflows.py` (new, train + food templates + clarification/confirm gate
logic), `tests/unit/test_website_workflows.py` (new). No provider. Reports the `CRITICAL`
submit-capability definitions; I register them.

**Agent E — Tests / security / verification:** owns `tests/e2e/c42_*` (new) and the controlled
local website fixtures in `tests/fixtures/` (new). Read-only on `actionmesh/` and `desktop/`.
Runs the full suites and writes the verification evidence; does **not** edit product code
(it reports defects, I or the owning agent fixes them).

**Seams are at the handler-module boundary.** No two agents own the same file. The
`definitions.py`/`provider.py`/`catalog.py` touch-points are *appended* by the integration
lead in one pass after agents report, so there is no concurrent write to a shared file.

---

## 11. Verification evidence contract (for the final C42 report)

Report results in **three separated tiers** — never blend them:
1. **Mocked tests** — provider/registry/policy with simulated backends.
2. **Local fixture tests** — controlled local websites + temp roots (no live 3rd-party
   dependency).
3. **Live third-party tests** — real sites (train/food). State clearly if not run (no
   credentials, or not practicable) — do **not** claim a live booking/order happened without
   real evidence.

Every claim of "a booking/order/media session/browser action happened" must cite the evidence:
the verified result (independent `observe()` confirmation), the transient stream, or the live
capture. **A `REQUIRES_USER` (executed-but-unconfirmed) node is never reported as success.**

Commands the report must show ran: `.venv/bin/python -m pytest -q`, `cd desktop && npm test`,
JS syntax checks, and a real Electron launch exercising the actual UI. **Preserve the Electron
sandbox** (`sandbox:true`); do **not** silently use `--no-sandbox`.

---

## 12. Definition of DONE (from the brief)

A user can speak naturally, ask VoiceOS to open and inspect a real folder, operate a supported
website, search train options, browse pizza options, and control supported media. Multi-step
tasks execute through **real registered ActionMesh capabilities**, with visible progress,
reliable cancellation, independent verification and the right approval boundaries. The
application must never fake a successful action. The seven acceptance scenarios
(§13) pass against a real running desktop app.

## 13. Seven mandatory acceptance scenarios

1. "Open my Downloads folder and check what's inside."
2. "Open my ActionMesh project and check if tests are passing."
3. "Open train booking and find a train around 4 PM."
4. "Book that 4 PM train."  → must reach the CRITICAL confirm gate; never claims booked.
5. "Open Zomato and show me pizza."  → search only; must not place an order.
6. "Play this."  → resolves the reference; honest if no player reachable.
7. "Stop."  → during a long-running browser/desktop task, reliably cancels.
